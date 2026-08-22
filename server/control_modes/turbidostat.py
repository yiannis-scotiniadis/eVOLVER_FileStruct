"""Turbidostat control mode (SPEC §9).

Pure-Python control state with no I/O — entirely deterministic given
(od, time) inputs. The engine wraps this controller per active vial and
calls :meth:`push_od` each cycle and :meth:`decide` to discover whether
to fire pumps.

Algorithm (SPEC §9)::

    average_od = mean(od_history[-history_window:])

    # Hysteresis target switching
    if average_od > od_upper and target != od_lower:
        target = od_lower                       # start diluting
    if average_od < midpoint(od_lower, od_upper) and target != od_upper:
        target = od_upper                       # stop diluting

    # Pump decision
    if average_od > target:
        if (now - last_pump_time) < pump_wait:  # refractory gate FIRST
            return

        pump_time = -(ln(od_lower / latest_od) * volume) / flow_rate
        pump_time = min(pump_time, 20)          # SPEC §9 hard cap

        whole = int(pump_time)                  # truncate, never carry
        if whole >= 1:
            fire influx + efflux for whole seconds
            fire efflux alone for efflux_extra seconds

Variable names mirror SPEC §9 directly.

**There is deliberately no deficit accumulator here** (CONTROL_MODE_AUDIT.md
T-1/T-3, 2026-08-20). ``pump_time`` is an *absolute correction* — the seconds
needed to bring the current OD down to ``od_lower`` — not a per-cycle
increment. Accumulating it makes an integrator with no anti-windup: every
cycle that evaluated the formula but fired nothing (refractory window, lag
cycles) charged the accumulator, and the next dilution over-dosed by the
accumulated charge. Measured floor violation was 17–47 % depending on band
width. The chemostat's accumulator is *correct* because the quantity it
accumulates really is a per-interval increment; see ``chemostat.py``.

Two consequences of truncating with no carry, both intended:

- ``int(t) <= t_needed`` always, so a dilution can never drive OD below
  ``od_lower``. The floor is unbreachable by construction.
- A *second* bolus inside one diluting episode may need < 1 s and is then
  dropped. That is bounded and safe: the first bolus of every episode is
  guaranteed >= 1 s by the band validation in
  ``experiment_engine.validate_control_parameters`` (which rejects a band
  too narrow to ever produce a fireable bolus), and dropping a sub-second
  top-up only under-doses, leaving OD slightly *above* ``od_lower``.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


def _rebaseline_future(
    ts: Optional[float], now: Optional[float], label: str
) -> Optional[float]:
    """Clamp a restored timestamp that lies in the future back to ``now``
    (CONTROL_MODE_AUDIT.md X-2).

    The RPi has no RTC. After a power cut it boots with a stale clock, so
    epoch timestamps persisted in ``state.json`` can be *ahead* of wall
    time. Every gate in these controllers is of the form
    ``now - last_x >= interval``, which then goes negative and blocks
    dilution until wall time catches up -- silently, for as long as the
    skew lasts. Wall time is still the right clock here (a monotonic one
    would not survive the restart at all); only the guard was missing.

    Returns ``ts`` unchanged when it is ``None``, when ``now`` was not
    supplied, or when it is already in the past.
    """
    if ts is None or now is None or ts <= now:
        return ts
    log.warning(
        "%s restored %.1f s in the future (stale RTC?); re-baselining to now",
        label,
        ts - now,
    )
    return float(now)


@dataclass(frozen=True)
class PumpAction:
    """One dilution event returned from :meth:`TurbidostatController.decide`.

    The engine translates this into two ``SerialManager.pump_command`` calls:
    influx for ``pump_time`` seconds, efflux for
    ``pump_time + efflux_extra_seconds`` seconds.

    ``average_od`` is the rolling mean that decided *whether* to dilute (it
    is what lands in ``pump_log.csv``'s ``od_at_pump`` column). ``sizing_od``
    is the reading the bolus duration was computed from -- the latest valid
    sample for a turbidostat, the last seen OD for a chemostat. They differ
    because the lagged mean is a noise filter, not an estimate of the
    culture's density right now (CONTROL_MODE_AUDIT.md T-4).
    """

    pump_time: float
    efflux_extra_seconds: float
    average_od: float
    sizing_od: Optional[float] = None

    @property
    def efflux_seconds(self) -> float:
        """Total efflux pump duration (= pump_time + efflux_extra_seconds)."""
        return self.pump_time + self.efflux_extra_seconds


# Legacy warmup gate: custom_script.py:83 required ``len(data) > 7`` — at
# least 8 OD samples accumulated — before any dilution decision. Without
# this guard the controller can fire on noisy startup data (the first 1-2
# reads after a fresh vial can be wildly off from the steady-state value).
DEFAULT_MIN_SAMPLES_BEFORE_ACTION = 8


class TurbidostatController:
    # The engine's run_cycle consults this to decide whether a dropped OD
    # read should suspend control for the vial (CONTROL_MODE_AUDIT.md C-2).
    # A turbidostat is closed-loop on OD, so it genuinely cannot act without
    # one. The chemostat overrides this with a property.
    requires_od = True

    def __init__(
        self,
        vial: int,
        *,
        od_lower: float,
        od_upper: float,
        pump_wait_seconds: float,
        flow_rate_ml_s: Optional[float] = None,
        flow_rate_influx_ml_s: Optional[float] = None,
        flow_rate_efflux_ml_s: Optional[float] = None,
        volume_ml: float,
        efflux_extra_seconds: float = 0.0,
        history_window: int = 5,
        pump_duration_cap_seconds: float = 20.0,
        min_samples_before_action: int = DEFAULT_MIN_SAMPLES_BEFORE_ACTION,
    ) -> None:
        if not (0 <= vial < 16):
            raise ValueError(f"vial must be in 0..15, got {vial}")
        if not (od_lower > 0):
            raise ValueError(f"od_lower must be > 0, got {od_lower}")
        if not (od_upper > od_lower):
            raise ValueError(
                f"od_upper ({od_upper}) must be > od_lower ({od_lower})"
            )
        if pump_wait_seconds < 0:
            raise ValueError(
                f"pump_wait_seconds must be >= 0, got {pump_wait_seconds}"
            )
        # Influx and efflux are physically separate pumps (SPEC §16.1) and
        # carry independent rates. `flow_rate_ml_s` is the deprecated
        # single-rate form: it seeds whichever direction wasn't given
        # explicitly, so influx and efflux start equal until a per-pump
        # calibration says otherwise.
        influx = flow_rate_influx_ml_s if flow_rate_influx_ml_s is not None else flow_rate_ml_s
        if influx is None:
            raise ValueError(
                "one of flow_rate_influx_ml_s or flow_rate_ml_s is required"
            )
        efflux = flow_rate_efflux_ml_s if flow_rate_efflux_ml_s is not None else influx
        if influx <= 0:
            raise ValueError(f"flow_rate_ml_s must be > 0, got {influx}")
        if efflux <= 0:
            raise ValueError(f"flow_rate_efflux_ml_s must be > 0, got {efflux}")
        if volume_ml <= 0:
            raise ValueError(f"volume_ml must be > 0, got {volume_ml}")
        if history_window < 1:
            raise ValueError(f"history_window must be >= 1, got {history_window}")
        if pump_duration_cap_seconds <= 0:
            raise ValueError(
                f"pump_duration_cap_seconds must be > 0, got {pump_duration_cap_seconds}"
            )
        if min_samples_before_action < 1:
            raise ValueError(
                f"min_samples_before_action must be >= 1, got {min_samples_before_action}"
            )

        self.vial = vial
        self.od_lower = float(od_lower)
        self.od_upper = float(od_upper)
        self.pump_wait_seconds = float(pump_wait_seconds)
        self.flow_rate_influx_ml_s = float(influx)
        self.flow_rate_efflux_ml_s = float(efflux)
        self.volume_ml = float(volume_ml)
        self.efflux_extra_seconds = float(efflux_extra_seconds)
        self.history_window = int(history_window)
        self.pump_duration_cap_seconds = float(pump_duration_cap_seconds)
        self.min_samples_before_action = int(min_samples_before_action)

        # State
        self.target: float = self.od_upper
        self.last_pump_time: Optional[float] = None
        self.od_history: deque[float] = deque(maxlen=self.history_window)
        # Cumulative count of non-NaN samples ever pushed; the warmup gate
        # in decide() requires this to reach min_samples_before_action.
        # Persisted across restart so resumed experiments don't reset the
        # warmup period.
        self.total_samples_seen: int = 0

    @property
    def flow_rate_ml_s(self) -> float:
        """Deprecated single-rate alias — the influx rate. Dilution timing
        and media debits key off influx; per-direction consumers should use
        ``flow_rate_influx_ml_s`` / ``flow_rate_efflux_ml_s``."""
        return self.flow_rate_influx_ml_s

    # ------------------------------------------------------------------
    # Observations
    # ------------------------------------------------------------------

    def push_od(self, od: float) -> None:
        """Append one OD reading to the rolling history. NaN samples are
        silently dropped so a single bus glitch doesn't poison the average
        and does not advance the warmup counter."""
        try:
            if od != od:  # NaN check
                return
        except TypeError:
            return
        self.od_history.append(float(od))
        self.total_samples_seen += 1

    def average_od(self) -> Optional[float]:
        """Mean of all samples currently in ``od_history`` (capped at
        ``history_window``), or ``None`` if no samples yet."""
        if not self.od_history:
            return None
        return sum(self.od_history) / len(self.od_history)

    def time_since_last_pump(self, now: float) -> float:
        """Seconds since the most recent pump fire, or ``+inf`` if never."""
        if self.last_pump_time is None:
            return float("inf")
        return now - self.last_pump_time

    # ------------------------------------------------------------------
    # Control decision (SPEC §9 turbidostat block, verbatim)
    # ------------------------------------------------------------------

    def decide(self, now: float) -> Optional[PumpAction]:
        """Return a :class:`PumpAction` if a dilution should fire now,
        else ``None``. Mutates ``target`` and ``last_pump_time`` when a
        fire is returned.

        Warmup gate (legacy custom_script.py:83): the first
        ``min_samples_before_action`` cycles never fire, regardless of OD.
        This protects against acting on noisy startup data."""
        if self.total_samples_seen < self.min_samples_before_action:
            return None
        average_od = self.average_od()
        if average_od is None:
            return None  # cold start, no samples yet

        # Hysteresis target switching
        if average_od > self.od_upper and self.target != self.od_lower:
            self.target = self.od_lower
        midpoint = (self.od_lower + self.od_upper) / 2.0
        if average_od < midpoint and self.target != self.od_upper:
            self.target = self.od_upper

        # Pump-fire decision
        if average_od <= self.target:
            return None
        if average_od <= self.od_lower:
            # Guard against log(<=0): would happen if user sets od_lower above
            # the actual OD floor. Caller has already gated on average_od > target,
            # so this only fires when target == od_lower and avg is still below.
            return None

        # T-1: the refractory gate runs BEFORE the formula is evaluated. When
        # the formula's output was accumulated first, cycles that fired
        # nothing still charged the accumulator and the next dilution
        # over-dosed by the accumulated charge (up to 2.5x).
        if self.time_since_last_pump(now) < self.pump_wait_seconds:
            return None

        # T-4: the lagged mean answers "should we dilute?" (that is what the
        # rolling window is for -- suppressing OD noise at the threshold).
        # The bolus is sized from the latest valid sample, because the mean
        # of the last `history_window` reads is systematically below the
        # culture's present density while it is growing.
        sizing_od = self.od_history[-1]
        if sizing_od <= self.od_lower:
            # A single low sample under a mean that is still above target:
            # nothing to correct, and log() of a non-positive argument.
            return None

        pump_time = -(math.log(self.od_lower / sizing_od) * self.volume_ml) / self.flow_rate_influx_ml_s
        # SPEC §9: cap at 20 s. Engine then re-clips against its own hard cap.
        pump_time = min(pump_time, self.pump_duration_cap_seconds)

        # T-3: the firmware accepts whole seconds; truncate and discard the
        # remainder rather than carrying it. int(t) <= t_needed, so the
        # dilution can never overshoot past od_lower. See the module
        # docstring for why no accumulator belongs here.
        whole_seconds = int(pump_time)
        if whole_seconds < 1:
            return None

        self.last_pump_time = now
        return PumpAction(
            pump_time=float(whole_seconds),
            efflux_extra_seconds=self.efflux_extra_seconds,
            average_od=average_od,
            sizing_od=sizing_od,
        )

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def to_state(self) -> dict:
        """Serialise mutable state for ``state.json`` persistence."""
        return {
            "target": self.target,
            "last_pump_time": self.last_pump_time,
            "od_history": list(self.od_history),
            "total_samples_seen": self.total_samples_seen,
        }

    def restore_state(self, state: dict, now: Optional[float] = None) -> None:
        """Restore mutable state from a previously-saved dict. Tolerant of
        missing keys (treats them as initial values).

        ``now`` is the current wall clock. When supplied, a restored
        timestamp that lies in the *future* is re-baselined to ``now``
        (CONTROL_MODE_AUDIT.md X-2): the RPi has no RTC, so a stale boot
        clock puts persisted epoch seconds ahead of wall time,
        ``now - last_pump_time`` goes negative, and every dilution is
        blocked until wall time catches up.
        """
        if "target" in state and state["target"] is not None:
            self.target = float(state["target"])
        if "last_pump_time" in state:
            v = state["last_pump_time"]
            self.last_pump_time = None if v is None else float(v)
            self.last_pump_time = _rebaseline_future(
                self.last_pump_time, now, f"vial {self.vial} last_pump_time"
            )
        if "od_history" in state and state["od_history"] is not None:
            self.od_history = deque(
                (float(x) for x in state["od_history"]),
                maxlen=self.history_window,
            )
        if "total_samples_seen" in state and state["total_samples_seen"] is not None:
            self.total_samples_seen = int(state["total_samples_seen"])
        elif "od_history" in state and state["od_history"]:
            # Back-compat: state.json from before the warmup gate was added
            # doesn't have total_samples_seen. The conservative recovery is
            # "at least as many as we have in the history" so we don't undo
            # already-elapsed warmup time.
            self.total_samples_seen = max(
                self.total_samples_seen, len(state["od_history"])
            )
        # "pump_time_deficit_seconds" may still be present in a state.json
        # written before CONTROL_MODE_AUDIT.md T-3 removed the accumulator.
        # Deliberately ignored rather than rejected, so a run in flight on
        # the rig resumes cleanly across the upgrade.
