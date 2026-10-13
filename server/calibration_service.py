"""server/calibration_service.py — Session O calibration provenance + wizards.

Implements CALIBRATION_PROTOCOL.md Part II (§10–§13) / SPEC §19.1–§19.4:

- O1: the common calibration envelope, versioned per-subsystem storage under
  ``calibration/``, a ``current.json`` pointer, and the legacy ``.txt`` files
  regenerated as a *derived view* so ``SerialManager.load_calibration()``
  needs no change.
- O2: the per-run OD blank — :func:`reanchor_od_calibration` re-anchors
  **row 2 only** (rows 0/1/3 are fitted parameters and the reader's validity
  domain; see §19.2's correction), driven by :class:`OdBlankSession`.
- O3: the resumable 32-pump gravimetric session (:class:`PumpCalSession`),
  persisted to ``calibration/_sessions/pump.json`` after every mutation.
- O4: post-run mass reconciliation, logged per run and into the store's
  ``reconciliation_log.json`` (the tubing-wear staleness signal).
- §13 guards: immutable version files, mandatory ``conditions``, condition-
  match + thermal-settling + domain checks on the blank, QC refusal with a
  recorded ``override_reason``, and staleness reporting.

Deliberately Flask-free: the API layer (app.py) maps
``ValueError`` → 400, :class:`CalibrationConflict` → 409, and
:class:`QCRefusal` → 422 (body carries the qc block).
"""

from __future__ import annotations

import bisect
import csv
import json
import logging
import math
import os
import threading
import time
import uuid
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

import od_curve_fit
import run_config
from serial_manager import DEFAULT_RAW_FLOOR, HEATER_OFF_SETPOINT, MAX_SAFE_TEMP_C

N_VIALS = 16
N_PUMPS = 32  # canonical pump index: 0..15 influx vial i, 16..31 efflux vial i-16

SCHEMA = "evolver.calibration/1"
SUBSYSTEMS = ("od", "temperature", "pump", "stir")

# --- Blank acceptance criteria (CALIBRATION_PROTOCOL §5.4). The SD limits
# are PROVISIONAL first-principles estimates; replace with mean + 3 SD of
# measured repeatability once ~5 runs of data exist.
BLANK_DARK_SD_MAX = 150.0
BLANK_SIGNAL_SD_MAX = 300.0
BLANK_MEDIAN_TOL_FRACTION = 0.10     # vs the previous run's blank
C_RUN_DELTA_MAX = 0.15               # OD units, vs the previous run's c_run
THERMAL_SETTLE_TOL_C = 0.3
THERMAL_SETTLE_MINUTES = 10.0

# --- Pump acceptance criteria (CALIBRATION_PROTOCOL §7).
PUMP_DEFAULT_FIRE_SECONDS = 20.0
PUMP_DEFAULT_REPLICATES = 3
PUMP_CV_MAX = 0.05
PUMP_PREV_DELTA_MAX = 0.15
PUMP_MEDIAN_FACTOR = 2.0

# --- Growth-rate service (SPEC §17 / GROWTH_RATE_METHOD.md §5, §8).
# The per-vial low-OD floor is `max(GROWTH_OD_FLOOR_MIN, N x blank_sd_od)`.
# Neither §17's single 0.1 nor Session N's 0.05 is right on its own: the
# Session O audit found optical sensitivity varying FOUR-FOLD across sleeves
# (92-375 counts per 0.01 OD), so the floor has to be per-vial and derived
# from that run's own blank read noise.
GROWTH_OD_FLOOR_MIN = 0.05
GROWTH_OD_FLOOR_SD_MULTIPLE = 10.0
# Above this fitted lower asymptote the OD curve diverges inside the range it
# is used over (CALIBRATION_PROTOCOL §1.4). Same threshold `_od_domain_warnings`
# uses to raise the human-readable warning; shared so the machine-readable
# suspect-vial list and the printed warning can never disagree.
OD_LOWER_ASYMPTOTE_SUSPECT = 40000.0

# --- Tier 3 sessions (Session AA / CALIBRATION_PROTOCOL §8). A Tier 3
# session reserves the whole machine for hours, so it carries its own heater
# watchdog: with no experiment loaded, the engine's _handle_heater_safety_locked
# never runs. Critical is one degree above the request cap -- tighter than the
# engine's 50 °C, because nothing in a calibration should ever approach it.
TIER3_KINDS = ("temperature", "od_series")
CAL_HEATER_CRITICAL_C = MAX_SAFE_TEMP_C + 1.0
CAL_HEATER_DEBOUNCE_READS = 3
RAW_TRACKER_WINDOW_MINUTES = 30.0

# --- Two-point thermistor calibration (CALIBRATION_PROTOCOL §8.1).
TEMP_CAL_DEFAULT_HOT_C = 40.0        # "40 °C, not higher" (§8.1 step 4)
TEMP_CAL_DEFAULT_VERIFY_C = 30.0     # the independent middle point
TEMP_CAL_MAX_TARGET_C = MAX_SAFE_TEMP_C - 3.0
TEMP_CAL_MIN_SPAN_C = 15.0
TEMP_CAL_SLOPE_TOL_FRACTION = 0.10   # vs the leave-one-out pack median
TEMP_CAL_INTERCEPT_TOL_C = 2.0
TEMP_CAL_VERIFY_TOL_C = 0.5
TEMP_CAL_VERIFY_MIN_VIALS = 4        # or every measured vial, if fewer
TEMP_CAL_RETARGET_TOL_C = 2.0        # hot reference this far off target -> retarget
# Equilibrium gates. PROVISIONAL, like the blank SD limits: the protocol's
# "stable to ±2 counts over 10 min", read as a fitted drift of per-minute
# medians. Replace with measured behaviour after the first bench session.
TEMP_CAL_COLD_WINDOW_MIN = 10.0
TEMP_CAL_HOT_WINDOW_MIN = 5.0
TEMP_CAL_STABLE_DRIFT_COUNTS = 2.0
# Prerequisite P2 built into the cold phase: a vial whose raw counts FALL
# (it is heating -- the slope is negative) by more than this per 10 min
# while commanded off, both absolutely AND relative to the pack, has a
# heater that is not off. Absolute alone would flag every vial when the
# room warms; relative alone would flag a vial that merely cools slower.
TEMP_CAL_P2_HEATING_COUNTS = 5.0
# A vial whose INSTALLED-calibration reading sits this far above the pack at
# ambient gets a "probe this vial" advisory (stuck-on heater, or -- vial 0 --
# a bad installed curve). Advisory only: it never excludes a vial.
TEMP_CAL_PROBE_ADVISORY_C = 5.0
TEMP_CAL_SOURCE = "two-point-reference"

# --- OD dilution series (CALIBRATION_PROTOCOL §8.2, amended in Session AA).
OD_SERIES_SOURCE = "dilution-series"

# --- Per-organism OD profiles. Cells scatter light by size, shape and
# contents, so the counts -> OD600 mapping is organism-specific: each
# organism gets its own versioned curve, and a run pins one at create.
# The DEFAULT slot is current["od"] -- the 2016 import, whose organism is
# unknown -- and OD_cal.txt stays its view. It is never recalibrated by a
# series: an organism only comes into existence by being calibrated.
LEGACY_OD_PROFILE = "legacy-2016"
LEGACY_OD_ORGANISM = "Unknown organism — 2016 inherited curve (unverified)"
MACHINE_DEFAULT_OD_PROFILE = "machine-default"
RESERVED_OD_PROFILES = (LEGACY_OD_PROFILE, MACHINE_DEFAULT_OD_PROFILE)
OD_ORGANISM_NAME_MAX = 80
OD_SERIES_MIN_LEVELS = 8             # "at least 8 points" (§8.2 step 1)
OD_SERIES_R2_MIN = 0.99
OD_SERIES_RESIDUAL_ABS = 0.02        # OD
OD_SERIES_RESIDUAL_FRACTION = 0.05   # of the point's OD, whichever is larger
OD_SERIES_LOWER_MARGIN_SD = 5.0      # lowest signal must clear a by this many SDs
OD_SERIES_MIN_SD_COUNTS = 10.0       # floor on the SD used for that margin
OD_SERIES_BLANK_OD_MAX = 0.05        # a point at or below this counts as a blank
OD_SERIES_TOP_RESOLUTION_SD = 2.0    # counts per 0.01 OD at the top vs blank SD
OD_SERIES_SD_WARN = 300.0            # counts, provisional (the blank's limit)
OD_SERIES_SETTLE_TOL_C = 0.5
OD_SERIES_SETTLE_MINUTES = 5.0
ADC_CEILING = 65535.0                # b above this is an extrapolation (§8.2)

# --- Staleness thresholds (CALIBRATION_PROTOCOL §13).
PUMP_STALE_DAYS = 30.0
PUMP_STALE_CUMULATIVE_SECONDS = 40.0 * 3600.0
RECONCILE_TOLERANCE = 0.10
CURVE_STALE_DAYS = 365.0

log = logging.getLogger(__name__)


class CalibrationConflict(Exception):
    """State conflict (experiment running, session exists/missing, immutable
    version collision). The API layer maps this to HTTP 409."""


class QCRefusal(Exception):
    """A fit failed its acceptance criteria and no ``override_reason`` was
    given (§13 "Refuse to save a bad fit"). Carries the qc block so the API
    can return it with HTTP 422."""

    def __init__(self, message: str, qc: dict) -> None:
        super().__init__(message)
        self.qc = qc


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat(timespec="seconds")


def _version_stamp(dt: Optional[datetime] = None) -> str:
    """Filename-safe UTC version stamp, e.g. ``2026-08-20T142203Z``."""
    return (dt or _utc_now()).strftime("%Y-%m-%dT%H%M%SZ")


def _parse_version(version: str) -> Optional[datetime]:
    try:
        return datetime.strptime(version, "%Y-%m-%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return None


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a sibling tmp file + ``os.replace`` so a crash mid-write
    never leaves a half-written file where a reader expects a whole one."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def per_vial_sources(env: Optional[dict]) -> list[Optional[str]]:
    """Where each vial's constants in ``env`` were originally measured.

    Tier 3 envelopes record ``fit.per_vial_source`` (a vial carried forward
    from an earlier version keeps that version's origin, so a 2016 constant
    stays "legacy-import-2016" however many partial calibrations it rides
    through). Older envelopes -- the legacy import, the pump wizard -- carry
    no per-vial record, so every vial takes the envelope's own ``source``."""
    if env is None:
        return [None] * N_VIALS
    per_vial = (env.get("fit") or {}).get("per_vial_source")
    if isinstance(per_vial, list) and len(per_vial) == N_VIALS:
        return [None if s is None else str(s) for s in per_vial]
    return [str(env.get("source", ""))] * N_VIALS


def _is_legacy_source(source: Optional[str]) -> bool:
    return str(source or "").startswith("legacy-import")


def od_profile_id_of(env: Optional[dict]) -> Optional[str]:
    """Which OD profile an envelope belongs to: its recorded ``profile.id``;
    else, by SOURCE, the 2016 import -> ``legacy-2016``, and any other
    profile-less OD curve (a Session AA series installed before profiles
    existed) -> ``machine-default``. Never "everything untagged is legacy":
    that would label a dark-subtracted dilution series as the 2016 curve."""
    if env is None:
        return None
    profile = env.get("profile")
    if isinstance(profile, dict) and profile.get("id"):
        return str(profile["id"])
    if _is_legacy_source(env.get("source")):
        return LEGACY_OD_PROFILE
    return MACHINE_DEFAULT_OD_PROFILE


def od_profile_slug(organism: str) -> str:
    """A stable profile id from an organism name: lowercase ASCII words
    joined by '-', e.g. "Pichia pastoris GS115" -> "pichia-pastoris-gs115"."""
    import re
    import unicodedata
    ascii_name = unicodedata.normalize("NFKD", str(organism)).encode(
        "ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug[:48].strip("-")


def _per_vial_values(values) -> dict[int, float]:
    """``{vial: value}`` from a 16-list or a ``{vial: value}`` map,
    dropping missing and non-finite entries."""
    if isinstance(values, dict):
        items = values.items()
    elif isinstance(values, list):
        items = enumerate(values)
    else:
        return {}
    out: dict[int, float] = {}
    for key, value in items:
        try:
            v, x = int(key), float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(x):
            out[v] = x
    return out


def _finite_or_none(x) -> Optional[float]:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def make_envelope(
    subsystem: str,
    *,
    operator: str,
    source: str,
    conditions: dict,
    data: dict,
    fit: Optional[dict] = None,
    qc: Optional[dict] = None,
    supersedes: Optional[str] = None,
    version: Optional[str] = None,
) -> dict:
    """Build the common calibration envelope (CALIBRATION_PROTOCOL §11.1).

    ``conditions`` is mandatory and must be non-empty — two calibrations
    without their conditions cannot be compared (Principle 5)."""
    if subsystem not in SUBSYSTEMS and subsystem != "od_blank":
        raise ValueError(
            f"subsystem must be one of {SUBSYSTEMS + ('od_blank',)}, got {subsystem!r}"
        )
    if not isinstance(conditions, dict) or not conditions:
        raise ValueError(
            "calibration 'conditions' must be a non-empty object — record at "
            "least the conditions the numbers were taken under (Principle 5)"
        )
    if not isinstance(data, dict):
        raise ValueError("'data' must be an object")
    return {
        "schema": SCHEMA,
        "subsystem": subsystem,
        "version": version or _version_stamp(),
        "supersedes": supersedes,
        "operator": str(operator or "unknown"),
        "source": str(source),
        "conditions": dict(conditions),
        "data": dict(data),
        "fit": dict(fit or {}),
        "qc": dict(qc) if qc is not None else {
            "passed": True, "warnings": [], "failures": [], "overridden_by": None,
        },
    }


def reanchor_od_calibration(od_cal: np.ndarray, blank_raw: np.ndarray) -> np.ndarray:
    """Return a copy of od_cal whose row 2 (inflection OD) is shifted so that
    each vial reports OD == 0.0 at its measured blank signal.

    Rows 0 (lower asymptote), 1 (upper asymptote) and 3 (Hill coefficient) are
    NOT modified: they are fitted parameters of the logistic, not measurements,
    and row 1 exceeds the 16-bit ADC ceiling on every vial. Rows 0 and 1 are
    also the validity domain used by SerialManager._read_od_enhanced_locked;
    changing them changes which readings are accepted.

    Equivalent to OD_new(S) = OD_old(S) - OD_old(blank) — a blank subtraction
    in OD units, preserving the curve shape from the dilution-series fit.
    Derivation: CALIBRATION_PROTOCOL.md Appendix B.1.
    """
    od_cal = np.asarray(od_cal, dtype=float)
    blank_raw = np.asarray(blank_raw, dtype=float)
    if od_cal.shape != (4, N_VIALS):
        raise ValueError(f"od_cal shape {od_cal.shape}, expected (4, {N_VIALS})")
    if blank_raw.shape != (N_VIALS,):
        raise ValueError(
            f"blank_raw shape {blank_raw.shape}, expected ({N_VIALS},)"
        )
    a, b, _c, d = od_cal
    if np.any(blank_raw <= a) or np.any(blank_raw >= b):
        bad = [
            v for v in range(N_VIALS)
            if not (a[v] < blank_raw[v] < b[v])
        ]
        raise ValueError(
            f"blank signal outside the calibration domain (a, b) on vials {bad}"
        )
    c_run = np.log10((b - a) / (blank_raw - a) - 1.0) / d
    out = od_cal.copy()
    out[2] = c_run
    return out


# ---------------------------------------------------------------------------
# Versioned store (O1)
# ---------------------------------------------------------------------------

class CalibrationStore:
    """Versioned calibration artefacts under ``calibration/`` (§11.2).

    Immutability rule (§13): a version file, once written, is never
    overwritten. ``current.json`` is the only mutable pointer, and the
    legacy ``.txt`` files are regenerated from it as a derived view.
    """

    def __init__(self, root: Path, experiments_root: Optional[Path] = None) -> None:
        self.root = Path(root)
        self.experiments_root = Path(experiments_root) if experiments_root else None
        self.current_path = self.root / "current.json"
        self.sessions_dir = self.root / "_sessions"
        self.reconciliation_log_path = self.root / "reconciliation_log.json"
        self._lock = threading.RLock()
        # Per-file pump-log index; see _pump_log_index. Keyed by path string.
        self._pump_log_cache: dict[str, dict] = {}

    # -- layout helpers --------------------------------------------------

    def _subsystem_dir(self, subsystem: str) -> Path:
        return self.root / subsystem

    def _version_path(self, subsystem: str, version: str) -> Path:
        return self._subsystem_dir(subsystem) / f"{version}.json"

    def _read_current(self) -> dict:
        if not self.current_path.is_file():
            return {}
        try:
            return json.loads(self.current_path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("failed to parse %s", self.current_path)
            return {}

    def _write_current(self, current: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.current_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(current, indent=4), encoding="utf-8")
        os.replace(tmp, self.current_path)

    # -- bootstrap (legacy import) ---------------------------------------

    def bootstrap_from_legacy(self, operator: str = "system") -> list[str]:
        """One-time import of the inherited 2016 ``.txt`` files as versioned
        envelopes. No-op when ``current.json`` already exists. The ``.txt``
        files are NOT rewritten here — they are the import source; they only
        regenerate when a *new* od/temperature version is installed later.

        The import carries the audit's QC findings (CALIBRATION_PROTOCOL §1)
        so the review surfaces them instead of burying them for another
        decade: vial 0's temperature fit is a 5-sigma outlier, and vial 1's
        OD lower asymptote sits inside the working signal range."""
        with self._lock:
            if self.current_path.is_file():
                return []
            created: list[str] = []
            current: dict = {s: None for s in SUBSYSTEMS}
            temp_txt = self.root / "temp_calibration.txt"
            od_txt = self.root / "OD_cal.txt"
            conditions = {
                "note": (
                    "conditions unrecorded — inherited 2016 files imported "
                    "without provenance (SPEC §14 Q3); never bench-verified"
                ),
            }
            version = _version_stamp()

            if temp_txt.is_file():
                arr = np.genfromtxt(temp_txt, delimiter=",")
                if arr.shape == (2, N_VIALS):
                    qc = {"passed": True, "warnings": [], "failures": [],
                          "overridden_by": None}
                    qc["warnings"].extend(self._temp_outlier_warnings(arr))
                    env = make_envelope(
                        "temperature", operator=operator,
                        source="legacy-import-2016", conditions=conditions,
                        data={"slope": arr[0].tolist(),
                              "intercept": arr[1].tolist()},
                        fit={}, qc=qc, version=version,
                    )
                    self._write_version_locked("temperature", env)
                    current["temperature"] = version
                    created.append("temperature")

            if od_txt.is_file():
                arr = np.genfromtxt(od_txt, delimiter=",")
                if arr.shape == (4, N_VIALS):
                    qc = {"passed": True, "warnings": [], "failures": [],
                          "overridden_by": None}
                    qc["warnings"].extend(self._od_domain_warnings(arr))
                    env = make_envelope(
                        "od", operator=operator,
                        source="legacy-import-2016", conditions=conditions,
                        data={"rows": arr.tolist(),
                              "dark_subtracted": False},
                        fit={}, qc=qc, version=version,
                    )
                    self._write_version_locked("od", env)
                    current["od"] = version
                    created.append("od")

            if created:
                self._write_current(current)
                log.info("calibration store bootstrapped from legacy: %s", created)
            return created

    @staticmethod
    def _temp_outlier_warnings(arr: np.ndarray) -> list[str]:
        """Flag vials whose slope or intercept sits > 4 SD from the pack
        (computed excluding the candidate, so the outlier can't hide by
        inflating the spread). Vial 0 trips this on the 2016 files."""
        warnings: list[str] = []
        for row, label in ((arr[0], "slope"), (arr[1], "intercept")):
            for v in range(N_VIALS):
                rest = np.delete(row, v)
                sd = float(np.std(rest))
                if sd == 0:
                    continue
                z = abs((float(row[v]) - float(np.mean(rest))) / sd)
                if z > 4.0:
                    warnings.append(
                        f"vial {v} {label} is a {z:.1f}-sigma outlier vs the "
                        "other fifteen — probable bad fit "
                        "(CALIBRATION_PROTOCOL §1.3); bench-verify before use"
                    )
        return warnings

    @staticmethod
    def _od_domain_warnings(arr: np.ndarray) -> list[str]:
        """Flag vials whose fitted lower asymptote sits inside the plausible
        working signal range (~45-65k counts) — their curve diverges inside
        the range it is used over. Vial 1 trips this on the 2016 files."""
        warnings: list[str] = []
        for v in range(N_VIALS):
            if float(arr[0, v]) > OD_LOWER_ASYMPTOTE_SUSPECT:
                warnings.append(
                    f"vial {v} OD lower asymptote ({arr[0, v]:.0f}) sits inside "
                    "the working signal range — curve diverges above OD ~1 "
                    "(CALIBRATION_PROTOCOL §1.4); exclude from quantitative use"
                )
        return warnings

    # -- reads -----------------------------------------------------------

    def current_versions(self) -> dict:
        """The raw per-subsystem version pointers from current.json."""
        with self._lock:
            return dict(self._read_current())

    def current_index(self) -> dict:
        """Per-subsystem summary: version, age_days, source, qc summary."""
        with self._lock:
            current = self._read_current()
            out: dict = {}
            now = _utc_now()
            for subsystem in SUBSYSTEMS:
                version = current.get(subsystem)
                if version is None:
                    out[subsystem] = None
                    continue
                env = self.get_version(subsystem, version)
                age_days = None
                dt = _parse_version(version)
                if dt is not None:
                    age_days = (now - dt).total_seconds() / 86400.0
                out[subsystem] = {
                    "version": version,
                    "age_days": round(age_days, 2) if age_days is not None else None,
                    "source": env.get("source") if env else None,
                    "operator": env.get("operator") if env else None,
                    "qc": env.get("qc") if env else None,
                }
            return out

    def get_current(self, subsystem: str) -> Optional[dict]:
        with self._lock:
            version = self._read_current().get(subsystem)
            if version is None:
                return None
            return self.get_version(subsystem, version)

    def get_version(self, subsystem: str, version: str) -> Optional[dict]:
        path = self._version_path(subsystem, version)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("failed to parse %s", path)
            return None

    def history(self) -> dict:
        """All stored versions per subsystem, newest first."""
        out: dict = {}
        for subsystem in SUBSYSTEMS:
            d = self._subsystem_dir(subsystem)
            versions = []
            if d.is_dir():
                for p in sorted(d.glob("*.json"), reverse=True):
                    versions.append(p.stem)
            out[subsystem] = versions
        # Which organism profile each OD version belongs to.
        out["od_profile_of"] = {
            v: od_profile_id_of(self.get_version("od", v)) for v in out.get("od", [])
        }
        return out

    def current_pump_rates(self) -> Optional[list[float]]:
        """The active pump calibration's flat-32 rates, or None when absent
        or incomplete (a partial spot-check calibration must not feed the
        engine a rate array with holes)."""
        env = self.get_current("pump")
        if env is None:
            return None
        rates = env.get("fit", {}).get("flow_rates_ml_s")
        if not isinstance(rates, list) or len(rates) != N_PUMPS:
            return None
        if any(r is None for r in rates):
            return None
        return [float(r) for r in rates]

    # -- per-organism OD profiles ----------------------------------------

    def od_default_profile(self) -> Optional[str]:
        """The default slot's profile id (``current["od"]``), or None when
        no OD curve is installed at all."""
        env = self.get_current("od")
        return od_profile_id_of(env) if env is not None else None

    def od_profiles(self) -> dict:
        """``{profile_id: {"version", "organism", "is_default"}}`` -- every
        selectable OD profile. Derived, not migrated: the default slot comes
        from ``current["od"]`` (so a boot writes nothing to the tracked
        current.json), named organisms from ``current["od_profiles"]``."""
        with self._lock:
            current = self._read_current()
            out: dict = {}
            default_env = self.get_current("od")
            if default_env is not None:
                pid = od_profile_id_of(default_env)
                organism = (LEGACY_OD_ORGANISM if pid == LEGACY_OD_PROFILE else
                            "Default OD curve (organism unrecorded)")
                out[pid] = {"version": current.get("od"), "organism": organism,
                            "is_default": True}
            for pid, entry in (current.get("od_profiles") or {}).items():
                if isinstance(entry, dict) and entry.get("version"):
                    out[str(pid)] = {"version": str(entry["version"]),
                                     "organism": str(entry.get("organism") or pid),
                                     "is_default": False}
            return out

    def get_od_profile(self, profile_id: str, version: Optional[str] = None) -> Optional[dict]:
        """A profile's envelope: its CURRENT version, or the given (pinned)
        one. A pinned version must belong to the profile."""
        if version is None:
            entry = self.od_profiles().get(profile_id)
            if entry is None:
                return None
            version = entry["version"]
        env = self.get_version("od", version)
        if env is not None and od_profile_id_of(env) != profile_id:
            raise ValueError(
                f"OD calibration {version} belongs to profile "
                f"{od_profile_id_of(env)!r}, not {profile_id!r}"
            )
        return env

    def od_profile_versions(self, profile_id: str) -> list[str]:
        """Every stored version of a profile, newest first."""
        d = self._subsystem_dir("od")
        out = []
        if d.is_dir():
            for path in sorted(d.glob("*.json"), reverse=True):
                env = self.get_version("od", path.stem)
                if env is not None and od_profile_id_of(env) == profile_id:
                    out.append(path.stem)
        return out

    def experiment_od_pin(self, experiment: Optional[str]) -> Optional[tuple[str, str]]:
        """``(profile_id, version)`` the experiment pinned at create, from its
        config.json. A config from before profiles has only ``od`` (a
        version); its profile is whatever that version belongs to."""
        if not experiment or self.experiments_root is None:
            return None
        try:
            config = json.loads(
                (self.experiments_root / experiment / "config.json").read_text(encoding="utf-8")
            )
        except Exception:
            return None
        cal = config.get("calibration") or {}
        version = cal.get("od")
        if not version:
            return None
        profile = cal.get("od_profile") or od_profile_id_of(self.get_version("od", version))
        return (str(profile), str(version)) if profile else None

    def od_vials_measured(self, profile_id: str, env: Optional[dict]) -> list[int]:
        """The vials whose constants in ``env`` were measured under
        ``profile_id`` -- the rest were carried in from another curve
        (a new organism's unmeasured vials come from the default slot)."""
        if env is None:
            return []
        fit = env.get("fit") or {}
        per_profile = fit.get("per_vial_profile")
        if isinstance(per_profile, list) and len(per_profile) == N_VIALS:
            return [v for v in range(N_VIALS) if per_profile[v] == profile_id]
        # Pre-profile envelopes: the whole curve is the profile's own.
        return list(range(N_VIALS)) if od_profile_id_of(env) == profile_id else []

    def od_profile_summaries(self) -> list[dict]:
        """One summary per selectable profile, default first, for the
        experiment designer and the Calibration tab."""
        out = []
        profiles = self.od_profiles()
        order = sorted(profiles, key=lambda p: (not profiles[p]["is_default"], p))
        for pid in order:
            entry = profiles[pid]
            env = self.get_version("od", entry["version"])
            data = (env or {}).get("data") or {}
            age = None
            dt = _parse_version(entry["version"])
            if dt is not None:
                age = round((_utc_now() - dt).total_seconds() / 86400.0, 2)
            reasons = self._od_env_staleness(env, entry["version"])
            sources = per_vial_sources(env)
            out.append({
                "id": pid,
                "organism": entry["organism"],
                "version": entry["version"],
                "is_default": entry["is_default"],
                "source": (env or {}).get("source"),
                "age_days": age,
                "dark_subtracted": bool(data.get("dark_subtracted", False)),
                "vials_measured": self.od_vials_measured(pid, env),
                "legacy_vials": [v for v in range(N_VIALS) if _is_legacy_source(sources[v])],
                "stale": bool(reasons),
                "reasons": reasons,
                "age_reasons": self._od_env_age_reasons(env, entry["version"]),
                "versions": self.od_profile_versions(pid),
            })
        return out

    def _od_env_staleness(self, env: Optional[dict], version: Optional[str]) -> list[str]:
        """Staleness reasons for one OD curve (any profile)."""
        if env is None:
            return ["no OD calibration installed"]
        reasons = []
        legacy = [v for v, src in enumerate(per_vial_sources(env)) if _is_legacy_source(src)]
        if len(legacy) == N_VIALS:
            reasons.append("never verified against a spectrophotometer "
                           "(2016 inherited constants)")
        elif legacy:
            reasons.append(f"vials {legacy} are still on 2016 constants never "
                           "verified against a spectrophotometer")
        return reasons + self._od_env_age_reasons(env, version)

    @staticmethod
    def _od_env_age_reasons(env: Optional[dict], version: Optional[str]) -> list[str]:
        """The age half of :meth:`_od_env_staleness` -- what the designer
        shows beside its own per-run list of carried vials."""
        if env is None:
            return []
        stamps = (env.get("fit") or {}).get("per_vial_version")
        ages = []
        now = _utc_now()
        for stamp in (stamps if isinstance(stamps, list) else [version]):
            dt = _parse_version(stamp) if stamp else None
            if dt is not None:
                ages.append((now - dt).total_seconds() / 86400.0)
        if ages and max(ages) > CURVE_STALE_DAYS:
            return [f"calibration is {max(ages):.0f} days old (> {CURVE_STALE_DAYS:.0f})"]
        return []

    def save_od_profile_version(self, profile_id: str, organism: str, envelope: dict) -> str:
        """Install a new version of a NAMED organism profile. Never touches
        ``current["od"]`` or the ``OD_cal.txt`` view: the default slot is not
        an organism, and machine idle reads keep using it."""
        if profile_id in RESERVED_OD_PROFILES or not od_profile_slug(profile_id):
            raise ValueError(f"{profile_id!r} is not a valid organism profile id")
        with self._lock:
            current = self._read_current()
            if profile_id == self.od_default_profile():
                raise ValueError("the default OD slot is not an organism profile")
            profiles = dict(current.get("od_profiles") or {})
            previous = (profiles.get(profile_id) or {}).get("version")
            env = dict(envelope)
            env["profile"] = {"id": profile_id, "organism": str(organism)}
            env["supersedes"] = previous
            self._write_version_locked("od", env)
            profiles[profile_id] = {"version": env["version"], "organism": str(organism)}
            current["od_profiles"] = profiles
            self._write_current(current)
            return env["version"]

    # -- writes ----------------------------------------------------------

    def _write_version_locked(self, subsystem: str, envelope: dict) -> None:
        path = self._version_path(subsystem, envelope["version"])
        if path.exists():
            raise CalibrationConflict(
                f"calibration version file already exists: {path} — versions "
                "are immutable (Principle 3); pick a new version stamp"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(envelope, indent=4), encoding="utf-8")
        os.replace(tmp, path)

    def fresh_version(self, subsystem: str) -> str:
        """A version stamp for a NEW ``subsystem`` version: now, bumped a
        second at a time past any existing file and past the current
        version, so versions stay unique AND sort in install order even when
        two installs land in the same second."""
        with self._lock:
            dt = _utc_now().replace(microsecond=0)
            # Past the newest stamp ON DISK, not just the current pointer:
            # with per-organism OD profiles the current pointer is only one
            # of several chains, and the Pi has no real-time clock, so a
            # clock set back after a reboot must not interleave versions.
            stamps = [_parse_version(p.stem)
                      for p in self._subsystem_dir(subsystem).glob("*.json")]
            stamps.append(_parse_version(self._read_current().get(subsystem) or ""))
            newest = max((s for s in stamps if s is not None), default=None)
            if newest is not None and dt <= newest:
                dt = newest + timedelta(seconds=1)
            while self._version_path(subsystem, _version_stamp(dt)).exists():
                dt += timedelta(seconds=1)
            return _version_stamp(dt)

    def save_version(self, subsystem: str, envelope: dict) -> str:
        """Install ``envelope`` as the new current version for ``subsystem``.
        Sets ``supersedes`` to the outgoing version, never overwrites an
        existing file, and regenerates the legacy ``.txt`` views for
        od/temperature."""
        if subsystem not in SUBSYSTEMS:
            raise ValueError(f"unknown subsystem {subsystem!r}")
        with self._lock:
            current = self._read_current()
            envelope = dict(envelope)
            # make_envelope always writes the key (None), so setdefault never
            # took effect and every version claimed to supersede nothing.
            if envelope.get("supersedes") is None:
                envelope["supersedes"] = current.get(subsystem)
            self._write_version_locked(subsystem, envelope)
            current[subsystem] = envelope["version"]
            self._write_current(current)
            if subsystem in ("od", "temperature"):
                self.regenerate_legacy_views()
            return envelope["version"]

    def regenerate_legacy_views(self) -> None:
        """Rewrite ``OD_cal.txt`` / ``temp_calibration.txt`` (+ the
        ``OD_cal.meta.json`` sidecar) from the current envelopes, so
        ``SerialManager.load_calibration()`` keeps working unchanged. The
        JSON is the source of truth; the ``.txt`` files are a derived view
        and are never hand-edited."""
        with self._lock:
            od = self.get_current("od")
            if od is not None:
                rows = od.get("data", {}).get("rows")
                if isinstance(rows, list) and len(rows) == 4:
                    lines = [
                        ",".join(format(float(v), ".10g") for v in row)
                        for row in rows
                    ]
                    # Atomic: the manager parses these at boot, and a
                    # half-written view must never be what it finds.
                    _atomic_write_text(
                        self.root / "OD_cal.txt", "\n".join(lines) + "\n",
                    )
                    meta = {
                        "dark_subtracted": bool(
                            od.get("data", {}).get("dark_subtracted", False)
                        ),
                        "version": od.get("version"),
                    }
                    _atomic_write_text(
                        self.root / "OD_cal.meta.json",
                        json.dumps(meta, indent=4),
                    )
            temp = self.get_current("temperature")
            if temp is not None:
                slope = temp.get("data", {}).get("slope")
                intercept = temp.get("data", {}).get("intercept")
                if isinstance(slope, list) and isinstance(intercept, list):
                    lines = [
                        ",".join(format(float(v), ".10g") for v in slope),
                        ",".join(format(float(v), ".10g") for v in intercept),
                    ]
                    _atomic_write_text(
                        self.root / "temp_calibration.txt",
                        "\n".join(lines) + "\n",
                    )

    def ensure_legacy_views(self) -> list[str]:
        """Boot-time integrity check of the derived ``.txt`` views.

        ``save_version`` points ``current.json`` at the new version before it
        regenerates the views, so a crash between the two -- or a hand-edit,
        or a truncated write from before the views were atomic -- leaves the
        manager loading numbers that are not the recorded calibration.
        Returns the subsystems whose view disagreed with ``current.json``
        and was regenerated.

        Verify first, rewrite only on a real mismatch: the views are tracked
        in git, and rewriting them on every boot would reformat the 2016
        files and leave the Pi's checkout dirty for the next update-by-tag."""
        with self._lock:
            if not self.current_path.is_file():
                return []
            stale: list[str] = []
            od = self.get_current("od")
            if od is not None:
                rows = (od.get("data") or {}).get("rows")
                want_ds = bool((od.get("data") or {}).get("dark_subtracted", False))
                if not self._view_matches(self.root / "OD_cal.txt", rows) \
                        or self._view_dark_subtracted() != want_ds:
                    stale.append("od")
            temp = self.get_current("temperature")
            if temp is not None:
                data = temp.get("data") or {}
                rows = [data.get("slope"), data.get("intercept")]
                if not self._view_matches(self.root / "temp_calibration.txt", rows):
                    stale.append("temperature")
            if stale:
                self.regenerate_legacy_views()
            return stale

    @staticmethod
    def _view_matches(path: Path, rows) -> bool:
        try:
            want = np.asarray(rows, dtype=float)
            have = np.genfromtxt(path, delimiter=",")
        except Exception:
            return False
        return (
            have.shape == want.shape
            and bool(np.all(np.isfinite(have)))
            and bool(np.allclose(have, want, rtol=1e-9, atol=0.0))
        )

    def _view_dark_subtracted(self) -> bool:
        """The sidecar flag as the manager will read it: absent -> False."""
        try:
            meta = json.loads(
                (self.root / "OD_cal.meta.json").read_text(encoding="utf-8")
            )
            return bool(meta.get("dark_subtracted", False))
        except Exception:
            return False

    # -- reconciliation log (O4 -> staleness input) ----------------------

    def record_reconciliation(self, record: dict) -> None:
        with self._lock:
            entries: list = []
            if self.reconciliation_log_path.is_file():
                try:
                    entries = json.loads(
                        self.reconciliation_log_path.read_text(encoding="utf-8")
                    )
                except Exception:
                    log.exception("failed to parse reconciliation log")
            entries.append(record)
            tmp = self.reconciliation_log_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(entries, indent=4), encoding="utf-8")
            os.replace(tmp, self.reconciliation_log_path)

    def last_reconciliation(self) -> Optional[dict]:
        if not self.reconciliation_log_path.is_file():
            return None
        try:
            entries = json.loads(
                self.reconciliation_log_path.read_text(encoding="utf-8")
            )
            return entries[-1] if entries else None
        except Exception:
            log.exception("failed to parse reconciliation log")
            return None

    # -- staleness (§13) -------------------------------------------------

    def _pump_log_index(self, csv_path: Path) -> Optional[dict]:
        """Cumulative pump-time index for one pump log, parsed incrementally.

        Pump logs are append-only, which is the property this exploits: after
        the first pass only newly-appended bytes are parsed, so the cost of a
        staleness check stops growing with the length of the campaign. The
        uncached scan was measured at 1.4 s over a 17-experiment tree and grew
        without bound; it is dormant today only because ``current.json`` has
        ``"pump": null``, and goes live on the dashboard banner the moment Tier
        2 calibration lands.

        Returns ``{"total_all", "epochs", "cum", "ordered"}``:

        * ``total_all`` -- every row's duration, INCLUDING rows whose timestamp
          does not parse. This is what the ``since=None`` query returns.
        * ``epochs`` / ``cum`` -- ascending timestamps and their prefix sums,
          over parseable rows only. A ``since`` query is a bisect plus one
          subtraction.

        The split preserves an asymmetry in the behaviour this replaces:
        ``since=None`` counted unparseable-timestamp rows, ``since=<version>``
        skipped them. Both are still true.
        """
        try:
            st = csv_path.stat()
        except OSError:
            return None
        key = str(csv_path)
        entry = self._pump_log_cache.get(key)
        if (
            entry is None
            or st.st_size < entry["parsed_to"]
            or st.st_mtime < entry["mtime"]
        ):
            # Absent, truncated or replaced -> parse from the top.
            entry = {"parsed_to": 0, "mtime": st.st_mtime, "total_all": 0.0,
                     "epochs": [], "cum": [0.0], "ordered": True}
        elif st.st_size == entry["parsed_to"]:
            entry["mtime"] = st.st_mtime
            self._pump_log_cache[key] = entry
            return entry

        try:
            with csv_path.open("rb") as f:
                if entry["parsed_to"]:
                    f.seek(entry["parsed_to"])
                blob = f.read()
        except OSError:
            log.exception("failed reading %s", csv_path)
            return entry if entry["cum"] else None

        # Only consume complete lines; a row half-flushed at EOF is left for
        # the next call rather than being parsed as a truncated record.
        cut = blob.rfind(b"\n")
        if cut == -1:
            entry["mtime"] = st.st_mtime
            self._pump_log_cache[key] = entry
            return entry
        consumed = blob[:cut + 1]
        lines = consumed.decode("utf-8", errors="replace").splitlines()
        if entry["parsed_to"] == 0:
            lines = lines[1:]                       # header
        entry["parsed_to"] += len(consumed)
        entry["mtime"] = st.st_mtime

        epochs, cum = entry["epochs"], entry["cum"]
        for line in lines:
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < 4:
                continue
            try:
                duration = float(parts[3] or 0.0)
            except (TypeError, ValueError):
                continue
            entry["total_all"] += duration
            try:
                row_dt = datetime.fromisoformat(parts[0])
            except ValueError:
                continue
            if row_dt.tzinfo is None:
                row_dt = row_dt.replace(tzinfo=timezone.utc)
            ts = row_dt.timestamp()
            if epochs and ts < epochs[-1]:
                # A clock step backwards would break the bisect; fall back to a
                # linear scan for this file rather than returning a wrong number.
                entry["ordered"] = False
            epochs.append(ts)
            cum.append(cum[-1] + duration)

        self._pump_log_cache[key] = entry
        return entry

    def pump_seconds_since(self, version: Optional[str]) -> Optional[float]:
        """Cumulative pump-on seconds across every experiment's pump logs
        since the given calibration version's timestamp (the tubing-wear
        signal). Returns None when the experiments root is unknown.

        Backed by a per-file incremental index (:meth:`_pump_log_index`), so
        this is O(newly-appended rows) after the first call rather than O(every
        pump event ever logged).
        """
        if self.experiments_root is None or not self.experiments_root.is_dir():
            return None
        since = _parse_version(version) if version else None
        since_ts = since.timestamp() if since is not None else None
        total = 0.0
        with self._lock:
            seen: set = set()
            for csv_path in self.experiments_root.glob("*/vial*_pump_log.csv"):
                entry = self._pump_log_index(csv_path)
                if entry is None:
                    continue
                seen.add(str(csv_path))
                if since_ts is None:
                    total += entry["total_all"]
                elif entry["ordered"]:
                    i = bisect.bisect_left(entry["epochs"], since_ts)
                    total += entry["cum"][-1] - entry["cum"][i]
                else:
                    total += sum(
                        entry["cum"][j + 1] - entry["cum"][j]
                        for j, ts in enumerate(entry["epochs"])
                        if ts >= since_ts
                    )
            # Drop entries for files that have gone away (deleted experiments).
            for stale in [k for k in self._pump_log_cache if k not in seen]:
                del self._pump_log_cache[stale]
        return total

    def staleness(
        self,
        *,
        loaded_experiment: Optional[str] = None,
        loaded_status: Optional[str] = None,
    ) -> dict:
        """Per-subsystem staleness report (CALIBRATION_PROTOCOL §13)."""
        now = _utc_now()
        current = self._read_current()
        out: dict = {}

        def _age_days(version: Optional[str]) -> Optional[float]:
            dt = _parse_version(version) if version else None
            if dt is None:
                return None
            return (now - dt).total_seconds() / 86400.0

        # Pump
        pump_version = current.get("pump")
        reasons: list[str] = []
        if pump_version is None:
            reasons.append(
                "never calibrated — flow rates are the hardcoded defaults; "
                "every mL figure is an uncalibrated estimate"
            )
        else:
            age = _age_days(pump_version)
            if age is not None and age > PUMP_STALE_DAYS:
                reasons.append(f"calibration is {age:.0f} days old (> {PUMP_STALE_DAYS:.0f})")
            seconds = self.pump_seconds_since(pump_version)
            if seconds is not None and seconds > PUMP_STALE_CUMULATIVE_SECONDS:
                reasons.append(
                    f"{seconds / 3600.0:.1f} h cumulative pump time since "
                    f"calibration (> {PUMP_STALE_CUMULATIVE_SECONDS / 3600.0:.0f} h)"
                )
            recon = self.last_reconciliation()
            if recon is not None and not recon.get("within_tolerance", True):
                reasons.append(
                    "last mass reconciliation was outside ±10 % "
                    f"({recon.get('experiment', '?')})"
                )
        out["pump"] = {"stale": bool(reasons), "reasons": reasons,
                       "version": pump_version}

        # OD blank (per-run; hard block, not a warning)
        blank_reasons: list[str] = []
        blank_present = None
        if loaded_experiment and loaded_status in ("created", "running"):
            blank_present = False
            if self.experiments_root is not None:
                blank_present = (
                    self.experiments_root / loaded_experiment / "od_blank.json"
                ).is_file()
            if not blank_present:
                blank_reasons.append(
                    f"no per-run OD blank taken for '{loaded_experiment}' — "
                    "the machine reports OD 0.12-0.44 for a sterile blank "
                    "without one (hard block at start)"
                )
        out["od_blank"] = {
            "stale": bool(blank_reasons), "reasons": blank_reasons,
            "experiment": loaded_experiment, "present": blank_present,
        }

        def _curve_age_days(env: Optional[dict], version: str) -> Optional[float]:
            """Age of the OLDEST constant in the curve. A partial Tier 3
            calibration carries the other vials forward, so the version
            stamp alone would make a year-old vial look fresh."""
            stamps = (env or {}).get("fit", {}).get("per_vial_version")
            ages = []
            if isinstance(stamps, list):
                ages = [a for a in (_age_days(s) for s in stamps) if a is not None]
            return max(ages) if ages else _age_days(version)

        def _legacy_reason(env: Optional[dict], instrument: str) -> Optional[str]:
            # Keyed off the structured per-vial record, never off warning
            # text: a new version carries a vial-map warning until P1 is
            # done, and that alone must not read as "stale".
            legacy = [v for v, s in enumerate(per_vial_sources(env))
                      if _is_legacy_source(s)]
            if len(legacy) == N_VIALS:
                return (f"never verified against a {instrument} "
                        "(2016 inherited constants)")
            if legacy:
                return (f"vials {legacy} are still on 2016 constants never "
                        f"verified against a {instrument}")
            return None

        # OD curve: the loaded experiment's organism profile (the curve its
        # numbers actually go through) when there is one, else the default.
        od_version = current.get("od")
        od_profile = self.od_default_profile()
        pinned = self.experiment_od_pin(loaded_experiment) if loaded_experiment else None
        if pinned is not None:
            od_profile, od_version = pinned
        od_env = self.get_version("od", od_version) if od_version else None
        reasons = (self._od_env_staleness(od_env, od_version) if od_version
                   else ["no OD calibration installed"])
        out["od"] = {"stale": bool(reasons), "reasons": reasons,
                     "version": od_version, "profile": od_profile}
        out["od_profiles"] = {
            p["id"]: {"stale": p["stale"], "reasons": p["reasons"],
                      "version": p["version"]}
            for p in self.od_profile_summaries()
        }

        # Temperature
        temp_version = current.get("temperature")
        temp_env = self.get_current("temperature") if temp_version else None
        reasons = []
        if temp_version is None:
            reasons.append("no temperature calibration installed")
        else:
            legacy = _legacy_reason(temp_env, "reference thermometer")
            if legacy:
                reasons.append(legacy)
            fit = (temp_env or {}).get("fit") or {}
            if temp_env is not None and "per_vial_source" not in fit:
                # Legacy-format envelope: its qc warnings are the audit's
                # leave-one-out outlier findings (bootstrap_from_legacy).
                if temp_env.get("qc", {}).get("warnings"):
                    reasons.append(
                        "outlier vial flagged in the fit "
                        "(see the calibration's qc warnings — vial 0 is the "
                        "prime suspect)"
                    )
            else:
                flagged = sorted(int(v) for v in (fit.get("flagged_vials") or {}))
                verification = (temp_env or {}).get("qc", {}).get("verification") or {}
                verified = {int(v) for v in verification.get("passed_vials", [])}
                unverified = [v for v in flagged if v not in verified]
                if unverified:
                    reasons.append(
                        f"vials {unverified} fit outside the pack "
                        "(slope/intercept) and were not independently verified "
                        "against the reference thermometer"
                    )
            age = _curve_age_days(temp_env, temp_version)
            if age is not None and age > CURVE_STALE_DAYS:
                reasons.append(f"calibration is {age:.0f} days old (> {CURVE_STALE_DAYS:.0f})")
        out["temperature"] = {"stale": bool(reasons), "reasons": reasons,
                              "version": temp_version}

        # Stir
        stir_version = current.get("stir")
        out["stir"] = {
            "stale": stir_version is None,
            "reasons": (["no stir calibration — stir is reported as raw PWM "
                         "(Tier 3.3, deferred pending tachometry equipment)"]
                        if stir_version is None else []),
            "version": stir_version,
        }

        out["any_stale"] = any(
            v.get("stale") for k, v in out.items() if isinstance(v, dict)
        )
        return out


# ---------------------------------------------------------------------------
# Thermal-settling tracker (§13 guard, fed by the sensor loop)
# ---------------------------------------------------------------------------

class TempStabilityTracker:
    """Rolling window of temperature reads. The blank commit guard requires
    the vials to have been within ±0.3 °C of setpoint for >= 10 minutes —
    enforced from what the sensor loop actually saw, not trusted."""

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        window_minutes: float = 15.0,
    ) -> None:
        self._clock = clock
        self._window_seconds = float(window_minutes) * 60.0
        self._samples: list[tuple[float, list[float]]] = []
        self._lock = threading.Lock()

    def note(self, temps: list) -> None:
        now = self._clock()
        with self._lock:
            self._samples.append((now, [float(t) for t in temps]))
            cutoff = now - self._window_seconds
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.pop(0)

    def latest(self) -> list[float]:
        """The most recent reading (NaN per vial before the first)."""
        with self._lock:
            if not self._samples:
                return [float("nan")] * N_VIALS
            return list(self._samples[-1][1])

    def reset(self) -> None:
        """Forget the window. Called when a new temperature calibration is
        installed: the history is °C on the OLD scale, and the blank's
        settle guard must not certify a vial against numbers the current
        calibration would not have produced."""
        with self._lock:
            self._samples.clear()

    def settled(
        self,
        vials: list[int],
        targets_c: dict,
        *,
        tol_c: float = THERMAL_SETTLE_TOL_C,
        minutes: float = THERMAL_SETTLE_MINUTES,
    ) -> dict:
        """Check the last ``minutes`` of readings for the given vials against
        their per-vial targets. Returns ``{"settled": bool, "detail": ...}``.
        NaN samples (dropped frames) are ignored per vial, but each vial needs
        at least 5 valid samples spanning the window to count as settled."""
        now = self._clock()
        cutoff = now - minutes * 60.0
        with self._lock:
            window = [(t, v) for t, v in self._samples if t >= cutoff]
        detail: dict = {}
        settled = True
        for vial in vials:
            target = float(targets_c[vial])
            times = [t for t, temps in window
                     if vial < len(temps) and not math.isnan(temps[vial])]
            values = [temps[vial] for _t, temps in window
                      if vial < len(temps) and not math.isnan(temps[vial])]
            span_ok = (
                len(values) >= 5
                and times
                and (times[0] - cutoff) <= 60.0  # coverage back to window start
            )
            within = all(abs(v - target) <= tol_c for v in values) if values else False
            vial_ok = bool(span_ok and within)
            detail[str(vial)] = {
                "target_c": target,
                "n_samples": len(values),
                "max_dev_c": (round(max(abs(v - target) for v in values), 3)
                              if values else None),
                "settled": vial_ok,
            }
            settled = settled and vial_ok
        return {
            "settled": settled,
            "required_minutes": minutes,
            "tolerance_c": tol_c,
            "per_vial": detail,
        }


# ---------------------------------------------------------------------------
# Per-run OD blank session (O2)
# ---------------------------------------------------------------------------

# A blank session idle longer than this may be replaced by another
# experiment's (the procedure itself takes ~10 minutes, §5.4).
BLANK_SESSION_STALE_SECONDS = 60 * 60


def stir_pause_blank_warning(stir_paused_vials: dict) -> Optional[str]:
    """The warning for a per-run blank whose run pauses some vials' stirrers
    before OD reads (``od_stir_pause``), or None when no vial pauses.

    The blank is taken with the stirrers running. The re-anchor it computes
    is therefore an offset measured in a different optical condition from the
    settled reads it will correct, and the organism's OD curve was fitted
    stirring too. Warn-only by decision (SPEC §19.2): the error is the size
    of the vortex effect, which is exactly what the operator chose to remove."""
    if not stir_paused_vials:
        return None
    vials = sorted(int(v) for v in stir_paused_vials)
    return (
        f"vials {vials} pause their stirrer before every OD read, but this "
        "blank (and the organism's OD curve) was measured with stirring ON. "
        "Their OD will carry an offset roughly the size of the vortex effect; "
        "compare against an off-line reading, or recalibrate the run post hoc."
    )


class OdBlankSession:
    """State for one §5.4 dark/blank sequence. Deliberately in-memory: the
    whole procedure is ~10 minutes at the bench, and a server restart
    mid-blank simply means retaking it (only the pump session is required
    to be resumable)."""

    def __init__(
        self,
        *,
        experiment: str,
        vials: list[int],
        led_power: int,
        stir_by_vial: dict,
        targets_c: dict,
        n_samples: int = 5,
    ) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.experiment = experiment
        self.started_monotonic_wall = time.time()
        self.vials = list(vials)
        self.led_power = int(led_power)
        # Per vial: vial groups may stir at different rates.
        self.stir_by_vial = {int(k): int(v) for k, v in stir_by_vial.items()}
        self.targets_c = dict(targets_c)
        self.n_samples = int(n_samples)
        # Vials whose group pauses its stirrer before every OD read
        # (od_stir_pause): {vial: pause config}. The blank itself is always
        # taken STIRRING, so for these vials it is measured in a different
        # optical condition from every read it will correct -- warned, not
        # refused (SPEC §19.2), and recorded so a future paused blank can be
        # told apart from this one.
        self.stir_paused_vials: dict[int, dict] = {}
        self.created_at = _iso_now()
        self.dark: Optional[dict] = None
        self.blank: Optional[dict] = None
        # The run's pinned OD curve (organism profile + version): the blank
        # re-anchors THAT curve, never "whatever is current".
        self.od_profile: Optional[str] = None
        self.od_version: Optional[str] = None


# ---------------------------------------------------------------------------
# Pump gravimetric session (O3) — resumable
# ---------------------------------------------------------------------------

class PumpCalSession:
    """Resumable per-campaign gravimetric session (§7). Persisted after every
    mutation so ~an hour of bench work survives a server restart; ``abort``
    deletes the file and writes nothing to ``calibration/``."""

    @staticmethod
    def pump_direction(pump_id: int) -> tuple[int, str]:
        """Canonical pump index -> (vial, direction)."""
        if not (0 <= pump_id < N_PUMPS):
            raise ValueError(f"pump_id must be in 0..{N_PUMPS - 1}, got {pump_id}")
        if pump_id < N_VIALS:
            return pump_id, "influx"
        return pump_id - N_VIALS, "efflux"

    def __init__(
        self,
        *,
        pumps: list[int],
        fire_seconds: float = PUMP_DEFAULT_FIRE_SECONDS,
        replicates: int = PUMP_DEFAULT_REPLICATES,
        fluid: str = "water",
        fluid_density_g_ml: float = 0.99777,
        bench_temp_c: Optional[float] = None,
        operator: str = "unknown",
    ) -> None:
        pumps = sorted({int(p) for p in pumps})
        for p in pumps:
            if not (0 <= p < N_PUMPS):
                raise ValueError(f"pump id {p} out of range 0..{N_PUMPS - 1}")
        if not pumps:
            raise ValueError("'pumps' must be a non-empty list of pump ids")
        if not (0 < fire_seconds <= 60):
            raise ValueError(f"fire_seconds must be in (0, 60], got {fire_seconds}")
        if not (1 <= int(replicates) <= 10):
            raise ValueError(f"replicates must be in 1..10, got {replicates}")
        if not (0.5 <= fluid_density_g_ml <= 2.0):
            raise ValueError(
                f"fluid_density_g_ml {fluid_density_g_ml} is implausible"
            )
        self.started_at = _iso_now()
        self.operator = str(operator or "unknown")
        self.pumps = pumps
        self.fire_seconds = float(fire_seconds)
        self.replicates = int(replicates)
        self.fluid = str(fluid)
        self.fluid_density_g_ml = float(fluid_density_g_ml)
        self.bench_temp_c = None if bench_temp_c is None else float(bench_temp_c)
        # pump_id (str for JSON) -> {"masses_g": [...], "fired": int}
        self.results: dict = {str(p): {"masses_g": [], "fired": 0} for p in pumps}

    # -- persistence -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "operator": self.operator,
            "pumps": self.pumps,
            "fire_seconds": self.fire_seconds,
            "replicates": self.replicates,
            "fluid": self.fluid,
            "fluid_density_g_ml": self.fluid_density_g_ml,
            "bench_temp_c": self.bench_temp_c,
            "results": self.results,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PumpCalSession":
        s = cls(
            pumps=d["pumps"],
            fire_seconds=d.get("fire_seconds", PUMP_DEFAULT_FIRE_SECONDS),
            replicates=d.get("replicates", PUMP_DEFAULT_REPLICATES),
            fluid=d.get("fluid", "water"),
            fluid_density_g_ml=d.get("fluid_density_g_ml", 0.99777),
            bench_temp_c=d.get("bench_temp_c"),
            operator=d.get("operator", "unknown"),
        )
        s.started_at = d.get("started_at", s.started_at)
        for key, val in (d.get("results") or {}).items():
            if key in s.results:
                s.results[key] = {
                    "masses_g": [float(m) for m in val.get("masses_g", [])],
                    "fired": int(val.get("fired", 0)),
                }
        return s

    # -- bench flow ------------------------------------------------------

    def record_mass(self, pump_id: int, replicate: int, mass_g: float) -> dict:
        key = str(int(pump_id))
        if key not in self.results:
            raise ValueError(f"pump {pump_id} is not part of this session")
        if not isinstance(mass_g, (int, float)) or isinstance(mass_g, bool):
            raise ValueError("'mass_g' must be a number")
        if not (0 < float(mass_g) < 1000):
            raise ValueError(f"mass_g {mass_g} is implausible")
        masses = self.results[key]["masses_g"]
        replicate = int(replicate)
        if replicate == len(masses):
            masses.append(float(mass_g))
        elif 0 <= replicate < len(masses):
            masses[replicate] = float(mass_g)  # re-record a bad weighing
        else:
            raise ValueError(
                f"replicate {replicate} out of order — {len(masses)} recorded "
                f"so far for pump {pump_id}"
            )
        return self.pump_stats(pump_id)

    def pump_stats(self, pump_id: int) -> dict:
        key = str(int(pump_id))
        entry = self.results[key]
        masses = entry["masses_g"]
        rate = cv = None
        if masses:
            mean = float(np.mean(masses))
            rate = mean / self.fluid_density_g_ml / self.fire_seconds
            cv = float(np.std(masses) / mean) if mean > 0 and len(masses) > 1 else None
        return {
            "pump_id": int(pump_id),
            "masses_g": list(masses),
            "recorded": len(masses),
            "replicates": self.replicates,
            "fired": entry["fired"],
            "rate_ml_s": rate,
            "cv": cv,
            "done": len(masses) >= self.replicates,
        }

    def progress(self) -> dict:
        per_pump = {str(p): self.pump_stats(p) for p in self.pumps}
        remaining = [p for p in self.pumps
                     if len(self.results[str(p)]["masses_g"]) < self.replicates]
        return {
            "started_at": self.started_at,
            "operator": self.operator,
            "fire_seconds": self.fire_seconds,
            "replicates": self.replicates,
            "fluid": self.fluid,
            "fluid_density_g_ml": self.fluid_density_g_ml,
            "bench_temp_c": self.bench_temp_c,
            "pumps": self.pumps,
            "remaining": remaining,
            "per_pump": per_pump,
        }

    def fit_and_qc(self, previous_rates: Optional[list]) -> tuple[dict, dict]:
        """Compute per-pump rates and run the §7 acceptance criteria.
        Returns ``(fit, qc)``; the caller decides refusal vs override."""
        incomplete = [p for p in self.pumps
                      if len(self.results[str(p)]["masses_g"]) < self.replicates]
        if incomplete:
            raise ValueError(
                f"pumps {incomplete} still need masses recorded "
                f"({self.replicates} replicates each) — finish or abort"
            )
        stats = {p: self.pump_stats(p) for p in self.pumps}
        rates = {p: stats[p]["rate_ml_s"] for p in self.pumps}
        manifold_median = float(np.median([r for r in rates.values()]))
        failures: list[str] = []
        warnings: list[str] = []
        review: dict = {}
        for p in self.pumps:
            st = stats[p]
            rate = st["rate_ml_s"]
            prev = None
            if previous_rates is not None and previous_rates[p] is not None:
                prev = float(previous_rates[p])
            delta_pct = (
                100.0 * (rate / prev - 1.0) if prev not in (None, 0) else None
            )
            review[str(p)] = {
                "rate_ml_s": round(rate, 5),
                "previous_rate_ml_s": prev,
                "delta_pct": round(delta_pct, 2) if delta_pct is not None else None,
                "cv": round(st["cv"], 4) if st["cv"] is not None else None,
                "masses_g": st["masses_g"],
            }
            if rate <= 0:
                failures.append(f"pump {p}: zero/negative rate — stalled or mis-addressed")
            elif not (manifold_median / PUMP_MEDIAN_FACTOR
                      <= rate <= manifold_median * PUMP_MEDIAN_FACTOR):
                failures.append(
                    f"pump {p}: rate {rate:.3f} mL/s outside 2x of the manifold "
                    f"median ({manifold_median:.3f}) — check the binary address "
                    "mapping before blaming the pump"
                )
            if st["cv"] is not None and st["cv"] > PUMP_CV_MAX:
                failures.append(
                    f"pump {p}: replicate CV {100 * st['cv']:.1f}% > "
                    f"{100 * PUMP_CV_MAX:.0f}% — re-prime and repeat"
                )
            if delta_pct is not None and abs(delta_pct) > 100 * PUMP_PREV_DELTA_MAX:
                warnings.append(
                    f"pump {p}: {delta_pct:+.1f}% vs previous calibration — "
                    "tubing wears, but a line drifting >15% twice in a row "
                    "should be re-tubed"
                )
        # Merge: measured pumps overwrite; unmeasured carry the previous rate.
        merged: list = [None] * N_PUMPS
        if previous_rates is not None:
            for i in range(N_PUMPS):
                merged[i] = previous_rates[i]
        for p in self.pumps:
            merged[p] = rates[p]
        fit = {
            "flow_rates_ml_s": merged,
            "measured_pumps": list(self.pumps),
            "manifold_median_ml_s": round(manifold_median, 5),
            "review": review,
        }
        qc = {
            "passed": not failures,
            "warnings": warnings,
            "failures": failures,
            "overridden_by": None,
        }
        return fit, qc


# ---------------------------------------------------------------------------
# Tier 3 sessions (Session AA) — machine-reserving, resumable
# ---------------------------------------------------------------------------

class RawTempTracker:
    """Rolling window of RAW thermistor ADC vectors, fed by the sensor loop
    while a Tier 3 session exists. The thermistor wizard gates its captures
    on these counts, not on °C: the °C scale is the thing being calibrated.

    Stability is judged on PER-MINUTE MEDIANS, so one noisy read cannot
    un-settle a vial and a slow drift cannot hide inside the noise."""

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        window_minutes: float = RAW_TRACKER_WINDOW_MINUTES,
    ) -> None:
        self._clock = clock
        self._window_seconds = float(window_minutes) * 60.0
        self._samples: list[tuple[float, np.ndarray]] = []
        self._lock = threading.Lock()

    def note(self, raw) -> None:
        if raw is None:
            return
        now = self._clock()
        arr = np.asarray(raw, dtype=float)
        if arr.shape != (N_VIALS,):
            return
        with self._lock:
            self._samples.append((now, arr))
            cutoff = now - self._window_seconds
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.pop(0)

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()

    def latest(self) -> list[float]:
        with self._lock:
            if not self._samples:
                return [float("nan")] * N_VIALS
            return self._samples[-1][1].tolist()

    def minute_medians(
        self, minutes: float, *, relative_to: Optional[list[int]] = None,
    ) -> tuple[np.ndarray, float]:
        """``(medians, coverage_minutes)``: an ``(n_minutes, 16)`` array of
        per-minute medians over the last ``minutes``, oldest first, and how
        far back the samples actually reach.

        With ``relative_to`` (a vial list), every sample is first expressed
        as its offset from those vials' pack median at the same instant.
        That cancels room drift the whole pack shares -- harmless to a
        calibration point, since the reference is read at the same moment --
        without hiding a single vial that is still moving."""
        now = self._clock()
        cutoff = now - float(minutes) * 60.0
        with self._lock:
            window = [(t, a) for t, a in self._samples if t >= cutoff]
        if not window:
            return np.empty((0, N_VIALS)), 0.0
        coverage = (now - window[0][0]) / 60.0
        buckets: dict[int, list[np.ndarray]] = {}
        for t, arr in window:
            if relative_to:
                pack = np.nanmedian(arr[relative_to]) if np.any(
                    np.isfinite(arr[relative_to])) else np.nan
                arr = arr - pack
            buckets.setdefault(int((t - cutoff) // 60.0), []).append(arr)
        rows = []
        for key in sorted(buckets):
            stack = np.vstack(buckets[key])
            # nanmedian of an all-NaN column warns; a dropped vial is normal.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)
                rows.append(np.nanmedian(stack, axis=0))
        return np.vstack(rows), coverage

    def drift(
        self, minutes: float, *, relative_to: Optional[list[int]] = None,
    ) -> tuple[np.ndarray, float, int]:
        """``(drift, coverage_minutes, n_minutes)``: per vial, the change in
        counts across the window implied by a least-squares line through the
        per-minute medians (NaN where fewer than 3 minutes are valid).

        The equilibrium gates use this rather than max-min: a range grows
        with read noise however long you wait, a fitted drift does not --
        "stable to ±2 counts over 10 min" is a statement about drift."""
        med, coverage = self.minute_medians(minutes, relative_to=relative_to)
        out = np.full(N_VIALS, np.nan)
        n_minutes = int(med.shape[0])
        if n_minutes < 3:
            return out, coverage, n_minutes
        t = np.arange(n_minutes, dtype=float)
        for v in range(N_VIALS):
            y = med[:, v]
            ok = np.isfinite(y)
            if ok.sum() < 3:
                continue
            slope = np.polyfit(t[ok], y[ok], 1)[0]
            out[v] = slope * (n_minutes - 1)
        return out, coverage, n_minutes


class Tier3Session:
    """Common state of a Tier 3 wizard session (thermistor two-point, OD
    dilution series).

    A Tier 3 session reserves the whole machine for hours. Its ``state``:

    - ``armed`` -- the session drives actuators: the sensor loop's tick
      re-sends its stir and runs its heater watchdog. Only an ``apply`` on
      an idle machine arms it.
    - ``suspended`` -- reloaded after a restart, or stopped by shutdown. It
      writes nothing; its captured points are kept.
    - ``faulted`` -- emergency stop, watchdog, or its own heater watchdog.
      Same as suspended, with the reason on record.

    Persisted after every mutation (like :class:`PumpCalSession`) under
    ``calibration/_sessions/<kind>.json``; ``abort`` deletes it and writes
    nothing to ``calibration/``."""

    kind = ""

    def __init__(self, *, operator: str, vials: list[int], stir_pwm: int) -> None:
        vials = sorted({int(v) for v in vials})
        if not vials:
            raise ValueError("'vials' must be a non-empty list")
        for v in vials:
            if not (0 <= v < N_VIALS):
                raise ValueError(f"vial {v} out of range 0..{N_VIALS - 1}")
        stir_pwm = int(stir_pwm)
        if not (0 <= stir_pwm <= 15):
            raise ValueError(f"stir_pwm must be in 0..15, got {stir_pwm}")
        self.id = uuid.uuid4().hex[:12]
        self.started_at = _iso_now()
        self.operator = str(operator or "unknown")
        self.vials = vials
        self.stir_pwm = stir_pwm
        self.state = "suspended"
        self.state_reason = "not yet applied"
        # vial -> reason; a faulted vial is excluded from the fit and is
        # never re-heated by the session.
        self.vial_faults: dict[int, str] = {}
        # vial -> the raw xr setpoint the session last commanded.
        self.commanded_raw: dict[int, int] = {}
        self.phase: Optional[str] = None

    # -- actuator targets -------------------------------------------------

    def stir_vector(self) -> list[int]:
        return [self.stir_pwm if v in self.vials else 0 for v in range(N_VIALS)]

    def estimate_temps(self, installed_c: list, raw: list) -> list[float]:
        """Best °C estimate per vial for the heater watchdog. The base
        session trusts the installed calibration; the thermistor session
        overrides this with each vial's own reference readings once it has
        them (the installed curve is what it is there to replace)."""
        return [float(x) if x is not None else float("nan") for x in installed_c]

    def floor_overrides(self) -> dict:
        """``{vial: raw floor}`` to pass with EVERY heater write this session
        makes -- including the watchdog's park. Without it a write for one
        vial would clamp another, retargeted vial back up to the installed
        curve's floor mid-measurement."""
        return {}

    # -- persistence ------------------------------------------------------

    def _base_dict(self) -> dict:
        return {
            "kind": self.kind,
            "id": self.id,
            "started_at": self.started_at,
            "operator": self.operator,
            "vials": self.vials,
            "stir_pwm": self.stir_pwm,
            "state": self.state,
            "state_reason": self.state_reason,
            "vial_faults": {str(k): v for k, v in self.vial_faults.items()},
            "commanded_raw": {str(k): int(v) for k, v in self.commanded_raw.items()},
            "phase": self.phase,
        }

    def _restore_base(self, d: dict) -> None:
        self.id = d.get("id", self.id)
        self.started_at = d.get("started_at", self.started_at)
        self.state = d.get("state", "suspended")
        self.state_reason = d.get("state_reason", "")
        self.vial_faults = {int(k): str(v) for k, v in (d.get("vial_faults") or {}).items()}
        self.commanded_raw = {int(k): int(v) for k, v in (d.get("commanded_raw") or {}).items()}
        self.phase = d.get("phase")

    def to_dict(self) -> dict:
        return self._base_dict()

    @classmethod
    def from_dict(cls, d: dict) -> "Tier3Session":
        s = cls(operator=d.get("operator", "unknown"), vials=d["vials"],
                stir_pwm=d.get("stir_pwm", 8))
        s._restore_base(d)
        return s

    def summary(self) -> dict:
        return {
            "kind": self.kind,
            "id": self.id,
            "state": self.state,
            "state_reason": self.state_reason,
            "phase": self.phase,
            "operator": self.operator,
            "started_at": self.started_at,
            "vials": self.vials,
            "vial_faults": {str(k): v for k, v in self.vial_faults.items()},
        }


# kind -> session class, for reloading a persisted session at boot. The
# concrete wizards register themselves below their definitions.
TIER3_SESSION_CLASSES: dict[str, type] = {}


def _linear_temp_fit(points: list[tuple[float, float]]) -> Optional[tuple[float, float]]:
    """Least-squares ``(slope, intercept)`` of reference °C on raw counts --
    exactly the two-point line when there are two points. None without two
    distinct raw values."""
    if len(points) < 2:
        return None
    raw = np.array([p[0] for p in points], dtype=float)
    ref = np.array([p[1] for p in points], dtype=float)
    if np.ptp(raw) <= 0:
        return None
    slope, intercept = np.polyfit(raw, ref, 1)
    return float(slope), float(intercept)


class TempCalSession(Tier3Session):
    """Two-point thermistor calibration (CALIBRATION_PROTOCOL §8.1).

    Cold point: heaters off, all vials at room temperature, ONE reference
    reading serves every vial (the free 16-way ambient point). Hot point:
    each vial commanded to ~40 °C and probed in turn. A vial whose installed
    curve is wrong -- vial 0's, the reason this exists -- lands well short
    of 40 °C; ``retarget`` re-commands it through a provisional line fitted
    from its own two reference readings. Verification at ~30 °C runs
    through the CANDIDATE fit before anything is installed.

    ``points`` holds every capture ever made, superseded ones included, so
    the envelope records what was measured and not just what was used."""

    kind = "temperature"

    def __init__(
        self,
        *,
        operator: str,
        vials: list[int],
        stir_pwm: int,
        target_hot_c: float = TEMP_CAL_DEFAULT_HOT_C,
        target_verify_c: float = TEMP_CAL_DEFAULT_VERIFY_C,
        reference_thermometer: str = "",
        bench_temp_c: Optional[float] = None,
        n_samples: int = 5,
    ) -> None:
        super().__init__(operator=operator, vials=vials, stir_pwm=stir_pwm)
        target_hot_c = float(target_hot_c)
        target_verify_c = float(target_verify_c)
        if not (25.0 <= target_hot_c <= TEMP_CAL_MAX_TARGET_C):
            raise ValueError(
                f"target_hot_c must be in 25..{TEMP_CAL_MAX_TARGET_C:.0f} °C "
                f"(MAX_SAFE_TEMP_C is {MAX_SAFE_TEMP_C:.0f}), got {target_hot_c}"
            )
        if not (22.0 < target_verify_c < target_hot_c):
            raise ValueError(
                "target_verify_c must lie between ambient and the hot point, "
                f"got {target_verify_c}"
            )
        if not (1 <= int(n_samples) <= 25):
            raise ValueError(f"n_samples must be in 1..25, got {n_samples}")
        self.target_hot_c = target_hot_c
        self.target_verify_c = target_verify_c
        self.reference_thermometer = str(reference_thermometer or "")
        self.bench_temp_c = None if bench_temp_c is None else float(bench_temp_c)
        self.n_samples = int(n_samples)
        self.points: list[dict] = []
        # vial -> {"status": "pending"|"ok"|"heater_fault", ...}
        self.p2: dict[int, dict] = {}
        # vial -> {"slope", "intercept", "xr", "floor", "at"}
        self.retargets: dict[int, dict] = {}
        # vial -> raw floor the verify phase commanded under (candidate fit)
        self.verify_floors: dict[int, int] = {}
        # vial -> clock time of the last heater command (settle timing)
        self.commanded_at: dict[int, float] = {}
        self.phase_started_at: Optional[float] = None
        self._next_point = 1

    # -- points -------------------------------------------------------------

    def add_point(self, **fields) -> dict:
        point = {"id": self._next_point, "superseded": False, **fields}
        self._next_point += 1
        self.points.append(point)
        return point

    def active_points(self, vial: int, labels=("cold", "hot")) -> list[dict]:
        return [p for p in self.points
                if p["vial"] == vial and p["label"] in labels and not p["superseded"]]

    def supersede(self, vial: int, label: str) -> None:
        for p in self.points:
            if p["vial"] == vial and p["label"] == label:
                p["superseded"] = True

    def fit_for(self, vial: int) -> Optional[tuple[float, float]]:
        return _linear_temp_fit([
            (p["raw_median"], p["reference_c"]) for p in self.active_points(vial)
        ])

    def span_c(self, vial: int) -> Optional[float]:
        refs = [p["reference_c"] for p in self.active_points(vial)]
        return (max(refs) - min(refs)) if len(refs) >= 2 else None

    # -- actuator safety ----------------------------------------------------

    def estimate_temps(self, installed_c: list, raw: list) -> list[float]:
        """Once a vial has its own two reference readings, the watchdog
        trusts those over the installed curve -- that curve is the thing on
        trial (vial 0's reads ~8 °C hot at 40 °C, which would trip the
        watchdog on a correctly retargeted vial)."""
        out = super().estimate_temps(installed_c, raw)
        for v in range(N_VIALS):
            fit = self.fit_for(v)
            if fit is None and v in self.retargets:
                # A retarget supersedes the hot point it was fitted from;
                # until a new one is captured, judge the vial by that line.
                fit = (self.retargets[v]["slope"], self.retargets[v]["intercept"])
            r = raw[v] if raw is not None and v < len(raw) else None
            if fit is not None and r is not None and math.isfinite(float(r)):
                out[v] = fit[0] * float(r) + fit[1]
        return out

    def floor_overrides(self) -> dict:
        floors = {int(v): int(r["floor"]) for v, r in self.retargets.items()}
        if self.phase == "verify":
            floors.update({int(v): int(f) for v, f in self.verify_floors.items()})
        return floors

    # -- persistence --------------------------------------------------------

    def to_dict(self) -> dict:
        d = self._base_dict()
        d.update({
            "target_hot_c": self.target_hot_c,
            "target_verify_c": self.target_verify_c,
            "reference_thermometer": self.reference_thermometer,
            "bench_temp_c": self.bench_temp_c,
            "n_samples": self.n_samples,
            "points": self.points,
            "p2": {str(k): v for k, v in self.p2.items()},
            "retargets": {str(k): v for k, v in self.retargets.items()},
            "verify_floors": {str(k): v for k, v in self.verify_floors.items()},
            "commanded_at": {str(k): v for k, v in self.commanded_at.items()},
            "phase_started_at": self.phase_started_at,
            "next_point": self._next_point,
        })
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TempCalSession":
        s = cls(
            operator=d.get("operator", "unknown"), vials=d["vials"],
            stir_pwm=d.get("stir_pwm", 8),
            target_hot_c=d.get("target_hot_c", TEMP_CAL_DEFAULT_HOT_C),
            target_verify_c=d.get("target_verify_c", TEMP_CAL_DEFAULT_VERIFY_C),
            reference_thermometer=d.get("reference_thermometer", ""),
            bench_temp_c=d.get("bench_temp_c"),
            n_samples=d.get("n_samples", 5),
        )
        s._restore_base(d)
        s.points = list(d.get("points") or [])
        s.p2 = {int(k): v for k, v in (d.get("p2") or {}).items()}
        s.retargets = {int(k): v for k, v in (d.get("retargets") or {}).items()}
        s.verify_floors = {int(k): int(v) for k, v in (d.get("verify_floors") or {}).items()}
        s.commanded_at = {int(k): float(v) for k, v in (d.get("commanded_at") or {}).items()}
        s.phase_started_at = d.get("phase_started_at")
        s._next_point = int(d.get("next_point", len(s.points) + 1))
        return s


TIER3_SESSION_CLASSES["temperature"] = TempCalSession


class OdSeriesSession(Tier3Session):
    """OD dilution series (CALIBRATION_PROTOCOL §8.2): known turbidity
    standards in the vials, true OD600 from a benchtop spectrophotometer,
    raw photodiode signal AND a dark read at every capture.

    Every capture reads all sixteen vials lit and dark, so every vial --
    measured or not -- has a dark median to convert its previous curve to
    dark-subtracted signal with. Points may give each vial its own OD (a
    rotated design) or one OD to many vials (one dilution in all of them).
    Discarded points stay in the record."""

    kind = "od_series"

    def __init__(
        self,
        *,
        operator: str,
        vials: list[int],
        stir_pwm: int,
        led_power: int = 2125,
        target_temp_c: Optional[float] = None,
        n_samples: int = 5,
        standard: str = "",
        medium: str = "",
        spectrophotometer: str = "",
        points_expected: int = OD_SERIES_MIN_LEVELS,
        profile_id: str = "",
        organism: str = "",
        new_profile: bool = False,
        base_profile: str = "",
    ) -> None:
        super().__init__(operator=operator, vials=vials, stir_pwm=stir_pwm)
        # The organism profile this series recalibrates, or creates on
        # finish (new_profile); unmeasured vials come from base_profile.
        self.profile_id = str(profile_id)
        self.organism = str(organism)
        self.new_profile = bool(new_profile)
        self.base_profile = str(base_profile)
        led_power = int(led_power)
        if not (1 <= led_power <= 2200):
            raise ValueError(f"led_power must be in 1..2200, got {led_power}")
        if target_temp_c is not None:
            target_temp_c = float(target_temp_c)
            if not (22.0 <= target_temp_c <= MAX_SAFE_TEMP_C):
                raise ValueError(
                    f"target_temp_c must be in 22..{MAX_SAFE_TEMP_C:.0f} °C, "
                    "or null to run with the heaters off"
                )
        if not (1 <= int(n_samples) <= 25):
            raise ValueError(f"n_samples must be in 1..25, got {n_samples}")
        self.led_power = led_power
        self.target_temp_c = target_temp_c
        self.n_samples = int(n_samples)
        self.standard = str(standard or "")
        self.medium = str(medium or "")
        self.spectrophotometer = str(spectrophotometer or "")
        self.points_expected = int(points_expected)
        self.points: list[dict] = []
        # One per capture: the dark medians/SDs of ALL sixteen vials.
        self.dark_captures: list[dict] = []
        self._next_point = 1
        self._next_capture = 1

    def active_points(self, vial: int) -> list[dict]:
        return [p for p in self.points if p["vial"] == vial and not p["discarded"]]

    def dark_median(self, vial: int) -> float:
        values = [c["median"][vial] for c in self.dark_captures
                  if c["median"][vial] is not None and math.isfinite(c["median"][vial])]
        return float(np.median(values)) if values else float("nan")

    def to_dict(self) -> dict:
        d = self._base_dict()
        d.update({
            "led_power": self.led_power,
            "target_temp_c": self.target_temp_c,
            "n_samples": self.n_samples,
            "standard": self.standard,
            "medium": self.medium,
            "spectrophotometer": self.spectrophotometer,
            "points_expected": self.points_expected,
            "profile_id": self.profile_id,
            "organism": self.organism,
            "new_profile": self.new_profile,
            "base_profile": self.base_profile,
            "points": self.points,
            "dark_captures": self.dark_captures,
            "next_point": self._next_point,
            "next_capture": self._next_capture,
        })
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "OdSeriesSession":
        s = cls(
            operator=d.get("operator", "unknown"), vials=d["vials"],
            stir_pwm=d.get("stir_pwm", 8), led_power=d.get("led_power", 2125),
            target_temp_c=d.get("target_temp_c"), n_samples=d.get("n_samples", 5),
            standard=d.get("standard", ""), medium=d.get("medium", ""),
            spectrophotometer=d.get("spectrophotometer", ""),
            points_expected=d.get("points_expected", OD_SERIES_MIN_LEVELS),
            profile_id=d.get("profile_id", ""),
            organism=d.get("organism", ""),
            new_profile=d.get("new_profile", False),
            base_profile=d.get("base_profile", ""),
        )
        s._restore_base(d)
        s.points = list(d.get("points") or [])
        s.dark_captures = list(d.get("dark_captures") or [])
        s._next_point = int(d.get("next_point", len(s.points) + 1))
        s._next_capture = int(d.get("next_capture", len(s.dark_captures) + 1))
        return s


    def summary(self) -> dict:
        out = super().summary()
        out.update({"profile_id": self.profile_id, "organism": self.organism,
                    "new_profile": self.new_profile})
        return out


TIER3_SESSION_CLASSES["od_series"] = OdSeriesSession


# ---------------------------------------------------------------------------
# Service façade
# ---------------------------------------------------------------------------

class CalibrationService:
    """Owns the store, the wizard sessions, and the thermal tracker. All
    methods raise ``ValueError`` (→400), :class:`CalibrationConflict` (→409)
    or :class:`QCRefusal` (→422); the API layer does the HTTP mapping."""

    def __init__(
        self,
        cal_root: Path,
        experiments_root: Path,
        manager: Any,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = CalibrationStore(cal_root, experiments_root)
        self.experiments_root = Path(experiments_root)
        self.manager = manager
        self.tracker = TempStabilityTracker(clock=clock)
        self.raw_tracker = RawTempTracker(clock=clock)
        self._clock = clock
        self._lock = threading.RLock()
        self._blank: Optional[OdBlankSession] = None
        self._pump: Optional[PumpCalSession] = None
        # At most one Tier 3 session (Session AA) machine-wide.
        self._tier3: Optional[Tier3Session] = None
        self._overtemp_streak = [0] * N_VIALS

    # -- wiring ----------------------------------------------------------

    def note_temperatures(self, temps: list) -> None:
        try:
            self.tracker.note(temps)
        except Exception:
            log.exception("temp stability tracker note failed")

    def bootstrap(self) -> list[str]:
        created = self.store.bootstrap_from_legacy()
        self._load_pump_session()
        self._load_tier3_sessions()
        return created

    def _pump_session_path(self) -> Path:
        return self.store.sessions_dir / "pump.json"

    def _load_pump_session(self) -> None:
        path = self._pump_session_path()
        if not path.is_file():
            return
        try:
            self._pump = PumpCalSession.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
            log.info(
                "resumed pump calibration session from %s (%d pumps remaining)",
                path, len(self._pump.progress()["remaining"]),
            )
        except Exception:
            log.exception("failed to resume pump calibration session %s", path)

    def _persist_pump_session(self) -> None:
        if self._pump is None:
            return
        path = self._pump_session_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._pump.to_dict(), indent=4), encoding="utf-8")
        os.replace(tmp, path)

    # -- reads -----------------------------------------------------------

    def index(
        self,
        *,
        loaded_experiment: Optional[str] = None,
        loaded_status: Optional[str] = None,
    ) -> dict:
        tier3 = self.tier3_status()
        return {
            "subsystems": self.store.current_index(),
            "staleness": self.store.staleness(
                loaded_experiment=loaded_experiment, loaded_status=loaded_status,
            ),
            "pump_session_active": self._pump is not None,
            "blank_session_active": self._blank is not None,
            "tier3_session": tier3,
            "od_profiles": self.store.od_profile_summaries(),
            "temperature_session_active": bool(tier3 and tier3["kind"] == "temperature"),
            "od_series_session_active": bool(tier3 and tier3["kind"] == "od_series"),
        }

    def subsystem(self, name: str) -> dict:
        if name not in SUBSYSTEMS:
            raise ValueError(f"unknown subsystem {name!r}; one of {SUBSYSTEMS}")
        env = self.store.get_current(name)
        if env is None:
            raise FileNotFoundError(f"no calibration installed for {name!r}")
        return env

    # -- Tier 3 session registry (Session AA) ----------------------------

    def _tier3_path(self, kind: str) -> Path:
        return self.store.sessions_dir / f"{kind}.json"

    def _persist_tier3(self) -> None:
        s = self._tier3
        if s is None:
            return
        path = self._tier3_path(s.kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, json.dumps(s.to_dict(), indent=4))

    def _load_tier3_sessions(self) -> None:
        """Reload an in-flight Tier 3 session at boot, SUSPENDED.

        Never resume driving actuators across a restart: this runs before
        resume_on_startup, the manager booted with heaters off and stir at
        0, and what the machine is doing now is unknown. An operator
        re-arms it with ``apply``, which re-checks that the machine is idle.
        The captured points survive -- that is what resumability buys."""
        for kind, cls in TIER3_SESSION_CLASSES.items():
            path = self._tier3_path(kind)
            if not path.is_file():
                continue
            try:
                session = cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                log.exception("failed to resume Tier 3 session from %s", path)
                continue
            if self._tier3 is not None:
                log.error(
                    "two Tier 3 session files; keeping '%s', ignoring %s",
                    self._tier3.kind, path,
                )
                continue
            if session.state == "armed":
                session.state = "suspended"
                session.state_reason = "server restarted — re-apply to continue"
            self._tier3 = session
            self._persist_tier3()
            log.info("resumed Tier 3 %s calibration session (suspended)", kind)

    def tier3_status(self) -> Optional[dict]:
        """Summary of the open Tier 3 session, or None. The app uses it to
        refuse anything that would drive the machine behind its back."""
        with self._lock:
            return None if self._tier3 is None else self._tier3.summary()

    def _begin_tier3(self, session: Tier3Session) -> None:
        if self._tier3 is not None:
            raise CalibrationConflict(
                f"a {self._tier3.kind} calibration session is already open "
                f"(started {self._tier3.started_at}) — resume or abort it first"
            )
        if self._pump is not None:
            raise CalibrationConflict(
                "a pump calibration session is open (calibration/_sessions/"
                f"pump.json, started {self._pump.started_at}) — finish or "
                "abort it first; a Tier 3 session reserves the whole machine"
            )
        if self._blank is not None:
            raise CalibrationConflict(
                f"an OD blank for '{self._blank.experiment}' is in progress — "
                "finish or abort it first"
            )
        self._tier3 = session
        self.raw_tracker.reset()
        self._overtemp_streak = [0] * N_VIALS
        self._persist_tier3()

    def _refuse_during_tier3(self, what: str) -> None:
        if self._tier3 is not None:
            raise CalibrationConflict(
                f"{what} cannot start while a {self._tier3.kind} calibration "
                f"session is open ({self._tier3.state}) — it reserves the "
                "whole machine; finish or abort it first"
            )

    def _require_tier3(self, kind: str) -> Tier3Session:
        s = self._tier3
        if s is None or s.kind != kind:
            raise CalibrationConflict(f"no {kind} calibration session is open")
        return s

    def _arm_tier3(self, s: Tier3Session, phase: str) -> None:
        s.state = "armed"
        s.state_reason = ""
        s.phase = phase
        self._overtemp_streak = [0] * N_VIALS
        self._persist_tier3()

    def _require_armed(self, s: Tier3Session) -> None:
        if s.state != "armed":
            raise CalibrationConflict(
                f"the {s.kind} session is {s.state}"
                + (f" ({s.state_reason})" if s.state_reason else "")
                + " — re-apply it before capturing"
            )

    def _end_tier3(self) -> None:
        """Close the session: park every heater (the session reserved the
        whole machine, so none is anyone else's), delete the session file."""
        s = self._tier3
        if s is None:
            return
        try:
            self.manager.set_temperature_raw([HEATER_OFF_SETPOINT] * N_VIALS)
        except Exception:
            log.exception("parking heaters at the end of a Tier 3 session failed")
        try:
            self._tier3_path(s.kind).unlink(missing_ok=True)
        except OSError:
            log.exception("failed to remove Tier 3 session file")
        self._tier3 = None
        self.raw_tracker.reset()

    def fault(self, reason: str) -> None:
        """Emergency stop / watchdog: stop driving actuators at once. The
        stop itself has already zeroed stir and parked the heaters; this
        keeps the tick from switching the stirrers back on 10 s later."""
        with self._lock:
            s = self._tier3
            if s is None:
                return
            s.state = "faulted"
            s.state_reason = str(reason)
            self._persist_tier3()

    def suspend(self, reason: str) -> None:
        """Clean shutdown: park the session without calling it a fault."""
        with self._lock:
            s = self._tier3
            if s is None or s.state != "armed":
                return
            s.state = "suspended"
            s.state_reason = str(reason)
            self._persist_tier3()

    def tick(self, temps_c: list, temps_raw: Optional[list]) -> list[dict]:
        """One sensor-loop tick for the open Tier 3 session, on the FAST lane.

        Records the raw thermistor counts; and, while armed, re-sends the
        session's stir (drift protection, as the supervisor does for an
        experiment) and runs the calibration heater watchdog over ALL
        sixteen vials -- a stuck heater on an excluded vial still matters.
        Returns alert payloads for the app to emit through its funnel;
        nothing here talks to the browser."""
        with self._lock:
            s = self._tier3
            if s is None:
                return []
            self.raw_tracker.note(temps_raw)
            if s.state != "armed":
                return []
            self.manager.set_stir(s.stir_vector())
            estimates = s.estimate_temps(temps_c, temps_raw or [float("nan")] * N_VIALS)
            parked: list[int] = []
            for v in range(N_VIALS):
                t = estimates[v]
                if t is None or not math.isfinite(t):
                    continue  # a dropped read neither extends nor resets a streak
                if t > CAL_HEATER_CRITICAL_C:
                    self._overtemp_streak[v] += 1
                    if (self._overtemp_streak[v] >= CAL_HEATER_DEBOUNCE_READS
                            and s.vial_faults.get(v) != "overtemp"):
                        parked.append(v)
                else:
                    self._overtemp_streak[v] = 0
            alerts: list[dict] = []
            if parked:
                alerts.extend(self._park_overtemp_locked(s, parked, estimates))
            if isinstance(s, TempCalSession):
                alerts.extend(self._temp_p2_check_locked(s))
            return alerts

    def _park_overtemp_locked(
        self, s: Tier3Session, parked: list[int], estimates: list[float],
    ) -> list[dict]:
        setpoints = [int(x) for x in np.asarray(self.manager.temp_setpoint_raw)]
        for v in parked:
            setpoints[v] = HEATER_OFF_SETPOINT
            s.commanded_raw[v] = HEATER_OFF_SETPOINT
            s.vial_faults[v] = "overtemp"
        self.manager.set_temperature_raw(
            setpoints, floor_override=s.floor_overrides(),
        )
        self._persist_tier3()
        return [
            {
                "level": "critical",
                "message": (
                    f"Calibration heater watchdog: vial {v} read "
                    f"{estimates[v]:.1f} °C (> {CAL_HEATER_CRITICAL_C:.0f} °C) "
                    f"on {CAL_HEATER_DEBOUNCE_READS} consecutive reads — "
                    "heater parked OFF and the vial excluded from this "
                    f"{s.kind} calibration"
                ),
                "category": "heater",
                "vial": v,
                "dedup_key": ("cal_heater_critical", v),
            }
            for v in parked
        ]

    # -- Tier 3: two-point thermistor calibration (§8.1) ------------------

    def _installed_temp_cal(self) -> np.ndarray:
        """The live (2, 16) slope/intercept the heaters are commanded
        through -- the manager's, which a reload keeps equal to the store's."""
        cal = getattr(self.manager, "temp_cal", None)
        if cal is None:
            env = self.store.get_current("temperature")
            if env is None:
                raise CalibrationConflict(
                    "no temperature calibration is installed — the hot point "
                    "is commanded through one"
                )
            cal = [env["data"]["slope"], env["data"]["intercept"]]
        return np.asarray(cal, dtype=float)

    def temp_start(self, body: dict) -> dict:
        """Open a thermistor calibration session. It drives nothing until
        ``temp_apply("cold")``."""
        with self._lock:
            self._installed_temp_cal()
            s = TempCalSession(
                operator=body.get("operator", "unknown"),
                vials=body.get("vials") or list(range(N_VIALS)),
                stir_pwm=body.get("stir_pwm", 8),
                target_hot_c=body.get("target_hot_c", TEMP_CAL_DEFAULT_HOT_C),
                target_verify_c=body.get("target_verify_c", TEMP_CAL_DEFAULT_VERIFY_C),
                reference_thermometer=body.get("reference_thermometer", ""),
                bench_temp_c=body.get("bench_temp_c"),
                n_samples=body.get("n_samples", 5),
            )
            self._begin_tier3(s)
            warnings_: list[str] = []
            if s.bench_temp_c is not None and s.bench_temp_c + TEMP_CAL_MIN_SPAN_C > s.target_hot_c:
                warnings_.append(
                    f"at a bench temperature of {s.bench_temp_c:.1f} °C a "
                    f"{s.target_hot_c:.0f} °C hot point cannot reach the "
                    f"{TEMP_CAL_MIN_SPAN_C:.0f} °C span §8.1 requires"
                )
            if not s.reference_thermometer:
                warnings_.append(
                    "no reference thermometer recorded — say which one, so the "
                    "calibration can be traced to it"
                )
            self._vial_map_version({"warnings": warnings_})
            return {"status": "started", "warnings": warnings_, **self.temp_session()}

    def temp_apply(self, phase: str) -> dict:
        """(Re-)command the heaters and stir for ``phase`` and ARM the session
        (the app checks the machine is idle first). Also how a suspended or
        faulted session resumes.

        - ``cold``: every heater off (xr = 4095); the P2 check runs.
        - ``hot``: each live vial to ``target_hot_c`` through the INSTALLED
          curve, or through its provisional line if it has been retargeted.
        - ``verify``: each vial with a candidate fit to ``target_verify_c``
          through that CANDIDATE, via raw setpoints -- equivalent to
          installing then verifying, without installing something that may
          fail."""
        with self._lock:
            s = self._require_tier3("temperature")
            if phase not in ("cold", "hot", "verify"):
                raise ValueError("phase must be 'cold', 'hot' or 'verify'")
            cal = self._installed_temp_cal()
            live = [v for v in s.vials if v not in s.vial_faults]
            setpoints = [HEATER_OFF_SETPOINT] * N_VIALS
            if phase == "hot":
                if not any(p["label"] == "cold" and not p["superseded"] for p in s.points):
                    raise CalibrationConflict(
                        "capture the cold point first — heaters off, every vial "
                        "at room temperature (§8.1 step 3)"
                    )
                for v in live:
                    if v in s.retargets:
                        setpoints[v] = int(s.retargets[v]["xr"])
                    else:
                        setpoints[v] = int(np.rint((s.target_hot_c - cal[1][v]) / cal[0][v]))
            elif phase == "verify":
                s.verify_floors = {}
                for v in live:
                    fit = s.fit_for(v)
                    if fit is None:
                        continue
                    slope, intercept = fit
                    setpoints[v] = int(np.rint((s.target_verify_c - intercept) / slope))
                    s.verify_floors[v] = max(
                        DEFAULT_RAW_FLOOR, int(np.rint((MAX_SAFE_TEMP_C - intercept) / slope))
                    )
                if not s.verify_floors:
                    raise CalibrationConflict(
                        "no vial has both a cold and a hot point yet — there is "
                        "no candidate fit to verify"
                    )
            now = self._clock()
            before = [int(x) for x in np.asarray(self.manager.temp_setpoint_raw)]
            s.phase = phase
            self.manager.set_temperature_raw(setpoints, floor_override=s.floor_overrides())
            self.manager.set_stir(s.stir_vector())
            sent = [int(x) for x in np.asarray(self.manager.temp_setpoint_raw)]
            for v in range(N_VIALS):
                # Settle timing restarts only where the hardware actually
                # changed (a re-apply after a restart DOES: boot parks all).
                if sent[v] != before[v]:
                    s.commanded_at[v] = now
                s.commanded_raw[v] = sent[v]
            s.phase_started_at = now
            self._arm_tier3(s, phase)
            return self.temp_session()

    def _temp_stability_locked(self, s: TempCalSession, live: list[int]) -> dict:
        """Per-vial equilibrium verdict for the current phase (see the
        TEMP_CAL_* gate constants)."""
        now = self._clock()
        out: dict = {}
        phase_age = ((now - s.phase_started_at) / 60.0
                     if s.phase_started_at is not None else 0.0)
        if s.phase == "cold":
            window = TEMP_CAL_COLD_WINDOW_MIN
            gate, coverage, _n = self.raw_tracker.drift(window, relative_to=live or None)
            absolute, _c, _n = self.raw_tracker.drift(window)
        else:
            window = TEMP_CAL_HOT_WINDOW_MIN
            gate, coverage, _n = self.raw_tracker.drift(window)
            absolute = gate
        for v in range(N_VIALS):
            if s.phase == "cold":
                waited = phase_age
            else:
                at = s.commanded_at.get(v, s.phase_started_at)
                waited = (now - at) / 60.0 if at is not None else 0.0
            drift = gate[v]
            reason = None
            if waited < window:
                reason = f"{window - waited:.1f} min until a full {window:.0f}-min window"
            elif coverage < window - 0.5:
                reason = "not enough readings in the window yet"
            elif not math.isfinite(drift):
                reason = "no valid readings"
            elif abs(drift) > TEMP_CAL_STABLE_DRIFT_COUNTS:
                reason = (f"drifting {drift:+.1f} counts over {window:.0f} min "
                          f"(limit ±{TEMP_CAL_STABLE_DRIFT_COUNTS:.0f})")
            out[v] = {
                "settled": reason is None,
                "reason": reason,
                "window_minutes": window,
                "drift_counts": None if not math.isfinite(drift) else round(float(drift), 2),
                "absolute_drift_counts": (None if not math.isfinite(absolute[v])
                                          else round(float(absolute[v]), 2)),
                "pack_relative": s.phase == "cold",
            }
        return out

    def temp_session(self) -> dict:
        """Progress for the wizard: per-vial live counts, equilibrium, P2,
        points, span and provisional fit. Polled by the GUI."""
        with self._lock:
            s = self._tier3
            if not isinstance(s, TempCalSession):
                return {"active": False}
            live = [v for v in s.vials if v not in s.vial_faults]
            latest = self.raw_tracker.latest()
            try:
                cal = self._installed_temp_cal()
            except CalibrationConflict:
                cal = None
            installed_c = [
                (float(cal[0][v] * latest[v] + cal[1][v])
                 if cal is not None and math.isfinite(latest[v]) else None)
                for v in range(N_VIALS)
            ]
            pack = [installed_c[v] for v in live if installed_c[v] is not None]
            pack_median = float(np.median(pack)) if pack else None
            stability = self._temp_stability_locked(s, live)
            per_vial: dict = {}
            for v in range(N_VIALS):
                fit = s.fit_for(v)
                span = s.span_c(v)
                hot = s.active_points(v, ("hot",))
                ref_hot = hot[-1]["reference_c"] if hot else None
                verify = s.active_points(v, ("verify",))
                verification = None
                if verify and fit is not None:
                    p = verify[-1]
                    verification = round(fit[0] * p["raw_median"] + fit[1] - p["reference_c"], 3)
                live_v = v in live
                advisory = None
                if (s.phase == "cold" and live_v and pack_median is not None
                        and installed_c[v] is not None
                        and installed_c[v] - pack_median > TEMP_CAL_PROBE_ADVISORY_C):
                    advisory = (
                        f"reads {installed_c[v] - pack_median:+.1f} °C vs the pack "
                        "on the installed curve at ambient — probe this vial: a "
                        "heater stuck on, or a bad installed curve (vial 0's is)"
                    )
                per_vial[str(v)] = {
                    "in_session": v in s.vials,
                    "fault": s.vial_faults.get(v),
                    "latest_raw": None if not math.isfinite(latest[v]) else round(latest[v], 2),
                    "installed_c": None if installed_c[v] is None else round(installed_c[v], 2),
                    "provisional_c": (None if fit is None or not math.isfinite(latest[v])
                                      else round(fit[0] * latest[v] + fit[1], 2)),
                    "stability": stability[v],
                    "p2": s.p2.get(v),
                    "advisory": advisory,
                    "commanded_raw": s.commanded_raw.get(v),
                    "provisional_fit": (None if fit is None
                                        else {"slope": fit[0], "intercept": fit[1]}),
                    "span_c": None if span is None else round(span, 2),
                    "retargeted": s.retargets.get(v),
                    "retarget_suggested": bool(
                        live_v and fit is not None and (
                            (span is not None and span < TEMP_CAL_MIN_SPAN_C)
                            or (ref_hot is not None and abs(ref_hot - s.target_hot_c)
                                > TEMP_CAL_RETARGET_TOL_C)
                        )
                    ),
                    "verification_delta_c": verification,
                    "points": [p for p in s.points if p["vial"] == v],
                }
            cold_blockers = [
                v for v in live if not stability[v]["settled"]
            ] if s.phase == "cold" else []
            return {
                "active": True,
                **s.summary(),
                "config": {
                    "target_hot_c": s.target_hot_c,
                    "target_verify_c": s.target_verify_c,
                    "reference_thermometer": s.reference_thermometer,
                    "bench_temp_c": s.bench_temp_c,
                    "n_samples": s.n_samples,
                    "stir_pwm": s.stir_pwm,
                },
                "phase_age_minutes": (
                    None if s.phase_started_at is None
                    else round((self._clock() - s.phase_started_at) / 60.0, 2)
                ),
                "cold_ready": s.phase == "cold" and not cold_blockers,
                "cold_blockers": cold_blockers,
                "per_vial": per_vial,
            }

    def _temp_p2_check_locked(self, s: "TempCalSession") -> list[dict]:
        """Prerequisite P2, enforced rather than trusted: during the cold
        phase every heater is commanded off, so a vial that keeps HEATING
        has a heater that is not off. Latched per vial; the vial is excluded
        and the operator alerted (it is a hardware fault)."""
        if s.phase != "cold" or s.phase_started_at is None:
            return []
        if (self._clock() - s.phase_started_at) / 60.0 < TEMP_CAL_COLD_WINDOW_MIN:
            for v in s.vials:
                s.p2.setdefault(v, {"status": "pending"})
            return []
        live = [v for v in s.vials if v not in s.vial_faults]
        absolute, _c, _n = self.raw_tracker.drift(TEMP_CAL_COLD_WINDOW_MIN)
        relative, _c, _n = self.raw_tracker.drift(TEMP_CAL_COLD_WINDOW_MIN,
                                                  relative_to=live or None)
        alerts: list[dict] = []
        changed = False
        for v in s.vials:
            if v in s.vial_faults:
                continue
            a, r = float(absolute[v]), float(relative[v])
            if not (math.isfinite(a) and math.isfinite(r)):
                continue
            detail = {"abs_drift_counts": round(a, 2), "rel_drift_counts": round(r, 2),
                      "at": _iso_now()}
            if a < -TEMP_CAL_P2_HEATING_COUNTS and r < -TEMP_CAL_P2_HEATING_COUNTS:
                s.p2[v] = {"status": "heater_fault", **detail}
                s.vial_faults[v] = "heater not off (P2): heating while commanded xr=4095"
                s.supersede(v, "cold")
                changed = True
                alerts.append({
                    "level": "critical",
                    "message": (
                        f"Vial {v} is HEATING with its heater commanded off "
                        f"(raw counts {a:+.1f} over {TEMP_CAL_COLD_WINDOW_MIN:.0f} "
                        f"min, {r:+.1f} relative to the pack) — prerequisite P2 "
                        "fails for this sleeve. Excluded from the calibration; "
                        "treat it as a hardware fault and run no temperature-"
                        "controlled experiment on it"
                    ),
                    "category": "heater",
                    "vial": v,
                    "dedup_key": ("cal_p2_heater_fault", v),
                })
            elif s.p2.get(v, {}).get("status") != "ok":
                s.p2[v] = {"status": "ok", **detail}
                changed = True
        if changed:
            self._persist_tier3()
        return alerts

    def temp_point(
        self,
        label: str,
        reference_c,
        *,
        vials: Optional[list] = None,
        reference_vial: Optional[int] = None,
        settle_check: bool = True,
    ) -> dict:
        """Capture raw thermistor counts against one reference reading.

        ``cold``: every live vial at once, ``reference_vial`` naming where
        the probe sits. ``hot`` / ``verify``: the listed vial(s), as the
        probe moves. Refused until the vials have equilibrated unless
        ``settle_check`` is False, which the point records."""
        with self._lock:
            s = self._require_tier3("temperature")
            self._require_armed(s)
            if label not in ("cold", "hot", "verify"):
                raise ValueError("label must be 'cold', 'hot' or 'verify'")
            if label != s.phase:
                raise CalibrationConflict(
                    f"the session is in the {s.phase} phase — apply '{label}' first"
                )
            if (not isinstance(reference_c, (int, float)) or isinstance(reference_c, bool)
                    or not math.isfinite(float(reference_c))
                    or not (5.0 <= float(reference_c) <= 60.0)):
                raise ValueError("'reference_c' must be the reference thermometer's reading in °C")
            ref = float(reference_c)
            live = [v for v in s.vials if v not in s.vial_faults]
            if label == "cold":
                targets = live
                if reference_vial is None or int(reference_vial) not in live:
                    raise ValueError(
                        "'reference_vial' must name the live vial the probe sits "
                        "in — the reference is read in a vial, not in air"
                    )
            else:
                targets = sorted({int(v) for v in (vials or [])})
                if not targets:
                    raise ValueError(f"name the vial(s) the probe is in for the {label} point")
                bad = [v for v in targets if v not in live]
                if bad:
                    raise ValueError(f"vials {bad} are not live in this session")
            stability = self._temp_stability_locked(s, live)
            unsettled = [v for v in targets if not stability[v]["settled"]]
            if unsettled and settle_check:
                raise CalibrationConflict(
                    f"vials {unsettled} have not equilibrated: "
                    + "; ".join(f"{v}: {stability[v]['reason']}" for v in unsettled)
                    + " — wait, or capture with settle_check=false (recorded)"
                )
            stats = self.manager.collect_temp_raw(s.n_samples)
            captured: list[dict] = []
            skipped: list[int] = []
            when = _iso_now()
            for v in targets:
                med = stats["median"][v]
                if med is None or not math.isfinite(float(med)):
                    skipped.append(v)
                    continue
                if label in ("cold", "hot"):
                    s.supersede(v, label)
                captured.append(s.add_point(
                    label=label, vial=v, reference_c=ref,
                    raw_median=float(med), raw_sd=float(stats["sd"][v]),
                    n_valid=int(stats["n_valid"][v]), captured_at=when,
                    settle_checked=v not in unsettled,
                    commanded_raw=s.commanded_raw.get(v),
                    reference_vial=(int(reference_vial) if label == "cold" else v),
                    p2=(dict(s.p2.get(v) or {}) if label == "cold" else None),
                ))
            if not captured:
                raise ValueError("no vial returned a valid reading — check the bus and repeat")
            self._persist_tier3()
            return {
                "label": label,
                "captured": captured,
                "skipped": skipped,
                "session": self.temp_session(),
            }

    def temp_retarget(self, vials: list) -> dict:
        """Re-command vials whose hot point landed off target through their
        own provisional line (cold + hot reference readings). The safety
        floor for these vials comes from that line too -- the installed
        curve's floor would stop vial 0 short of 40 °C, which is exactly the
        miscalibration being corrected. The heater watchdog judges them by
        the same line; the superseded hot point stays in the record."""
        with self._lock:
            s = self._require_tier3("temperature")
            self._require_armed(s)
            if s.phase != "hot":
                raise CalibrationConflict("retargeting is part of the hot phase")
            targets = sorted({int(v) for v in (vials or [])})
            live = [v for v in s.vials if v not in s.vial_faults]
            if not targets or any(v not in live for v in targets):
                raise ValueError(f"'vials' must be live session vials, got {vials}")
            out: dict = {}
            for v in targets:
                fit = s.fit_for(v)
                if fit is None:
                    raise ValueError(
                        f"vial {v} needs its cold point and one hot reading "
                        "before it can be retargeted"
                    )
                slope, intercept = fit
                if not slope < 0:
                    raise ValueError(
                        f"vial {v}: provisional slope {slope:+.5f} is not negative "
                        "— a reading is wrong (or the probe was in another "
                        "vial); re-capture before retargeting"
                    )
                hot = s.active_points(v, ("hot",))
                s.retargets[v] = {
                    "slope": slope,
                    "intercept": intercept,
                    "xr": int(np.rint((s.target_hot_c - intercept) / slope)),
                    "floor": max(DEFAULT_RAW_FLOOR,
                                 int(np.rint((MAX_SAFE_TEMP_C - intercept) / slope))),
                    "from_hot_reference_c": hot[-1]["reference_c"] if hot else None,
                    "at": _iso_now(),
                }
                s.supersede(v, "hot")
                out[str(v)] = s.retargets[v]
            setpoints = [int(x) for x in np.asarray(self.manager.temp_setpoint_raw)]
            for v in targets:
                setpoints[v] = int(s.retargets[v]["xr"])
            self.manager.set_temperature_raw(setpoints, floor_override=s.floor_overrides())
            sent = [int(x) for x in np.asarray(self.manager.temp_setpoint_raw)]
            now = self._clock()
            for v in targets:
                s.commanded_raw[v] = sent[v]
                s.commanded_at[v] = now
            self._persist_tier3()
            return {"retargeted": out, "session": self.temp_session()}

    def _temp_fit_and_qc_locked(
        self, s: "TempCalSession", exclude_vials, version: str,
    ) -> tuple[np.ndarray, np.ndarray, dict, dict, Optional[str]]:
        prev_env = self.store.get_current("temperature")
        if prev_env is None:
            raise CalibrationConflict(
                "no previous temperature calibration to carry the unmeasured "
                "vials forward from"
            )
        prev_slope = np.asarray(prev_env["data"]["slope"], dtype=float)
        prev_int = np.asarray(prev_env["data"]["intercept"], dtype=float)
        per_vial_source = per_vial_sources(prev_env)
        per_vial_version = list(
            (prev_env.get("fit") or {}).get("per_vial_version")
            or [prev_env.get("version")] * N_VIALS
        )
        exclude = {int(v) for v in (exclude_vials or [])}
        failures: list[str] = []
        warnings_: list[str] = []
        new_slope, new_int = prev_slope.copy(), prev_int.copy()
        measured: list[int] = []
        for v in s.vials:
            if v in s.vial_faults:
                warnings_.append(
                    f"vial {v} excluded ({s.vial_faults[v]}) — keeps its previous constants"
                )
                continue
            if v in exclude:
                warnings_.append(
                    f"vial {v} excluded by the operator — keeps its previous constants"
                )
                continue
            fit = s.fit_for(v)
            if fit is None:
                failures.append(
                    f"vial {v}: needs a cold and a hot reference point (or exclude it)"
                )
                continue
            measured.append(v)
            new_slope[v], new_int[v] = fit
            per_vial_source[v] = TEMP_CAL_SOURCE
            per_vial_version[v] = version
        if not measured and not failures:
            raise ValueError("no vial was measured — nothing to install")

        for v in measured:
            span = s.span_c(v)
            if span is None or span < TEMP_CAL_MIN_SPAN_C:
                failures.append(
                    f"vial {v}: reference span {span or 0:.1f} °C < "
                    f"{TEMP_CAL_MIN_SPAN_C:.0f} °C — the slope is badly "
                    "conditioned; retarget its hot point"
                )
            if not new_slope[v] < 0:
                failures.append(
                    f"vial {v}: fitted slope {new_slope[v]:+.5f} is not negative "
                    "— the thermistor reads backwards or a point is wrong"
                )

        # Pack comparison, leave-one-out over the MERGED sixteen so an
        # outlier cannot hide by moving the median it is compared with.
        flagged: dict = {}
        for v in measured:
            others = [u for u in range(N_VIALS) if u != v]
            med_s = float(np.median(new_slope[others]))
            med_i = float(np.median(new_int[others]))
            reasons = []
            if med_s != 0 and abs(new_slope[v] / med_s - 1.0) > TEMP_CAL_SLOPE_TOL_FRACTION:
                reasons.append(
                    f"slope {new_slope[v]:.5f} is {100 * (new_slope[v] / med_s - 1):+.1f}% "
                    f"from the pack median {med_s:.5f}"
                )
            if abs(new_int[v] - med_i) > TEMP_CAL_INTERCEPT_TOL_C:
                reasons.append(
                    f"intercept {new_int[v]:.3f} is {new_int[v] - med_i:+.2f} °C "
                    f"from the pack median {med_i:.3f}"
                )
            if reasons:
                flagged[str(v)] = "; ".join(reasons)
                warnings_.append(
                    f"vial {v} fits outside the pack ({flagged[str(v)]}) — "
                    "flagged in the calibration, not refused; its verification "
                    "point decides whether the sleeve is genuinely different"
                )

        verified: dict = {}
        for v in measured:
            pts = s.active_points(v, ("verify",))
            if pts:
                p = pts[-1]
                verified[str(v)] = round(
                    float(new_slope[v] * p["raw_median"] + new_int[v] - p["reference_c"]), 3
                )
        required = min(TEMP_CAL_VERIFY_MIN_VIALS, len(measured))
        passed_vials = sorted(int(v) for v, d in verified.items()
                              if abs(d) <= TEMP_CAL_VERIFY_TOL_C)
        for v, d in verified.items():
            if abs(d) > TEMP_CAL_VERIFY_TOL_C:
                failures.append(
                    f"vial {v}: at the verification point the new fit reads "
                    f"{d:+.2f} °C off the reference (> ±{TEMP_CAL_VERIFY_TOL_C} °C) "
                    "— the response is not linear over the range; a third point "
                    "is needed"
                )
        if len(verified) < required:
            failures.append(
                f"independent verification incomplete: {len(verified)} of the "
                f"{required} required vial(s) checked at ~{s.target_verify_c:.0f} °C "
                "(§8.1) — a two-point fit reproduces its own two points "
                "trivially; only the middle tests it"
            )
        carried_legacy = [v for v in range(N_VIALS)
                          if v not in measured and _is_legacy_source(per_vial_source[v])]
        if carried_legacy:
            warnings_.append(
                f"vials {carried_legacy} keep their 2016 constants (not measured "
                "in this session)"
            )

        qc = {
            "passed": not failures,
            "warnings": warnings_,
            "failures": failures,
            "overridden_by": None,
            "verification": {
                "target_c": s.target_verify_c,
                "tolerance_c": TEMP_CAL_VERIFY_TOL_C,
                "required": required,
                "verified": verified,
                "passed_vials": passed_vials,
            },
            "excluded_vials": sorted(exclude),
        }
        vial_map_version = self._vial_map_version(qc)

        review: dict = {}
        for v in range(N_VIALS):
            xr37_prev = int(np.rint((37.0 - prev_int[v]) / prev_slope[v]))
            xr37_new = int(np.rint((37.0 - new_int[v]) / new_slope[v])) if new_slope[v] else None
            entry = {
                "measured": v in measured,
                "fault": s.vial_faults.get(v),
                "slope": float(new_slope[v]),
                "intercept": float(new_int[v]),
                "previous_slope": float(prev_slope[v]),
                "previous_intercept": float(prev_int[v]),
                "delta_slope_pct": (round(100.0 * (new_slope[v] / prev_slope[v] - 1.0), 2)
                                    if prev_slope[v] else None),
                "delta_intercept_c": round(float(new_int[v] - prev_int[v]), 3),
                "xr_37_previous": xr37_prev,
                "xr_37_new": xr37_new,
                "span_c": None if s.span_c(v) is None else round(s.span_c(v), 2),
                "flag": flagged.get(str(v)),
                "verification_delta_c": verified.get(str(v)),
                "source": per_vial_source[v],
            }
            if v in measured:
                # THE number the audit could only guess at: what the old
                # curve actually delivered when the UI said 37 °C.
                entry["previous_delivered_at_37_c"] = round(
                    float(new_slope[v] * xr37_prev + new_int[v]), 2
                )
            review[str(v)] = entry
        fit = {
            "measured_vials": measured,
            "per_vial_source": per_vial_source,
            "per_vial_version": per_vial_version,
            "flagged_vials": flagged,
            "review": review,
        }
        return new_slope, new_int, fit, qc, vial_map_version

    def temp_fit(self, exclude_vials: Optional[list] = None) -> dict:
        """The review screen: fit + QC + per-vial review, written nowhere."""
        with self._lock:
            s = self._require_tier3("temperature")
            _slope, _int, fit, qc, _vm = self._temp_fit_and_qc_locked(
                s, exclude_vials, self.store.fresh_version("temperature"),
            )
            return {"fit": fit, "qc": qc}

    def temp_finish(
        self,
        *,
        exclude_vials: Optional[list] = None,
        override_reason: Optional[str] = None,
        operator: Optional[str] = None,
    ) -> dict:
        """Fit, QC, install a new temperature version, close the session.
        Refuses a failing fit unless ``override_reason`` is recorded. The
        caller (app.py) makes the new curve live."""
        with self._lock:
            s = self._require_tier3("temperature")
            version = self.store.fresh_version("temperature")
            slope, intercept, fit, qc, vial_map_version = self._temp_fit_and_qc_locked(
                s, exclude_vials, version,
            )
            if not qc["passed"]:
                if not override_reason:
                    raise QCRefusal(
                        "thermistor calibration failed its acceptance criteria "
                        "(§8.1) — fix and re-capture, exclude the failing vials, "
                        "or finish with an explicit override_reason",
                        qc,
                    )
                qc["overridden_by"] = str(override_reason)
            envelope = make_envelope(
                "temperature",
                operator=operator or s.operator,
                source=TEMP_CAL_SOURCE,
                conditions={
                    "fluid": "water, working volume (§8.1)",
                    "stir_pwm": s.stir_pwm,
                    "reference_thermometer": s.reference_thermometer or None,
                    "bench_temp_c": s.bench_temp_c,
                    "target_hot_c": s.target_hot_c,
                    "target_verify_c": s.target_verify_c,
                    "n_samples": s.n_samples,
                    "vial_map_version": vial_map_version,
                    "settle_overridden_points": [
                        p["id"] for p in s.points if not p.get("settle_checked", True)
                    ],
                },
                data={
                    "slope": [float(x) for x in slope],
                    "intercept": [float(x) for x in intercept],
                    "points": s.points,
                    "p2": {str(k): v for k, v in s.p2.items()},
                    "retargets": {str(k): v for k, v in s.retargets.items()},
                },
                fit=fit,
                qc=qc,
                version=version,
            )
            installed = self.store.save_version("temperature", envelope)
            self._end_tier3()
            return {
                "status": "installed",
                "version": installed,
                "measured_vials": fit["measured_vials"],
                "review": fit["review"],
                "qc": qc,
            }

    def temp_abort(self) -> dict:
        with self._lock:
            self._require_tier3("temperature")
            self._end_tier3()
            return {"status": "aborted"}

    # -- Tier 3: OD dilution series (§8.2) ---------------------------------

    def series_start(self, body: dict) -> dict:
        """Open an OD dilution-series session. It drives nothing until
        ``series_apply``."""
        with self._lock:
            target = self._series_target(body)
            s = OdSeriesSession(
                operator=body.get("operator", "unknown"),
                vials=body.get("vials") or list(range(N_VIALS)),
                stir_pwm=body.get("stir_pwm", 8),
                led_power=body.get("led_power", 2125),
                target_temp_c=body.get("target_temp_c"),
                n_samples=body.get("n_samples", 5),
                standard=body.get("standard", ""),
                medium=body.get("medium", ""),
                spectrophotometer=body.get("spectrophotometer", ""),
                points_expected=body.get("points_expected", OD_SERIES_MIN_LEVELS),
                **target,
            )
            if s.target_temp_c is not None:
                self._installed_temp_cal()
            self._begin_tier3(s)
            warnings_: list[str] = []
            if not s.standard:
                warnings_.append(
                    "record the turbidity standard (killed cells, heat-fixed "
                    "cells or beads) — it has to hold still for ~2 hours"
                )
            if not s.spectrophotometer:
                warnings_.append("record which spectrophotometer gave the true OD600")
            if s.target_temp_c is not None:
                warnings_.append(
                    "the heaters are driven during this series — prerequisite "
                    "P2 (heaters really turn off) should have passed first; "
                    "the thermistor wizard's cold phase tests it"
                )
            self._vial_map_version({"warnings": warnings_})
            return {"status": "started", "warnings": warnings_, **self.series_session()}

    def _series_target(self, body: dict) -> dict:
        """Which organism profile a series is for. Exactly one of
        ``profile`` (recalibrate an existing organism) or ``new_organism``
        (introduce one -- the ONLY way an organism comes into existence).
        The default slot is not an organism and is never recalibrated."""
        existing = body.get("profile")
        new_name = body.get("new_organism")
        if bool(existing) == bool(new_name):
            raise ValueError(
                "name exactly one of 'profile' (recalibrate an existing "
                "organism) or 'new_organism' (introduce a new one)"
            )
        profiles = self.store.od_profiles()
        default = self.store.od_default_profile()
        if default is None:
            raise CalibrationConflict(
                "no OD calibration is installed — vials a series does not "
                "measure are carried forward from the default curve"
            )
        if existing:
            existing = str(existing)
            if existing not in profiles:
                raise ValueError(
                    f"unknown OD profile {existing!r}; known: {sorted(profiles)}"
                )
            if existing == default:
                raise ValueError(
                    f"{profiles[existing]['organism']!r} is the machine's default "
                    "curve, not an organism — calibrate a named organism instead "
                    "(new_organism)"
                )
            return {"profile_id": existing, "organism": profiles[existing]["organism"],
                    "new_profile": False, "base_profile": existing}
        organism = " ".join(str(new_name).split())
        if not (2 <= len(organism) <= OD_ORGANISM_NAME_MAX):
            raise ValueError(
                f"new_organism must be 2..{OD_ORGANISM_NAME_MAX} characters"
            )
        profile_id = od_profile_slug(organism)
        clash = [pid for pid, e in profiles.items()
                 if pid == profile_id or e["organism"].casefold() == organism.casefold()]
        if not profile_id or profile_id in RESERVED_OD_PROFILES or clash:
            raise ValueError(
                f"an OD profile for {organism!r} already exists"
                + (f" ({clash[0]!r}) — recalibrate it instead" if clash else "")
            )
        return {"profile_id": profile_id, "organism": organism,
                "new_profile": True, "base_profile": default}

    def series_apply(self) -> dict:
        """(Re-)command stir, and the heaters (to the series' temperature
        through the installed curve, or off), and ARM the session."""
        with self._lock:
            s = self._require_tier3("od_series")
            setpoints = [HEATER_OFF_SETPOINT] * N_VIALS
            if s.target_temp_c is not None:
                cal = self._installed_temp_cal()
                for v in s.vials:
                    if v not in s.vial_faults:
                        setpoints[v] = int(np.rint((s.target_temp_c - cal[1][v]) / cal[0][v]))
            self.manager.set_temperature_raw(setpoints, floor_override=s.floor_overrides())
            self.manager.set_stir(s.stir_vector())
            sent = [int(x) for x in np.asarray(self.manager.temp_setpoint_raw)]
            s.commanded_raw = {v: sent[v] for v in range(N_VIALS)}
            self._arm_tier3(s, "points")
            return self.series_session()

    def series_session(self, preview: bool = False) -> dict:
        """Progress: per-vial points, OD levels and dark medians; with
        ``preview`` a provisional fit for every vial that has enough levels
        (so the operator sees early whether the top of the range is
        constraining the curve)."""
        with self._lock:
            s = self._tier3
            if not isinstance(s, OdSeriesSession):
                return {"active": False}
            live = [v for v in s.vials if v not in s.vial_faults]
            thermal = None
            if s.target_temp_c is not None and live:
                thermal = self.tracker.settled(
                    live, {v: s.target_temp_c for v in live},
                    tol_c=OD_SERIES_SETTLE_TOL_C, minutes=OD_SERIES_SETTLE_MINUTES,
                )
            per_vial: dict = {}
            for v in range(N_VIALS):
                pts = s.active_points(v)
                levels = sorted({p["true_od600"] for p in pts})
                entry = {
                    "in_session": v in s.vials,
                    "fault": s.vial_faults.get(v),
                    "n_points": len(pts),
                    "n_levels": len(levels),
                    "od_range": [levels[0], levels[-1]] if levels else None,
                    "dark_median": _finite_or_none(s.dark_median(v)),
                    "points": [p for p in s.points if p["vial"] == v],
                }
                if preview and len(levels) >= od_curve_fit.MIN_OD_LEVELS:
                    try:
                        f = od_curve_fit.fit_logistic4(
                            [p["true_od600"] for p in pts],
                            [self._series_signal(p) for p in pts],
                        )
                        entry["preview"] = {k: f[k] for k in (
                            "a", "b", "c", "d", "r2", "rmse", "b_at_bound", "residuals",
                        )}
                    except ValueError as exc:
                        entry["preview_error"] = str(exc)
                per_vial[str(v)] = entry
            return {
                "active": True,
                **s.summary(),
                "config": {
                    "profile_id": s.profile_id,
                    "organism": s.organism,
                    "new_profile": s.new_profile,
                    "led_power": s.led_power,
                    "stir_pwm": s.stir_pwm,
                    "target_temp_c": s.target_temp_c,
                    "n_samples": s.n_samples,
                    "standard": s.standard,
                    "medium": s.medium,
                    "spectrophotometer": s.spectrophotometer,
                    "points_expected": s.points_expected,
                },
                "thermal": thermal,
                "n_captures": len(s.dark_captures),
                "per_vial": per_vial,
            }

    def series_point(
        self,
        *,
        entries: Optional[list] = None,
        spec_reading=None,
        dilution_factor=1,
        vials: Optional[list] = None,
        settle_check: bool = True,
    ) -> dict:
        """Capture one loading: a lit read and a dark read of all sixteen
        vials, recorded as a point for each listed vial at its true OD600
        (``spec_reading x dilution_factor``: benchtop spectrophotometers are
        only linear to ~OD 1, so dense standards are read diluted)."""
        with self._lock:
            s = self._require_tier3("od_series")
            self._require_armed(s)
            live = [v for v in s.vials if v not in s.vial_faults]
            if entries is None:
                entries = [{"vial": v, "spec_reading": spec_reading,
                            "dilution_factor": dilution_factor} for v in (vials or [])]
            if not isinstance(entries, list) or not entries:
                raise ValueError(
                    "give 'entries' [{vial, spec_reading, dilution_factor}] or "
                    "'spec_reading' with 'vials'"
                )
            wanted: list[tuple[int, float, float]] = []
            for e in entries:
                if not isinstance(e, dict):
                    raise ValueError("each entry must be an object")
                v = int(e.get("vial", -1))
                spec = e.get("spec_reading")
                factor = e.get("dilution_factor", 1)
                if v not in live:
                    raise ValueError(f"vial {v} is not live in this session")
                for label, x, lo, hi in (("spec_reading", spec, 0.0, 5.0),
                                         ("dilution_factor", factor, 1.0, 1000.0)):
                    if (not isinstance(x, (int, float)) or isinstance(x, bool)
                            or not math.isfinite(float(x)) or not (lo <= float(x) <= hi)):
                        raise ValueError(f"vial {v}: '{label}' must be a number in {lo:g}..{hi:g}")
                wanted.append((v, float(spec), float(factor)))
            if len({v for v, _s, _f in wanted}) != len(wanted):
                raise ValueError("a vial is listed twice in one capture")
            unsettled: list[int] = []
            if s.target_temp_c is not None:
                thermal = self.tracker.settled(
                    [v for v, _s, _f in wanted],
                    {v: s.target_temp_c for v, _s, _f in wanted},
                    tol_c=OD_SERIES_SETTLE_TOL_C, minutes=OD_SERIES_SETTLE_MINUTES,
                )
                unsettled = [int(k) for k, d in thermal["per_vial"].items() if not d["settled"]]
                if unsettled and settle_check:
                    raise CalibrationConflict(
                        f"vials {unsettled} have not held ±{OD_SERIES_SETTLE_TOL_C} °C "
                        f"of {s.target_temp_c} °C for {OD_SERIES_SETTLE_MINUTES:.0f} "
                        "min since loading — wait, or capture with "
                        "settle_check=false (recorded)"
                    )
            lit = self.manager.collect_od_raw(s.led_power, n_samples=s.n_samples)
            dark = self.manager.collect_od_raw(0, n_samples=s.n_samples)
            capture = s._next_capture
            s._next_capture += 1
            when = _iso_now()
            s.dark_captures.append({
                "capture": capture, "at": when,
                "median": [_finite_or_none(x) for x in dark["median"]],
                "sd": [_finite_or_none(x) for x in dark["sd"]],
            })
            temps = self.tracker.latest()
            captured: list[dict] = []
            rejected: list[dict] = []
            for v, spec, factor in wanted:
                lm, dm = lit["median"][v], dark["median"][v]
                if not (lm is not None and dm is not None
                        and math.isfinite(lm) and math.isfinite(dm)):
                    rejected.append({"vial": v, "reason": "dropped read — repeat"})
                    continue
                # The fit signal is the RAW LED-on reading -- the signal every
                # curve that has worked on this rig was fitted on (the 2016
                # constants, the live loop's default). Measured on the rig
                # 2026-10-11: the photodiode front end inverts, so LED-off
                # (``we0``) reads ~64 500, near the ADC rail, ABOVE every
                # LED-on reading; "LED-on minus LED-off" is negative on every
                # vial and rejected them all. The LED-off read is kept on the
                # point as a diagnostic only.
                signal = float(lm)
                if signal <= 0:
                    rejected.append({
                        "vial": v,
                        "reason": f"LED-on reading {lm:.0f} is not positive",
                    })
                    continue
                flags = []
                if lit["sd"][v] > OD_SERIES_SD_WARN:
                    flags.append(
                        f"signal SD {lit['sd'][v]:.0f} counts > {OD_SERIES_SD_WARN:.0f} "
                        "(provisional) — bubbles, a wobbling bar, or a poorly seated vial"
                    )
                point = {
                    "id": s._next_point, "capture": capture, "vial": v,
                    "spec_reading": spec, "dilution_factor": factor,
                    "true_od600": round(spec * factor, 6),
                    "lit_median": float(lm), "lit_sd": float(lit["sd"][v]),
                    "dark_median": float(dm), "dark_sd": float(dark["sd"][v]),
                    "signal": signal, "n_valid": int(lit["n_valid"][v]),
                    "temp_c": _finite_or_none(temps[v]) if v < len(temps) else None,
                    "captured_at": when,
                    "settle_checked": v not in unsettled,
                    "flags": flags,
                    "discarded": False,
                }
                s._next_point += 1
                s.points.append(point)
                captured.append(point)
            self._persist_tier3()
            if not captured:
                raise ValueError(f"no point recorded: {rejected}")
            return {"captured": captured, "rejected": rejected,
                    "session": self.series_session()}

    @staticmethod
    def _series_signal(point: dict) -> float:
        """A captured point's fit signal: its raw LED-on median. Points taken
        before 2026-10-11 stored ``signal = lit - dark``; re-deriving it from
        ``lit_median`` keeps one session consistent across that change."""
        return float(point["lit_median"])

    def series_discard(self, point_id) -> dict:
        with self._lock:
            s = self._require_tier3("od_series")
            for p in s.points:
                if p["id"] == int(point_id):
                    p["discarded"] = True
                    p["discarded_at"] = _iso_now()
                    self._persist_tier3()
                    return self.series_session()
            raise ValueError(f"no point {point_id} in this session")

    def _series_fit_and_qc_locked(
        self, s: "OdSeriesSession", exclude_vials, version: str,
    ) -> tuple[np.ndarray, dict, dict, Optional[str]]:
        # Unmeasured vials carry forward from the profile itself (a
        # recalibration) or from the default slot (a new organism); which
        # profile measured each vial is recorded, so a new organism's
        # carried-in 2016 vials are never mistaken for its own.
        prev_env = self.store.get_od_profile(s.base_profile)
        if prev_env is None:
            raise CalibrationConflict(
                f"OD profile {s.base_profile!r} has no calibration to carry "
                "vials forward from"
            )
        prev_rows = np.asarray(prev_env["data"]["rows"], dtype=float)
        prev_ds = bool(prev_env["data"].get("dark_subtracted", False))
        per_vial_source = per_vial_sources(prev_env)
        per_vial_version = list(
            (prev_env.get("fit") or {}).get("per_vial_version")
            or [prev_env.get("version")] * N_VIALS
        )
        prev_profile_of = (prev_env.get("fit") or {}).get("per_vial_profile")
        per_vial_profile = (
            list(prev_profile_of)
            if isinstance(prev_profile_of, list) and len(prev_profile_of) == N_VIALS
            else [od_profile_id_of(prev_env)] * N_VIALS
        )
        exclude = {int(v) for v in (exclude_vials or [])}
        failures: list[str] = []
        warnings_: list[str] = []
        dark = np.array([s.dark_median(v) for v in range(N_VIALS)])

        # The previous curve in RAW LED-on terms, the space the series now
        # fits in. A raw curve (the 2016 default, any series since
        # 2026-10-11) carries as-is. A dark-subtracted one -- installed by
        # the earlier software -- is converted back exactly: a = a'+D and
        # b = b'+D undo the a-D, b-D it was installed with.
        prev_raw_rows = prev_rows.copy()
        converted: list[int] = []
        if prev_ds:
            for v in range(N_VIALS):
                if not math.isfinite(dark[v]):
                    failures.append(
                        f"vial {v}: no LED-off reading to convert its previous "
                        "dark-subtracted curve back to raw signal"
                    )
                    continue
                prev_raw_rows[0, v] += dark[v]
                prev_raw_rows[1, v] += dark[v]
        rows = prev_raw_rows.copy()

        measured: list[int] = []
        review: dict = {}
        criteria: dict = {}
        for v in s.vials:
            if v in s.vial_faults:
                warnings_.append(f"vial {v} excluded ({s.vial_faults[v]})")
                continue
            if v in exclude:
                warnings_.append(f"vial {v} excluded by the operator")
                continue
            pts = s.active_points(v)
            ods = np.array([p["true_od600"] for p in pts], dtype=float)
            sig = np.array([self._series_signal(p) for p in pts], dtype=float)
            sds = np.array([p["lit_sd"] for p in pts], dtype=float)
            n_levels = len(set(ods.tolist()))
            if n_levels < od_curve_fit.MIN_OD_LEVELS:
                failures.append(
                    f"vial {v}: {n_levels} OD levels — the fit needs at least "
                    f"{od_curve_fit.MIN_OD_LEVELS}, §8.2 asks for "
                    f"{OD_SERIES_MIN_LEVELS} (or exclude it)"
                )
                continue
            try:
                f = od_curve_fit.fit_logistic4(ods, sig, sds)
            except ValueError as exc:
                failures.append(f"vial {v}: {exc}")
                continue
            measured.append(v)
            rows[:, v] = (f["a"], f["b"], f["c"], f["d"])
            per_vial_source[v] = OD_SERIES_SOURCE
            per_vial_version[v] = version
            per_vial_profile[v] = s.profile_id

            res = np.asarray(f["residuals"])
            allowed = np.maximum(OD_SERIES_RESIDUAL_ABS, OD_SERIES_RESIDUAL_FRACTION * ods)
            worst = int(np.argmax(np.abs(res) / allowed))
            lowest = int(np.argmin(sig))
            margin_sd = max(OD_SERIES_MIN_SD_COUNTS, float(sds[lowest]))
            margin = float(sig[lowest] - f["a"])
            blank_idx = [i for i in range(len(ods)) if ods[i] <= OD_SERIES_BLANK_OD_MAX]
            blank_sd = (max(OD_SERIES_MIN_SD_COUNTS, float(np.median(sds[blank_idx])))
                        if blank_idx else None)
            top = int(np.argmax(ods))
            top_resolution = float(f["counts_per_0p01_od"][top])
            crit = {
                "n_levels": n_levels >= OD_SERIES_MIN_LEVELS,
                "r2": f["r2"] >= OD_SERIES_R2_MIN,
                "monotone": not f["monotone_violations"],
                "residuals": bool(np.all(np.abs(res) <= allowed)),
                "lower_asymptote_margin": margin >= OD_SERIES_LOWER_MARGIN_SD * margin_sd,
                "blank_point": bool(blank_idx),
                "inflection_in_range": bool(ods.min() <= f["c"] <= ods.max()),
                "upper_asymptote_measured": not (f["b"] > ADC_CEILING or f["b_at_bound"]),
                "top_resolvable": (blank_sd is None
                                   or top_resolution >= OD_SERIES_TOP_RESOLUTION_SD * blank_sd),
            }
            criteria[str(v)] = crit
            if not crit["n_levels"]:
                failures.append(
                    f"vial {v}: {n_levels} OD levels < {OD_SERIES_MIN_LEVELS} (§8.2)"
                )
            if not crit["r2"]:
                failures.append(f"vial {v}: R² {f['r2']:.4f} < {OD_SERIES_R2_MIN}")
            if not crit["monotone"]:
                bad = f["monotone_violations"][0]
                failures.append(
                    f"vial {v}: signal does not fall between OD {bad['od_low']} and "
                    f"{bad['od_high']} — a mis-loaded vial, a bubble or a "
                    "transcription error"
                )
            if not crit["residuals"]:
                failures.append(
                    f"vial {v}: residual {res[worst]:+.3f} OD at OD {ods[worst]:.3f} "
                    f"exceeds ±{allowed[worst]:.3f} (0.02 OD or 5 %)"
                )
            if not crit["lower_asymptote_margin"]:
                failures.append(
                    f"vial {v}: lower asymptote {f['a']:.0f} sits only {margin:.0f} "
                    f"counts below the lowest signal ({OD_SERIES_LOWER_MARGIN_SD:.0f} SD "
                    f"= {OD_SERIES_LOWER_MARGIN_SD * margin_sd:.0f} needed) — the "
                    "curve diverges inside its own range (vial 1's 2016 failure)"
                )
            if not crit["blank_point"]:
                warnings_.append(
                    f"vial {v}: no point at or below OD {OD_SERIES_BLANK_OD_MAX} — "
                    "the bottom of the curve is extrapolated"
                )
            if not crit["inflection_in_range"]:
                warnings_.append(
                    f"vial {v}: inflection c = {f['c']:.2f} lies outside the "
                    f"measured OD range [{ods.min():g}, {ods.max():g}] — expected "
                    "for transmitted light (§8.2 as amended); reported, not refused"
                )
            if not crit["upper_asymptote_measured"]:
                warnings_.append(
                    f"vial {v}: upper asymptote b = {f['b']:.0f} is an "
                    "extrapolation" + (" at the fit's cap" if f["b_at_bound"] else "")
                    + " — OD inside the measured range is unaffected"
                )
            if not crit["top_resolvable"]:
                warnings_.append(
                    f"vial {v}: at OD {ods[top]:g} a 0.01 OD step moves the signal "
                    f"only {top_resolution:.0f} counts (< {OD_SERIES_TOP_RESOLUTION_SD:.0f} "
                    "x blank SD) — the top of the range is not resolvable"
                )

            # Review: the new curve against the previous one, both anchored
            # to this session's blank point so the numbers compare.
            prev = prev_raw_rows[:, v]
            entry = {
                "measured": True,
                "a": f["a"], "b": f["b"], "c": f["c"], "d": f["d"],
                "previous": prev.tolist(),
                "r2": f["r2"], "rmse": f["rmse"],
                "max_abs_residual": float(np.max(np.abs(res))),
                "n_points": int(ods.size), "n_levels": n_levels,
                "od_range": [float(ods.min()), float(ods.max())],
                "b_at_bound": f["b_at_bound"],
                "practical_od_ceiling": f["practical_od_ceiling"],
                "counts_per_0p01_od_at_0p3": float(
                    0.01 * od_curve_fit.counts_per_od(0.3, f["a"], f["b"], f["c"], f["d"])),
                "criteria": crit,
                "points": [
                    {"id": p["id"], "true_od600": p["true_od600"],
                     "signal": self._series_signal(p),
                     "residual": float(r)}
                    for p, r in zip(pts, res)
                ],
            }
            if blank_idx and prev[0] < float(sig[blank_idx[0]]) < prev[1]:
                blank_signal = float(np.median(sig[blank_idx]))
                entry["previous_od_at_blank"] = _finite_or_none(float(
                    od_curve_fit.od_from_signal(blank_signal, *prev)))
                a0, b0, _c0, d0 = prev
                if a0 < blank_signal < b0:
                    c_anchor = math.log10((b0 - a0) / (blank_signal - a0) - 1.0) / d0
                    entry["previous_counts_per_0p01_od_at_0p3"] = float(
                        0.01 * od_curve_fit.counts_per_od(0.3, a0, b0, c_anchor, d0))
                    ceiling = a0 + od_curve_fit.PRACTICAL_CEILING_FRACTION * (b0 - a0)
                    entry["previous_practical_od_ceiling"] = _finite_or_none(float(
                        od_curve_fit.od_from_signal(ceiling, a0, b0, c_anchor, d0)))
            review[str(v)] = entry

        if not measured and not failures:
            raise ValueError("no vial was measured — nothing to install")
        converted = [v for v in range(N_VIALS) if v not in measured and prev_ds]
        if converted:
            warnings_.append(
                f"vials {converted} keep their previous curve, converted from "
                "dark-subtracted back to raw signal with this session's LED-off "
                "read (a+D, b+D)"
            )
        carried_legacy = [v for v in range(N_VIALS)
                          if v not in measured and _is_legacy_source(per_vial_source[v])]
        if carried_legacy:
            warnings_.append(
                f"vials {carried_legacy} are still on 2016 constants never "
                "verified against a spectrophotometer"
            )
        for v in range(N_VIALS):
            review.setdefault(str(v), {
                "measured": False,
                "previous": prev_raw_rows[:, v].tolist(),
                "converted_from_raw": v in converted,
                "source": per_vial_source[v],
            })
        qc = {
            "passed": not failures,
            "warnings": warnings_,
            "failures": failures,
            "overridden_by": None,
            "criteria": criteria,
            "excluded_vials": sorted(exclude),
        }
        vial_map_version = self._vial_map_version(qc)
        not_this_organism = [v for v in range(N_VIALS) if per_vial_profile[v] != s.profile_id]
        if not_this_organism and s.new_profile:
            warnings_.append(
                f"vials {not_this_organism} were not measured for {s.organism!r}: "
                "this new profile carries the default curve on them, and an "
                "experiment on those vials is warned at create"
            )
        fit = {
            "profile_id": s.profile_id,
            "organism": s.organism,
            "base_profile": s.base_profile,
            "base_version": prev_env.get("version"),
            "measured_vials": measured,
            "converted_vials": converted,
            "per_vial_source": per_vial_source,
            "per_vial_version": per_vial_version,
            "per_vial_profile": per_vial_profile,
            "dark_medians": [_finite_or_none(x) for x in dark],
            "review": review,
        }
        return rows, fit, qc, vial_map_version

    def series_fit(self, exclude_vials: Optional[list] = None) -> dict:
        """The review screen for the series, written nowhere."""
        with self._lock:
            s = self._require_tier3("od_series")
            _rows, fit, qc, _vm = self._series_fit_and_qc_locked(
                s, exclude_vials, self.store.fresh_version("od"),
            )
            return {"fit": fit, "qc": qc}

    def series_finish(
        self,
        *,
        exclude_vials: Optional[list] = None,
        override_reason: Optional[str] = None,
        operator: Optional[str] = None,
    ) -> dict:
        """Fit, QC, and install an OD profile version on RAW LED-on signal
        (``dark_subtracted: false``): on this rig the LED-off read sits at the
        ADC rail above every LED-on read, so it cannot be subtracted. The
        caller makes it live."""
        with self._lock:
            s = self._require_tier3("od_series")
            version = self.store.fresh_version("od")
            base_env = self.store.get_od_profile(s.base_profile)
            prev_version = base_env.get("version") if base_env else None
            rows, fit, qc, vial_map_version = self._series_fit_and_qc_locked(
                s, exclude_vials, version,
            )
            if not qc["passed"]:
                if not override_reason:
                    raise QCRefusal(
                        "OD dilution series failed its acceptance criteria "
                        "(§8.2) — re-capture, discard bad points, exclude the "
                        "failing vials, or finish with an explicit override_reason",
                        qc,
                    )
                qc["overridden_by"] = str(override_reason)
            envelope = make_envelope(
                "od",
                operator=operator or s.operator,
                source=OD_SERIES_SOURCE,
                conditions={
                    "organism": s.organism,
                    "led_power": s.led_power,
                    "stir_pwm": s.stir_pwm,
                    "target_temp_c": s.target_temp_c,
                    "n_samples": s.n_samples,
                    "standard": s.standard or None,
                    "medium": s.medium or None,
                    "spectrophotometer": s.spectrophotometer or None,
                    "dark_subtracted": False,
                    "vial_map_version": vial_map_version,
                    "settle_overridden_points": [
                        p["id"] for p in s.points if not p.get("settle_checked", True)
                    ],
                },
                data={
                    "rows": rows.tolist(),
                    "dark_subtracted": False,
                    "points": s.points,
                    "dark_captures": s.dark_captures,
                    "previous_version": prev_version,
                },
                fit=fit,
                qc=qc,
                version=version,
            )
            installed = self.store.save_od_profile_version(
                s.profile_id, s.organism, envelope,
            )
            self._end_tier3()
            return {
                "status": "installed",
                "version": installed,
                "profile_id": s.profile_id,
                "organism": s.organism,
                "new_profile": s.new_profile,
                "measured_vials": fit["measured_vials"],
                "converted_vials": fit["converted_vials"],
                "review": fit["review"],
                "qc": qc,
            }

    def series_abort(self) -> dict:
        with self._lock:
            self._require_tier3("od_series")
            self._end_tier3()
            return {"status": "aborted"}

    # -- per-run OD blank (O2) -------------------------------------------

    def blank_start(
        self,
        *,
        experiment: str,
        config: dict,
        engine_status: str,
        led_power: int,
        stir_pwm: Optional[int],
        expected_led_power: int,
        n_samples: int = 5,
    ) -> dict:
        """Open a blank session for the loaded CREATED experiment, enforcing
        the §13 condition-match guard: a blank at a different LED power or
        stir PWM than the run is not a blank.

        Each vial is expected at its own group's stir rate (vial groups,
        ``run_config.py``). ``stir_pwm``, when given, is the operator's claim
        of one PWM for every vial and must match every vial's group; it is
        optional because an experiment whose groups stir differently has no
        single right answer. The stir the manager is actually sending is
        reported beside the expectation (``stir_mismatch``) as a warning, not
        a refusal -- the thermal-settling check is the hard gate."""
        with self._lock:
            self._refuse_during_tier3("an OD blank")
            if engine_status != "created":
                raise CalibrationConflict(
                    "the OD blank is taken against a CREATED experiment, "
                    f"immediately before start; engine status is '{engine_status}'"
                )
            # One blank session at a time, machine-wide. With parallel
            # experiments a second operator starting a blank would otherwise
            # silently replace the first one's session mid-procedure.
            other = self._blank
            if other is not None and other.experiment != experiment:
                age = time.time() - other.started_monotonic_wall
                if age < BLANK_SESSION_STALE_SECONDS:
                    raise CalibrationConflict(
                        f"an OD blank for '{other.experiment}' is in progress "
                        f"(started {age / 60:.0f} min ago) -- finish or abort "
                        "it first"
                    )
            expected_stir = run_config.config_stir_by_vial(config or {})
            if stir_pwm is not None:
                rates = sorted(set(expected_stir.values()))
                if len(rates) == 1 and int(stir_pwm) != rates[0]:
                    raise ValueError(
                        f"stir_pwm {stir_pwm} != the run's stir_rate {rates[0]} — "
                        "a blank taken at a different stir PWM is not a blank "
                        "(Principle 1)"
                    )
                if len(rates) > 1:
                    raise ValueError(
                        f"this run's groups stir at different rates {rates}, so "
                        f"no single stir_pwm ({stir_pwm}) matches every vial — "
                        "omit stir_pwm; each vial is checked against its own "
                        "group's rate"
                    )
            if int(led_power) != int(expected_led_power):
                raise ValueError(
                    f"led_power {led_power} != the run's LED power "
                    f"{expected_led_power} — a blank at a different LED power "
                    "is not a blank (Principle 1)"
                )
            if not (1 <= int(n_samples) <= 25):
                raise ValueError(f"n_samples must be in 1..25, got {n_samples}")
            vials = sorted(int(v) for v in (config or {}).get("vials", []))
            if not vials:
                raise ValueError("experiment has no vials")
            # Per vial, from each vial's group (scalar or 16-list
            # temperature_c, legacy `temperature`, then 37 °C).
            all_targets = run_config.config_temperature_c_by_vial(config or {})
            targets = {v: all_targets[v] for v in vials}
            stir_by_vial = {v: expected_stir.get(v, run_config.DEFAULT_STIR_RATE)
                            for v in vials}
            self._blank = OdBlankSession(
                experiment=experiment,
                vials=vials,
                led_power=int(led_power),
                stir_by_vial=stir_by_vial,
                targets_c=targets,
                n_samples=int(n_samples),
            )
            pin = self._od_pin_from_config(config)
            if pin is not None:
                self._blank.od_profile, self._blank.od_version = pin
            try:
                paused = run_config.config_stir_pause_by_vial(config or {})
            except ValueError:
                paused = {}
            self._blank.stir_paused_vials = {
                v: p.to_dict() for v, p in paused.items() if v in vials
            }
            out = {
                "session": self._blank.id,
                "experiment": experiment,
                "vials": vials,
                "n_samples": self._blank.n_samples,
                "thermal": self.tracker.settled(vials, targets),
                "stir_mismatch": self._stir_mismatch(stir_by_vial),
                "warnings": [],
            }
            warning = stir_pause_blank_warning(self._blank.stir_paused_vials)
            if warning:
                out["warnings"].append(warning)
            return out

    def _od_pin_from_config(self, config: Optional[dict]) -> Optional[tuple[str, str]]:
        """``(profile, version)`` from an experiment config's calibration
        block; a pre-profile config's profile is its version's owner."""
        cal = (config or {}).get("calibration") or {}
        version = cal.get("od")
        if not version:
            return None
        profile = cal.get("od_profile") or od_profile_id_of(
            self.store.get_version("od", version))
        return (str(profile), str(version)) if profile else None

    def _stir_mismatch(self, expected: dict) -> dict:
        """Vials whose actually-commanded stir differs from the run's.

        Read from the manager's last-sent stir vector. Empty when they agree,
        or when the manager does not expose one."""
        actual = getattr(self.manager, "stir_speed", None)
        if actual is None:
            return {}
        try:
            actual = [int(x) for x in list(actual)]
        except Exception:
            return {}
        return {
            str(v): {"expected": int(e), "actual": actual[v]}
            for v, e in expected.items()
            if 0 <= v < len(actual) and actual[v] != int(e)
        }

    def blank_experiment(self) -> Optional[str]:
        """The experiment the open blank session was started for, if any.
        With parallel experiments the commit must re-anchor THAT run, not
        whichever happens to be loaded first."""
        with self._lock:
            return None if self._blank is None else self._blank.experiment

    def _require_blank(self, session_id: str) -> OdBlankSession:
        if self._blank is None:
            raise CalibrationConflict("no OD blank session is active")
        if session_id and session_id != self._blank.id:
            raise CalibrationConflict(
                f"session {session_id!r} is not the active blank session"
            )
        return self._blank

    def blank_dark(self, session_id: str = "") -> dict:
        with self._lock:
            s = self._require_blank(session_id)
            # Dark only THIS run's vials. With parallel experiments another
            # run's LEDs must stay lit -- its next OD read follows this one.
            leds = [0 if v in s.vials else s.led_power for v in range(N_VIALS)]
            stats = self.manager.collect_od_raw(leds, n_samples=s.n_samples)
            s.dark = stats
            return {"session": s.id, "phase": "dark", **stats}

    def blank_measure(self, session_id: str = "") -> dict:
        with self._lock:
            s = self._require_blank(session_id)
            if s.dark is None:
                raise CalibrationConflict(
                    "take the dark read before the blank read (§5.4 order)"
                )
            stats = self.manager.collect_od_raw(s.led_power, n_samples=s.n_samples)
            s.blank = stats
            return {"session": s.id, "phase": "blank", **stats}

    def _previous_blank(self, excluding_experiment: str,
                        od_profile: Optional[str] = None) -> Optional[dict]:
        """Most recent od_blank.json across experiments/ (the campaign
        reference for the §5.4 drift checks), excluding the current one --
        and, with ``od_profile``, only blanks taken on that organism's
        curve: another organism's run used another medium and vials, and
        its blank median is no reference for this one. A blank from before
        profiles counts as the default slot's."""
        latest: Optional[tuple[str, dict]] = None
        if not self.experiments_root.is_dir():
            return None
        default = self.store.od_default_profile()
        for path in self.experiments_root.glob("*/od_blank.json"):
            if path.parent.name == excluding_experiment:
                continue
            try:
                env = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if od_profile is not None:
                theirs = (env.get("conditions") or {}).get("od_profile") or default
                if theirs != od_profile:
                    continue
            version = str(env.get("version", ""))
            if latest is None or version > latest[0]:
                latest = (version, env)
        return latest[1] if latest else None

    def blank_commit(
        self,
        session_id: str = "",
        *,
        exclude_vials: Optional[list[int]] = None,
        override_reason: Optional[str] = None,
        operator: str = "unknown",
        settle_check: bool = True,
    ) -> dict:
        """Run the §5.4 acceptance checks, write ``od_blank.json`` into the
        experiment directory, and return the re-anchor results. The caller
        (app.py) applies ``c_run`` to the live SerialManager and records the
        provenance in config.json."""
        with self._lock:
            s = self._require_blank(session_id)
            if s.dark is None or s.blank is None:
                raise CalibrationConflict(
                    "both the dark and blank reads must be taken before commit"
                )
            exclude = sorted({int(v) for v in (exclude_vials or [])})
            vials = [v for v in s.vials if v not in exclude]
            if not vials:
                raise ValueError("every vial is excluded — nothing to commit")

            # Thermal-settling guard (§13): enforced, not trusted.
            thermal = self.tracker.settled(vials, s.targets_c)
            if settle_check and not thermal["settled"]:
                raise CalibrationConflict(
                    "temperature has not been within "
                    f"±{THERMAL_SETTLE_TOL_C} °C of setpoint for "
                    f"{THERMAL_SETTLE_MINUTES:.0f} min on every vial — wait "
                    "for equilibration (Principle 1); see 'thermal' detail "
                    f"in GET status: {json.dumps(thermal['per_vial'])}"
                )

            if s.od_version is not None:
                od_env = self.store.get_od_profile(s.od_profile, s.od_version)
            else:
                od_env = self.store.get_current("od")
            if od_env is None:
                raise CalibrationConflict("the run's OD calibration is not installed")
            od_cal = np.asarray(od_env["data"]["rows"], dtype=float)
            # A dark-subtracted curve (Session AA's dilution series) needs a
            # dark-subtracted blank, and the reverse: mixing the two is
            # silently wrong OD (§5.4). The curve's own flag decides.
            dark_subtracted = bool(od_env["data"].get("dark_subtracted", False))

            blank_median = np.asarray(s.blank["median"], dtype=float)
            blank_signal = (
                blank_median - np.asarray(s.dark["median"], dtype=float)
                if dark_subtracted else blank_median
            )
            # Domain guard evaluated per included vial (excluded vials may
            # be NaN or out of domain without blocking the rest).
            a, b, _c, d = od_cal
            domain_bad = [
                v for v in vials
                if not (a[v] < blank_signal[v] < b[v])
            ]
            if domain_bad:
                raise ValueError(
                    f"blank signal outside the calibration domain (a, b) on "
                    f"vials {domain_bad} — reseat/repeat or exclude them"
                )

            # Re-anchor c for the included vials only.
            with np.errstate(invalid="ignore", divide="ignore"):
                c_run_all = np.log10(
                    (b - a) / (blank_signal - a) - 1.0
                ) / d
            c_run = {v: float(c_run_all[v]) for v in vials}
            od_offset_removed = {
                v: float(od_cal[2, v] - c_run_all[v]) for v in vials
            }

            # §5.4 acceptance criteria -> qc block.
            failures: list[str] = []
            warnings: list[str] = []
            paused_warning = stir_pause_blank_warning(s.stir_paused_vials)
            if paused_warning:
                warnings.append(paused_warning)
            dark_sd = s.dark["sd"]
            blank_sd = s.blank["sd"]
            for v in vials:
                if not math.isnan(dark_sd[v]) and dark_sd[v] > BLANK_DARK_SD_MAX:
                    failures.append(
                        f"vial {v}: dark SD {dark_sd[v]:.0f} > "
                        f"{BLANK_DARK_SD_MAX:.0f} counts (provisional) — "
                        "ambient light or electrical noise"
                    )
                if not math.isnan(blank_sd[v]) and blank_sd[v] > BLANK_SIGNAL_SD_MAX:
                    failures.append(
                        f"vial {v}: blank SD {blank_sd[v]:.0f} > "
                        f"{BLANK_SIGNAL_SD_MAX:.0f} counts (provisional) — "
                        "bubbles, wobbling stir bar, or poorly seated vial"
                    )
            previous = self._previous_blank(s.experiment, od_profile_id_of(od_env))
            reference = None
            if previous is not None:
                reference = previous.get("version")
                prev_blank = previous.get("data", {}).get("blank_median")
                prev_c_run = previous.get("fit", {}).get("c_run", {})
                # c_run is a position on ONE curve: against a blank taken on a
                # different OD calibration its drift is meaningless (a new
                # Tier 3 curve moves every c). The raw blank median still
                # compares -- it is counts, not OD.
                prev_parent = (previous.get("conditions") or {}).get("parent_od_cal")
                if prev_parent != od_env.get("version"):
                    prev_c_run = {}
                    warnings.append(
                        "first blank against OD calibration "
                        f"{od_env.get('version')} — the c_run drift reference "
                        "resets (the previous blank used "
                        f"{prev_parent or 'an unrecorded curve'})"
                    )
                for v in vials:
                    if isinstance(prev_blank, list) and not math.isnan(
                        blank_median[v]
                    ):
                        prev_b = prev_blank[v]
                        if prev_b and abs(
                            blank_median[v] / prev_b - 1.0
                        ) > BLANK_MEDIAN_TOL_FRACTION:
                            warnings.append(
                                f"vial {v}: blank median {blank_median[v]:.0f} "
                                f"is {100 * (blank_median[v] / prev_b - 1):+.1f}% "
                                "vs the previous run — optical path changed?"
                            )
                    prev_c = prev_c_run.get(str(v)) if isinstance(
                        prev_c_run, dict
                    ) else None
                    if prev_c is not None and abs(
                        c_run[v] - float(prev_c)
                    ) > C_RUN_DELTA_MAX:
                        failures.append(
                            f"vial {v}: c_run shifted "
                            f"{c_run[v] - float(prev_c):+.3f} OD vs the "
                            f"previous run (> {C_RUN_DELTA_MAX}) — investigate "
                            "before trusting the run"
                        )

            qc = {
                "passed": not failures,
                "warnings": warnings,
                "failures": failures,
                "overridden_by": None,
                "reference_blank": reference,
                "excluded_vials": exclude,
            }
            if failures:
                if not override_reason:
                    raise QCRefusal(
                        "blank failed acceptance criteria (§5.4) — reseat and "
                        "repeat once, exclude the failing vials, or commit "
                        "with an explicit override_reason",
                        qc,
                    )
                qc["overridden_by"] = str(override_reason)

            envelope = make_envelope(
                "od_blank",
                operator=operator,
                source=f"per-run-blank-x{s.n_samples}",
                conditions={
                    "led_power": s.led_power,
                    # Scalar when every vial stirs alike (the pre-groups
                    # shape); None with the per-vial map when groups differ.
                    "stir_pwm": (
                        next(iter(set(s.stir_by_vial.values())))
                        if len(set(s.stir_by_vial.values())) == 1 else None
                    ),
                    "stir_pwm_by_vial": {
                        str(k): v for k, v in s.stir_by_vial.items()
                    },
                    "target_temp_c": {str(k): v for k, v in s.targets_c.items()},
                    # The blank is always measured with the stirrers running.
                    # Vials listed below will be READ with their stirrer
                    # paused (od_stir_pause), so this blank corrects them from
                    # a different optical condition -- see the QC warning.
                    "stir_condition": "stirring",
                    "stir_paused_reads_by_vial": {
                        str(k): v for k, v in s.stir_paused_vials.items()
                    },
                    "parent_od_cal": od_env.get("version"),
                    "od_profile": od_profile_id_of(od_env),
                    "dark_subtracted": dark_subtracted,
                },
                data={
                    "dark_median": s.dark["median"],
                    "dark_sd": s.dark["sd"],
                    "blank_median": s.blank["median"],
                    "blank_sd": s.blank["sd"],
                    # What c_run was computed from: blank_median, or blank
                    # minus dark against a dark-subtracted curve.
                    "blank_signal": [_finite_or_none(x) for x in blank_signal],
                    "n_samples": s.n_samples,
                },
                fit={
                    "updated_rows": [2],
                    "c_run": {str(k): v for k, v in c_run.items()},
                    "od_offset_removed": {
                        str(k): v for k, v in od_offset_removed.items()
                    },
                },
                qc=qc,
            )
            exp_dir = self.experiments_root / s.experiment
            if not exp_dir.is_dir():
                raise FileNotFoundError(f"experiment directory missing: {exp_dir}")
            blank_path = exp_dir / "od_blank.json"
            tmp = blank_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(envelope, indent=4), encoding="utf-8")
            os.replace(tmp, blank_path)

            self._blank = None
            return {
                "status": "ok",
                "updated_rows": [2],
                "c_run": {str(k): v for k, v in c_run.items()},
                "od_offset_removed": {
                    str(k): round(v, 4) for k, v in od_offset_removed.items()
                },
                "qc": qc,
                "path": str(blank_path),
                "parent_od_cal": od_env.get("version"),
                "vials": vials,
            }

    def blank_abort(self, session_id: str = "") -> dict:
        with self._lock:
            self._require_blank(session_id)
            self._blank = None
            return {"status": "aborted"}

    def load_experiment_blank(self, experiment: str) -> Optional[dict]:
        """Read an experiment's committed od_blank.json (for re-applying the
        re-anchor after a crash resume). Returns None when absent."""
        path = self.experiments_root / experiment / "od_blank.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("failed to parse %s", path)
            return None

    # -- growth-rate service context (SPEC §17) ---------------------------

    @staticmethod
    def _od_counts_to_od_sd(od_cal, vial: int, signal: float, sd_counts: float):
        """Convert a blank SD in ADC counts to OD units for one vial.

        The blank wizard records ``blank_sd`` in raw counts, but the growth
        service needs a floor in OD. The four-parameter logistic

            OD(S) = c - log10((b - a) / (S - a) - 1) / d

        has the closed-form local slope

            dOD/dS = (b - a) / (d * ln10 * (b - S) * (S - a))

        evaluated at the blank signal. Multiplying the count-domain SD by that
        slope is the correct first-order propagation, and it is what makes the
        floor genuinely per-vial: the same count noise maps to a four-fold
        range of OD noise across sleeves.

        Returns ``None`` when the signal sits outside the calibration's
        ``(a, b)`` domain, where the conversion has no meaning.
        """
        try:
            a = float(od_cal[0][vial])
            b = float(od_cal[1][vial])
            d = float(od_cal[3][vial])
        except Exception:
            return None
        if not (a < float(signal) < b) or d == 0:
            return None
        slope = (b - a) / (d * math.log(10.0) * (b - float(signal)) * (float(signal) - a))
        return abs(slope) * float(sd_counts)

    def growth_context(self, experiment: Optional[str]) -> dict:
        """Everything the growth-rate estimator needs from the calibration
        layer, in the shape ``ExperimentEngine.set_growth_context`` expects.

        Three separate facts, each with its own failure mode:

        * ``od_floor`` -- per-vial, from this run's committed blank. Absent a
          blank (**the live case today: none has ever been committed**), every
          vial gets the global fallback and ``blank_present`` is False, which
          makes the engine flag every estimate ``uncalibrated_floor``.
        * ``suspect_vials`` -- vials whose OD calibration the store's own QC
          says to exclude from quantitative use. Derived from the envelope's
          numbers, not by parsing its English warning text.
        * ``pump_calibrated`` -- gates the dilution diagnostic (§4.4). False
          today, because ``calibration/current.json`` has ``"pump": null``:
          until the Tier 2 gravimetric bench work runs, every flow rate is a
          hardcoded default and the diagnostic would false-alarm.
        """
        floors: dict = {}
        blank_present = False
        suspect: list = []

        # The rows this run's OD actually goes through: its pinned organism
        # profile version, not the machine default.
        pin = self.store.experiment_od_pin(experiment) if experiment else None
        od_env = None
        if pin is not None:
            try:
                od_env = self.store.get_od_profile(*pin)
            except ValueError:
                od_env = None
        if od_env is None:
            od_env = self.store.get_current("od")
        od_rows = None
        if od_env is not None:
            rows = (od_env.get("data") or {}).get("rows")
            if isinstance(rows, list) and len(rows) == 4:
                od_rows = rows
                for v in range(N_VIALS):
                    try:
                        if float(rows[0][v]) > OD_LOWER_ASYMPTOTE_SUSPECT:
                            suspect.append(v)
                    except Exception:
                        continue

        blank = (
            self.store.experiments_root is not None and experiment
            and self.load_experiment_blank(experiment)
        ) or None
        if blank:
            data = blank.get("data") or {}
            # blank_commit writes 16-element lists; accept a {vial: value}
            # map too. (Before Session AA this iterated .items() on the list
            # and raised on every real blank -- caught upstream, so no run
            # ever got its per-vial floors.)
            sd = _per_vial_values(data.get("blank_sd"))
            signal = _per_vial_values(
                data.get("blank_signal") or data.get("blank_median")
            )
            for v, sd_counts in sd.items():
                sd_od = None
                if od_rows is not None and v in signal:
                    sd_od = self._od_counts_to_od_sd(
                        od_rows, v, signal[v], sd_counts,
                    )
                if sd_od is None or not math.isfinite(sd_od):
                    continue
                floors[v] = max(
                    GROWTH_OD_FLOOR_MIN, GROWTH_OD_FLOOR_SD_MULTIPLE * sd_od,
                )
            blank_present = bool(floors)

        pump_rates = self.store.current_pump_rates()
        return {
            "od_floor": floors,
            "blank_present": blank_present,
            "suspect_vials": suspect,
            "pump_calibrated": pump_rates is not None,
            "pump_reason_unavailable": (
                None if pump_rates is not None else
                "pump flow rates have not been calibrated on this machine "
                "(calibration/current.json has \"pump\": null) \u2014 the dilution "
                "cross-check would compare growth against a hardcoded default "
                "flow array. Run the gravimetric wizard to enable it."
            ),
        }

    # -- pump gravimetric (O3) -------------------------------------------

    def pump_start(self, body: dict) -> dict:
        with self._lock:
            self._refuse_during_tier3("a pump calibration")
            if self._pump is not None:
                if body.get("resume"):
                    return {"status": "resumed", **self._pump.progress()}
                raise CalibrationConflict(
                    "a pump calibration session is already in progress "
                    f"(started {self._pump.started_at}) — pass resume=true to "
                    "continue it, or abort it first"
                )
            pumps = body.get("pumps")
            if pumps is None:
                pumps = list(range(N_PUMPS))
            self._pump = PumpCalSession(
                pumps=pumps,
                fire_seconds=body.get("fire_seconds", PUMP_DEFAULT_FIRE_SECONDS),
                replicates=body.get("replicates", PUMP_DEFAULT_REPLICATES),
                fluid=body.get("fluid", "water"),
                fluid_density_g_ml=body.get("fluid_density_g_ml", 0.99777),
                bench_temp_c=body.get("bench_temp_c"),
                operator=body.get("operator", "unknown"),
            )
            self._persist_pump_session()
            return {"status": "started", **self._pump.progress()}

    def _require_pump(self) -> PumpCalSession:
        if self._pump is None:
            raise CalibrationConflict(
                "no pump calibration session — POST /api/calibration/pump/start"
            )
        return self._pump

    def pump_fire(self, pump_id: int) -> dict:
        with self._lock:
            s = self._require_pump()
            key = str(int(pump_id))
            if key not in s.results:
                raise ValueError(f"pump {pump_id} is not part of this session")
            vial, direction = PumpCalSession.pump_direction(int(pump_id))
            self.manager.pump_command(vial, direction, s.fire_seconds)
            s.results[key]["fired"] += 1
            self._persist_pump_session()
            return {
                "status": "fired",
                "pump_id": int(pump_id),
                "vial": vial,
                "direction": direction,
                "seconds": s.fire_seconds,
            }

    def pump_record(self, pump_id: int, replicate: int, mass_g: float) -> dict:
        with self._lock:
            s = self._require_pump()
            stats = s.record_mass(pump_id, replicate, mass_g)
            self._persist_pump_session()
            return stats

    def pump_session(self) -> dict:
        with self._lock:
            if self._pump is None:
                return {"active": False}
            return {"active": True, **self._pump.progress()}

    def pump_finish(
        self,
        *,
        override_reason: Optional[str] = None,
        operator: Optional[str] = None,
    ) -> dict:
        """Fit + QC + install a new versioned pump calibration. Refuses on QC
        failure unless an ``override_reason`` is recorded (§13)."""
        with self._lock:
            s = self._require_pump()
            previous_env = self.store.get_current("pump")
            previous_rates = None
            if previous_env is not None:
                pr = previous_env.get("fit", {}).get("flow_rates_ml_s")
                if isinstance(pr, list) and len(pr) == N_PUMPS:
                    previous_rates = pr
            fit, qc = s.fit_and_qc(previous_rates)
            if not qc["passed"]:
                if not override_reason:
                    raise QCRefusal(
                        "pump calibration failed acceptance criteria (§7) — "
                        "fix and re-record, or finish with an explicit "
                        "override_reason",
                        qc,
                    )
                qc = dict(qc)
                qc["overridden_by"] = str(override_reason)
            conditions = {
                "fluid": s.fluid,
                "fluid_density_g_ml": s.fluid_density_g_ml,
                "bench_temp_c": s.bench_temp_c,
                "fire_seconds": s.fire_seconds,
                "replicates": s.replicates,
                "vial_map_version": self._vial_map_version(qc),
            }
            envelope = make_envelope(
                "pump",
                operator=operator or s.operator,
                source=f"gravimetric-{s.fire_seconds:.0f}s-x{s.replicates}",
                conditions=conditions,
                data={"per_pump": {k: v["masses_g"]
                                   for k, v in s.results.items()}},
                fit=fit,
                qc=qc,
            )
            version = self.store.save_version("pump", envelope)
            # Session complete — remove the resumable file.
            self._pump = None
            try:
                self._pump_session_path().unlink(missing_ok=True)
            except OSError:
                log.exception("failed to remove pump session file")
            complete = all(r is not None for r in fit["flow_rates_ml_s"])
            return {
                "status": "installed",
                "version": version,
                "review": fit["review"],
                "qc": qc,
                "flow_rates_complete": complete,
                "flow_rates_ml_s": fit["flow_rates_ml_s"],
            }

    def _vial_map_version(self, qc: dict) -> Optional[str]:
        """Prerequisite P1: record the vial map version with every write and
        warn loudly when it is null."""
        vial_map_path = self.store.root / "vial_map.json"
        if vial_map_path.is_file():
            try:
                vm = json.loads(vial_map_path.read_text(encoding="utf-8"))
                return vm.get("version") or "unversioned"
            except Exception:
                log.exception("failed to parse vial_map.json")
        qc.setdefault("warnings", []).append(
            "vial_map.json is missing (prerequisite P1) — per-vial constants "
            "are attached to logical indices that have never been verified "
            "against physical sleeve positions"
        )
        return None

    def pump_abort(self) -> dict:
        with self._lock:
            self._require_pump()
            self._pump = None
            try:
                self._pump_session_path().unlink(missing_ok=True)
            except OSError:
                log.exception("failed to remove pump session file")
            return {"status": "aborted"}

    # -- post-run reconciliation (O4) ------------------------------------

    def reconcile(
        self,
        experiment: str,
        experiment_state: dict,
        body: dict,
    ) -> dict:
        """Compare measured mass deltas against the software's accumulated
        ``duration x flow_rate`` volumes (§6 / §19.4). Writes the run's
        ``reconciliation.json`` and appends to the store's log."""
        density = body.get("density_g_ml")
        if not isinstance(density, (int, float)) or isinstance(density, bool) \
                or not (0.5 <= float(density) <= 2.0):
            raise ValueError(
                "'density_g_ml' is required (use the bench-temperature water "
                "density, CALIBRATION_PROTOCOL Appendix B.3)"
            )
        density = float(density)

        media_state = (experiment_state or {}).get("media_state") or {}
        bottles = media_state.get("bottles") or {}
        inferred_media_ml = sum(
            float(b.get("consumed_ml", 0.0)) for b in bottles.values()
        )
        waste_state = media_state.get("waste") or {}
        inferred_waste_ml = float(waste_state.get("filled_ml", 0.0))

        def _side(start_key: str, end_key: str, inferred: float,
                  sign: float) -> Optional[dict]:
            start = body.get(start_key)
            end = body.get(end_key)
            if start is None or end is None:
                return None
            for label, v in ((start_key, start), (end_key, end)):
                if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                    raise ValueError(f"'{label}' must be a non-negative number")
            measured_ml = sign * (float(end) - float(start)) / density
            ratio = (measured_ml / inferred) if inferred > 0 else None
            within = (
                ratio is not None and abs(ratio - 1.0) <= RECONCILE_TOLERANCE
            )
            return {
                "measured_ml": round(measured_ml, 2),
                "inferred_ml": round(inferred, 2),
                "ratio": round(ratio, 4) if ratio is not None else None,
                "within_tolerance": within,
            }

        media = _side("media_start_g", "media_end_g", inferred_media_ml, -1.0)
        waste = _side("waste_start_g", "waste_end_g", inferred_waste_ml, +1.0)
        if media is None and waste is None:
            raise ValueError(
                "provide media_start_g/media_end_g and/or "
                "waste_start_g/waste_end_g"
            )
        sides = [x for x in (media, waste) if x is not None]
        overall = all(x["within_tolerance"] for x in sides)
        record = {
            "experiment": experiment,
            "timestamp": _iso_now(),
            "density_g_ml": density,
            "media": media,
            "waste": waste,
            "within_tolerance": overall,
            "pump_calibration_version": self.store.current_versions().get("pump"),
            "operator": str(body.get("operator", "unknown")),
        }
        exp_dir = self.experiments_root / experiment
        if not exp_dir.is_dir():
            raise FileNotFoundError(f"experiment '{experiment}' not found")
        recon_path = exp_dir / "reconciliation.json"
        tmp = recon_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=4), encoding="utf-8")
        os.replace(tmp, recon_path)
        self.store.record_reconciliation(record)
        return record
