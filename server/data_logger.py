"""server/data_logger.py — per-experiment CSV file management (SPEC §8).

One experiment, one directory under ``experiments/{name}/``. Per active
vial, three append-only CSV files::

    config.json
    vial00_OD.csv          # timestamp,elapsed_hours,raw_adc,calibrated_od,n_valid,flag,dark
    vial00_temp.csv        # timestamp,elapsed_hours,raw_adc,calibrated_temp_c
    vial00_pump_log.csv    # timestamp,elapsed_hours,direction,duration_seconds,od_at_pump
    ...

State machine::

    idle  --start_experiment-->  running  --stop_experiment-->  idle

While idle, the ``log_*`` methods are no-ops. While running, they append
to disk for vials in the active experiment's vial list. The logger is
safe for concurrent calls; file writes happen outside the state lock so
a long fsync on one vial does not block another caller.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


N_VIALS = 16

# raw_adc is the aggregated, dark-subtracted signal fed to the calibration;
# n_valid/flag/dark are the enhanced-acquisition diagnostics (blank when a
# cycle used the naive single read, e.g. idle standby).
OD_HEADER = (
    "timestamp",
    "elapsed_hours",
    "raw_adc",
    "calibrated_od",
    "n_valid",
    "flag",
    "dark",
)
TEMP_HEADER = ("timestamp", "elapsed_hours", "raw_adc", "calibrated_temp_c")
PUMP_HEADER = (
    "timestamp",
    "elapsed_hours",
    "direction",
    "duration_seconds",
    "od_at_pump",
)
ESCALATION_HEADER = (
    "timestamp",
    "elapsed_hours",
    "vial",
    "old_drug_conc",
    "proposed_new_drug_conc",
    "growth_rate_per_hour",
    "confirmed_time",
    "confirmed_drug_conc",
    "bottle_contents_after",
)

# Constrained to keep filesystem paths sane and to avoid path traversal.
_VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")

log = logging.getLogger(__name__)


def _parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 timestamp; tolerate trailing 'Z'."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _is_nan(x) -> bool:
    try:
        return x != x  # NaN is the only value that is not equal to itself
    except TypeError:
        return False


def _format_number(x, precision: int) -> str:
    """Compact float formatting; empty string for None/NaN."""
    if x is None or _is_nan(x):
        return ""
    return f"{float(x):.{precision}f}"


def _format_int(x) -> str:
    if x is None or _is_nan(x):
        return ""
    return str(int(round(float(x))))


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursive merge: dict values are merged key-by-key; everything
    else (lists, scalars) is replaced wholesale."""
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class DataLogger:
    def __init__(self, experiments_root: Path) -> None:
        self.experiments_root = Path(experiments_root)
        self.experiments_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._active_name: Optional[str] = None
        self._active_dir: Optional[Path] = None
        self._active_start: Optional[datetime] = None
        self._active_vials: Optional[list[int]] = None
        # Last calibrated OD per vial, refreshed each log_sensor_cycle. Used
        # to populate od_at_pump when the caller doesn't supply it (e.g.
        # manual pump commands from the dashboard).
        self._latest_od: Optional[list[float]] = None

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._active_name is not None

    def active_experiment(self) -> Optional[dict]:
        with self._lock:
            if self._active_name is None:
                return None
            return {
                "name": self._active_name,
                "directory": str(self._active_dir),
                "started": self._active_start.isoformat(timespec="seconds"),
                "vials": list(self._active_vials),
            }

    def list_experiments(self) -> list[dict]:
        """Return one entry per experiment directory on disk, with the
        currently-active one (if any) marked ``status=running``."""
        with self._lock:
            active = self._active_name
        results: list[dict] = []
        if not self.experiments_root.exists():
            return results
        for entry in sorted(self.experiments_root.iterdir()):
            if not entry.is_dir():
                continue
            info: dict = {"name": entry.name, "status": "stopped"}
            config_path = entry / "config.json"
            if config_path.exists():
                try:
                    info["config"] = json.loads(
                        config_path.read_text(encoding="utf-8")
                    )
                except Exception:
                    log.exception("failed to parse %s", config_path)
            if entry.name == active:
                info["status"] = "running"
            results.append(info)
        return results

    # ------------------------------------------------------------------
    # Lifecycle  (create -> activate -> deactivate)
    # ------------------------------------------------------------------
    # The ExperimentEngine (SPEC §9) wants a CREATED state between "config
    # exists" and "control loop running" so the dashboard can show a
    # parameter summary before launching. `create_experiment` materialises
    # the directory and headers; `activate_experiment` flips the logger
    # into write mode. `start_experiment` below remains as a convenience
    # wrapper for the simple (test) flow that wants both at once.

    def create_experiment(
        self,
        name: str,
        mode: str,
        vials: list[int],
        parameters: Optional[dict] = None,
        calibration: Optional[dict] = None,
        notes: str = "",
        media: Optional[dict] = None,
    ) -> dict:
        """Create the experiment directory, write ``config.json``, and
        pre-create per-vial CSV files with their headers. Does NOT flip
        the logger into write mode — call :meth:`activate_experiment`
        for that. Returns the saved config dict.

        ``media`` is an opaque dict (see SPEC §8 / engine plan) round-tripped
        through ``config.json`` — DataLogger does not interpret it; the
        engine does.

        Raises:
            ValueError: name / vials are malformed.
            FileExistsError: directory ``experiments_root/name`` already exists.
        """
        if not isinstance(name, str) or not _VALID_NAME.match(name):
            raise ValueError(
                f"experiment name must match {_VALID_NAME.pattern!r}; got {name!r}"
            )
        if not isinstance(vials, list) or not vials:
            raise ValueError("'vials' must be a non-empty list of ints in 0..15")
        for v in vials:
            if isinstance(v, bool) or not isinstance(v, int) or not (0 <= v < N_VIALS):
                raise ValueError(
                    f"invalid vial number {v!r}; expected int in 0..{N_VIALS - 1}"
                )
        vials_sorted = sorted(set(vials))
        if len(vials_sorted) != len(vials):
            raise ValueError("duplicate vial numbers in 'vials'")

        created = datetime.now(timezone.utc)
        config = {
            "name": name,
            "created": created.isoformat(timespec="seconds"),
            "mode": str(mode),
            "vials": vials_sorted,
            "parameters": parameters or {},
            "calibration": calibration or {},
            "notes": notes or "",
        }
        if media is not None:
            config["media"] = media

        exp_dir = self.experiments_root / name
        if exp_dir.exists():
            raise FileExistsError(
                f"experiment directory already exists: {exp_dir}"
            )
        exp_dir.mkdir(parents=True)
        (exp_dir / "config.json").write_text(
            json.dumps(config, indent=4), encoding="utf-8"
        )
        for v in vials_sorted:
            self._init_csv(exp_dir / f"vial{v:02d}_OD.csv", OD_HEADER)
            self._init_csv(exp_dir / f"vial{v:02d}_temp.csv", TEMP_HEADER)
            self._init_csv(exp_dir / f"vial{v:02d}_pump_log.csv", PUMP_HEADER)

        log.info(
            "experiment '%s' created in %s (vials=%s)",
            name,
            exp_dir,
            vials_sorted,
        )
        return config

    def activate_experiment(
        self,
        name: str,
        *,
        start: Optional[datetime] = None,
    ) -> dict:
        """Flip the logger into write mode for an already-created experiment.

        ``start`` defaults to ``now()``; the engine passes the original
        ``started`` timestamp from ``state.json`` on a resume so the
        ``elapsed_hours`` column stays continuous across server restarts.

        Raises:
            FileNotFoundError: directory or config.json missing.
            RuntimeError: another experiment is already activated.
        """
        exp_dir = self.experiments_root / name
        config_path = exp_dir / "config.json"
        if not exp_dir.is_dir() or not config_path.is_file():
            raise FileNotFoundError(
                f"experiment '{name}' has not been created (missing {config_path})"
            )
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"failed to read {config_path}: {exc}") from exc
        vials = sorted(int(v) for v in config.get("vials", []))

        with self._lock:
            if self._active_name is not None:
                raise RuntimeError(
                    f"experiment '{self._active_name}' is already activated; "
                    "deactivate it first"
                )
            self._active_name = name
            self._active_dir = exp_dir
            self._active_start = start or datetime.now(timezone.utc)
            self._active_vials = vials
            self._latest_od = None
        log.info("experiment '%s' activated (start=%s)", name, self._active_start)
        return config

    def deactivate_experiment(self) -> Optional[str]:
        """Stop writing rows. The experiment directory and CSVs are left
        on disk. Returns the previously-active experiment name (or None)."""
        with self._lock:
            name = self._active_name
            self._active_name = None
            self._active_dir = None
            self._active_start = None
            self._active_vials = None
            self._latest_od = None
        if name is not None:
            log.info("experiment '%s' deactivated", name)
        return name

    def start_experiment(
        self,
        name: str,
        mode: str,
        vials: list[int],
        parameters: Optional[dict] = None,
        calibration: Optional[dict] = None,
        notes: str = "",
    ) -> dict:
        """Convenience wrapper: create_experiment + activate_experiment.

        Preserved so existing tests and any caller that just wants
        "create and start logging right now" stays one call. The
        ExperimentEngine uses :meth:`create_experiment` and
        :meth:`activate_experiment` separately so the CREATED → RUNNING
        transition has a real seam.
        """
        # Pre-check so we don't leave a partial directory on disk if
        # another experiment is already activated. Without this, the
        # `create_experiment` call below would succeed (it doesn't gate
        # on active state) and only `activate_experiment` would raise —
        # at which point the new directory is already on disk and the
        # next call to start_experiment fails with FileExistsError.
        with self._lock:
            if self._active_name is not None:
                raise RuntimeError(
                    f"experiment '{self._active_name}' is already activated; "
                    "deactivate it first"
                )
        config = self.create_experiment(
            name=name,
            mode=mode,
            vials=vials,
            parameters=parameters,
            calibration=calibration,
            notes=notes,
        )
        self.activate_experiment(name)
        return config

    def stop_experiment(self) -> Optional[str]:
        """Alias for :meth:`deactivate_experiment` (back-compat)."""
        return self.deactivate_experiment()

    # ------------------------------------------------------------------
    # Logging — no-ops when no experiment is running
    # ------------------------------------------------------------------

    def log_sensor_cycle(
        self,
        timestamp_iso: str,
        temperature_calibrated: list,
        temperature_raw: list,
        od_calibrated: list,
        od_raw: list,
        od_n_valid: Optional[list] = None,
        od_flags: Optional[list] = None,
        od_dark: Optional[list] = None,
    ) -> None:
        """Append one OD row and one temperature row per active vial.

        ``od_n_valid`` / ``od_flags`` / ``od_dark`` are the optional
        enhanced-acquisition diagnostics; when omitted (naive read) the
        corresponding CSV columns are left blank.

        File writes are serialized via ``self._lock`` so concurrent callers
        cannot corrupt CSV rows (Windows lacks POSIX O_APPEND atomic-write
        semantics for multi-handle scenarios)."""
        with self._lock:
            if self._active_name is None:
                return
            for series, label in (
                (temperature_calibrated, "temperature_calibrated"),
                (temperature_raw, "temperature_raw"),
                (od_calibrated, "od_calibrated"),
                (od_raw, "od_raw"),
            ):
                if len(series) != N_VIALS:
                    raise ValueError(
                        f"'{label}' must have {N_VIALS} entries, got {len(series)}"
                    )
            for series, label in (
                (od_n_valid, "od_n_valid"),
                (od_flags, "od_flags"),
                (od_dark, "od_dark"),
            ):
                if series is not None and len(series) != N_VIALS:
                    raise ValueError(
                        f"'{label}' must have {N_VIALS} entries, got {len(series)}"
                    )
            exp_dir = self._active_dir
            vials = list(self._active_vials)
            elapsed_h = self._elapsed_hours_locked(timestamp_iso)
            self._latest_od = [
                float(x) if not _is_nan(x) else float("nan") for x in od_calibrated
            ]

            elapsed_str = f"{elapsed_h:.4f}"
            for v in vials:
                self._append_row(
                    exp_dir / f"vial{v:02d}_temp.csv",
                    [
                        timestamp_iso,
                        elapsed_str,
                        _format_int(temperature_raw[v]),
                        _format_number(temperature_calibrated[v], 4),
                    ],
                )
                self._append_row(
                    exp_dir / f"vial{v:02d}_OD.csv",
                    [
                        timestamp_iso,
                        elapsed_str,
                        _format_int(od_raw[v]),
                        _format_number(od_calibrated[v], 4),
                        _format_int(od_n_valid[v]) if od_n_valid is not None else "",
                        (od_flags[v] if od_flags is not None else "") or "",
                        _format_int(od_dark[v]) if od_dark is not None else "",
                    ],
                )

    def log_pump_event(
        self,
        timestamp_iso: str,
        vial: int,
        direction: str,
        duration_seconds: float,
        od_at_pump: Optional[float] = None,
    ) -> None:
        """Append a pump event for ``vial`` to its pump log CSV.

        Silently skipped if no experiment is running or if ``vial`` is
        not part of the active experiment (e.g. a manual pump of a
        non-experiment vial — fire the pump, just don't log it here)."""
        if direction not in ("influx", "efflux"):
            raise ValueError(
                f"direction must be 'influx' or 'efflux'; got {direction!r}"
            )
        with self._lock:
            if self._active_name is None:
                return
            if vial not in self._active_vials:
                return
            exp_dir = self._active_dir
            elapsed_h = self._elapsed_hours_locked(timestamp_iso)
            if od_at_pump is None and self._latest_od is not None:
                od_at_pump = self._latest_od[vial]
            path = exp_dir / f"vial{vial:02d}_pump_log.csv"
            self._append_row(
                path,
                [
                    timestamp_iso,
                    f"{elapsed_h:.4f}",
                    direction,
                    _format_number(duration_seconds, 2),
                    _format_number(od_at_pump, 4),
                ],
            )

    def log_escalation_event(
        self,
        timestamp_iso: str,
        vial: int,
        *,
        old_drug_conc: Optional[float] = None,
        proposed_new_drug_conc: Optional[float] = None,
        growth_rate_per_hour: Optional[float] = None,
        confirmed_time: Optional[str] = None,
        confirmed_drug_conc: Optional[float] = None,
        bottle_contents_after: Optional[str] = None,
    ) -> None:
        """Append one row to ``escalation_log.csv`` for the active experiment.

        Used twice per escalation cycle: once when the engine emits the
        proposal (proposal fields filled, confirmed_* blank) and once
        when the user confirms (confirmed_* filled, proposal fields
        blank). Both rows share ``vial`` so analysis can correlate by
        ``(vial, ordering)``.
        """
        with self._lock:
            if self._active_name is None:
                return
            if vial not in self._active_vials:
                return
            exp_dir = self._active_dir
            elapsed_h = self._elapsed_hours_locked(timestamp_iso)
            path = exp_dir / "escalation_log.csv"
            if not path.exists():
                self._init_csv(path, ESCALATION_HEADER)
            self._append_row(
                path,
                [
                    timestamp_iso,
                    f"{elapsed_h:.4f}",
                    int(vial),
                    _format_number(old_drug_conc, 4),
                    _format_number(proposed_new_drug_conc, 4),
                    _format_number(growth_rate_per_hour, 4),
                    confirmed_time or "",
                    _format_number(confirmed_drug_conc, 4),
                    bottle_contents_after or "",
                ],
            )

    def update_experiment_config(self, name: str, partial: dict) -> dict:
        """Deep-merge ``partial`` into the experiment's ``config.json``
        and write the result back atomically (tmp + rename).

        Returns the merged config dict. Works regardless of whether the
        experiment is the currently-active one — the engine calls this
        only for the running experiment, but the method itself is
        passive."""
        if not isinstance(partial, dict):
            raise TypeError("partial must be a dict")
        exp_dir = self.experiments_root / name
        config_path = exp_dir / "config.json"
        if not config_path.is_file():
            raise FileNotFoundError(f"missing {config_path}")
        with self._lock:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            merged = _deep_merge(config, partial)
            tmp = config_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(merged, indent=4), encoding="utf-8")
            tmp.replace(config_path)
        return merged

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _elapsed_hours_locked(self, timestamp_iso: str) -> float:
        try:
            now = _parse_iso(timestamp_iso)
        except Exception:
            now = datetime.now(timezone.utc)
        if self._active_start is None:
            return 0.0
        return max(0.0, (now - self._active_start).total_seconds() / 3600.0)

    @staticmethod
    def _init_csv(path: Path, header: Iterable[str]) -> None:
        with path.open("w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow(list(header))

    @staticmethod
    def _append_row(path: Path, row: Iterable) -> None:
        with path.open("a", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow(list(row))
