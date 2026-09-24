"""server/data_logger.py — per-experiment CSV file management (SPEC §8).

One experiment, one directory under ``experiments/{name}/``. Per active
vial, three append-only CSV files::

    config.json
    vial00_OD.csv          # timestamp,elapsed_hours,raw_adc,calibrated_od,n_valid,flag,dark
    vial00_temp.csv        # timestamp,elapsed_hours,raw_adc,calibrated_temp_c
    vial00_pump_log.csv    # timestamp,elapsed_hours,direction,duration_seconds,od_at_pump
    vial00_growth.csv      # SPEC §17 growth estimates, written at the engine's
                           # 60 s recompute cadence (not the 10 s sensor tick)
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
# SPEC §17 / GROWTH_RATE_METHOD.md §7.7 — a PARALLEL file, not new columns
# in vial{NN}_OD.csv. That file's header is load-bearing: data_export.py
# carries no version or schema marker at all, so any positional parser --
# including the lab's own analysis scripts -- would break silently on an
# inserted column.
#
# `flags` is PIPE-separated, not comma. events.csv and the export filters are
# read back with line/comma splitting (data_export.filter_rows_by_hours), so a
# quoted comma inside a field would corrupt every downstream reader; the same
# hazard is why `_one_line` exists below.
GROWTH_HEADER = (
    "timestamp",
    "elapsed_hours",
    "regime",
    "growth_rate_per_hour",
    "doubling_time_min",
    "r_squared",
    "windows_searched",
    "fit_span_s",
    "fit_od_start",
    "fit_od_end",
    "flags",
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

# SPEC §20.2 — the unified per-experiment event log. One row per discrete
# occurrence: lifecycle, pump fires AND suppressed pump attempts, alerts,
# maintenance transitions, refills, escalations, sensor and serial faults.
# `vial` is blank for machine-wide events; `data_json` carries the structured
# detail the message summarises.
EVENT_HEADER = (
    "timestamp",
    "elapsed_hours",
    "level",
    "category",
    "vial",
    "message",
    "data_json",
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


def _one_line(text) -> str:
    """Collapse newlines to spaces.

    events.csv is read back line-by-line (data_export._read_csv_lines,
    filter_rows_by_hours), so an embedded newline -- easy to get from an
    exception string -- would split one event across two rows and corrupt every
    downstream reader. Quoting alone would not save them.
    """
    return " ".join(str(text).split())


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


class _ActiveRun:
    """One experiment the logger is writing rows for."""

    __slots__ = ("name", "dir", "start", "vials")

    def __init__(self, name: str, directory: Path, start: datetime, vials: list[int]):
        self.name = name
        self.dir = directory
        self.start = start
        self.vials = list(vials)


class DataLogger:
    """Per-vial CSV writers for every ACTIVE experiment.

    Parallel experiments: several experiments can be active at once, each
    over its own vials. A row about a vial goes to the experiment that owns
    that vial; ``log_event`` routes by experiment name when it has one, then
    by vial owner, and otherwise -- a machine-wide event such as a bus fault
    or an emergency stop -- to every active experiment (PARALLEL_EXPERIMENTS.md
    T6). Two active experiments may never share a vial.
    """

    def __init__(self, experiments_root: Path) -> None:
        self.experiments_root = Path(experiments_root)
        self.experiments_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # name -> _ActiveRun, in activation order.
        self._active: dict[str, _ActiveRun] = {}
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
            return bool(self._active)

    @staticmethod
    def _describe(run: _ActiveRun) -> dict:
        return {
            "name": run.name,
            "directory": str(run.dir),
            "started": run.start.isoformat(timespec="seconds"),
            "vials": list(run.vials),
        }

    def active_experiment(self) -> Optional[dict]:
        """The first active experiment (the only one, before parallel runs)."""
        with self._lock:
            if not self._active:
                return None
            return self._describe(next(iter(self._active.values())))

    def active_experiments(self) -> list[dict]:
        with self._lock:
            return [self._describe(r) for r in self._active.values()]

    def _owner_locked(self, vial: int) -> Optional[_ActiveRun]:
        for run in self._active.values():
            if vial in run.vials:
                return run
        return None

    def list_experiments(self) -> list[dict]:
        """Return one entry per experiment directory on disk, with the
        currently-active ones marked ``status=running``."""
        with self._lock:
            active = set(self._active)
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
            if entry.name in active:
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
        groups: Optional[list] = None,
        operator: str = "",
        expected_end: Optional[str] = None,
    ) -> dict:
        """Create the experiment directory, write ``config.json``, and
        pre-create per-vial CSV files with their headers. Does NOT flip
        the logger into write mode — call :meth:`activate_experiment`
        for that. Returns the saved config dict.

        ``media`` is an opaque dict (see SPEC §8 / engine plan) round-tripped
        through ``config.json`` — DataLogger does not interpret it; the
        engine does. ``groups`` (vial groups, ``run_config.py``) is the same:
        opaque here, written only when the experiment has explicit groups, so
        a single-mode config is unchanged on disk.

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
        if groups:
            config["groups"] = groups
        # Parallel experiments: who is running this, and until when. Only
        # written when given, so a config from before them is unchanged.
        if operator:
            config["operator"] = str(operator)
        if expected_end:
            config["expected_end"] = str(expected_end)

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
            self._init_csv(exp_dir / f"vial{v:02d}_growth.csv", GROWTH_HEADER)
        # events.csv is machine-wide, not per-vial, so it lives beside them
        # rather than being one file per vial. Pre-created (rather than lazily
        # created on first write) so an export always finds it.
        self._init_csv(exp_dir / "events.csv", EVENT_HEADER)

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
            RuntimeError: this experiment is already active, or one of its
                vials is being logged by another active experiment.
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
            self._check_can_activate_locked(name, vials)
            run = _ActiveRun(name, exp_dir, start or datetime.now(timezone.utc), vials)
            self._active[name] = run
        log.info("experiment '%s' activated (start=%s)", name, run.start)
        return config

    def _check_can_activate_locked(self, name: str, vials: list[int]) -> None:
        if name in self._active:
            raise RuntimeError(f"experiment '{name}' is already activated")
        for other in self._active.values():
            shared = sorted(set(vials) & set(other.vials))
            if shared:
                raise RuntimeError(
                    f"vials {shared} are already being logged for experiment "
                    f"'{other.name}'; two experiments cannot share a vial"
                )

    def deactivate_experiment(self, name: Optional[str] = None) -> Optional[str]:
        """Stop writing rows for ``name``. The experiment directory and CSVs
        are left on disk. Returns the deactivated name (or None).

        ``name=None`` means the only active experiment -- the pre-parallel
        call shape. With several active it is ambiguous and raises."""
        with self._lock:
            if name is None:
                if not self._active:
                    return None
                if len(self._active) > 1:
                    raise ValueError(
                        "several experiments are active; name the one to "
                        f"deactivate ({sorted(self._active)})"
                    )
                name = next(iter(self._active))
            run = self._active.pop(name, None)
            if not self._active:
                self._latest_od = None
        if run is not None:
            log.info("experiment '%s' deactivated", name)
            return name
        return None

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
            self._check_can_activate_locked(name, sorted(set(vials)))
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

    def stop_experiment(self, name: Optional[str] = None) -> Optional[str]:
        """Alias for :meth:`deactivate_experiment` (back-compat)."""
        return self.deactivate_experiment(name)

    # ------------------------------------------------------------------
    # Logging — no-ops when no experiment is running
    # ------------------------------------------------------------------

    def log_sensor_cycle(
        self,
        timestamp_iso: str,
        temperature_calibrated: list,
        temperature_raw: list,
        od_calibrated: Optional[list] = None,
        od_raw: Optional[list] = None,
        od_n_valid: Optional[list] = None,
        od_flags: Optional[list] = None,
        od_dark: Optional[list] = None,
    ) -> None:
        """Append one temperature row per active vial, and one OD row when
        OD was actually acquired this cycle.

        The sensor loop can read temperature more often than OD (``app.py``
        OD_EVERY_N_TICKS; currently 1, so both are on the same 10 s tick and
        the two files gain rows together). If that lane is ever decimated,
        **the two files legitimately have different row counts** -- pass
        ``od_calibrated=None`` for a temperature-only tick and only the
        temperature row is written. That is safe because ``vialNN_temp.csv``
        and
        ``vialNN_OD.csv`` are separate files, each carrying its own
        timestamp and elapsed-hours column, and every reader
        (``data_export``, ``get_data``, the lab's own scripts) opens them
        independently. Nothing joins them positionally by row index.

        No column is added to or removed from either file. ``data_export``
        has no schema marker, so a positional parser breaks silently on an
        inserted column (CLAUDE.md fact 6) -- changing the row *rate* is
        invisible to such a parser, changing the row *shape* is not.

        ``od_n_valid`` / ``od_flags`` / ``od_dark`` are the optional
        enhanced-acquisition diagnostics; when omitted (naive read) the
        corresponding CSV columns are left blank.

        File writes are serialized via ``self._lock`` so concurrent callers
        cannot corrupt CSV rows (Windows lacks POSIX O_APPEND atomic-write
        semantics for multi-handle scenarios)."""
        with self._lock:
            if not self._active:
                return
            has_od = od_calibrated is not None
            if has_od and od_raw is None:
                raise ValueError("'od_raw' is required when 'od_calibrated' is given")
            required = [
                (temperature_calibrated, "temperature_calibrated"),
                (temperature_raw, "temperature_raw"),
            ]
            if has_od:
                required += [(od_calibrated, "od_calibrated"), (od_raw, "od_raw")]
            for series, label in required:
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
            if has_od:
                # Only refreshed on an OD tick, so this stays the last
                # ACTUALLY measured OD rather than being reset by the five
                # temperature-only ticks between acquisitions.
                self._latest_od = [
                    float(x) if not _is_nan(x) else float("nan")
                    for x in od_calibrated
                ]

            for run in self._active.values():
                self._write_sensor_rows_locked(
                    run, timestamp_iso, temperature_calibrated, temperature_raw,
                    od_calibrated, od_raw, od_n_valid, od_flags, od_dark,
                )

    def _write_sensor_rows_locked(
        self, run: _ActiveRun, timestamp_iso: str,
        temperature_calibrated, temperature_raw,
        od_calibrated, od_raw, od_n_valid, od_flags, od_dark,
    ) -> None:
        """One temperature row (and one OD row on an OD tick) per vial of
        ``run``, stamped with ``run``'s own elapsed hours."""
        has_od = od_calibrated is not None
        exp_dir = run.dir
        elapsed_str = f"{self._elapsed_hours(run, timestamp_iso):.4f}"
        for v in run.vials:
            self._append_row(
                exp_dir / f"vial{v:02d}_temp.csv",
                [
                    timestamp_iso,
                    elapsed_str,
                    _format_int(temperature_raw[v]),
                    _format_number(temperature_calibrated[v], 4),
                ],
            )
            if not has_od:
                continue
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
            run = self._owner_locked(vial)
            if run is None:
                return
            exp_dir = run.dir
            elapsed_h = self._elapsed_hours(run, timestamp_iso)
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

    def log_growth(
        self,
        timestamp_iso: str,
        vial: int,
        report,
    ) -> None:
        """Append one growth estimate for ``vial`` to its growth CSV.

        ``report`` is a ``growth_rate.GrowthReport``. Taken as an opaque
        object rather than imported, so ``data_logger`` keeps no dependency on
        the estimator.

        Written at the engine's recompute cadence (60 s), not the 10 s sensor
        tick: the estimate does not change meaningfully in one cycle, and this
        keeps the file roughly a sixth the size of the OD file.

        A row is written even when ``mu_per_hour`` is ``None`` -- the flags
        column is then the record of *why* nothing was estimable, which is the
        part an operator actually needs when a run reports no growth.
        """
        with self._lock:
            run = self._owner_locked(vial)
            if run is None:
                return
            exp_dir = run.dir
            elapsed_h = self._elapsed_hours(run, timestamp_iso)
            est = report.growth
            path = exp_dir / f"vial{vial:02d}_growth.csv"
            self._append_row(
                path,
                [
                    timestamp_iso,
                    f"{elapsed_h:.4f}",
                    _one_line(report.regime),
                    _format_number(est.mu_per_hour, 5),
                    _format_number(est.doubling_time_min, 2),
                    _format_number(est.r_squared, 5),
                    _format_int(est.windows_searched),
                    _format_number(est.span_seconds, 1),
                    _format_number(est.window_start_od, 4),
                    _format_number(est.window_end_od, 4),
                    # Pipe-separated: a comma here would split one estimate
                    # across two columns for every line-based reader.
                    "|".join(est.flags),
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
            run = self._owner_locked(vial)
            if run is None:
                return
            exp_dir = run.dir
            elapsed_h = self._elapsed_hours(run, timestamp_iso)
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

    def log_event(
        self,
        timestamp_iso: str,
        level: str,
        category: str,
        message: str,
        vial: Optional[int] = None,
        data: Optional[dict] = None,
        experiment: Optional[str] = None,
    ) -> bool:
        """Append one row to ``events.csv`` (SPEC §20.2), for the right runs.

        Routing (PARALLEL_EXPERIMENTS.md T6), first match wins:

        1. ``experiment`` names an active experiment -> that one only. Every
           engine alert and event carries its experiment's name.
        2. ``vial`` belongs to an active experiment -> its owner.
        3. Otherwise -- a machine-wide event (bus, disk, watchdog, emergency
           stop, calibration install) or one about a vial no run owns (a
           manual pump on a free sleeve) -- every active experiment, because
           it is part of each run's story.

        Returns True if any row was written, False if skipped (nothing active).

        ``data`` is serialised to the ``data_json`` column; an unserialisable
        value degrades to its ``repr`` rather than losing the whole row.
        """
        with self._lock:
            if not self._active:
                return False
            if experiment is not None and experiment in self._active:
                targets = [self._active[experiment]]
            else:
                owner = self._owner_locked(int(vial)) if vial is not None else None
                targets = [owner] if owner is not None else list(self._active.values())
            if data:
                try:
                    data_json = json.dumps(data, sort_keys=True, default=str)
                except (TypeError, ValueError):
                    data_json = json.dumps({"repr": repr(data)})
            else:
                data_json = ""
            for run in targets:
                path = run.dir / "events.csv"
                if not path.exists():
                    self._init_csv(path, EVENT_HEADER)
                self._append_row(
                    path,
                    [
                        timestamp_iso,
                        f"{self._elapsed_hours(run, timestamp_iso):.4f}",
                        str(level),
                        str(category),
                        "" if vial is None else int(vial),
                        _one_line(message),
                        data_json,
                    ],
                )
            return True

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

    @staticmethod
    def _elapsed_hours(run: _ActiveRun, timestamp_iso: str) -> float:
        """Hours since ``run`` started -- each experiment keeps its own clock."""
        try:
            now = _parse_iso(timestamp_iso)
        except Exception:
            now = datetime.now(timezone.utc)
        return max(0.0, (now - run.start).total_seconds() / 3600.0)

    @staticmethod
    def _init_csv(path: Path, header: Iterable[str]) -> None:
        with path.open("w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow(list(header))

    @staticmethod
    def _append_row(path: Path, row: Iterable) -> None:
        with path.open("a", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow(list(row))
