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

import csv
import json
import logging
import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

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
            if float(arr[0, v]) > 40000.0:
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
            envelope.setdefault("supersedes", current.get(subsystem))
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
                    (self.root / "OD_cal.txt").write_text(
                        "\n".join(lines) + "\n", encoding="utf-8"
                    )
                    meta = {
                        "dark_subtracted": bool(
                            od.get("data", {}).get("dark_subtracted", False)
                        ),
                        "version": od.get("version"),
                    }
                    (self.root / "OD_cal.meta.json").write_text(
                        json.dumps(meta, indent=4), encoding="utf-8"
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
                    (self.root / "temp_calibration.txt").write_text(
                        "\n".join(lines) + "\n", encoding="utf-8"
                    )

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

    def pump_seconds_since(self, version: Optional[str]) -> Optional[float]:
        """Cumulative pump-on seconds across every experiment's pump logs
        since the given calibration version's timestamp (the tubing-wear
        signal). Returns None when the experiments root is unknown."""
        if self.experiments_root is None or not self.experiments_root.is_dir():
            return None
        since = _parse_version(version) if version else None
        total = 0.0
        for csv_path in self.experiments_root.glob("*/vial*_pump_log.csv"):
            try:
                with csv_path.open("r", encoding="utf-8", newline="") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if since is not None:
                            ts = row.get("timestamp", "")
                            try:
                                row_dt = datetime.fromisoformat(ts)
                            except ValueError:
                                continue
                            if row_dt.tzinfo is None:
                                row_dt = row_dt.replace(tzinfo=timezone.utc)
                            if row_dt < since:
                                continue
                        try:
                            total += float(row.get("duration_seconds") or 0.0)
                        except (TypeError, ValueError):
                            continue
            except Exception:
                log.exception("failed reading %s", csv_path)
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

        # OD curve
        od_version = current.get("od")
        od_env = self.get_current("od") if od_version else None
        reasons = []
        if od_version is None:
            reasons.append("no OD calibration installed")
        else:
            if od_env is not None and str(od_env.get("source", "")).startswith(
                "legacy-import"
            ):
                reasons.append(
                    "never verified against a spectrophotometer "
                    "(2016 inherited constants)"
                )
            age = _age_days(od_version)
            if age is not None and age > CURVE_STALE_DAYS:
                reasons.append(f"calibration is {age:.0f} days old (> {CURVE_STALE_DAYS:.0f})")
        out["od"] = {"stale": bool(reasons), "reasons": reasons,
                     "version": od_version}

        # Temperature
        temp_version = current.get("temperature")
        temp_env = self.get_current("temperature") if temp_version else None
        reasons = []
        if temp_version is None:
            reasons.append("no temperature calibration installed")
        else:
            if temp_env is not None and str(temp_env.get("source", "")).startswith(
                "legacy-import"
            ):
                reasons.append(
                    "never verified against a reference thermometer "
                    "(2016 inherited constants)"
                )
            if temp_env is not None and temp_env.get("qc", {}).get("warnings"):
                reasons.append(
                    "outlier vial flagged in the fit "
                    "(see the calibration's qc warnings — vial 0 is the prime suspect)"
                )
            age = _age_days(temp_version)
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
        stir_pwm: int,
        targets_c: dict,
        n_samples: int = 5,
    ) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.experiment = experiment
        self.vials = list(vials)
        self.led_power = int(led_power)
        self.stir_pwm = int(stir_pwm)
        self.targets_c = dict(targets_c)
        self.n_samples = int(n_samples)
        self.created_at = _iso_now()
        self.dark: Optional[dict] = None
        self.blank: Optional[dict] = None


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
        self._lock = threading.RLock()
        self._blank: Optional[OdBlankSession] = None
        self._pump: Optional[PumpCalSession] = None

    # -- wiring ----------------------------------------------------------

    def note_temperatures(self, temps: list) -> None:
        try:
            self.tracker.note(temps)
        except Exception:
            log.exception("temp stability tracker note failed")

    def bootstrap(self) -> list[str]:
        created = self.store.bootstrap_from_legacy()
        self._load_pump_session()
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
        return {
            "subsystems": self.store.current_index(),
            "staleness": self.store.staleness(
                loaded_experiment=loaded_experiment, loaded_status=loaded_status,
            ),
            "pump_session_active": self._pump is not None,
            "blank_session_active": self._blank is not None,
        }

    def subsystem(self, name: str) -> dict:
        if name not in SUBSYSTEMS:
            raise ValueError(f"unknown subsystem {name!r}; one of {SUBSYSTEMS}")
        env = self.store.get_current(name)
        if env is None:
            raise FileNotFoundError(f"no calibration installed for {name!r}")
        return env

    # -- per-run OD blank (O2) -------------------------------------------

    def blank_start(
        self,
        *,
        experiment: str,
        config: dict,
        engine_status: str,
        led_power: int,
        stir_pwm: int,
        expected_led_power: int,
        n_samples: int = 5,
    ) -> dict:
        """Open a blank session for the loaded CREATED experiment, enforcing
        the §13 condition-match guard: a blank at a different LED power or
        stir PWM than the run is not a blank."""
        with self._lock:
            if engine_status != "created":
                raise CalibrationConflict(
                    "the OD blank is taken against a CREATED experiment, "
                    f"immediately before start; engine status is '{engine_status}'"
                )
            params = (config or {}).get("parameters", {})
            run_stir = int(params.get("stir_rate", 10))
            if int(stir_pwm) != run_stir:
                raise ValueError(
                    f"stir_pwm {stir_pwm} != the run's stir_rate {run_stir} — "
                    "a blank taken at a different stir PWM is not a blank "
                    "(Principle 1)"
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
            temp_param = params.get("temperature_c", params.get("temperature", 37.0))
            if isinstance(temp_param, (list, tuple)):
                targets = {v: float(temp_param[v]) for v in vials}
            else:
                targets = {v: float(temp_param) for v in vials}
            self._blank = OdBlankSession(
                experiment=experiment,
                vials=vials,
                led_power=int(led_power),
                stir_pwm=int(stir_pwm),
                targets_c=targets,
                n_samples=int(n_samples),
            )
            return {
                "session": self._blank.id,
                "experiment": experiment,
                "vials": vials,
                "n_samples": self._blank.n_samples,
                "thermal": self.tracker.settled(vials, targets),
            }

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
            stats = self.manager.collect_od_raw(0, n_samples=s.n_samples)
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

    def _previous_blank(self, excluding_experiment: str) -> Optional[dict]:
        """Most recent od_blank.json across experiments/ (the campaign
        reference for the §5.4 drift checks), excluding the current one."""
        latest: Optional[tuple[str, dict]] = None
        if not self.experiments_root.is_dir():
            return None
        for path in self.experiments_root.glob("*/od_blank.json"):
            if path.parent.name == excluding_experiment:
                continue
            try:
                env = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
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

            od_env = self.store.get_current("od")
            if od_env is None:
                raise CalibrationConflict("no OD calibration installed")
            od_cal = np.asarray(od_env["data"]["rows"], dtype=float)

            blank_median = np.asarray(s.blank["median"], dtype=float)
            # Domain guard evaluated per included vial (excluded vials may
            # be NaN or out of domain without blocking the rest).
            a, b, _c, d = od_cal
            domain_bad = [
                v for v in vials
                if not (a[v] < blank_median[v] < b[v])
            ]
            if domain_bad:
                raise ValueError(
                    f"blank signal outside the calibration domain (a, b) on "
                    f"vials {domain_bad} — reseat/repeat or exclude them"
                )

            # Re-anchor c for the included vials only.
            with np.errstate(invalid="ignore", divide="ignore"):
                c_run_all = np.log10(
                    (b - a) / (blank_median - a) - 1.0
                ) / d
            c_run = {v: float(c_run_all[v]) for v in vials}
            od_offset_removed = {
                v: float(od_cal[2, v] - c_run_all[v]) for v in vials
            }

            # §5.4 acceptance criteria -> qc block.
            failures: list[str] = []
            warnings: list[str] = []
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
            previous = self._previous_blank(s.experiment)
            reference = None
            if previous is not None:
                reference = previous.get("version")
                prev_blank = previous.get("data", {}).get("blank_median")
                prev_c_run = previous.get("fit", {}).get("c_run", {})
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
                    "stir_pwm": s.stir_pwm,
                    "target_temp_c": {str(k): v for k, v in s.targets_c.items()},
                    "parent_od_cal": od_env.get("version"),
                    "dark_subtracted": False,
                },
                data={
                    "dark_median": s.dark["median"],
                    "dark_sd": s.dark["sd"],
                    "blank_median": s.blank["median"],
                    "blank_sd": s.blank["sd"],
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

    # -- pump gravimetric (O3) -------------------------------------------

    def pump_start(self, body: dict) -> dict:
        with self._lock:
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
