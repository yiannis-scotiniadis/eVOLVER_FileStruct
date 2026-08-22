"""server/data_export.py — read-only export + storage reporting (SPEC §8).

Pure functions over an experiment directory on disk. Nothing here touches the
RS485 bus, the ExperimentEngine's in-memory state, or the serial manager, so an
export is safe to run while an experiment is RUNNING (the per-vial CSVs are
append-only, so a snapshot read is internally consistent) and works equally for
stopped or merely on-disk experiments.

Export shapes (chosen with the user):

  * OD / temperature -> one WIDE CSV per parameter, CALIBRATED values only::

        timestamp,elapsed_hours,vial00,vial01,vial05
        2026-05-12T14:30:00,0.0000,0.012,0.041,
        ...

    A blank cell is a NaN / dropped read (stored blank by the DataLogger), so
    sensor gaps survive into the export rather than being filled.

  * Pump events -> one LONG CSV (irregular events don't fit a wide layout),
    selected vials merged and time-sorted, with a ``vial`` column added::

        timestamp,elapsed_hours,vial,direction,duration_seconds,od_at_pump

A "bundle" is what a single export call produces: exactly one selected CSV is
returned bare; two or more are zipped together with ``config.json`` (provenance)
and a generated ``export_manifest.json``.

The ``hours`` time-window filter shares :func:`filter_rows_by_hours` with
:meth:`experiment_engine.ExperimentEngine.get_data` so the two stay identical.
"""

from __future__ import annotations

import csv
import io
import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

N_VIALS = 16

# Parameters this module knows how to export and the calibrated value column in
# each per-vial CSV (DataLogger.OD_HEADER / TEMP_HEADER).
_WIDE_VALUE_COL = {"od": "calibrated_od", "temp": "calibrated_temp_c"}
EXPORT_PARAMETERS = ("od", "temp", "pump")


# ---------------------------------------------------------------------------
# Shared time-window filter (also used by ExperimentEngine.get_data)
# ---------------------------------------------------------------------------

def filter_rows_by_hours(
    data_rows: list[str], elapsed_col: int, hours: Optional[float]
) -> list[str]:
    """Keep only CSV data rows within the last ``hours`` of available data.

    The cutoff is measured from the LAST row's ``elapsed_hours`` value (not
    wall-clock), so the window also works for stopped experiments. ``data_rows``
    are raw CSV line strings (header already stripped). Rows whose
    ``elapsed_hours`` cell is unparseable are kept (fail-open).

    Returns ``data_rows`` unchanged when ``hours`` is None or the list is empty.
    Shared with ``ExperimentEngine.get_data`` so the window semantics can't
    drift between the live-plot path and the export path.
    """
    if hours is None or not data_rows:
        return data_rows
    last_elapsed: Optional[float] = None
    for line in reversed(data_rows):
        parts = line.split(",")
        if len(parts) > elapsed_col:
            try:
                last_elapsed = float(parts[elapsed_col])
                break
            except ValueError:
                continue
    if last_elapsed is None:
        return data_rows
    cutoff = last_elapsed - hours
    kept: list[str] = []
    for line in data_rows:
        parts = line.split(",")
        try:
            if float(parts[elapsed_col]) >= cutoff:
                kept.append(line)
        except (ValueError, IndexError):
            kept.append(line)  # keep unparseable rows
    return kept


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_csv_lines(path: Path) -> tuple[list[str], list[str]]:
    """Return ``(header_fields, data_lines)`` for a CSV; ``([], [])`` if the
    file is missing or empty."""
    if not path.is_file():
        return [], []
    with path.open("r", encoding="utf-8", newline="") as f:
        lines = f.read().splitlines()
    if not lines:
        return [], []
    return lines[0].split(","), lines[1:]


def _vial_csv_name(vial: int, parameter: str) -> str:
    if parameter == "od":
        return f"vial{vial:02d}_OD.csv"
    if parameter == "temp":
        return f"vial{vial:02d}_temp.csv"
    if parameter == "pump":
        return f"vial{vial:02d}_pump_log.csv"
    raise ValueError(f"unknown parameter {parameter!r}")


def _elapsed_sort_key(elapsed_cell: str) -> tuple[int, float]:
    """Sort key for a row's elapsed_hours cell; unparseable cells sink to the
    end so a malformed row never reorders good data."""
    try:
        return (0, float(elapsed_cell))
    except (ValueError, TypeError):
        return (1, 0.0)


def _normalize_vials(vials: Iterable[int]) -> list[int]:
    out = sorted({int(v) for v in vials})
    for v in out:
        if not (0 <= v < N_VIALS):
            raise ValueError(f"vial {v} out of range 0..{N_VIALS - 1}")
    return out


# ---------------------------------------------------------------------------
# Wide CSV (OD / temperature)
# ---------------------------------------------------------------------------

def wide_csv(
    exp_dir: Path,
    parameter: str,
    vials: Iterable[int],
    hours: Optional[float] = None,
) -> str:
    """Build a wide, calibrated-only CSV for ``parameter`` ('od' or 'temp').

    Columns: ``timestamp, elapsed_hours, vialNN, ...`` (one per selected vial,
    sorted ascending). Rows are joined across vials on ``timestamp`` — the
    DataLogger writes every active vial with the same timestamp each cycle, so
    rows line up — and emitted in ascending ``elapsed_hours`` order. A vial with
    no data (or a dropped read) contributes a blank cell.
    """
    if parameter not in _WIDE_VALUE_COL:
        raise ValueError(f"wide_csv parameter must be 'od' or 'temp', got {parameter!r}")
    value_name = _WIDE_VALUE_COL[parameter]
    vials = _normalize_vials(vials)

    elapsed_by_ts: dict[str, str] = {}     # timestamp -> elapsed_hours cell
    value_by_vial: dict[int, dict[str, str]] = {v: {} for v in vials}

    for v in vials:
        header, data_rows = _read_csv_lines(exp_dir / _vial_csv_name(v, parameter))
        if not header:
            continue
        try:
            elapsed_col = header.index("elapsed_hours")
        except ValueError:
            elapsed_col = 1
        try:
            val_col = header.index(value_name)
        except ValueError:
            val_col = 3
        data_rows = filter_rows_by_hours(data_rows, elapsed_col, hours)
        for line in data_rows:
            parts = line.split(",")
            ts = parts[0] if parts else ""
            elapsed = parts[elapsed_col] if len(parts) > elapsed_col else ""
            val = parts[val_col] if len(parts) > val_col else ""
            elapsed_by_ts.setdefault(ts, elapsed)
            value_by_vial[v][ts] = val

    order = sorted(elapsed_by_ts, key=lambda ts: (_elapsed_sort_key(elapsed_by_ts[ts]), ts))

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["timestamp", "elapsed_hours"] + [f"vial{v:02d}" for v in vials])
    for ts in order:
        w.writerow([ts, elapsed_by_ts[ts]] + [value_by_vial[v].get(ts, "") for v in vials])
    return out.getvalue()


# ---------------------------------------------------------------------------
# Pump events (long CSV)
# ---------------------------------------------------------------------------

PUMP_EXPORT_HEADER = (
    "timestamp",
    "elapsed_hours",
    "vial",
    "direction",
    "duration_seconds",
    "od_at_pump",
)


def pump_events_csv(
    exp_dir: Path,
    vials: Iterable[int],
    hours: Optional[float] = None,
) -> str:
    """Merge the per-vial pump logs for ``vials`` into one long CSV with a
    ``vial`` column, sorted by timestamp (ISO-8601 UTC sorts chronologically)."""
    vials = _normalize_vials(vials)
    merged: list[list] = []
    for v in vials:
        header, data_rows = _read_csv_lines(exp_dir / _vial_csv_name(v, "pump"))
        if not header:
            continue
        idx = {name: i for i, name in enumerate(header)}
        elapsed_col = idx.get("elapsed_hours", 1)
        data_rows = filter_rows_by_hours(data_rows, elapsed_col, hours)
        for line in data_rows:
            parts = line.split(",")

            def cell(col_name: str, default_i: int) -> str:
                i = idx.get(col_name, default_i)
                return parts[i] if i < len(parts) else ""

            merged.append([
                cell("timestamp", 0),
                cell("elapsed_hours", 1),
                v,
                cell("direction", 2),
                cell("duration_seconds", 3),
                cell("od_at_pump", 4),
            ])

    merged.sort(key=lambda row: row[0])  # stable; ties keep per-vial file order

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(list(PUMP_EXPORT_HEADER))
    for row in merged:
        w.writerow(row)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------

def _row_count(csv_text: str) -> int:
    """Data rows (excluding the header) in a CSV string produced here."""
    return max(0, csv_text.count("\n") - 1)


def events_csv(exp_dir: Path, hours: Optional[float] = None) -> Optional[str]:
    """The experiment's ``events.csv`` (SPEC §20.2), optionally windowed to the
    last ``hours`` of data. Returns None when the experiment predates the event
    log or has not written one yet.

    Not a member of ``EXPORT_PARAMETERS``: od/temp/pump are per-vial data
    selections, whereas the event log is provenance that ships with every
    bundle regardless of what was selected.
    """
    header, data_rows = _read_csv_lines(exp_dir / "events.csv")
    if not header:
        return None
    try:
        elapsed_col = header.index("elapsed_hours")
    except ValueError:
        elapsed_col = 1
    data_rows = filter_rows_by_hours(data_rows, elapsed_col, hours)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(header)
    for line in data_rows:
        w.writerow(next(csv.reader([line])))
    return out.getvalue()


def build_bundle(
    exp_dir: Path,
    *,
    name: str,
    vials: Iterable[int],
    parameters: Iterable[str],
    hours: Optional[float] = None,
) -> tuple[str, bytes]:
    """Produce the export for one request.

    Returns ``(filename, content_bytes)``. With exactly one selected parameter
    the bare CSV is returned (``{name}_OD.csv`` etc.); with two or more, a
    ``{name}_export.zip`` containing every CSV plus ``config.json`` (provenance)
    and a generated ``export_manifest.json``.

    Raises ``ValueError`` if ``parameters`` is empty or contains an unknown
    value, or if a vial is out of range.
    """
    requested = [p for p in parameters]
    if not requested:
        raise ValueError("at least one parameter (od/temp/pump) must be selected")
    for p in requested:
        if p not in EXPORT_PARAMETERS:
            raise ValueError(f"unknown parameter {p!r}; expected one of {EXPORT_PARAMETERS}")
    vials = _normalize_vials(vials)

    files: dict[str, str] = {}
    if "od" in requested:
        files[f"{name}_OD.csv"] = wide_csv(exp_dir, "od", vials, hours)
    if "temp" in requested:
        files[f"{name}_temp.csv"] = wide_csv(exp_dir, "temp", vials, hours)
    if "pump" in requested:
        files[f"{name}_pump_events.csv"] = pump_events_csv(exp_dir, vials, hours)

    if len(files) == 1:
        fname, text = next(iter(files.items()))
        return fname, text.encode("utf-8")

    events_text = events_csv(exp_dir, hours)

    manifest = {
        "experiment": name,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "filters": {
            "vials": vials,
            "parameters": [p for p in EXPORT_PARAMETERS if p in requested],
            "hours": hours,
        },
        "files": {fn: {"data_rows": _row_count(text)} for fn, text in files.items()},
    }
    if events_text is not None:
        manifest["files"]["events.csv"] = {"data_rows": _row_count(events_text)}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fn, text in files.items():
            zf.writestr(fn, text)
        config_path = exp_dir / "config.json"
        if config_path.is_file():
            zf.writestr("config.json", config_path.read_text(encoding="utf-8"))
        if events_text is not None:
            zf.writestr("events.csv", events_text)
        zf.writestr("export_manifest.json", json.dumps(manifest, indent=2))
    return f"{name}_export.zip", buf.getvalue()


# ---------------------------------------------------------------------------
# Storage / disk usage
# ---------------------------------------------------------------------------

def experiment_disk_usage(exp_dir: Path) -> dict:
    """Sum file sizes (recursively) and count files under ``exp_dir``."""
    total = 0
    n_files = 0
    if exp_dir.exists():
        for p in exp_dir.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                    n_files += 1
                except OSError:
                    pass
    return {"bytes": total, "files": n_files}


def storage_report(
    experiments_root: Path, exports_dir: Optional[Path] = None
) -> dict:
    """Filesystem free space + per-experiment sizes + exports-dir size.

    ``experiments_root`` must exist (the DataLogger creates it at init).
    """
    usage = shutil.disk_usage(experiments_root)
    free_pct = 100.0 * usage.free / usage.total if usage.total else 0.0
    experiments: list[dict] = []
    if experiments_root.exists():
        for entry in sorted(experiments_root.iterdir()):
            if entry.is_dir():
                experiments.append({"name": entry.name, **experiment_disk_usage(entry)})
    exports: Optional[dict] = None
    if exports_dir is not None and exports_dir.exists():
        exports = experiment_disk_usage(exports_dir)
    return {
        "filesystem": {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "free_pct": round(free_pct, 2),
        },
        "experiments": experiments,
        "exports": exports,
    }
