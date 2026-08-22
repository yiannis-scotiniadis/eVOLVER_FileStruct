"""server/experiment_engine.py — control-loop orchestrator (SPEC §9).

The engine owns the lifecycle of a single Phase-1 experiment: it builds
per-vial :class:`TurbidostatController` instances, sends initial actuator
commands, ticks every 10 s via :meth:`run_cycle` (called from the
sensor_loop), enforces per-vial heater safety (SPEC §10), and persists
state to ``experiments/{name}/state.json`` so a server restart can
resume an in-flight experiment.

State machine
-------------

::

    IDLE ─create_experiment─► CREATED ─start_experiment─► RUNNING
                                │                            │
                                │                stop / err  │
                                ▼                            ▼
                          (delete back to IDLE)        STOPPED / ERROR
                                                            │
                                                            ▼
                                                  (delete back to IDLE)

Only one experiment is loaded in memory at a time (Phase 1 scope). The
disk holds all experiments — :meth:`list_experiments` enumerates them.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from control_modes.chemostat import (
    MIN_BOLUS_INTERVAL_SECONDS,
    ChemostatController,
)
from control_modes.morbidostat import EscalationEvent, MorbidostatController
from control_modes.turbidostat import PumpAction, TurbidostatController
from data_export import filter_rows_by_hours
from data_logger import _VALID_NAME
from serial_manager import (
    HEATER_OFF_SETPOINT,
    MAX_SAFE_TEMP_C,
    OD_AGG_CHOICES,
    OD_AGG_DEFAULT,
    OD_DEFAULT_N_DARK,
    OD_DEFAULT_N_SAMPLES,
)

# Union of all controller types accepted by the engine. New modes that follow
# the same interface (push_od / decide / to_state / restore_state /
# flow_rate_ml_s) plug in by adding to this union and to SUPPORTED_MODES.
ControllerType = TurbidostatController | ChemostatController | MorbidostatController

SUPPORTED_MODES: frozenset[str] = frozenset({"turbidostat", "chemostat", "morbidostat"})


class ConflictError(Exception):
    """Raised by lifecycle methods when an operation conflicts with current
    runtime state (e.g. confirming an escalation that isn't pending). The
    API layer maps this to HTTP 409."""


_VALID_BOTTLE_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


N_VIALS = 16
N_PUMPS = 32  # 16 influx + 16 efflux; canonical pump index (CLAUDE.md)
STIR_MAX = 15
DEFAULT_HEATER_OVERRUN_C = 5.0
DEFAULT_HEATER_CRITICAL_C = 50.0
DEFAULT_HEATER_STEP_DOWN_C = 2.0  # SPEC.md §10: per-cycle target reduction on overrun
DEFAULT_SENSOR_FAILURE_THRESHOLD = 3
DEFAULT_CYCLE_INTERVAL_SECONDS = 10.0
PUMP_DURATION_HARD_CAP_SECONDS = 30.0  # engine-level safety cap (SPEC §10)
DEFAULT_MAINTENANCE_TIMEOUT_MINUTES = 30.0  # auto-resume failsafe

# Consumables safety interlock (SPEC §15). Reserve floors are generous
# because the tracked volume is inferred (duration x flow_rate), not
# measured, and drifts with pump wear/tubing compliance/calibration error.
MEDIA_RESERVE_MIN_ML = 50.0
MEDIA_RESERVE_FRACTION = 0.05
WASTE_RESERVE_MIN_ML = 100.0
WASTE_RESERVE_FRACTION = 0.05

# Defaults if no per-vial pump_flow_rates supplied. Same constants used by
# the mock; lifted here so the engine doesn't import the mock.
DEFAULT_FLOW_RATES_ML_PER_SEC: tuple[float, ...] = (
    0.95, 1.1, 0.975, 0.85, 0.95, 1.05, 1.05, 1.05,
    1.025, 1.125, 1.0, 1.0, 1.05, 1.15, 1.1, 1.025,
)
DEFAULT_VOLUME_ML = 25.0
DEFAULT_EFFLUX_EXTRA_SECONDS = 0.0
DEFAULT_HISTORY_WINDOW = 5
DEFAULT_PUMP_WAIT_MINUTES = 15.0

# Enhanced OD acquisition defaults (median-of-N averaging + dark subtraction +
# range guard). Used for the per-experiment "od_acquisition" config block; the
# sensor_loop reads these via ExperimentEngine.od_acquisition_params() and only
# while an experiment is RUNNING (idle reads use the naive single read).
DEFAULT_OD_ACQUISITION: dict = {
    "n_samples": OD_DEFAULT_N_SAMPLES,
    "dark_subtract": False,
    "n_dark": OD_DEFAULT_N_DARK,
    "agg": OD_AGG_DEFAULT,
}

log = logging.getLogger(__name__)


class ExperimentStatus:
    IDLE = "idle"
    CREATED = "created"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"

    ALL = ("idle", "created", "running", "stopped", "error")


class InvalidExperimentStateError(RuntimeError):
    """Raised by lifecycle methods when called from the wrong status."""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _media_reserve_ml(initial_volume_ml: float) -> float:
    """SPEC §15: reserve floor below which influx from this bottle is
    suppressed."""
    return max(MEDIA_RESERVE_MIN_ML, MEDIA_RESERVE_FRACTION * initial_volume_ml)


def _waste_reserve_ml(capacity_ml: float) -> float:
    """SPEC §15: headroom above which all pumping is suppressed (an influx
    without a matching efflux would overflow the vial)."""
    return max(WASTE_RESERVE_MIN_ML, WASTE_RESERVE_FRACTION * capacity_ml)


def _iso_now() -> str:
    return _now_utc().isoformat(timespec="seconds")


def _is_nan(x: Any) -> bool:
    try:
        return float(x) != float(x)
    except (TypeError, ValueError):
        return False


def _as_list_of_16(value: Any, *, default: float, name: str) -> list[float]:
    """Coerce a parameter into a length-16 list of floats. Accepts a scalar
    (broadcast across all vials) or a list of length 16."""
    if value is None:
        return [float(default)] * N_VIALS
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [float(value)] * N_VIALS
    if isinstance(value, (list, tuple)):
        if len(value) != N_VIALS:
            raise ValueError(
                f"'{name}' list must have length {N_VIALS}, got {len(value)}"
            )
        return [float(v) for v in value]
    raise ValueError(f"'{name}' must be a number or a list of {N_VIALS} numbers")


def _as_flow_rates_32(value: Any) -> list[float]:
    """Coerce ``pump_flow_rates`` into the canonical flat-32 form (CLAUDE.md
    "Pump command format": index 0..15 = influx pump for vial, 16..31 =
    efflux pump for vial-16, i.e. index == the exponent in the hardware's
    binary pump address).

    Accepts, in order of increasing information:
      - ``None``   → the hardcoded per-vial defaults, applied to both directions
      - a scalar   → broadcast to all 32 pumps
      - length 16  → each vial's rate applied to both its influx and efflux
                     pump (the pre-O3 behaviour, and the correct initial state:
                     the directions start equal until Tier 2 measures otherwise)
      - length 32  → used as-is
    """
    if value is None:
        value = list(DEFAULT_FLOW_RATES_ML_PER_SEC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [float(value)] * N_PUMPS
    if isinstance(value, (list, tuple)):
        if len(value) == N_VIALS:
            per_vial = [float(v) for v in value]
            return per_vial + per_vial
        if len(value) == N_PUMPS:
            return [float(v) for v in value]
        raise ValueError(
            f"'pump_flow_rates' list must have length {N_VIALS} (per vial, "
            f"broadcast to both directions) or {N_PUMPS} (canonical pump "
            f"index), got {len(value)}"
        )
    raise ValueError(
        f"'pump_flow_rates' must be a number or a list of {N_VIALS} or "
        f"{N_PUMPS} numbers"
    )


def validate_control_parameters(
    mode: str,
    parameters: dict,
    flow_rates: list[float],
    vials: list[int],
) -> list[str]:
    """Validate the control parameters for `mode` at experiment-creation
    time (CONTROL_MODE_AUDIT.md C-3, and the precondition for T-3).

    Raises ``ValueError`` for a configuration that cannot dilute at all --
    the API maps that to HTTP 400. Returns a list of human-readable warnings
    for configurations that *will* run but not deliver what was asked for.

    Until this existed, ``create_experiment`` validated the mode name, the
    OD acquisition block and the flow-rate array shape, but **no control
    parameters at all**: a band too narrow to produce a fireable bolus, or a
    bolus interval below the firmware's resolution, both started happily and
    then delivered nothing for the length of the run.
    """
    warnings: list[str] = []
    if not vials:
        return warnings
    volume_ml = float(parameters.get("volume_ml", DEFAULT_VOLUME_ML))
    if volume_ml <= 0:
        raise ValueError(f"'volume_ml' must be > 0, got {volume_ml}")
    influx_rates = [flow_rates[v] for v in vials]

    if mode in ("turbidostat", "morbidostat"):
        if mode == "turbidostat":
            od_lower = _as_list_of_16(
                parameters.get("od_lower_thresh", parameters.get("od_lower", 0.2)),
                default=0.2, name="od_lower_thresh",
            )
            od_upper = _as_list_of_16(
                parameters.get("od_upper_thresh", parameters.get("od_upper", 0.4)),
                default=0.4, name="od_upper_thresh",
            )
        else:
            od_lower = _as_list_of_16(
                parameters.get("od_lower", 0.2), default=0.2, name="od_lower",
            )
            od_upper = _as_list_of_16(
                parameters.get("target_od", 0.4), default=0.4, name="target_od",
            )
        for vial in vials:
            lo, hi = od_lower[vial], od_upper[vial]
            if lo <= 0:
                raise ValueError(f"vial {vial}: OD lower threshold must be > 0, got {lo}")
            if hi <= lo:
                raise ValueError(
                    f"vial {vial}: OD upper threshold ({hi}) must be > lower ({lo})"
                )
            flow = flow_rates[vial]
            # Smallest bolus the controller can ever be asked for: the one
            # that takes OD from `hi` (where hysteresis flips the target)
            # down to `lo`. The turbidostat truncates to whole seconds and
            # carries nothing (T-3), so if even this is sub-second the vial
            # never dilutes -- silently, exactly the legacy `%d` bug.
            min_bolus_s = math.log(hi / lo) * volume_ml / flow
            if min_bolus_s < 1.0:
                widest = lo * math.exp(flow / volume_ml)
                raise ValueError(
                    f"vial {vial}: OD band [{lo:g}, {hi:g}] is too narrow to "
                    f"dilute -- the largest bolus it can ever call for is "
                    f"{min_bolus_s:.2f} s, and the 2016 firmware accepts whole "
                    f"seconds only, so nothing would ever fire. With "
                    f"volume_ml={volume_ml:g} and this vial's influx rate "
                    f"{flow:g} mL/s the upper threshold must be at least "
                    f"{widest:.3f}"
                )
            if min_bolus_s < 2.0:
                warnings.append(
                    f"vial {vial}: OD band [{lo:g}, {hi:g}] gives a "
                    f"{min_bolus_s:.2f} s bolus; whole-second truncation "
                    f"discards up to "
                    f"{100.0 * (min_bolus_s - int(min_bolus_s)) / min_bolus_s:.0f}% "
                    "of each dilution. Widen the band for finer control."
                )

    elif mode == "chemostat":
        dilution_rate = float(parameters.get("dilution_rate_per_hour", 0.5))
        if dilution_rate <= 0:
            raise ValueError(
                f"'dilution_rate_per_hour' must be > 0, got {dilution_rate}"
            )
        bolus_interval = parameters.get("bolus_interval_seconds")
        if bolus_interval is None:
            bolus_interval = DEFAULT_CYCLE_INTERVAL_SECONDS
        bolus_interval = float(bolus_interval)
        if bolus_interval < MIN_BOLUS_INTERVAL_SECONDS:
            raise ValueError(
                f"'bolus_interval_seconds' must be >= "
                f"{MIN_BOLUS_INTERVAL_SECONDS} s, got {bolus_interval}: the "
                "per-bolus duration is capped at bolus_interval - 1 s so "
                "consecutive boli cannot overlap, and below 2 s that cap falls "
                "under the firmware's 1 s resolution -- the run would deliver "
                "no media at all while booking the full requested volume"
            )
        safety_cap = min(20.0, max(bolus_interval - 1.0, 0.1))
        for vial in vials:
            flow = flow_rates[vial]
            needed_s = dilution_rate * volume_ml * bolus_interval / 3600.0 / flow
            if needed_s > safety_cap:
                achievable_d = safety_cap * flow * 3600.0 / (volume_ml * bolus_interval)
                warnings.append(
                    f"vial {vial}: D={dilution_rate:g}/h needs {needed_s:.1f} s "
                    f"per bolus but the safety cap is {safety_cap:.1f} s -- every "
                    f"bolus will be clipped and the delivered rate will be about "
                    f"{achievable_d:.2f}/h. Lengthen bolus_interval_seconds or "
                    "lower the dilution rate."
                )
        start_od = parameters.get("start_od")
        if start_od is not None and float(start_od) <= 0:
            raise ValueError(f"'start_od' must be > 0 when given, got {start_od}")
        start_after = parameters.get("start_after_seconds")
        if start_after is not None and float(start_after) < 0:
            raise ValueError(
                f"'start_after_seconds' must be >= 0 when given, got {start_after}"
            )

    if float(parameters.get("efflux_extra_seconds", DEFAULT_EFFLUX_EXTRA_SECONDS)) <= 0:
        warnings.append(
            "efflux_extra_seconds is 0: vial volume is not pinned by the efflux "
            "straw, so level drifts with influx/efflux flow mismatch and no "
            "sensor can detect it (SPEC.md §16.2)."
        )
    if min(influx_rates) <= 0:
        raise ValueError("every active vial needs a positive influx flow rate")
    return warnings


def compute_pump_quantization(volume_ml: float, flow_rate_ml_s: float) -> dict:
    """SPEC §16: the 2016 firmware accepts whole seconds only, so a
    requested mL dose quantises to ``floor(volume_ml / flow_rate)`` seconds.

    Returns ``{"seconds": int, "deliverable_ml": float, "min_ml": float,
    "quantised": bool}``. ``min_ml`` is the smallest non-zero dose this
    vial/direction can deliver (one whole second); the caller is
    responsible for rejecting requests where ``seconds == 0`` rather than
    silently firing nothing -- that silent-truncation failure mode is the
    legacy `%d` bug documented in SPEC §9."""
    if flow_rate_ml_s <= 0:
        raise ValueError(f"flow_rate_ml_s must be positive, got {flow_rate_ml_s}")
    if volume_ml < 0:
        raise ValueError(f"volume_ml must be >= 0, got {volume_ml}")
    raw_seconds = volume_ml / flow_rate_ml_s
    # Epsilon nudge so an exact multiple isn't floored down by fp noise
    # (e.g. 5.0 / 1.0000000000000002 landing at 4.999999999999999).
    seconds = int(math.floor(raw_seconds + 1e-9))
    deliverable_ml = seconds * flow_rate_ml_s
    return {
        "seconds": seconds,
        "deliverable_ml": deliverable_ml,
        "min_ml": flow_rate_ml_s,
        "quantised": abs(deliverable_ml - volume_ml) > 1e-9,
    }


def _parse_od_acquisition(parameters: dict) -> dict:
    """Validate and normalize the optional ``od_acquisition`` block from an
    experiment's parameters. Returns a complete dict with all four keys, falling
    back to :data:`DEFAULT_OD_ACQUISITION` for anything omitted. Raises
    ``ValueError`` on malformed values (the API maps these to HTTP 400)."""
    block = parameters.get("od_acquisition") or {}
    if not isinstance(block, dict):
        raise ValueError("'od_acquisition' must be an object")
    out = dict(DEFAULT_OD_ACQUISITION)
    out.update({k: block[k] for k in block if k in DEFAULT_OD_ACQUISITION})

    n_samples = int(out["n_samples"])
    if n_samples < 1:
        raise ValueError(f"od_acquisition.n_samples must be >= 1, got {n_samples}")
    n_dark = int(out["n_dark"])
    if n_dark < 0:
        raise ValueError(f"od_acquisition.n_dark must be >= 0, got {n_dark}")
    if not isinstance(out["dark_subtract"], bool):
        raise ValueError("od_acquisition.dark_subtract must be a boolean")
    agg = str(out["agg"])
    if agg not in OD_AGG_CHOICES:
        raise ValueError(
            f"od_acquisition.agg must be one of {OD_AGG_CHOICES}, got {agg!r}"
        )
    return {
        "n_samples": n_samples,
        "dark_subtract": out["dark_subtract"],
        "n_dark": n_dark,
        "agg": agg,
    }


def _validate_and_normalize_media(media: dict) -> dict:
    """Validate the media block from POST /experiments/create.

    Returns a normalized copy with: ``bottles`` carrying defaults for
    ``low_volume_alert_ml`` (= 10 % of initial), ``vial_to_bottle`` with
    string keys, and ``waste`` carrying a default ``high_fill_alert_ml``
    (= 90 % of capacity). Raises ``ValueError`` with a descriptive message
    on any failure — caller maps these to HTTP 400.
    """
    if not isinstance(media, dict):
        raise ValueError("'media' must be an object")
    bottles_in = media.get("bottles")
    if not isinstance(bottles_in, list) or not bottles_in:
        raise ValueError("'media.bottles' must be a non-empty list")
    seen_ids: set[str] = set()
    bottles: list[dict] = []
    for i, b in enumerate(bottles_in):
        if not isinstance(b, dict):
            raise ValueError(f"'media.bottles[{i}]' must be an object")
        bid = b.get("id")
        if not isinstance(bid, str) or not _VALID_BOTTLE_ID.match(bid):
            raise ValueError(
                f"'media.bottles[{i}].id' must match {_VALID_BOTTLE_ID.pattern!r}; got {bid!r}"
            )
        if bid in seen_ids:
            raise ValueError(f"duplicate bottle id {bid!r}")
        seen_ids.add(bid)
        name = b.get("name", "")
        if not isinstance(name, str):
            raise ValueError(f"'media.bottles[{i}].name' must be a string")
        contents = b.get("contents", "")
        if not isinstance(contents, str):
            raise ValueError(f"'media.bottles[{i}].contents' must be a string")
        initial = b.get("initial_volume_ml")
        if not isinstance(initial, (int, float)) or isinstance(initial, bool) or initial <= 0:
            raise ValueError(
                f"'media.bottles[{i}].initial_volume_ml' must be a positive number"
            )
        initial = float(initial)
        low = b.get("low_volume_alert_ml")
        if low is None:
            low = initial * 0.10  # default 10 % remaining
        if not isinstance(low, (int, float)) or isinstance(low, bool) or low < 0:
            raise ValueError(
                f"'media.bottles[{i}].low_volume_alert_ml' must be >= 0"
            )
        bottles.append({
            "id": bid,
            "name": name,
            "contents": contents,
            "initial_volume_ml": initial,
            "low_volume_alert_ml": float(low),
        })

    v2b_in = media.get("vial_to_bottle")
    if not isinstance(v2b_in, dict) or not v2b_in:
        raise ValueError("'media.vial_to_bottle' must be a non-empty object")
    vial_to_bottle: dict[str, str] = {}
    for k, v in v2b_in.items():
        try:
            vi = int(k)
        except (TypeError, ValueError):
            raise ValueError(f"'media.vial_to_bottle' key {k!r} must be an int 0..15")
        if not (0 <= vi < N_VIALS):
            raise ValueError(f"vial {vi} out of range 0..{N_VIALS - 1}")
        if v not in seen_ids:
            raise ValueError(
                f"'media.vial_to_bottle[{k}]' references unknown bottle id {v!r}"
            )
        vial_to_bottle[str(vi)] = v

    waste_in = media.get("waste") or {}
    if not isinstance(waste_in, dict):
        raise ValueError("'media.waste' must be an object")
    capacity = waste_in.get("capacity_ml")
    if not isinstance(capacity, (int, float)) or isinstance(capacity, bool) or capacity <= 0:
        raise ValueError("'media.waste.capacity_ml' must be a positive number")
    capacity = float(capacity)
    high = waste_in.get("high_fill_alert_ml")
    if high is None:
        high = capacity * 0.90  # default 90 % full
    if not isinstance(high, (int, float)) or isinstance(high, bool) or high < 0:
        raise ValueError("'media.waste.high_fill_alert_ml' must be >= 0")
    if high > capacity:
        raise ValueError(
            "'media.waste.high_fill_alert_ml' must be <= capacity_ml"
        )
    waste_name = waste_in.get("name", "")
    if not isinstance(waste_name, str):
        raise ValueError("'media.waste.name' must be a string")

    return {
        "bottles": bottles,
        "vial_to_bottle": vial_to_bottle,
        "waste": {
            "name": waste_name,
            "capacity_ml": capacity,
            "high_fill_alert_ml": float(high),
        },
    }


class ExperimentEngine:
    def __init__(
        self,
        serial_manager,
        data_logger,
        experiments_root: Path,
        *,
        on_event: Optional[Callable[[dict], None]] = None,
        on_alert: Optional[Callable[[dict], None]] = None,
        temp_cal: Optional[np.ndarray] = None,
        clock: Callable[[], float] = time.time,
        cycle_interval_seconds: float = DEFAULT_CYCLE_INTERVAL_SECONDS,
        sensor_failure_threshold: int = DEFAULT_SENSOR_FAILURE_THRESHOLD,
        heater_overrun_C: float = DEFAULT_HEATER_OVERRUN_C,
        heater_critical_C: float = DEFAULT_HEATER_CRITICAL_C,
        maintenance_timeout_minutes: float = DEFAULT_MAINTENANCE_TIMEOUT_MINUTES,
    ) -> None:
        self._manager = serial_manager
        self._data_logger = data_logger
        self._experiments_root = Path(experiments_root)
        self._on_event = on_event
        self._on_alert = on_alert
        self._temp_cal = temp_cal
        self._clock = clock
        self._cycle_interval_seconds = float(cycle_interval_seconds)
        self._sensor_failure_threshold = int(sensor_failure_threshold)
        self._heater_overrun_C = float(heater_overrun_C)
        self._heater_critical_C = float(heater_critical_C)
        self._maintenance_timeout_seconds = float(maintenance_timeout_minutes) * 60.0

        self._lock = threading.RLock()

        # Loaded experiment (single-experiment Phase 1)
        self._status: str = ExperimentStatus.IDLE
        self._name: Optional[str] = None
        self._config: Optional[dict] = None
        self._vials: list[int] = []
        self._controllers: dict[int, ControllerType] = {}
        self._setpoint_raw: dict[int, int] = {}
        self._setpoint_stir: int = 0
        self._nan_streak: dict[int, int] = {}
        self._od_range_streak: dict[int, int] = {}
        self._vial_faults: dict[int, Optional[str]] = {}
        self._created_at: Optional[datetime] = None
        self._started_at: Optional[datetime] = None
        self._stopped_at: Optional[datetime] = None
        self._stop_reason: Optional[str] = None

        # Media tracking (Phase 1: bottles are purely logical; static for run)
        self._media_bottles: dict[str, dict] = {}     # id -> static config
        self._vial_to_bottle: dict[int, str] = {}     # vial -> bottle id
        self._bottle_consumed_ml: dict[str, float] = {}
        self._bottle_alerted_low: dict[str, bool] = {}
        # Consumables interlock (SPEC §15) — one-shot critical-alert latches,
        # distinct from the low/high warning latches above (different
        # thresholds: low_volume_alert_ml/high_fill_alert_ml are a
        # user-configurable heads-up, reserve_ml is the hard-stop floor).
        self._bottle_alerted_blocked: dict[str, bool] = {}
        self._waste_config: Optional[dict] = None
        self._waste_filled_ml: float = 0.0
        self._waste_alerted_high: bool = False
        self._waste_alerted_blocked: bool = False

        # Maintenance mode (pauses pump execution; coalesces decisions per
        # vial so we don't over-dilute on resume). Cleared on stop.
        self._maintenance_active: bool = False
        self._maintenance_entered_at: Optional[datetime] = None
        # "manual" (user-requested) or "consumables" (auto-entered by the
        # interlock when every active vial is blocked). Consumables-reason
        # maintenance is exempt from the 30 min auto-resume failsafe.
        self._maintenance_reason: Optional[str] = None
        # vial -> (PumpAction, ts_iso captured at decide time)
        self._pending_pump_actions: dict[int, tuple[PumpAction, str]] = {}

        self._experiments_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._status == ExperimentStatus.RUNNING

    @property
    def loaded_experiment(self) -> Optional[str]:
        with self._lock:
            return self._name

    @property
    def loaded_vials(self) -> list[int]:
        with self._lock:
            return list(self._vials)

    @property
    def status_string(self) -> str:
        with self._lock:
            return self._status

    def od_acquisition_params(self) -> dict:
        """Enhanced-OD acquisition parameters for the loaded experiment, as
        keyword args for ``SerialManager.read_od_enhanced`` (``n_samples``,
        ``dark_subtract``, ``n_dark``, ``agg``). Returns the validated
        per-experiment ``od_acquisition`` block merged over defaults; falls back
        to :data:`DEFAULT_OD_ACQUISITION` when nothing is loaded."""
        with self._lock:
            params = (self._config or {}).get("parameters", {})
        try:
            return _parse_od_acquisition(params)
        except ValueError:
            # Config was validated at create time; if somehow malformed (e.g.
            # hand-edited), fall back to safe defaults rather than break reads.
            log.exception("invalid od_acquisition in loaded config; using defaults")
            return dict(DEFAULT_OD_ACQUISITION)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def create_experiment(
        self,
        name: str,
        mode: str,
        vials: Optional[list[int]] = None,
        parameters: Optional[dict] = None,
        calibration: Optional[dict] = None,
        notes: str = "",
        media: Optional[dict] = None,
    ) -> dict:
        """Validate, create the experiment directory via DataLogger, write
        the initial ``state.json``, and transition IDLE → CREATED. Returns
        the saved config.

        When ``media`` is provided, ``vials`` may be omitted — the engine
        derives the vial list from ``media.vial_to_bottle.keys()``. If both
        are supplied they must agree (sorted-equal).
        """
        if mode not in SUPPORTED_MODES:
            raise ValueError(
                f"unsupported mode {mode!r}; supported: {sorted(SUPPORTED_MODES)}"
            )

        # Validate the optional od_acquisition block up front so a malformed
        # config fails at create time (HTTP 400) rather than at first read.
        od_acq = _parse_od_acquisition(parameters or {})
        # Dark-subtraction coherence guard (SPEC §19.2 / CALIBRATION_PROTOCOL
        # §13): a curve fit on non-dark-subtracted signal gives silently wrong
        # OD when the reader subtracts the dark first. Hard error, not a
        # warning — the sidecar (OD_cal.meta.json) is the only thing that may
        # authorise it.
        if od_acq["dark_subtract"] and not getattr(
            self._manager, "od_cal_dark_subtracted", False
        ):
            raise ValueError(
                "od_acquisition.dark_subtract=true requires an OD calibration "
                "whose OD_cal.meta.json sidecar records dark_subtracted=true; "
                "the loaded calibration was fit on non-dark-subtracted signal "
                "(see CALIBRATION_PROTOCOL.md §5.4)"
            )

        # Validate pump_flow_rates shape up front so a malformed array fails
        # at create time (HTTP 400), not at start_experiment.
        flow_rates = self._resolve_flow_rates(parameters or {}, calibration or {})

        normalized_media: Optional[dict] = None
        if media is not None:
            normalized_media = _validate_and_normalize_media(media)
            derived_vials = sorted(int(k) for k in normalized_media["vial_to_bottle"])
            if vials is None or len(vials) == 0:
                vials = derived_vials
            else:
                if sorted(int(v) for v in vials) != derived_vials:
                    raise ValueError(
                        "'vials' must equal the keys of media.vial_to_bottle "
                        f"({derived_vials}); got {sorted(vials)}"
                    )

        if vials is None or len(vials) == 0:
            raise ValueError("'vials' must be a non-empty list (or supply 'media')")

        # Control parameters, now that the vial list is settled. Hard errors
        # (a band that cannot dilute, a sub-2 s bolus interval) become HTTP
        # 400; the warnings are returned to the caller and raised as alerts
        # so they land in the run's event log rather than only in a response
        # body someone may not read.
        control_warnings = validate_control_parameters(
            mode, parameters or {}, flow_rates,
            sorted(int(v) for v in vials),
        )

        with self._lock:
            self._assert_status(ExperimentStatus.IDLE, ExperimentStatus.STOPPED, ExperimentStatus.ERROR)
            # If a previous experiment is in STOPPED/ERROR, unload it before creating a new one.
            if self._status in (ExperimentStatus.STOPPED, ExperimentStatus.ERROR):
                self._unload_locked()

            config = self._data_logger.create_experiment(
                name=name,
                mode=mode,
                vials=vials,
                parameters=parameters or {},
                calibration=calibration or {},
                notes=notes,
                media=normalized_media,
            )
            self._status = ExperimentStatus.CREATED
            self._name = name
            self._config = config
            self._vials = sorted(int(v) for v in config["vials"])
            self._controllers = {}  # built at start()
            # Init experiment vials parked OFF; start_experiment will replace
            # these with the target temperatures derived from config parameters.
            self._setpoint_raw = {v: HEATER_OFF_SETPOINT for v in self._vials}
            self._setpoint_stir = 0
            self._nan_streak = {v: 0 for v in self._vials}
            self._od_range_streak = {v: 0 for v in self._vials}
            self._vial_faults = {v: None for v in self._vials}
            self._created_at = _now_utc()
            self._started_at = None
            self._stopped_at = None
            self._stop_reason = None
            # Reset media tracking; populated at start_experiment time below.
            self._media_bottles = {}
            self._vial_to_bottle = {}
            self._bottle_consumed_ml = {}
            self._bottle_alerted_low = {}
            self._waste_config = None
            self._waste_filled_ml = 0.0
            self._waste_alerted_high = False
            self._save_state_locked()

        self._broadcast_event({"type": "created", "name": name, "vials": self._vials})
        for warning in control_warnings:
            log.warning("experiment '%s': %s", name, warning)
            self._broadcast_alert(
                level="warning",
                message=f"'{name}' control parameters: {warning}",
                category="lifecycle",
            )
        log.info("experiment '%s' created (vials=%s)", name, self._vials)
        config = dict(config)
        config["warnings"] = control_warnings
        return config

    def start_experiment(self, name: Optional[str] = None) -> dict:
        """Build per-vial controllers, send initial actuator commands,
        activate the DataLogger, and transition CREATED → RUNNING."""
        with self._lock:
            self._assert_status(ExperimentStatus.CREATED)
            if name is not None and name != self._name:
                raise ValueError(
                    f"requested start of '{name}' but '{self._name}' is loaded"
                )
            params = self._config["parameters"]
            calibration = self._config.get("calibration", {})

            mode = self._config.get("mode", "turbidostat")
            self._controllers = self._build_controllers(
                mode, params, calibration, self._vials
            )
            self._setpoint_stir = int(params.get("stir_rate", 10))

            # Initialise media tracking from the persisted config (if present).
            self._load_media_locked(self._config.get("media"))

            self._started_at = _now_utc()
            self._data_logger.activate_experiment(self._name, start=self._started_at)

            try:
                self._apply_initial_actuators_locked(params)
            except Exception:
                log.exception("failed to apply initial actuators; aborting start")
                self._data_logger.deactivate_experiment()
                self._started_at = None
                raise

            self._status = ExperimentStatus.RUNNING
            self._save_state_locked()
            cfg = dict(self._config)
            efflux_extra = float(
                params.get("efflux_extra_seconds", DEFAULT_EFFLUX_EXTRA_SECONDS)
            )

        self._broadcast_event({"type": "started", "name": cfg["name"], "vials": self._vials})
        # X-1 (CONTROL_MODE_AUDIT.md / SPEC §16.2): efflux overrun is what
        # engages the ONLY volume-regulation loop this machine has -- the
        # efflux straw draws air once the level reaches its tip, pinning
        # working volume to the straw height on every dilution. At 0.0 that
        # loop is disengaged and the level becomes an open-loop integral of
        # influx/efflux flow mismatch, which no software can observe because
        # there is no level sensor. Whether 0.0 is right is a bench question
        # (see SPEC §16.2); making the run start silently is not.
        if efflux_extra <= 0.0:
            self._broadcast_alert(
                level="warning",
                message=(
                    "efflux_extra_seconds is 0 -- vial volume regulation is "
                    "DISENGAGED. Working volume is not pinned by the efflux "
                    "straw, so level drifts with influx/efflux flow mismatch "
                    "and there is no level sensor to catch it. See SPEC.md "
                    "§16.2 before running unattended."
                ),
                category="pump",
                dedup_key="efflux_overrun_disabled",
            )
        log.info("experiment '%s' started", cfg["name"])
        return cfg

    def stop_experiment(self, reason: str = "manual") -> Optional[str]:
        """Zero pumps/heater/stir for experiment vials, deactivate the
        DataLogger, persist state, and transition to STOPPED.

        Idempotent: a no-op if already STOPPED or IDLE."""
        with self._lock:
            if self._status == ExperimentStatus.IDLE:
                return None
            if self._status == ExperimentStatus.STOPPED:
                # Already stopped — idempotent return.
                return self._name
            if self._status == ExperimentStatus.CREATED:
                # CREATED never sent actuator commands, so no zeroing needed.
                name = self._name
                self._stopped_at = _now_utc()
                self._stop_reason = reason
                self._status = ExperimentStatus.STOPPED
                self._save_state_locked()
                self._broadcast_event({"type": "stopped", "name": name, "reason": reason})
                log.info("experiment '%s' stopped from CREATED state (%s)", name, reason)
                return name

            self._assert_status(ExperimentStatus.RUNNING, ExperimentStatus.ERROR)
            name = self._name

            # Zero actuators for experiment vials only (preserve others).
            self._zero_experiment_actuators_locked()
            # Stop logging
            try:
                self._data_logger.deactivate_experiment()
            except Exception:
                log.exception("data_logger.deactivate_experiment failed during stop")

            # Drop maintenance state — stopping an experiment dismisses any
            # pending pump actions (they would be stale and were never fired).
            self._maintenance_active = False
            self._maintenance_entered_at = None
            self._pending_pump_actions = {}

            self._stopped_at = _now_utc()
            self._stop_reason = reason
            self._status = ExperimentStatus.STOPPED
            self._save_state_locked()

        self._broadcast_event({"type": "stopped", "name": name, "reason": reason})
        log.info("experiment '%s' stopped (%s)", name, reason)
        return name

    def delete_experiment(self, name: str) -> None:
        """Remove an experiment's directory. Legal only when (a) the
        experiment is not the loaded one, OR (b) the loaded one is in
        STOPPED/ERROR status."""
        with self._lock:
            if self._name == name and self._status not in (
                ExperimentStatus.STOPPED,
                ExperimentStatus.ERROR,
                ExperimentStatus.IDLE,
            ):
                raise InvalidExperimentStateError(
                    f"cannot delete '{name}' while it is loaded with status={self._status}"
                )
            if self._name == name:
                self._unload_locked()
            exp_dir = self._experiments_root / name
            if not exp_dir.is_dir():
                raise FileNotFoundError(f"experiment directory not found: {exp_dir}")
            shutil.rmtree(exp_dir)
        log.info("experiment '%s' deleted", name)

    def rename_experiment(self, old: str, new: str) -> dict:
        """Rename an experiment directory and its ``name`` field in
        ``config.json`` / ``state.json``.

        Blocked while ``old`` is the loaded experiment AND it is RUNNING or in
        maintenance mode (mirrors the :meth:`delete_experiment` guard); allowed
        for unloaded experiments or a loaded one in CREATED/STOPPED/ERROR.
        Returns ``{"old": old, "new": new}``.

        Raises:
            ValueError: ``new`` is malformed or equal to ``old``.
            InvalidExperimentStateError: ``old`` is loaded and running.
            FileNotFoundError: ``old`` directory does not exist.
            FileExistsError: ``new`` directory already exists.
        """
        if not isinstance(new, str) or not _VALID_NAME.match(new):
            raise ValueError(
                f"experiment name must match {_VALID_NAME.pattern!r}; got {new!r}"
            )
        with self._lock:
            is_loaded = self._name == old
            if is_loaded and (
                self._status == ExperimentStatus.RUNNING or self._maintenance_active
            ):
                raise InvalidExperimentStateError(
                    f"cannot rename '{old}' while it is loaded with status="
                    f"{self._status}"
                )
            old_dir = self._experiments_root / old
            new_dir = self._experiments_root / new
            if not old_dir.is_dir():
                raise FileNotFoundError(f"experiment directory not found: {old_dir}")
            if new == old:
                raise ValueError("new name is the same as the old name")
            if new_dir.exists():
                raise FileExistsError(
                    f"experiment directory already exists: {new_dir}"
                )

            shutil.move(str(old_dir), str(new_dir))

            # Rewrite the name field in config.json (atomic) and state.json.
            try:
                self._data_logger.update_experiment_config(new, {"name": new})
            except Exception:
                log.exception("rename: failed to update config.json name")
            state_path = new_dir / "state.json"
            if state_path.is_file():
                try:
                    s = json.loads(state_path.read_text(encoding="utf-8"))
                    s["name"] = new
                    tmp = new_dir / "state.json.tmp"
                    tmp.write_text(json.dumps(s, indent=4), encoding="utf-8")
                    os.replace(tmp, state_path)
                except Exception:
                    log.exception("rename: failed to update state.json name")

            if is_loaded:
                self._name = new
                if self._config is not None:
                    self._config["name"] = new

        self._broadcast_event({"type": "renamed", "old": old, "new": new})
        log.info("experiment '%s' renamed to '%s'", old, new)
        return {"old": old, "new": new}

    def update_metadata(
        self,
        name: str,
        *,
        notes: Optional[str] = None,
        tags: Optional[list] = None,
    ) -> dict:
        """Edit free-text ``notes`` and/or ``tags`` on an experiment's
        ``config.json``. Works whether or not ``name`` is the loaded
        experiment; when it is loaded, the in-memory config is refreshed so
        :meth:`status` reflects the edit. Returns the merged config dict.

        Raises:
            ValueError: notes/tags are the wrong type, or nothing to update.
            FileNotFoundError: config.json missing (from the DataLogger).
        """
        partial: dict = {}
        if notes is not None:
            if not isinstance(notes, str):
                raise ValueError("notes must be a string")
            partial["notes"] = notes
        if tags is not None:
            if not isinstance(tags, list) or not all(
                isinstance(t, str) for t in tags
            ):
                raise ValueError("tags must be a list of strings")
            partial["tags"] = tags
        if not partial:
            raise ValueError("nothing to update (provide notes and/or tags)")

        merged = self._data_logger.update_experiment_config(name, partial)
        with self._lock:
            if self._name == name and self._config is not None:
                self._config = merged
        self._broadcast_event({"type": "metadata_updated", "name": name})
        log.info("experiment '%s' metadata updated (%s)", name, list(partial))
        return merged

    def record_calibration_provenance(self, name: str, partial: dict) -> dict:
        """Deep-merge keys into an experiment's ``config.json`` ``calibration``
        block (SPEC §19.1 provenance: subsystem versions, per-run blank path,
        measured ``pump_flow_rates``) and refresh the in-memory config when
        ``name`` is loaded, so a blank committed against a CREATED experiment
        is visible to :meth:`start_experiment` without a reload.

        Raises ``ValueError`` on a non-dict partial;
        ``FileNotFoundError`` from the DataLogger when config.json is missing.
        """
        if not isinstance(partial, dict) or not partial:
            raise ValueError("calibration provenance must be a non-empty object")
        merged = self._data_logger.update_experiment_config(
            name, {"calibration": partial}
        )
        with self._lock:
            if self._name == name and self._config is not None:
                self._config = merged
        log.info(
            "experiment '%s' calibration provenance updated (%s)",
            name, list(partial),
        )
        return merged

    def handle_emergency_stop(self) -> None:
        """Called by `api_emergency_stop` after `manager.emergency_shutdown()`
        has already zeroed everything: broadcast a critical alert, then
        fully stop any active experiment via `stop_experiment` (transitions
        to STOPPED). Idempotent — no-op when no experiment is loaded or
        the experiment is already stopped."""
        with self._lock:
            if self._status not in (
                ExperimentStatus.RUNNING,
                ExperimentStatus.CREATED,
                ExperimentStatus.ERROR,
            ):
                return
            name = self._name
        self._broadcast_alert(
            level="critical",
            message=f"Experiment '{name}' stopped by emergency stop",
            category="lifecycle",
        )
        # stop_experiment is idempotent and re-entrant under the RLock.
        self.stop_experiment(reason="emergency_stop")

    # ------------------------------------------------------------------
    # Maintenance mode
    # ------------------------------------------------------------------
    # Pauses pump execution so the user can refill bottles / empty the
    # waste carboy without disrupting the experiment. Sensor reads, CSV
    # logging, and per-vial control decisions all keep running — only the
    # PHYSICAL pump commands are suppressed. Pump actions decided during
    # maintenance are queued per-vial (newest wins, to avoid over-diluting
    # on resume).
    #
    # Auto-resume failsafe: if maintenance has been active for more than
    # `maintenance_timeout_minutes`, the engine self-exits with a critical
    # alert so the experiment doesn't stall indefinitely.

    @property
    def is_maintenance_active(self) -> bool:
        with self._lock:
            return self._maintenance_active

    def enter_maintenance(self, reason: str = "manual") -> dict:
        """Enter maintenance mode. Subsequent run_cycle calls queue pump
        actions instead of returning them for execution. Idempotent — a
        no-op when already active. Allowed only while RUNNING.

        ``reason`` distinguishes a manually-requested pause ("manual", the
        default) from an automatic one raised by the consumables interlock
        ("consumables") when every active vial is blocked. Consumables-reason
        maintenance is exempt from the 30 min auto-resume failsafe (see
        check_maintenance_timeout) — auto-resuming into a still-empty bottle
        would defeat SPEC §15's "clears only on refill_media" requirement."""
        with self._lock:
            if self._status != ExperimentStatus.RUNNING:
                raise InvalidExperimentStateError(
                    f"maintenance mode is only allowed while RUNNING; "
                    f"current status={self._status}"
                )
            return self._enter_maintenance_locked(reason)

    def _enter_maintenance_locked(self, reason: str = "manual") -> dict:
        if self._maintenance_active:
            return self._maintenance_status_locked()
        self._maintenance_active = True
        self._maintenance_entered_at = _now_utc()
        self._maintenance_reason = reason
        self._pending_pump_actions = {}
        self._save_state_locked()
        status = self._maintenance_status_locked()
        self._broadcast_event({
            "type": "maintenance_entered",
            "name": self._name,
            "reason": reason,
        })
        if reason == "consumables":
            self._broadcast_alert(
                level="critical",
                message=(
                    "Maintenance mode — pumps suppressed (consumables "
                    "blocked). Refill required; will not auto-resume."
                ),
                category="maintenance",
            )
        else:
            self._broadcast_alert(
                level="warning",
                message=(
                    "Maintenance mode — pumps suppressed. "
                    f"Auto-resume in {self._maintenance_timeout_seconds / 60:.0f} min."
                ),
                category="maintenance",
            )
        log.warning(
            "maintenance mode entered for experiment '%s' (reason=%s)",
            self._name, reason,
        )
        return status

    def exit_maintenance(
        self, reason: str = "manual"
    ) -> list[tuple[int, PumpAction, str]]:
        """Exit maintenance mode and return any queued pump actions for the
        caller (sensor_loop) to execute. Idempotent — returns an empty
        list when not in maintenance."""
        with self._lock:
            if not self._maintenance_active:
                return []
            queued = [
                (vial, action, ts_iso)
                for vial, (action, ts_iso) in sorted(self._pending_pump_actions.items())
            ]
            self._maintenance_active = False
            self._maintenance_entered_at = None
            self._maintenance_reason = None
            self._pending_pump_actions = {}
            self._save_state_locked()
        self._broadcast_event({
            "type": "maintenance_exited",
            "name": self._name,
            "reason": reason,
            "queued_actions": len(queued),
        })
        log.info(
            "maintenance mode exited (%s) for experiment '%s'; firing %d queued action(s)",
            reason, self._name, len(queued),
        )
        return queued

    def check_maintenance_timeout(
        self,
    ) -> Optional[list[tuple[int, PumpAction, str]]]:
        """Called from sensor_loop each cycle. If maintenance has been active
        for longer than the configured timeout, auto-exit with a critical
        alert and return any queued actions for the caller to execute.
        Returns ``None`` otherwise."""
        with self._lock:
            if not self._maintenance_active or self._maintenance_entered_at is None:
                return None
            if self._maintenance_reason == "consumables":
                # Sticky by design (SPEC §15) — only refill_media clears
                # this, never a timer.
                return None
            elapsed = (_now_utc() - self._maintenance_entered_at).total_seconds()
            if elapsed < self._maintenance_timeout_seconds:
                return None
        # Outside lock: emit alert + exit (exit takes the lock).
        self._broadcast_alert(
            level="critical",
            message=(
                f"Maintenance mode auto-resumed after "
                f"{self._maintenance_timeout_seconds / 60:.0f} min — "
                "experiment was at risk of stalling"
            ),
            category="maintenance",
        )
        return self.exit_maintenance(reason="auto_timeout")

    def refill_media(
        self,
        *,
        bottles: Optional[dict[str, float]] = None,
        waste_filled_ml: Optional[float] = None,
    ) -> dict:
        """Update bottle remaining volumes and/or waste fill level during
        maintenance mode. ``bottles`` maps bottle_id → new remaining_ml
        (i.e. user's measurement after refill). Resets the corresponding
        alert latches when levels are restored below their thresholds.
        Returns the updated media status."""
        with self._lock:
            if self._status != ExperimentStatus.RUNNING:
                raise InvalidExperimentStateError(
                    "refill is only allowed while the experiment is RUNNING"
                )
            if bottles:
                for bid, remaining_ml in bottles.items():
                    if bid not in self._media_bottles:
                        raise ValueError(f"unknown bottle id {bid!r}")
                    if not isinstance(remaining_ml, (int, float)) or isinstance(
                        remaining_ml, bool
                    ) or remaining_ml < 0:
                        raise ValueError(
                            f"bottles[{bid!r}] remaining_ml must be >= 0"
                        )
                    initial = self._media_bottles[bid]["initial_volume_ml"]
                    if remaining_ml > initial:
                        raise ValueError(
                            f"bottles[{bid!r}] remaining_ml ({remaining_ml}) "
                            f"exceeds initial volume ({initial})"
                        )
                    self._bottle_consumed_ml[bid] = initial - float(remaining_ml)
                    # Clear the low-volume and interlock-blocked latches so
                    # the next genuine crossing re-alerts (otherwise the
                    # alert would stay silent forever after a refill).
                    self._bottle_alerted_low[bid] = False
                    self._bottle_alerted_blocked[bid] = False
            if waste_filled_ml is not None:
                if self._waste_config is None:
                    raise ValueError("experiment has no waste container configured")
                if not isinstance(waste_filled_ml, (int, float)) or isinstance(
                    waste_filled_ml, bool
                ) or waste_filled_ml < 0:
                    raise ValueError("waste_filled_ml must be >= 0")
                cap = self._waste_config["capacity_ml"]
                if waste_filled_ml > cap:
                    raise ValueError(
                        f"waste_filled_ml ({waste_filled_ml}) exceeds capacity ({cap})"
                    )
                self._waste_filled_ml = float(waste_filled_ml)
                self._waste_alerted_high = False
                self._waste_alerted_blocked = False
            self._save_state_locked()
            media = self._media_status_locked()
        log.info("media refilled during maintenance: bottles=%s waste=%s",
                 bottles, waste_filled_ml)
        # SPEC §20.2 lists refills among the events a run must record -- without
        # this, a bottle change is invisible in the lab-notebook artefact even
        # though it resets the consumption baseline.
        self._broadcast_event({
            "type": "refill",
            "name": self._name,
            "bottles": dict(bottles or {}),
            "waste_filled_ml": waste_filled_ml,
        })
        return {"media": media}

    def _maintenance_status_locked(self) -> dict:
        """Build the maintenance block for status() / sensor_update payloads.
        Always returns a dict (even when inactive) so the dashboard can
        unconditionally read flags."""
        if not self._maintenance_active or self._maintenance_entered_at is None:
            return {
                "active": False,
                "reason": None,
                "entered_at": None,
                "auto_resume_at": None,
                "auto_resume_in_seconds": None,
                "queued_pump_count": 0,
            }
        entered = self._maintenance_entered_at
        # Consumables-triggered maintenance has no auto-resume (SPEC §15
        # sticky requirement) -- report no countdown rather than a
        # misleading one that will never fire.
        if self._maintenance_reason == "consumables":
            auto_resume_at = None
            auto_resume_in_seconds = None
        else:
            auto_at = entered.timestamp() + self._maintenance_timeout_seconds
            remaining = auto_at - _now_utc().timestamp()
            auto_resume_at = datetime.fromtimestamp(
                auto_at, tz=timezone.utc
            ).isoformat(timespec="seconds")
            auto_resume_in_seconds = max(0.0, round(remaining, 1))
        return {
            "active": True,
            "reason": self._maintenance_reason,
            "entered_at": entered.isoformat(timespec="seconds"),
            "auto_resume_at": auto_resume_at,
            "auto_resume_in_seconds": auto_resume_in_seconds,
            "queued_pump_count": len(self._pending_pump_actions),
        }

    # ------------------------------------------------------------------
    # Per-cycle entry point
    # ------------------------------------------------------------------

    def run_cycle(
        self,
        timestamp_iso: str,
        temperature_calibrated: list[float],
        od_calibrated: list[float],
        od_flags: Optional[list[str]] = None,
    ) -> list[tuple[int, PumpAction]]:
        """Run one control-loop tick. Returns pump actions the caller
        should execute (each as a ``(vial, PumpAction)`` tuple). Returns
        ``[]`` when the engine isn't RUNNING or no vial wants a pump.

        The caller (sensor_loop in app.py) is responsible for:
          - firing the pumps via ``serial_manager.pump_command``,
          - logging via ``data_logger.log_pump_event``,
          - emitting ``experiment_event`` over socketio.

        The engine still handles per-vial heater safety, NaN-streak
        fault latching, stir re-send, and state.json persistence —
        those are pure engine concerns that don't need the caller.

        ``od_flags`` is the optional per-vial flag list from the enhanced OD
        reader (``"ok" | "out_of_range" | "dropped"``). When provided, an
        ``out_of_range`` reading (OD past the calibrated domain — likely
        saturation) is tracked on a SEPARATE streak with a distinct warning,
        rather than being counted as a lossy-bus dropped read. ``None``
        preserves the legacy behavior (all NaN OD treated as dropped).
        """
        if len(temperature_calibrated) != N_VIALS or len(od_calibrated) != N_VIALS:
            log.warning(
                "run_cycle: sensor arrays must be length %d; got %d / %d",
                N_VIALS,
                len(temperature_calibrated),
                len(od_calibrated),
            )
            return []

        now = self._clock()
        pump_actions: list[tuple[int, PumpAction]] = []

        with self._lock:
            if self._status != ExperimentStatus.RUNNING:
                return []
            for vial in list(self._vials):
                if self._vial_faults.get(vial) is not None:
                    continue  # latched fault — skip
                temp_c = temperature_calibrated[vial]
                od = od_calibrated[vial]
                temp_nan = _is_nan(temp_c)
                od_nan = _is_nan(od)

                # (1a) OD out-of-range: a DISTINCT condition from a lossy
                # dropped read. The OD is NaN (range guard rejected it), but
                # the bus delivered data — likely the culture is denser than
                # the calibration covers (saturation). Track on its own streak,
                # warn once on crossing the threshold, and skip OD control —
                # but let heater safety still run if the temperature is valid.
                od_out_of_range = (
                    od_flags is not None
                    and vial < len(od_flags)
                    and od_flags[vial] == "out_of_range"
                )
                if od_out_of_range:
                    self._od_range_streak[vial] = self._od_range_streak.get(vial, 0) + 1
                    if self._od_range_streak[vial] == self._sensor_failure_threshold:
                        _msg = (
                            f"Vial {vial}: OD out of calibrated range for "
                            f"{self._od_range_streak[vial]} consecutive cycles "
                            "(possible saturation / culture denser than "
                            "calibration; continuing -- heater unaffected)"
                        )
                        log.warning(_msg)
                        self._broadcast_alert(
                            level="warning", message=_msg, vial=vial,
                            category="sensor",
                        )
                else:
                    self._od_range_streak[vial] = 0

                # (1b) lossy/dropped read handling. A NaN temperature, or a NaN
                # OD that is NOT a known out-of-range reading, is a dropped
                # sample. Skip this cycle's control decision (no valid data) but
                # DO NOT park the heater or latch a stopping fault -- the Arduino
                # keeps regulating temperature on its own thermistor whether or
                # not the Pi got this sample. Warn once on crossing the
                # threshold so a genuinely dead sensor stays visible.
                if temp_nan or (od_nan and not od_out_of_range):
                    self._nan_streak[vial] = self._nan_streak.get(vial, 0) + 1
                    if self._nan_streak[vial] == self._sensor_failure_threshold:
                        _msg = (
                            f"Vial {vial}: {self._nan_streak[vial]} consecutive "
                            "dropped sensor reads (continuing -- lossy bus; "
                            "heater unaffected)"
                        )
                        log.warning(_msg)
                        self._broadcast_alert(
                            level="warning", message=_msg, vial=vial,
                            category="sensor",
                        )
                else:
                    self._nan_streak[vial] = 0

                # (2) heater safety. Needs a real temperature, so it is
                # skipped -- and only it is skipped -- on a dropped temp
                # read. The Arduino closes the heater loop on its own
                # thermistor regardless of whether the Pi got this sample.
                if not temp_nan:
                    self._handle_heater_safety_locked(vial, float(temp_c))
                    if self._vial_faults.get(vial) is not None:
                        continue

                # (3) push OD, (4) decide.
                #
                # CONTROL_MODE_AUDIT.md C-2: the sensor-validity gate is
                # SEPARATE from the control gate. This block used to
                # `continue` out of the whole vial on any dropped read, which
                # stopped an open-loop chemostat from diluting whenever OD was
                # unavailable -- and the `out_of_range` case is the sharpest,
                # because it means the culture is DENSER than the calibration
                # covers, i.e. exactly when dilution must not stop. Measured
                # cost: -29 % D at 30 % dropped samples, -100 % on a dead or
                # saturated sensor. A mode declares its own dependency via
                # `requires_od`; only modes that genuinely close the loop on
                # OD are suspended.
                controller = self._controllers.get(vial)
                if controller is None:
                    continue
                if od_nan:
                    if getattr(controller, "requires_od", True):
                        continue
                else:
                    # MorbidostatController needs the timestamp for its
                    # growth-rate fit; others take only the OD.
                    if isinstance(controller, MorbidostatController):
                        controller.push_od(now=now, od=float(od))
                    else:
                        controller.push_od(float(od))
                action = controller.decide(now)
                if action is not None:
                    # Engine-level hard cap (SPEC §10 confirmation rule,
                    # machine-enforced as a guardrail above the controller's
                    # 20 s SPEC cap).
                    if action.pump_time > PUMP_DURATION_HARD_CAP_SECONDS:
                        action = PumpAction(
                            pump_time=PUMP_DURATION_HARD_CAP_SECONDS,
                            efflux_extra_seconds=action.efflux_extra_seconds,
                            average_od=action.average_od,
                        )
                    pump_actions.append((vial, action))

            # (4a) Morbidostat: poll each controller for escalation events
            # and reminder-due flags. Emitted as experiment_event + alert,
            # and logged to escalation_log.csv for provenance.
            self._broadcast_morbidostat_events_locked(now, timestamp_iso)

            # (4a1) Drain one-shot controller events (chemostat start gate,
            # bolus cap clipping). Controllers stay pure -- they record, the
            # engine funnels. Per CLAUDE.md fact 3 everything goes through
            # _broadcast_alert / _broadcast_event, never a bare emit.
            self._broadcast_controller_events_locked()

            # (4a2) Consumables safety interlock (SPEC §15). Runs BEFORE
            # debit so a suppressed pump is never counted as consumed/wasted
            # volume. A low-media bottle suppresses the WHOLE dilution event
            # for its vials (influx + efflux together) -- suppressing influx
            # alone while efflux keeps running would reproduce the exact
            # vial-drain bug this gate exists to prevent. A full waste
            # container suppresses every vial's pump action, since an influx
            # without a matching efflux overflows the vial.
            if self._media_bottles:
                allowed: list[tuple[int, PumpAction]] = []
                suppressed: list[tuple[int, PumpAction, str]] = []
                for vial, action in pump_actions:
                    reason = self._vial_consumables_blocked_locked(vial)
                    if reason is None:
                        allowed.append((vial, action))
                    else:
                        suppressed.append((vial, action, reason))
                pump_actions = allowed
                if suppressed:
                    self._handle_suppressed_pumps_locked(suppressed)
                if (
                    not self._maintenance_active
                    and self._vials
                    and all(
                        self._vial_consumables_blocked_locked(v) is not None
                        for v in self._vials
                    )
                ):
                    self._enter_maintenance_locked(reason="consumables")

            # (4b) debit media consumption + waste accumulation and
            # edge-trigger threshold alerts. Bookkeeping is optimistic
            # (assumes the caller's pump_command will succeed); for Phase 1
            # the sub-percent error is acceptable.
            if self._media_bottles:
                self._debit_media_locked(pump_actions)

            # (5) re-send stir setpoints (drift protection)
            try:
                self._resend_stir_locked()
            except Exception:
                log.exception("resend stir failed")

            # (4c) Maintenance mode: keep the decision but don't execute.
            # Coalesce per-vial so a long maintenance window doesn't queue
            # 10+ dilutions for the same vial (over-dilution on resume).
            if self._maintenance_active and pump_actions:
                for vial, action in pump_actions:
                    self._pending_pump_actions[vial] = (action, timestamp_iso)
                pump_actions_to_return: list[tuple[int, PumpAction]] = []
            else:
                pump_actions_to_return = pump_actions

            # (6) persist
            try:
                self._save_state_locked()
            except Exception:
                log.exception("state.json persist failed")

        return pump_actions_to_return

    # ------------------------------------------------------------------
    # Inspection / data
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Snapshot for the API and dashboard."""
        with self._lock:
            if self._status == ExperimentStatus.IDLE or self._name is None:
                return {"status": "idle", "name": None}
            started = self._started_at
            elapsed_h = (
                (_now_utc() - started).total_seconds() / 3600.0
                if started is not None
                else 0.0
            )
            now = self._clock()
            per_vial: dict[str, dict] = {}
            for vial in self._vials:
                c = self._controllers.get(vial)
                # Adapter for cross-mode status: turbidostat has the full
                # `target` / `average_od()` / `time_since_last_pump()` API;
                # chemostat and morbidostat expose a subset (morbidostat
                # forwards to its inner turbidostat, chemostat reports its
                # last bolus time).
                avg: Optional[float] = None
                target: Optional[float] = None
                age = float("inf")
                if isinstance(c, TurbidostatController):
                    avg = c.average_od()
                    target = c.target
                    age = c.time_since_last_pump(now)
                elif isinstance(c, MorbidostatController):
                    avg = c._inner.average_od()
                    target = c._inner.target
                    age = c._inner.time_since_last_pump(now)
                elif isinstance(c, ChemostatController):
                    avg = c.last_od
                    if c.last_bolus_time is not None:
                        age = now - c.last_bolus_time
                # SPEC §20.3 DEGRADED: these streaks have been tracked since
                # b9b135a but never surfaced, so a single failing sleeve was
                # only visible by reading the journal.
                nan_streak = self._nan_streak.get(vial, 0)
                od_range_streak = self._od_range_streak.get(vial, 0)
                if nan_streak >= self._sensor_failure_threshold:
                    sensor_health = "degraded"
                elif od_range_streak >= self._sensor_failure_threshold:
                    sensor_health = "out_of_range"
                elif nan_streak > 0:
                    sensor_health = "lossy"
                else:
                    sensor_health = "ok"
                per_vial[str(vial)] = {
                    "target": target,
                    "avg_od": avg,
                    "last_pump_age_s": (None if math.isinf(age) else age),
                    "fault": self._vial_faults.get(vial),
                    "setpoint_raw": self._setpoint_raw.get(vial, HEATER_OFF_SETPOINT),
                    "consumables_blocked": self._vial_consumables_blocked_locked(vial),
                    "nan_streak": nan_streak,
                    "od_range_streak": od_range_streak,
                    "sensor_health": sensor_health,
                }
            return {
                "name": self._name,
                "status": self._status,
                "mode": (self._config or {}).get("mode"),
                "created": self._created_at.isoformat(timespec="seconds") if self._created_at else None,
                "started": self._started_at.isoformat(timespec="seconds") if self._started_at else None,
                "stopped": self._stopped_at.isoformat(timespec="seconds") if self._stopped_at else None,
                "stop_reason": self._stop_reason,
                "elapsed_hours": round(elapsed_h, 4),
                "vials": self._vials,
                "per_vial": per_vial,
                "setpoint_stir": self._setpoint_stir,
                "media": self._media_status_locked(),
                "maintenance": self._maintenance_status_locked(),
                "morbidostat": self._morbidostat_status_locked(),
            }

    def _morbidostat_status_locked(self) -> Optional[dict]:
        """Build the ``morbidostat`` block for ``status()``. Returns None
        when the active experiment isn't morbidostat mode."""
        if (self._config or {}).get("mode") != "morbidostat":
            return None
        per_vial: dict[str, dict] = {}
        for vial in self._vials:
            c = self._controllers.get(vial)
            if not isinstance(c, MorbidostatController):
                continue
            last_t: Optional[str] = None
            if c.last_escalation_time is not None:
                last_t = datetime.fromtimestamp(
                    c.last_escalation_time, tz=timezone.utc
                ).isoformat(timespec="seconds")
            per_vial[str(vial)] = {
                "drug_conc": c.drug_conc,
                "growth_rate_per_hour": c.latest_growth_rate,
                "awaiting_escalation_confirm": c.awaiting_escalation_confirm,
                "escalation_count": c.escalation_count,
                "last_escalation_time": last_t,
                "proposed_new_drug_conc": c.proposed_new_drug_conc,
            }
        return {"per_vial": per_vial}

    def _media_status_locked(self) -> Optional[dict]:
        """Build the `media` block for status() responses. Returns None when
        no media config is loaded."""
        if not self._media_bottles:
            return None
        # SPEC §15 accuracy caveat: label the whole estimate calibrated/
        # uncalibrated depending on whether this run's resolved flow rates
        # are the hardcoded defaults or measured values. This is a stopgap
        # until Session O1 builds real calibration provenance tracking.
        flow_rates = self._resolve_flow_rates(
            (self._config or {}).get("parameters", {}),
            (self._config or {}).get("calibration", {}),
        )
        estimate_quality = (
            "uncalibrated"
            if flow_rates == _as_flow_rates_32(None)
            else "calibrated"
        )
        bottles: list[dict] = []
        for bid, b in self._media_bottles.items():
            initial = float(b["initial_volume_ml"])
            consumed = float(self._bottle_consumed_ml.get(bid, 0.0))
            remaining = max(0.0, initial - consumed)
            pct = 100.0 * remaining / initial if initial > 0 else 0.0
            bottles.append({
                "id": bid,
                "name": b["name"],
                "contents": b.get("contents", ""),
                "initial_volume_ml": initial,
                "consumed_ml": round(consumed, 3),
                "remaining_ml": round(remaining, 3),
                "remaining_pct": round(pct, 2),
                "low_volume_alert_ml": b["low_volume_alert_ml"],
                "alerted_low": bool(self._bottle_alerted_low.get(bid, False)),
                "reserve_ml": round(_media_reserve_ml(initial), 3),
                "blocked": self._bottle_blocked_locked(bid),
                "estimate_quality": estimate_quality,
            })
        waste: Optional[dict] = None
        if self._waste_config is not None:
            cap = float(self._waste_config["capacity_ml"])
            filled = float(self._waste_filled_ml)
            remaining = max(0.0, cap - filled)
            filled_pct = 100.0 * filled / cap if cap > 0 else 0.0
            waste = {
                "name": self._waste_config.get("name", ""),
                "capacity_ml": cap,
                "filled_ml": round(filled, 3),
                "remaining_ml": round(remaining, 3),
                "filled_pct": round(filled_pct, 2),
                "high_fill_alert_ml": self._waste_config["high_fill_alert_ml"],
                "alerted_high": bool(self._waste_alerted_high),
                "reserve_ml": round(_waste_reserve_ml(cap), 3),
                "blocked": self._waste_blocked_locked(),
                "estimate_quality": estimate_quality,
            }
        return {
            "bottles": bottles,
            "vial_to_bottle": {str(k): v for k, v in self._vial_to_bottle.items()},
            "waste": waste,
        }

    def list_experiments(self) -> list[dict]:
        """All experiment directories with their persisted status."""
        with self._lock:
            loaded_name = self._name
            loaded_status = self._status
            loaded_vials = list(self._vials)
        results: list[dict] = []
        if not self._experiments_root.exists():
            return results
        for entry in sorted(self._experiments_root.iterdir()):
            if not entry.is_dir():
                continue
            info: dict = {"name": entry.name, "status": "stopped"}
            state_path = entry / "state.json"
            config_path = entry / "config.json"
            if state_path.is_file():
                try:
                    s = json.loads(state_path.read_text(encoding="utf-8"))
                    info["status"] = s.get("status", "stopped")
                    for key in ("mode", "vials", "created", "started", "stopped", "stop_reason"):
                        if key in s:
                            info[key] = s[key]
                except Exception:
                    log.exception("failed to parse %s", state_path)
            elif config_path.is_file():
                try:
                    c = json.loads(config_path.read_text(encoding="utf-8"))
                    for key in ("mode", "vials", "created"):
                        if key in c:
                            info[key] = c[key]
                except Exception:
                    log.exception("failed to parse %s", config_path)
            if entry.name == loaded_name:
                info["status"] = loaded_status
                info["vials"] = loaded_vials
            results.append(info)
        return results

    def get_data(
        self,
        name: str,
        vial: int,
        parameter: str,
        last_n: Optional[int] = None,
        hours: Optional[float] = None,
        max_points: Optional[int] = None,
    ) -> dict:
        """Read CSV rows for a vial/parameter, return JSON-shaped data.

        ``hours`` keeps only rows within the last ``hours`` of available
        data (computed from the ``elapsed_hours`` column relative to the
        last row, so it works for stopped experiments too). ``max_points``
        downsamples od/temp series via min/max bucketing (see
        :meth:`_downsample_minmax`); pump events are never downsampled.
        """
        if parameter not in ("od", "temp", "pump"):
            raise ValueError(f"parameter must be od/temp/pump, got {parameter!r}")
        if not (0 <= vial < N_VIALS):
            raise ValueError(f"vial must be in 0..{N_VIALS - 1}, got {vial}")
        if last_n is not None and last_n <= 0:
            raise ValueError(f"last_n must be > 0, got {last_n}")
        if hours is not None and hours <= 0:
            raise ValueError(f"hours must be > 0, got {hours}")
        if max_points is not None and max_points <= 0:
            raise ValueError(f"max_points must be > 0, got {max_points}")

        fname = {
            "od": f"vial{vial:02d}_OD.csv",
            "temp": f"vial{vial:02d}_temp.csv",
            "pump": f"vial{vial:02d}_pump_log.csv",
        }[parameter]
        path = self._experiments_root / name / fname
        if not path.is_file():
            raise FileNotFoundError(f"data file not found: {path}")

        # Read whole file (CSVs are append-only and small-ish for Phase 1).
        with path.open("r", encoding="utf-8", newline="") as f:
            lines = f.read().splitlines()
        if not lines:
            return {"timestamps": [], "values": []}
        header = lines[0].split(",")
        data_rows = lines[1:]

        # Time-window filter on the elapsed_hours column, relative to the
        # last available row (not wall-clock, so stopped runs still work).
        # Shared with data_export.build_bundle so the window semantics match.
        if hours is not None and data_rows:
            try:
                elapsed_col = header.index("elapsed_hours")
            except ValueError:
                elapsed_col = 1
            data_rows = filter_rows_by_hours(data_rows, elapsed_col, hours)

        if last_n is not None:
            data_rows = data_rows[-last_n:]

        if parameter == "pump":
            rows: list[dict] = []
            timestamps: list[str] = []
            for line in data_rows:
                parts = line.split(",")
                if len(parts) < len(header):
                    parts += [""] * (len(header) - len(parts))
                row = dict(zip(header, parts))
                rows.append(row)
                timestamps.append(row.get("timestamp", ""))
            return {"timestamps": timestamps, "rows": rows, "header": header}

        # od / temp: timestamps + calibrated values
        value_col = header.index("calibrated_od") if parameter == "od" else header.index("calibrated_temp_c")
        timestamps = []
        values: list[Optional[float]] = []
        for line in data_rows:
            parts = line.split(",")
            timestamps.append(parts[0] if parts else "")
            cell = parts[value_col] if len(parts) > value_col else ""
            try:
                values.append(float(cell) if cell != "" else None)
            except ValueError:
                values.append(None)

        if max_points is not None:
            timestamps, values = self._downsample_minmax(
                timestamps, values, max_points
            )
        return {"timestamps": timestamps, "values": values}

    @staticmethod
    def _downsample_minmax(
        timestamps: list[str],
        values: list[Optional[float]],
        max_points: int,
    ) -> tuple[list[str], list[Optional[float]]]:
        """Reduce a ``(timestamps, values)`` series to roughly ``max_points``
        while preserving spikes and gaps.

        The series is split into ~``max_points / 2`` time-ordered buckets;
        each bucket contributes its minimum and maximum non-None points (in
        chronological order), so transient spikes survive. A bucket that is
        entirely ``None`` contributes a single ``None`` so sensor dropouts
        stay visible as gaps rather than being interpolated away.
        """
        n = len(values)
        if max_points <= 0 or n <= max_points:
            return timestamps, values
        n_buckets = max(1, max_points // 2)
        bucket_size = math.ceil(n / n_buckets)
        out_ts: list[str] = []
        out_v: list[Optional[float]] = []
        for start in range(0, n, bucket_size):
            end = min(start + bucket_size, n)
            lo_i = hi_i = None
            lo_val = hi_val = None
            for i in range(start, end):
                v = values[i]
                if v is None:
                    continue
                if lo_val is None or v < lo_val:
                    lo_val, lo_i = v, i
                if hi_val is None or v > hi_val:
                    hi_val, hi_i = v, i
            if lo_i is None:
                # entire bucket is None -> one gap marker
                out_ts.append(timestamps[start])
                out_v.append(None)
                continue
            for i in sorted({lo_i, hi_i}):  # min & max, chronological
                out_ts.append(timestamps[i])
                out_v.append(values[i])
        return out_ts, out_v

    # ------------------------------------------------------------------
    # Media tracking (must hold self._lock)
    # ------------------------------------------------------------------

    def _debit_media_locked(
        self, pump_actions: list[tuple[int, PumpAction]]
    ) -> None:
        """For each pump action, debit the assigned bottle by the influx
        volume and the waste container by the efflux volume; fire one-shot
        low-volume / high-fill alerts on threshold crossings."""
        for vial, action in pump_actions:
            bottle_id = self._vial_to_bottle.get(vial)
            if bottle_id is None or bottle_id not in self._media_bottles:
                continue
            controller = self._controllers.get(vial)
            if controller is None:
                continue
            influx_ml = action.pump_time * controller.flow_rate_influx_ml_s
            # TODO(SPEC §16.2): waste accumulation still uses the influx rate.
            # Deliberate: with efflux overrun engaged the physically correct
            # model is `waste += influx_ml` (volume pinned by the straw), and
            # without overrun no software model is right. Do NOT swap in the
            # efflux rate here until the overrun decision is made.
            efflux_ml = (
                (action.pump_time + action.efflux_extra_seconds)
                * controller.flow_rate_influx_ml_s
            )
            self._bottle_consumed_ml[bottle_id] = (
                self._bottle_consumed_ml.get(bottle_id, 0.0) + influx_ml
            )
            self._waste_filled_ml += efflux_ml

            self._check_bottle_threshold_locked(bottle_id)
        self._check_waste_threshold_locked()

    def record_manual_pump(self, vial: int, direction: str, delivered_ml: float) -> None:
        """Debit media/waste for a manual (UI-fired) pump command, mirroring
        `_debit_media_locked`'s accounting (SPEC §16). A no-op unless `vial`
        has a bottle mapping from the loaded experiment's media config --
        true for the common case of pumping a vial with no experiment at
        all, and for vials belonging to a RUNNING experiment (which are
        locked out of manual control before this is ever called -- see
        `_experiment_locks_vial` in app.py). It IS reachable for a
        STOPPED experiment's vials: media is loaded at start_experiment
        (`_load_media_locked`) and `_vial_to_bottle` stays populated after
        stop -- only the next create_experiment clears it.

        Deliberately does not apply the Session K consumables interlock --
        that gate runs in `run_cycle`'s automatic dispatch path only. A
        manual pump on an empty bottle still fires; only the accounting is
        handled here."""
        with self._lock:
            bottle_id = self._vial_to_bottle.get(vial)
            if bottle_id is None:
                return
            if direction == "influx":
                self._bottle_consumed_ml[bottle_id] = (
                    self._bottle_consumed_ml.get(bottle_id, 0.0) + delivered_ml
                )
                self._check_bottle_threshold_locked(bottle_id)
            else:
                self._waste_filled_ml += delivered_ml
                self._check_waste_threshold_locked()
            self._save_state_locked()

    def _broadcast_controller_events_locked(self) -> None:
        """Surface the one-shot events any controller exposes via
        ``pop_events()`` (currently the chemostat's start gate and its
        duration-cap clipping). Unknown event types are logged rather than
        dropped, so a new controller event can never go silently missing."""
        for vial, controller in self._controllers.items():
            pop = getattr(controller, "pop_events", None)
            if pop is None:
                continue
            try:
                events = pop()
            except Exception:
                log.exception("pop_events failed for vial %d", vial)
                continue
            for event in events:
                etype = event.get("type")
                if etype == "start_gate_released":
                    msg = (
                        f"Vial {vial}: chemostat start gate released, dilution "
                        f"begins ({event.get('reason')})"
                    )
                    level = "info"
                    category = "lifecycle"
                    dedup = None  # one-shot by construction
                elif etype == "bolus_cap_clipped":
                    msg = (
                        f"Vial {vial}: chemostat bolus clipped from "
                        f"{event.get('requested_seconds', 0.0):.1f} s to the "
                        f"{event.get('capped_seconds', 0.0):.1f} s safety cap -- "
                        "the requested dilution rate is not reachable with this "
                        "bolus_interval and flow rate"
                    )
                    level = "warning"
                    category = "pump"
                    dedup = ("bolus_cap_clipped", vial)
                else:
                    log.warning(
                        "unhandled controller event %r from vial %d", etype, vial
                    )
                    continue
                self._broadcast_event({**event, "vial": vial})
                self._broadcast_alert(
                    level=level, message=msg, vial=vial, category=category,
                    dedup_key=dedup,
                )

    def _broadcast_morbidostat_events_locked(
        self, now: float, timestamp_iso: str,
    ) -> None:
        """For each morbidostat controller: emit any pending escalation
        proposal (with experiment_event + warning alert + CSV log), and
        emit a reminder alert when the confirmation deadline ticks past."""
        for vial, controller in self._controllers.items():
            if not isinstance(controller, MorbidostatController):
                continue
            pending: Optional[EscalationEvent] = controller.pending_escalation()
            if pending is not None:
                self._broadcast_event({
                    "type": "escalation_proposed",
                    "vial": pending.vial,
                    "old_drug_conc": pending.old_drug_conc,
                    "new_drug_conc": pending.new_drug_conc,
                    "growth_rate": pending.growth_rate,
                })
                self._broadcast_alert(
                    level="warning",
                    vial=pending.vial,
                    message=(
                        f"Vial {pending.vial} escalation proposed: "
                        f"{pending.old_drug_conc:.2f}x -> {pending.new_drug_conc:.2f}x "
                        f"(growth {pending.growth_rate:.2f}/hr "
                        "exceeds threshold)"
                    ),
                    category="escalation",
                )
                try:
                    self._data_logger.log_escalation_event(
                        timestamp_iso,
                        vial=pending.vial,
                        old_drug_conc=pending.old_drug_conc,
                        proposed_new_drug_conc=pending.new_drug_conc,
                        growth_rate_per_hour=pending.growth_rate,
                    )
                except Exception:
                    log.exception("escalation_log.csv write failed")
            if controller.due_for_reminder(now):
                controller.mark_reminder_sent(now)
                self._broadcast_alert(
                    level="warning",
                    vial=vial,
                    message=f"Vial {vial} escalation still pending confirmation",
                    category="escalation",
                )

    def confirm_escalation(
        self,
        name: str,
        vial: int,
        *,
        new_drug_conc: Optional[float] = None,
        new_bottle_contents: Optional[str] = None,
    ) -> dict:
        """Apply a user-confirmed morbidostat escalation: bump drug_conc
        for the vial, optionally update the bottle contents text in
        ``config.json``, and log a confirmation row to
        ``escalation_log.csv``.

        Raises ``ConflictError`` when the vial isn't awaiting confirmation
        (mapped to 409 by the API)."""
        with self._lock:
            if self._status != ExperimentStatus.RUNNING:
                raise InvalidExperimentStateError(
                    f"confirm_escalation requires RUNNING, got {self._status!r}"
                )
            if self._name != name:
                raise ValueError(
                    f"experiment {name!r} is not the running experiment"
                )
            if (self._config or {}).get("mode") != "morbidostat":
                raise ValueError(
                    f"experiment {name!r} is not morbidostat mode"
                )
            controller = self._controllers.get(vial)
            if not isinstance(controller, MorbidostatController):
                raise ValueError(f"vial {vial} has no morbidostat controller")
            if not controller.awaiting_escalation_confirm:
                raise ConflictError(
                    f"vial {vial} is not awaiting escalation confirmation"
                )
            proposed = controller.proposed_new_drug_conc
            actual = float(new_drug_conc) if new_drug_conc is not None else proposed
            if actual is None or actual <= controller.drug_conc:
                raise ValueError(
                    f"new_drug_conc ({actual}) must be > current "
                    f"drug_conc ({controller.drug_conc})"
                )
            controller.confirm_escalation(new_conc=actual, timestamp=self._clock())
            updated_contents: Optional[str] = None
            if new_bottle_contents is not None:
                bottle_id = self._vial_to_bottle.get(vial)
                if bottle_id and bottle_id in self._media_bottles:
                    self._media_bottles[bottle_id]["contents"] = str(new_bottle_contents)
                    if isinstance(self._config, dict):
                        media = self._config.get("media")
                        if isinstance(media, dict):
                            for b in media.get("bottles", []):
                                if b.get("id") == bottle_id:
                                    b["contents"] = str(new_bottle_contents)
                                    break
                    updated_contents = str(new_bottle_contents)
                    try:
                        self._data_logger.update_experiment_config(
                            name, {"media": (self._config or {}).get("media")},
                        )
                    except Exception:
                        log.exception("update_experiment_config failed")
            try:
                self._data_logger.log_escalation_event(
                    _iso_now(),
                    vial=vial,
                    confirmed_time=_iso_now(),
                    confirmed_drug_conc=actual,
                    bottle_contents_after=updated_contents,
                )
            except Exception:
                log.exception("escalation_log.csv confirmation write failed")
            self._save_state_locked()
            result = {
                "vial": vial,
                "drug_conc": controller.drug_conc,
                "bottle_contents": updated_contents,
            }
        self._broadcast_event({
            "type": "escalation_confirmed",
            "vial": vial,
            "new_drug_conc": actual,
        })
        log.info(
            "morbidostat escalation confirmed for vial %d: drug_conc -> %.3f",
            vial, actual,
        )
        return result

    def escalation_pending_vials(self) -> list[int]:
        """List of morbidostat vials currently awaiting user confirmation
        of an escalation. Empty when not morbidostat or none pending. Used
        by the dashboard's compact sensor_update payload."""
        with self._lock:
            result: list[int] = []
            for vial, controller in self._controllers.items():
                if isinstance(controller, MorbidostatController) and (
                    controller.awaiting_escalation_confirm
                ):
                    result.append(vial)
            return sorted(result)

    # ------------------------------------------------------------------
    # Consumables safety interlock (SPEC §15, must hold self._lock)
    # ------------------------------------------------------------------

    def _bottle_blocked_locked(self, bottle_id: str) -> bool:
        b = self._media_bottles.get(bottle_id)
        if b is None:
            return False
        initial = b["initial_volume_ml"]
        consumed = self._bottle_consumed_ml.get(bottle_id, 0.0)
        remaining = initial - consumed
        return remaining <= _media_reserve_ml(initial)

    def _waste_blocked_locked(self) -> bool:
        if self._waste_config is None:
            return False
        cap = self._waste_config["capacity_ml"]
        return self._waste_filled_ml >= cap - _waste_reserve_ml(cap)

    def _vial_consumables_blocked_locked(self, vial: int) -> Optional[str]:
        """None, or "waste_full" / "media_empty" -- the reason this vial's
        pump actions are currently suppressed by the consumables interlock.
        Stateless by design: it is recomputed from already-persisted volume
        totals every call, so the block is sticky for free -- nothing but
        refill_media changes those totals once pumping is suppressed."""
        if self._waste_blocked_locked():
            return "waste_full"
        bottle_id = self._vial_to_bottle.get(vial)
        if bottle_id is not None and self._bottle_blocked_locked(bottle_id):
            return "media_empty"
        return None

    def _handle_suppressed_pumps_locked(
        self, suppressed: list[tuple[int, PumpAction, str]]
    ) -> None:
        """Alert + log the vials whose pump actions the interlock dropped
        this cycle. Alerts are edge-triggered (one-shot per bottle, or once
        for waste) so a persistently-blocked vial doesn't re-alert every
        10 s cycle -- the latches are cleared by refill_media."""
        if any(reason == "waste_full" for _v, _a, reason in suppressed):
            if not self._waste_alerted_blocked:
                self._waste_alerted_blocked = True
                self._broadcast_alert(
                    level="critical",
                    message="Waste at capacity -- all pumping suppressed",
                    category="waste",
                )
        blocked_bottles: set[str] = set()
        for vial, _action, reason in suppressed:
            if reason == "media_empty":
                bottle_id = self._vial_to_bottle.get(vial)
                if bottle_id is not None:
                    blocked_bottles.add(bottle_id)
        for bottle_id in blocked_bottles:
            if self._bottle_alerted_blocked.get(bottle_id, False):
                continue
            self._bottle_alerted_blocked[bottle_id] = True
            b = self._media_bottles.get(bottle_id, {})
            self._broadcast_alert(
                level="critical",
                message=(
                    f"Bottle '{b.get('name', bottle_id)}' at or below reserve "
                    "-- pumping suppressed for its vials"
                ),
                category="media",
            )
        for vial, _action, reason in suppressed:
            self._broadcast_event({
                "type": "pump_suppressed",
                "vial": vial,
                "reason": reason,
                "bottle_id": self._vial_to_bottle.get(vial),
            })
            log.warning(
                "vial %d pump suppressed by consumables interlock (reason=%s)",
                vial, reason,
            )

    def _check_bottle_threshold_locked(self, bottle_id: str) -> None:
        b = self._media_bottles.get(bottle_id)
        if b is None or self._bottle_alerted_low.get(bottle_id, False):
            return
        consumed = self._bottle_consumed_ml.get(bottle_id, 0.0)
        remaining = b["initial_volume_ml"] - consumed
        if remaining <= b["low_volume_alert_ml"]:
            self._bottle_alerted_low[bottle_id] = True
            self._broadcast_alert(
                level="warning",
                message=(
                    f"Bottle '{b['name']}' has {max(0.0, remaining):.0f} mL "
                    f"remaining (alert at {b['low_volume_alert_ml']:.0f} mL)"
                ),
                category="media",
            )

    def _check_waste_threshold_locked(self) -> None:
        if self._waste_config is None or self._waste_alerted_high:
            return
        if self._waste_filled_ml >= self._waste_config["high_fill_alert_ml"]:
            self._waste_alerted_high = True
            pct = 100.0 * self._waste_filled_ml / self._waste_config["capacity_ml"]
            self._broadcast_alert(
                level="warning",
                message=f"Waste container at {pct:.0f}% capacity",
                category="waste",
            )

    def _media_runtime_state_locked(self) -> Optional[dict]:
        """Build the state.json `media_state` block. Returns None if no
        media config is loaded so the field is omitted from disk."""
        if not self._media_bottles:
            return None
        return {
            "bottles": {
                bid: {
                    "consumed_ml": float(self._bottle_consumed_ml.get(bid, 0.0)),
                    "alerted_low": bool(self._bottle_alerted_low.get(bid, False)),
                    "alerted_blocked": bool(self._bottle_alerted_blocked.get(bid, False)),
                }
                for bid in self._media_bottles
            },
            "waste": {
                "filled_ml": float(self._waste_filled_ml),
                "alerted_high": bool(self._waste_alerted_high),
                "alerted_blocked": bool(self._waste_alerted_blocked),
            },
        }

    def _restore_media_runtime_locked(
        self, media_config: Optional[dict], media_state: Optional[dict]
    ) -> None:
        """Re-load the runtime media tracking from a persisted state.json
        snapshot. Called from resume_on_startup."""
        self._load_media_locked(media_config)
        if not media_state or not self._media_bottles:
            return
        bottles_state = media_state.get("bottles") or {}
        for bid, b in bottles_state.items():
            if bid in self._bottle_consumed_ml:
                self._bottle_consumed_ml[bid] = float(b.get("consumed_ml", 0.0))
                self._bottle_alerted_low[bid] = bool(b.get("alerted_low", False))
                self._bottle_alerted_blocked[bid] = bool(b.get("alerted_blocked", False))
        waste_state = media_state.get("waste") or {}
        self._waste_filled_ml = float(waste_state.get("filled_ml", 0.0))
        self._waste_alerted_high = bool(waste_state.get("alerted_high", False))
        self._waste_alerted_blocked = bool(waste_state.get("alerted_blocked", False))

    # ------------------------------------------------------------------
    # Safety / fault helpers (must hold self._lock)
    # ------------------------------------------------------------------

    def _handle_heater_safety_locked(self, vial: int, temp_c: float) -> None:
        """Per-cycle overtemp watchdog.

        Convention reminder: raw setpoint is INVERTED — lower = hotter. So
        "back off the heater" means *raising* the raw setpoint integer
        (lowering the target Celsius). The gate doesn't condition on
        ``setpoint_raw > 0`` (that would only trigger when the heater is
        already cooling); it fires whenever the measured temperature
        overshoots the target by more than the overrun threshold."""
        setpoint_raw = self._setpoint_raw.get(vial, HEATER_OFF_SETPOINT)
        setpoint_c = self._raw_to_C(setpoint_raw, vial)
        if temp_c > self._heater_critical_C:
            # Debounce: the thermistor/RS485 bus is lossy and can spike
            # spuriously, so a SINGLE over-critical sample must not park the
            # heater. Require 3 consecutive over-critical reads before latching.
            streak = getattr(self, "_overtemp_streak", None)
            if streak is None:
                streak = self._overtemp_streak = {}
            streak[vial] = streak.get(vial, 0) + 1
            if streak[vial] >= 3:
                self._latch_fault_locked(vial, "overtemp")
            else:
                log.warning(
                    "vial %d temp %.1f C over critical %.1f C (%d/3) -- "
                    "watching, not latching yet",
                    vial, temp_c, self._heater_critical_C, streak[vial],
                )
            return
        if getattr(self, "_overtemp_streak", None) is not None:
            self._overtemp_streak[vial] = 0
        if temp_c > setpoint_c + self._heater_overrun_C:
            # Overrun: lower the target by DEFAULT_HEATER_STEP_DOWN_C (SPEC.md §10).
            new_target_c = max(22.0, setpoint_c - DEFAULT_HEATER_STEP_DOWN_C)
            new_raw = self._C_to_raw(new_target_c, vial)
            self._setpoint_raw[vial] = new_raw
            self._apply_temperature_locked()
            log.warning(
                "vial %d overtemp: %.2f C > target %.2f C + %.1f; "
                "target -> %.2f C (raw %d -> %d)",
                vial, temp_c, setpoint_c, self._heater_overrun_C,
                new_target_c, setpoint_raw, new_raw,
            )

    def _latch_fault_locked(self, vial: int, kind: str) -> None:
        if self._vial_faults.get(vial) is not None:
            return  # already latched
        self._vial_faults[vial] = kind
        # Park this vial's heater OFF. Under the inverted convention "off"
        # is HEATER_OFF_SETPOINT (an unreachably cold target), NOT zero —
        # zero would pin the heater at maximum, which is what the safety
        # path is supposed to prevent.
        self._setpoint_raw[vial] = HEATER_OFF_SETPOINT
        try:
            self._apply_temperature_locked()
        except Exception:
            log.exception("latch_fault: heater park-off failed for vial %d", vial)
        # Stir is a 16-vial command; the experiment's _setpoint_stir is
        # shared, so we don't zero stir on a single-vial fault. The
        # vial's heater being parked off is the primary safety action.
        level = "critical" if kind == "overtemp" else "warning"
        self._broadcast_alert(
            level=level,
            message=f"Vial {vial} latched fault: {kind}",
            vial=vial,
            category="heater",
        )

    # ------------------------------------------------------------------
    # Actuator helpers (must hold self._lock)
    # ------------------------------------------------------------------

    def _apply_initial_actuators_locked(self, parameters: dict) -> None:
        """At start: convert ``temperature_c`` to per-vial raw setpoints,
        send ``set_temperature_raw`` and ``set_stir``. Preserves non-experiment
        vials' existing setpoints (which default to HEATER_OFF_SETPOINT)."""
        target_temps = _as_list_of_16(
            parameters.get("temperature_c"),
            default=parameters.get("temperature", 37.0) if isinstance(parameters.get("temperature"), (int, float)) else 37.0,
            name="temperature_c",
        )
        if self._temp_cal is None:
            raise RuntimeError(
                "temp_cal is None — engine cannot convert °C to raw setpoint. "
                "Load calibration first."
            )
        # Build the full 16-vial raw-setpoint list, splicing in experiment vials
        # only. Vials outside the experiment keep whatever the manager last sent
        # (defaults to HEATER_OFF_SETPOINT on fresh init).
        current_raw = np.asarray(
            getattr(
                self._manager, "temp_setpoint_raw",
                np.full(N_VIALS, HEATER_OFF_SETPOINT, dtype=int),
            )
        )
        raw_list = [int(v) for v in current_raw.tolist()]
        for vial in self._vials:
            target_raw = self._C_to_raw(target_temps[vial], vial)
            self._setpoint_raw[vial] = target_raw
            raw_list[vial] = target_raw
        self._manager.set_temperature_raw(raw_list)

        # Stir: splice the experiment's uniform stir into the existing array.
        current_stir = np.asarray(
            getattr(self._manager, "stir_speed", np.zeros(N_VIALS, dtype=int))
        )
        stir_list = [int(v) for v in current_stir.tolist()]
        for vial in self._vials:
            stir_list[vial] = self._setpoint_stir
        self._manager.set_stir(stir_list)

    def _apply_temperature_locked(self) -> None:
        """Re-send the temperature raw-setpoint array reflecting current
        ``self._setpoint_raw``, preserving non-experiment vials' values."""
        current_raw = np.asarray(
            getattr(
                self._manager, "temp_setpoint_raw",
                np.full(N_VIALS, HEATER_OFF_SETPOINT, dtype=int),
            )
        )
        raw_list = [int(v) for v in current_raw.tolist()]
        for vial, raw in self._setpoint_raw.items():
            raw_list[vial] = int(raw)
        self._manager.set_temperature_raw(raw_list)

    def _resend_stir_locked(self) -> None:
        """Re-send stir setpoints for experiment vials (SPEC §9 step 6).
        Preserves non-experiment vials' values."""
        current_stir = np.asarray(
            getattr(self._manager, "stir_speed", np.zeros(N_VIALS, dtype=int))
        )
        stir_list = [int(v) for v in current_stir.tolist()]
        for vial in self._vials:
            # Faulted vials don't get stirred either.
            stir_list[vial] = 0 if self._vial_faults.get(vial) is not None else self._setpoint_stir
        self._manager.set_stir(stir_list)

    def _zero_experiment_actuators_locked(self) -> None:
        """Zero pumps + heater + stir for experiment vials only. Used by
        stop_experiment. Manual control of other vials is preserved."""
        # Stop pumps for experiment vials (best-effort per-vial).
        for vial in self._vials:
            try:
                # Fire each direction with seconds=0 to ensure they're not running.
                # We rely on SerialManager.stop_all_pumps for a global stop, but
                # the engine wants per-vial precision so other vials' manual
                # pumps keep running. SerialManager doesn't expose per-vial
                # stop, so emit pump_command(vial, "influx", 0) is a no-op on
                # most firmwares; the safer path is stop_all_pumps. Trade-off
                # for Phase 1: prefer stopping all pumps — manual pumps in
                # progress on other vials get interrupted, but pump events are
                # short (<= 30 s) so this is acceptable.
                pass
            except Exception:
                log.exception("zero pump failed for vial %d", vial)
        try:
            self._manager.stop_all_pumps()
        except Exception:
            log.exception("stop_all_pumps failed during experiment stop")

        # Park experiment-vial heaters off (HEATER_OFF_SETPOINT, NOT zero —
        # the convention is inverted; zero would max the heaters). Stir is
        # genuinely raw-PWM-0-is-off, so it really does get zeroed.
        # _setpoint_stir is zeroed BEFORE _resend_stir_locked since the
        # resend reads it.
        for vial in self._vials:
            self._setpoint_raw[vial] = HEATER_OFF_SETPOINT
        self._setpoint_stir = 0
        try:
            self._apply_temperature_locked()
        except Exception:
            log.exception("heater park-off failed during stop")
        try:
            self._resend_stir_locked()
        except Exception:
            log.exception("stir zero failed during stop")

    # ------------------------------------------------------------------
    # Calibration helpers
    # ------------------------------------------------------------------

    def _C_to_raw(self, temp_c: float, vial: int) -> int:
        """Convert target °C to raw `xr` setpoint: raw = (°C - intercept)/slope.

        Calibration slope is NEGATIVE, so higher target °C yields a SMALLER
        raw setpoint (e.g. 37 °C → ~482, 22 °C → ~580). Targets above
        MAX_SAFE_TEMP_C are clamped; results are bounded above by
        HEATER_OFF_SETPOINT so we never send unreachably-large integers."""
        slope = float(self._temp_cal[0][vial])
        intercept = float(self._temp_cal[1][vial])
        if slope == 0:
            return HEATER_OFF_SETPOINT
        capped_c = min(float(temp_c), MAX_SAFE_TEMP_C)
        raw = (capped_c - intercept) / slope
        return int(max(0, min(HEATER_OFF_SETPOINT, round(raw))))

    def _raw_to_C(self, raw: int, vial: int) -> float:
        """temp_cal[0]*raw + temp_cal[1] = °C. Slope is negative, so
        smaller raw integers correspond to higher temperatures."""
        if self._temp_cal is None:
            return 0.0
        slope = float(self._temp_cal[0][vial])
        intercept = float(self._temp_cal[1][vial])
        return slope * raw + intercept

    # ------------------------------------------------------------------
    # Controllers
    # ------------------------------------------------------------------

    def _build_controllers(
        self,
        mode: str,
        parameters: dict,
        calibration: dict,
        vials: list[int],
    ) -> dict[int, ControllerType]:
        if mode == "turbidostat":
            return self._build_turbidostat_controllers(parameters, calibration, vials)
        if mode == "chemostat":
            return self._build_chemostat_controllers(parameters, calibration, vials)
        if mode == "morbidostat":
            return self._build_morbidostat_controllers(parameters, calibration, vials)
        raise ValueError(f"no controller builder for mode {mode!r}")

    def _resolve_flow_rates(self, parameters: dict, calibration: dict) -> list[float]:
        """Flow rates in canonical flat-32 form (0..15 influx, 16..31 efflux):
        prefer parameters['pump_flow_rates'], then
        calibration['pump_flow_rates'], else defaults. Scalars and per-vial
        length-16 lists broadcast per :func:`_as_flow_rates_32`."""
        flow_rates_raw = (
            parameters.get("pump_flow_rates")
            or calibration.get("pump_flow_rates")
            or None
        )
        return _as_flow_rates_32(flow_rates_raw)

    def flow_rate_ml_s(self, vial: int, direction: str = "influx") -> float:
        """Per-pump flow rate for mL <-> seconds conversion (SPEC §16).

        Uses the loaded experiment's resolved flow rate when `vial` belongs
        to it -- this works in CREATED and STOPPED too, not just RUNNING,
        since a stopped experiment's calibration is still the right one for
        its vials. Falls back to the hardcoded default for vials with no
        loaded experiment. ``direction`` selects the influx or efflux pump
        (canonical index vial / vial+16); they are physically separate pumps
        and carry independent rates once a Tier 2 pump calibration exists."""
        if direction not in ("influx", "efflux"):
            raise ValueError(
                f"direction must be 'influx' or 'efflux', got {direction!r}"
            )
        idx = vial if direction == "influx" else vial + N_VIALS
        with self._lock:
            if self._config is not None and vial in self._vials:
                rates = self._resolve_flow_rates(
                    self._config.get("parameters", {}),
                    self._config.get("calibration", {}),
                )
                return rates[idx]
        return DEFAULT_FLOW_RATES_ML_PER_SEC[vial]

    def _build_turbidostat_controllers(
        self,
        parameters: dict,
        calibration: dict,
        vials: list[int],
    ) -> dict[int, ControllerType]:
        od_lower = _as_list_of_16(
            parameters.get("od_lower_thresh", parameters.get("od_lower", 0.2)),
            default=0.2, name="od_lower_thresh",
        )
        od_upper = _as_list_of_16(
            parameters.get("od_upper_thresh", parameters.get("od_upper", 0.4)),
            default=0.4, name="od_upper_thresh",
        )
        pump_wait_minutes = float(
            parameters.get("pump_wait_minutes", DEFAULT_PUMP_WAIT_MINUTES)
        )
        pump_wait_seconds = pump_wait_minutes * 60.0
        volume_ml = float(parameters.get("volume_ml", DEFAULT_VOLUME_ML))
        efflux_extra = float(
            parameters.get("efflux_extra_seconds", DEFAULT_EFFLUX_EXTRA_SECONDS)
        )
        history_window = int(parameters.get("history_window", DEFAULT_HISTORY_WINDOW))
        # Legacy warmup gate (custom_script.py:83): block control actions
        # until ≥8 OD samples have accumulated. Configurable via the
        # experiment parameter "min_samples_before_action" for tests that
        # want to exercise the controller without warmup.
        min_samples_before_action = int(parameters.get(
            "min_samples_before_action", 8
        ))
        flow_rates = self._resolve_flow_rates(parameters, calibration)

        controllers: dict[int, ControllerType] = {}
        for vial in vials:
            controllers[vial] = TurbidostatController(
                vial=vial,
                od_lower=od_lower[vial],
                od_upper=od_upper[vial],
                pump_wait_seconds=pump_wait_seconds,
                flow_rate_influx_ml_s=flow_rates[vial],
                flow_rate_efflux_ml_s=flow_rates[vial + N_VIALS],
                volume_ml=volume_ml,
                efflux_extra_seconds=efflux_extra,
                history_window=history_window,
                pump_duration_cap_seconds=20.0,  # SPEC §9 cap
                min_samples_before_action=min_samples_before_action,
            )
        return controllers

    def _build_chemostat_controllers(
        self,
        parameters: dict,
        calibration: dict,
        vials: list[int],
    ) -> dict[int, ControllerType]:
        dilution_rate = float(parameters.get("dilution_rate_per_hour", 0.5))
        bolus_interval = float(parameters.get(
            "bolus_interval_seconds", self._cycle_interval_seconds,
        ))
        volume_ml = float(parameters.get("volume_ml", DEFAULT_VOLUME_ML))
        efflux_extra = float(
            parameters.get("efflux_extra_seconds", DEFAULT_EFFLUX_EXTRA_SECONDS)
        )
        # Optional start gate (SPEC §9 / CONTROL_MODE_AUDIT.md C-5). Absent
        # both, dilution begins at inoculation density as it always has.
        start_od = parameters.get("start_od")
        start_after_seconds = parameters.get("start_after_seconds")
        flow_rates = self._resolve_flow_rates(parameters, calibration)

        controllers: dict[int, ControllerType] = {}
        for vial in vials:
            controllers[vial] = ChemostatController(
                vial=vial,
                dilution_rate_per_hour=dilution_rate,
                bolus_interval_seconds=bolus_interval,
                volume_ml=volume_ml,
                flow_rate_influx_ml_s=flow_rates[vial],
                flow_rate_efflux_ml_s=flow_rates[vial + N_VIALS],
                efflux_extra_seconds=efflux_extra,
                pump_duration_cap_seconds=20.0,
                start_od=None if start_od is None else float(start_od),
                start_after_seconds=(
                    None if start_after_seconds is None
                    else float(start_after_seconds)
                ),
            )
        return controllers

    def _build_morbidostat_controllers(
        self,
        parameters: dict,
        calibration: dict,
        vials: list[int],
    ) -> dict[int, ControllerType]:
        # target_od accepts scalar or per-vial list (mirrors turbidostat
        # thresholds). od_lower likewise.
        target_od = _as_list_of_16(
            parameters.get("target_od", 0.4), default=0.4, name="target_od",
        )
        od_lower = _as_list_of_16(
            parameters.get("od_lower", 0.2), default=0.2, name="od_lower",
        )
        pump_wait_minutes = float(
            parameters.get("pump_wait_minutes", DEFAULT_PUMP_WAIT_MINUTES)
        )
        pump_wait_seconds = pump_wait_minutes * 60.0
        volume_ml = float(parameters.get("volume_ml", DEFAULT_VOLUME_ML))
        efflux_extra = float(
            parameters.get("efflux_extra_seconds", DEFAULT_EFFLUX_EXTRA_SECONDS)
        )
        history_window = int(parameters.get("history_window", DEFAULT_HISTORY_WINDOW))
        # Same warmup gate as turbidostat (the inner controller is one).
        min_samples_before_action = int(parameters.get(
            "min_samples_before_action", 8
        ))
        flow_rates = self._resolve_flow_rates(parameters, calibration)

        # Morbidostat-specific (scalar params, broadcast to all vials).
        initial_drug = _as_list_of_16(
            parameters.get("initial_drug_conc", 1.0),
            default=1.0, name="initial_drug_conc",
        )
        drug_step = float(parameters.get("drug_step", 2.0))
        mu_thresh = float(parameters.get("adaptation_threshold_per_hour", 0.4))
        growth_window = float(parameters.get("growth_window_seconds", 1800.0))
        growth_min_samples = int(parameters.get("growth_min_samples", 6))
        escalation_cooldown = float(
            parameters.get("escalation_cooldown_seconds", 3600.0)
        )
        reminder_interval = float(
            parameters.get("escalation_reminder_interval_seconds", 1800.0)
        )

        controllers: dict[int, ControllerType] = {}
        for vial in vials:
            controllers[vial] = MorbidostatController(
                vial=vial,
                target_od=target_od[vial],
                od_lower=od_lower[vial],
                pump_wait_seconds=pump_wait_seconds,
                flow_rate_influx_ml_s=flow_rates[vial],
                flow_rate_efflux_ml_s=flow_rates[vial + N_VIALS],
                volume_ml=volume_ml,
                efflux_extra_seconds=efflux_extra,
                history_window=history_window,
                pump_duration_cap_seconds=20.0,
                min_samples_before_action=min_samples_before_action,
                initial_drug_conc=initial_drug[vial],
                drug_step=drug_step,
                adaptation_threshold_per_hour=mu_thresh,
                growth_window_seconds=growth_window,
                growth_min_samples=growth_min_samples,
                escalation_cooldown_seconds=escalation_cooldown,
                escalation_reminder_interval_seconds=reminder_interval,
            )
        return controllers

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _save_state_locked(self) -> None:
        if self._name is None:
            return
        exp_dir = self._experiments_root / self._name
        if not exp_dir.is_dir():
            return
        payload: dict[str, Any] = {
            "name": self._name,
            "status": self._status,
            "mode": (self._config or {}).get("mode"),
            "vials": list(self._vials),
            "parameters": (self._config or {}).get("parameters", {}),
            "calibration": (self._config or {}).get("calibration", {}),
            "media": (self._config or {}).get("media"),
            "created": self._created_at.isoformat(timespec="seconds") if self._created_at else None,
            "started": self._started_at.isoformat(timespec="seconds") if self._started_at else None,
            "stopped": self._stopped_at.isoformat(timespec="seconds") if self._stopped_at else None,
            "stop_reason": self._stop_reason,
            "setpoint_raw": dict(self._setpoint_raw),
            "setpoint_stir": self._setpoint_stir,
            "controllers": {
                str(v): c.to_state() for v, c in self._controllers.items()
            },
            "vial_faults": {str(k): v for k, v in self._vial_faults.items()},
            "nan_streak": {str(k): v for k, v in self._nan_streak.items()},
            "od_range_streak": {str(k): v for k, v in self._od_range_streak.items()},
            "media_state": self._media_runtime_state_locked(),
            "maintenance": {
                "active": self._maintenance_active,
                "reason": self._maintenance_reason,
                "entered_at": (
                    self._maintenance_entered_at.isoformat(timespec="seconds")
                    if self._maintenance_entered_at else None
                ),
            },
            "last_persisted": _iso_now(),
        }
        state_path = exp_dir / "state.json"
        tmp_path = exp_dir / "state.json.tmp"
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        # On Windows, replace() handles the case where state.json already exists.
        os.replace(tmp_path, state_path)

    def _load_state(self, name: str) -> dict:
        state_path = self._experiments_root / name / "state.json"
        if not state_path.is_file():
            return {"status": ExperimentStatus.CREATED}
        return json.loads(state_path.read_text(encoding="utf-8"))

    def resume_on_startup(self) -> Optional[str]:
        """Scan experiments/ for any directory whose state.json shows
        status=RUNNING. If exactly one, rebuild its controllers, re-send
        actuators, activate the DataLogger, and transition to RUNNING.

        If multiple are marked RUNNING (corruption from a crashed write):
        pick the most recently started, mark the others as ERROR.
        """
        candidates: list[tuple[str, dict]] = []
        if not self._experiments_root.exists():
            return None
        for entry in sorted(self._experiments_root.iterdir()):
            if not entry.is_dir():
                continue
            state_path = entry / "state.json"
            if not state_path.is_file():
                continue
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                log.exception("failed to read %s on resume scan", state_path)
                continue
            if state.get("status") == ExperimentStatus.RUNNING:
                candidates.append((entry.name, state))

        if not candidates:
            return None

        # Pick most-recently-started; demote the rest.
        def _started_key(item: tuple[str, dict]) -> str:
            return (item[1].get("started") or "") + item[0]
        candidates.sort(key=_started_key, reverse=True)
        winner, state = candidates[0]

        for other_name, other_state in candidates[1:]:
            log.warning(
                "resume_on_startup: multiple RUNNING experiments; demoting '%s' to ERROR",
                other_name,
            )
            other_state["status"] = ExperimentStatus.ERROR
            other_state["stop_reason"] = "resume_conflict"
            other_state["stopped"] = _iso_now()
            (self._experiments_root / other_name / "state.json").write_text(
                json.dumps(other_state, indent=4), encoding="utf-8"
            )

        # Resume the winner
        with self._lock:
            self._status = ExperimentStatus.RUNNING
            self._name = winner
            self._config = {
                "name": winner,
                "mode": state.get("mode"),
                "vials": state.get("vials", []),
                "parameters": state.get("parameters", {}),
                "calibration": state.get("calibration", {}),
                "media": state.get("media"),
                "notes": state.get("notes", ""),
                "created": state.get("created"),
            }
            self._vials = sorted(int(v) for v in state.get("vials", []))
            self._setpoint_stir = int(state.get("setpoint_stir", 0))
            self._setpoint_raw = {
                int(k): int(v) for k, v in (state.get("setpoint_raw") or {}).items()
            }
            for vial in self._vials:
                self._setpoint_raw.setdefault(vial, HEATER_OFF_SETPOINT)
            self._vial_faults = {
                int(k): v for k, v in (state.get("vial_faults") or {}).items()
            }
            for vial in self._vials:
                self._vial_faults.setdefault(vial, None)
            self._nan_streak = {
                int(k): int(v) for k, v in (state.get("nan_streak") or {}).items()
            }
            for vial in self._vials:
                self._nan_streak.setdefault(vial, 0)
            self._od_range_streak = {
                int(k): int(v)
                for k, v in (state.get("od_range_streak") or {}).items()
            }
            for vial in self._vials:
                self._od_range_streak.setdefault(vial, 0)
            self._created_at = _parse_iso(state.get("created"))
            self._started_at = _parse_iso(state.get("started"))

            # Rebuild controllers and restore their state
            self._controllers = self._build_controllers(
                self._config.get("mode", "turbidostat"),
                self._config["parameters"],
                self._config["calibration"],
                self._vials,
            )
            saved_controllers = state.get("controllers") or {}
            # `now` lets each controller re-baseline a timestamp that was
            # persisted ahead of wall time (CONTROL_MODE_AUDIT.md X-2). The
            # RPi has no RTC, so a stale boot clock would otherwise make
            # every `now - last_x` gate negative and block dilution silently
            # until wall time caught up.
            resume_now = self._clock()
            for vial in self._vials:
                cstate = saved_controllers.get(str(vial))
                if cstate is not None:
                    self._controllers[vial].restore_state(cstate, now=resume_now)

            # Restore media tracking from the persisted snapshot (no-op when
            # the experiment was created without media config).
            self._restore_media_runtime_locked(
                self._config.get("media"), state.get("media_state")
            )

            # Restore maintenance flag. Pending pump actions are intentionally
            # NOT persisted — they may be hours stale by the time the server
            # restarts and firing them blindly could over-dilute. Controllers
            # decide fresh on the first cycle after resume.
            maint = state.get("maintenance") or {}
            if maint.get("active"):
                entered = _parse_iso(maint.get("entered_at"))
                if entered is not None:
                    self._maintenance_active = True
                    self._maintenance_entered_at = entered
                    self._maintenance_reason = maint.get("reason", "manual")
                    self._pending_pump_actions = {}
                    log.info(
                        "resumed in maintenance mode (entered at %s, reason=%s)",
                        entered.isoformat(timespec="seconds"), self._maintenance_reason,
                    )

            # Re-activate the data logger with the original start timestamp so
            # elapsed_hours in CSVs stays continuous.
            try:
                self._data_logger.activate_experiment(winner, start=self._started_at or _now_utc())
            except RuntimeError:
                log.exception("data_logger.activate_experiment failed on resume")
                raise

            # Re-send the heater + stir setpoints
            try:
                self._apply_temperature_locked()
                self._resend_stir_locked()
            except Exception:
                log.exception("resume: re-send actuators failed")

        self._broadcast_event({"type": "resumed", "name": winner, "vials": self._vials})
        log.info("experiment '%s' resumed on startup", winner)
        return winner

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------

    def _unload_locked(self) -> None:
        """Clear loaded-experiment state (called when the user creates a
        new experiment while the previous is STOPPED/ERROR)."""
        self._status = ExperimentStatus.IDLE
        self._name = None
        self._config = None
        self._vials = []
        self._controllers = {}
        self._setpoint_raw = {}
        self._setpoint_stir = 0
        self._nan_streak = {}
        self._od_range_streak = {}
        self._vial_faults = {}
        self._created_at = None
        self._started_at = None
        self._stopped_at = None
        self._stop_reason = None
        self._media_bottles = {}
        self._vial_to_bottle = {}
        self._bottle_consumed_ml = {}
        self._bottle_alerted_low = {}
        self._bottle_alerted_blocked = {}
        self._waste_config = None
        self._waste_filled_ml = 0.0
        self._waste_alerted_high = False
        self._waste_alerted_blocked = False
        self._maintenance_active = False
        self._maintenance_entered_at = None
        self._maintenance_reason = None
        self._pending_pump_actions = {}

    def _load_media_locked(self, media_config: Optional[dict]) -> None:
        """Initialise media tracking state from `config.media`. Called at
        start_experiment and resume_on_startup. No-op when media is absent."""
        if not media_config:
            self._media_bottles = {}
            self._vial_to_bottle = {}
            self._bottle_consumed_ml = {}
            self._bottle_alerted_low = {}
            self._bottle_alerted_blocked = {}
            self._waste_config = None
            self._waste_filled_ml = 0.0
            self._waste_alerted_high = False
            self._waste_alerted_blocked = False
            return
        self._media_bottles = {b["id"]: dict(b) for b in media_config["bottles"]}
        self._vial_to_bottle = {
            int(k): v for k, v in media_config["vial_to_bottle"].items()
        }
        self._bottle_consumed_ml = {bid: 0.0 for bid in self._media_bottles}
        self._bottle_alerted_low = {bid: False for bid in self._media_bottles}
        self._bottle_alerted_blocked = {bid: False for bid in self._media_bottles}
        self._waste_config = dict(media_config["waste"])
        self._waste_filled_ml = 0.0
        self._waste_alerted_high = False
        self._waste_alerted_blocked = False

    def _assert_status(self, *allowed: str) -> None:
        if self._status not in allowed:
            raise InvalidExperimentStateError(
                f"operation not allowed in status={self._status!r}; "
                f"allowed: {allowed}"
            )

    def _broadcast_event(self, payload: dict) -> None:
        if self._on_event is None:
            return
        try:
            payload = dict(payload)
            payload.setdefault("timestamp", _iso_now())
            self._on_event(payload)
        except Exception:
            log.exception("on_event callback failed")

    def _broadcast_alert(
        self,
        *,
        level: str,
        message: str,
        vial: Optional[int] = None,
        category: str = "system",
        dedup_key: Any = None,
    ) -> None:
        """Raise an operator-facing alert (SPEC §20.4).

        ``category`` is passed explicitly by every call site rather than being
        inferred from the message text -- string-matching prose would rot the
        first time a message is reworded.

        ``dedup_key`` is for faults that repeat every cycle: without a stable
        key the ring buffer's rate limiter falls back to
        ``(category, level, message)``, which never collapses a message whose
        text carries changing numbers.
        """
        if self._on_alert is None:
            return
        try:
            payload = {
                "level": level,
                "message": message,
                "category": category,
                "timestamp": _iso_now(),
            }
            if vial is not None:
                payload["vial"] = vial
            if dedup_key is not None:
                payload["dedup_key"] = dedup_key
            self._on_alert(payload)
        except Exception:
            log.exception("on_alert callback failed")


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if s is None:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None
