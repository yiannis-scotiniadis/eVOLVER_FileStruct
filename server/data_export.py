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
from typing import Iterable, Iterator, Optional

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
# Bounded reads
# ---------------------------------------------------------------------------
#
# These exist because the per-vial CSVs are NOT small. A 7-day run at the 10 s
# sensor cadence is 60 480 rows and ~3.2 MB per vial; the dashboard's Plots tab
# asks for a window from every active vial at once (up to 48 concurrent
# requests), and the deployment target is a pre-2016 Raspberry Pi sharing one
# core between Flask, the 10 s control loop and the RS485 path. Reading a whole
# file to return 361 points measured 1.23 s per vial and grew linearly with run
# length.
#
# The files are append-only and ordered by `elapsed_hours`, so the tail IS the
# recent data and a bounded read from EOF is exact.

#: First tail chunk. One hour of 10 s-cadence OD rows is ~3.5 KB, so 64 KiB
#: covers most of a day before the first expansion.
_TAIL_CHUNK_BYTES = 64 * 1024

#: Stop doubling past this and just read the file; a request this wide is not
#: a tail request any more.
_TAIL_MAX_BYTES = 32 * 1024 * 1024


def read_header(path: Path) -> list[str]:
    """The CSV header fields, read without touching the rest of the file."""
    if not path.is_file():
        return []
    with path.open("rb") as f:
        raw = f.readline()
    if not raw:
        return []
    lines = raw.decode("utf-8", errors="replace").splitlines()
    return lines[0].split(",") if lines else []


def _elapsed_of(line: str, elapsed_col: int) -> Optional[float]:
    parts = line.split(",")
    if len(parts) <= elapsed_col:
        return None
    try:
        return float(parts[elapsed_col])
    except ValueError:
        return None


def _cutoff_from_last_row(
    rows: list[str], elapsed_col: int, hours: float
) -> Optional[float]:
    """The cutoff :func:`filter_rows_by_hours` will use: measured from the last
    parseable row, not from wall clock."""
    for line in reversed(rows):
        last = _elapsed_of(line, elapsed_col)
        if last is not None:
            return last - hours
    return None


def read_tail_rows(
    path: Path,
    *,
    hours: Optional[float] = None,
    last_n: Optional[int] = None,
    elapsed_col: int = 1,
) -> tuple[list[str], list[str]]:
    """``(header_fields, data_lines)``, reading only as much of the tail as the
    request can possibly need.

    **This is a prefilter, not the filter.** It guarantees the returned rows are
    a SUPERSET of what :func:`filter_rows_by_hours` would keep; the caller still
    runs that function to trim exactly. That is what makes the optimisation
    provably semantics-preserving rather than approximately so, and it keeps the
    window semantics living in exactly one place.

    The guarantee is met by expanding until the FIRST buffered row is strictly
    older than the cutoff -- the whole window plus a row of slack -- or until
    the start of the file is reached.

    With neither ``hours`` nor ``last_n`` there is no bound to exploit, so the
    whole file is read.

    Splitting is done with ``str.splitlines`` on the decoded chunk, which
    handles both line endings in play: ``DataLogger._append_row`` opens with
    ``newline=""`` and lets ``csv.writer`` emit CRLF, while the hand-written
    fixtures in ``test_get_data.py`` use bare LF.
    """
    if not path.is_file():
        return [], []
    if hours is None and last_n is None:
        return _read_csv_lines(path)

    size = path.stat().st_size
    want = _TAIL_CHUNK_BYTES
    header: list[str] = []
    rows: list[str] = []
    while True:
        at_bof = want >= size
        start = 0 if at_bof else size - want
        with path.open("rb") as f:
            if start:
                f.seek(start)
            blob = f.read()
        lines = blob.decode("utf-8", errors="replace").splitlines()
        if at_bof:
            header = lines[0].split(",") if lines else []
            lines = lines[1:]
        else:
            # The first line is almost certainly truncated mid-row.
            lines = lines[1:]
        rows = [r for r in lines if r]

        if at_bof or want >= _TAIL_MAX_BYTES:
            break
        if not rows:
            want *= 2
            continue
        if last_n is not None and len(rows) < last_n:
            want *= 2
            continue
        if hours is not None:
            cutoff = _cutoff_from_last_row(rows, elapsed_col, hours)
            first = _elapsed_of(rows[0], elapsed_col)
            # Expand until a row strictly older than the cutoff is in hand. An
            # unparseable first row cannot prove coverage -- filter_rows_by_hours
            # keeps such rows (fail-open), so expand past it.
            if cutoff is not None and (first is None or first >= cutoff):
                want *= 2
                continue
        break

    if not header:
        header = read_header(path)
    return header, rows


#: Read granularity for the streaming path. Big enough that `str.splitlines`
#: (C-level) does the work rather than Python-level line iteration, small
#: enough that sixteen concurrent cursors do not each sit on a chunk's worth of
#: split lines.
#:
#: Measured on a 3-day 16-vial bundle: 16K -> 2.05 s / 1.8 MB, **64K -> 1.36 s /
#: 5.1 MB**, 256K -> 1.48 s / 18.4 MB, 1M -> 1.44 s / 52.4 MB. 64K is both the
#: fastest and an order of magnitude leaner than the megabyte first tried.
_STREAM_CHUNK_BYTES = 64 * 1024


def iter_csv_rows(path: Path) -> Iterator[str]:
    """Stream a CSV's data lines in chunks, without holding the whole file.

    For the one case a tail read cannot bound -- the Plots tab's "All" range,
    which genuinely asks for every row -- this keeps peak memory flat instead
    of materialising a str per line for the whole run.

    Chunked rather than ``for line in f``: iterating a text file line by line
    is Python-level and measured **3x SLOWER** than the bulk
    ``f.read().splitlines()`` it was meant to replace (4.1 s vs 1.2 s on a
    7-day file). Reading a megabyte at a time and letting ``splitlines`` split
    it in C keeps the speed and drops the peak.

    Opened in binary so a chunk boundary can be carried over as bytes; decoding
    happens per line-batch. Handles CRLF and LF alike.
    """
    if not path.is_file():
        return
    with path.open("rb") as f:
        remainder = b""
        first = True
        while True:
            chunk = f.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            buf = remainder + chunk
            # Keep the trailing partial line for the next round.
            nl = buf.rfind(b"\n")
            if nl == -1:
                remainder = buf
                continue
            remainder = buf[nl + 1:]
            lines = buf[:nl].decode("utf-8", errors="replace").splitlines()
            if first:
                lines = lines[1:]          # header
                first = False
            for line in lines:
                if line:
                    yield line
        if remainder:
            lines = remainder.decode("utf-8", errors="replace").splitlines()
            if first:
                lines = lines[1:]
            for line in lines:
                if line:
                    yield line


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
    if parameter == "growth":
        return f"vial{vial:02d}_growth.csv"
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

def _vial_row_source(
    exp_dir: Path, vial: int, parameter: str, hours: Optional[float]
) -> tuple[list[str], Iterator[str]]:
    """``(header, row_iterator)`` for one vial, reading as little as possible.

    With a window, the bounded tail read plus the exact
    :func:`filter_rows_by_hours` trim (a small list). Without one, a streaming
    chunk reader, so the caller never holds the whole file.
    """
    path = exp_dir / _vial_csv_name(vial, parameter)
    if hours is None:
        return read_header(path), iter_csv_rows(path)
    header = read_header(path)
    try:
        elapsed_col = header.index("elapsed_hours")
    except ValueError:
        elapsed_col = 1
    header2, rows = read_tail_rows(
        path, hours=hours, elapsed_col=elapsed_col,
    )
    if header2:
        header = header2
        try:
            elapsed_col = header.index("elapsed_hours")
        except ValueError:
            elapsed_col = 1
    return header, iter(filter_rows_by_hours(rows, elapsed_col, hours))


class _VialCursor:
    """One vial's position in the merge: the current row, parsed."""

    __slots__ = ("vial", "rows", "ts", "elapsed", "value", "_ts_col",
                 "_elapsed_col", "_val_col", "_pending")

    def __init__(self, exp_dir: Path, vial: int, parameter: str,
                 hours: Optional[float], value_name: str) -> None:
        header, rows = _vial_row_source(exp_dir, vial, parameter, hours)
        self.vial = vial
        self.rows = rows
        self._ts_col = 0
        try:
            self._elapsed_col = header.index("elapsed_hours")
        except ValueError:
            self._elapsed_col = 1
        try:
            self._val_col = header.index(value_name)
        except ValueError:
            self._val_col = 3
        self.ts: Optional[str] = None
        self.elapsed = ""
        self.value = ""
        self._pending: Optional[str] = None
        if header:
            self.advance()

    def _parse(self, line: str) -> None:
        parts = line.split(",")
        self.ts = parts[self._ts_col] if parts else ""
        self.elapsed = (parts[self._elapsed_col]
                        if len(parts) > self._elapsed_col else "")
        self.value = (parts[self._val_col]
                      if len(parts) > self._val_col else "")

    def advance(self) -> None:
        """Move to the next DISTINCT timestamp, last row winning.

        The dict-based join this replaces did ``value_by_vial[v][ts] = val``,
        so consecutive rows sharing a timestamp collapsed to the last one --
        which happens for real when a resumed run re-writes the same second.
        A naive merge would emit them as separate output rows. One row of
        lookahead reproduces the old behaviour exactly.
        """
        line = self._pending
        self._pending = None
        if line is None:
            line = next(self.rows, None)
        if line is None:
            self.ts = None
            return
        self._parse(line)
        for nxt in self.rows:
            parts = nxt.split(",")
            if (parts[self._ts_col] if parts else "") != self.ts:
                self._pending = nxt
                return
            self._parse(nxt)          # same timestamp -> last value wins


def wide_csv_iter(
    exp_dir: Path,
    parameter: str,
    vials: Iterable[int],
    hours: Optional[float] = None,
) -> Iterator[str]:
    """Streaming form of :func:`wide_csv`, yielding text a row at a time.

    **Why this is a merge and not a join.** The dict-based join it replaces held
    one ``{timestamp: value}`` entry per vial per row -- 967 000 entries for a
    7-day 16-vial run, measured at **190 MB peak**, on a machine with 512 MB
    (Pi 1 Model B). The per-vial files are append-only and already in ascending
    timestamp order, and ``DataLogger.log_sensor_cycle`` writes every active
    vial with the SAME timestamp each cycle, so one row per vial in flight is
    enough.

    Ordering matches the dict version exactly:

    * rows come out in ascending timestamp order, which for parseable
      ``elapsed_hours`` is the same as the old ``(_elapsed_sort_key, ts)``
      sort -- elapsed is a deterministic function of the timestamp, so the two
      orders agree;
    * a row whose chosen ``elapsed_hours`` does not parse is held back and
      emitted at the end in timestamp order, reproducing ``_elapsed_sort_key``'s
      ``(1, 0.0)`` sink. The buffer is bounded by the number of corrupt rows,
      not by run length;
    * ``elapsed_hours`` for a timestamp is taken from the lowest-numbered vial
      reporting it, matching the old ``elapsed_by_ts.setdefault`` under a loop
      in ascending vial order.
    """
    if parameter not in _WIDE_VALUE_COL:
        raise ValueError(f"wide_csv parameter must be 'od' or 'temp', got {parameter!r}")
    value_name = _WIDE_VALUE_COL[parameter]
    vials = _normalize_vials(vials)

    out = io.StringIO()
    w = csv.writer(out)

    def _flush() -> str:
        text = out.getvalue()
        out.seek(0)
        out.truncate(0)
        return text

    w.writerow(["timestamp", "elapsed_hours"] + [f"vial{v:02d}" for v in vials])
    yield _flush()

    cursors = [_VialCursor(exp_dir, v, parameter, hours, value_name)
               for v in vials]
    sunk: list[list] = []

    while True:
        live = [c for c in cursors if c.ts is not None]
        if not live:
            break
        ts = min(c.ts for c in live)
        elapsed = ""
        for c in cursors:                      # ascending vial order
            if c.ts == ts:
                elapsed = c.elapsed
                break
        row = [ts, elapsed]
        for c in cursors:
            if c.ts == ts:
                row.append(c.value)
            else:
                row.append("")
        for c in cursors:
            if c.ts == ts:
                c.advance()
        if _elapsed_sort_key(elapsed)[0]:
            sunk.append(row)                   # unparseable -> tail, in ts order
            continue
        w.writerow(row)
        chunk = _flush()
        if chunk:
            yield chunk

    for row in sunk:
        w.writerow(row)
        chunk = _flush()
        if chunk:
            yield chunk


def wide_csv(
    exp_dir: Path,
    parameter: str,
    vials: Iterable[int],
    hours: Optional[float] = None,
) -> str:
    """Build a wide, calibrated-only CSV for ``parameter`` ('od' or 'temp').

    Columns: ``timestamp, elapsed_hours, vialNN, ...`` (one per selected vial,
    sorted ascending). Rows are joined across vials on ``timestamp`` -- the
    DataLogger writes every active vial with the same timestamp each cycle, so
    rows line up -- and emitted in ascending ``elapsed_hours`` order. A vial with
    no data (or a dropped read) contributes a blank cell.

    Kept as the string-returning entry point for callers that want the whole
    thing; :func:`wide_csv_iter` is the streaming form ``build_bundle`` uses.
    """
    return "".join(wide_csv_iter(exp_dir, parameter, vials, hours))


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
# Growth estimates (long CSV)
# ---------------------------------------------------------------------------

GROWTH_EXPORT_HEADER = (
    "timestamp",
    "elapsed_hours",
    "vial",
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

#: Column order in vial{NN}_growth.csv, for reading rows positionally when a
#: file predates a header change.
_GROWTH_COLUMNS = (
    ("timestamp", 0),
    ("elapsed_hours", 1),
    ("regime", 2),
    ("growth_rate_per_hour", 3),
    ("doubling_time_min", 4),
    ("r_squared", 5),
    ("windows_searched", 6),
    ("fit_span_s", 7),
    ("fit_od_start", 8),
    ("fit_od_end", 9),
    ("flags", 10),
)


def growth_csv(
    exp_dir: Path,
    vials: Iterable[int],
    hours: Optional[float] = None,
) -> Optional[str]:
    """Merge the per-vial growth logs (SPEC §17) into one long CSV with a
    ``vial`` column, sorted by timestamp.

    Returns ``None`` when no vial has a growth file -- true for every
    experiment recorded before the growth service existed, and for a run whose
    estimator never produced a row.

    ``r_squared`` is meaningless without ``windows_searched`` beside it: the
    window is chosen by maximum R², which inflates it by up to +0.0022 on data
    that is exactly log-linear. Both columns are exported for that reason.
    """
    vials = _normalize_vials(vials)
    merged: list[list] = []
    found = False
    for v in vials:
        path = exp_dir / _vial_csv_name(v, "growth")
        if not path.is_file():
            continue
        found = True
        header, data_rows = _read_csv_lines(path)
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

            row = [cell("timestamp", 0), cell("elapsed_hours", 1), v]
            row.extend(
                cell(n, i) for n, i in _GROWTH_COLUMNS if n not in
                ("timestamp", "elapsed_hours")
            )
            merged.append(row)

    if not found:
        return None
    merged.sort(key=lambda row: row[0])

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(list(GROWTH_EXPORT_HEADER))
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

    # The wide CSVs are the big ones and are produced lazily, so a multi-file
    # bundle never holds one whole in memory (see the zip writer below). The
    # single-parameter case has to materialise -- the caller wants the bytes.
    wide_specs: list[tuple[str, str]] = []      # (filename, parameter)
    if "od" in requested:
        wide_specs.append((f"{name}_OD.csv", "od"))
    if "temp" in requested:
        wide_specs.append((f"{name}_temp.csv", "temp"))
    pump_name = f"{name}_pump_events.csv" if "pump" in requested else None

    n_files = len(wide_specs) + (1 if pump_name else 0)
    if n_files == 1:
        if wide_specs:
            fn, param = wide_specs[0]
            return fn, wide_csv(exp_dir, param, vials, hours).encode("utf-8")
        return pump_name, pump_events_csv(exp_dir, vials, hours).encode("utf-8")

    events_text = events_csv(exp_dir, hours)
    # SPEC §17 growth estimates ride along in every bundle rather than being a
    # selectable parameter, for the same reason events.csv does: it is a
    # derived per-experiment record, not one of the three raw per-vial
    # streams the plots let you toggle.
    growth_text = growth_csv(exp_dir, vials, hours)

    file_rows: dict[str, int] = {}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Streamed straight into the zip. Materialising a wide CSV first cost
        # 190 MB peak on a 7-day 16-vial run -- more than a Pi 1 Model B has.
        # Row counts are tallied on the way past, so the manifest (written last)
        # still reports them without a second pass.
        for fn, param in wide_specs:
            written = 0
            with zf.open(fn, "w") as dest:
                for chunk in wide_csv_iter(exp_dir, param, vials, hours):
                    written += len(chunk.splitlines())
                    dest.write(chunk.encode("utf-8"))
            file_rows[fn] = max(0, written - 1)      # minus the header
        if pump_name:
            text = pump_events_csv(exp_dir, vials, hours)
            file_rows[pump_name] = _row_count(text)
            zf.writestr(pump_name, text)
        config_path = exp_dir / "config.json"
        if config_path.is_file():
            zf.writestr("config.json", config_path.read_text(encoding="utf-8"))
        if events_text is not None:
            zf.writestr("events.csv", events_text)
        if growth_text is not None:
            zf.writestr(f"{name}_growth.csv", growth_text)

        manifest = {
            "experiment": name,
            "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "filters": {
                "vials": vials,
                "parameters": [p for p in EXPORT_PARAMETERS if p in requested],
                "hours": hours,
            },
            "files": dict(file_rows),
        }
        manifest["files"] = {fn: {"data_rows": n} for fn, n in file_rows.items()}
        if events_text is not None:
            manifest["files"]["events.csv"] = {"data_rows": _row_count(events_text)}
        if growth_text is not None:
            manifest["files"][f"{name}_growth.csv"] = {
                "data_rows": _row_count(growth_text)
            }
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
