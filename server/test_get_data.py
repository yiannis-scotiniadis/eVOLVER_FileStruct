"""Tests for ExperimentEngine.get_data time-window filtering and the
min/max downsampler that backs the per-vial plots feature.

Run from the project root:
    python server/test_get_data.py
or under pytest:
    pytest server/test_get_data.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import data_export as dx  # noqa: E402
from experiment_engine import ExperimentEngine  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class TmpRoot:
    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="evolver-getdata-"))
        return self.path

    def __exit__(self, *args) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _engine(root: Path) -> ExperimentEngine:
    # get_data only touches self._experiments_root; serial_manager and
    # data_logger are never referenced on this path.
    return ExperimentEngine(serial_manager=None, data_logger=None, experiments_root=root)


def _write_od_csv(root: Path, name: str, vial: int, rows: list[tuple[str, float, int, str]]) -> None:
    """rows: (timestamp, elapsed_hours, raw_adc, calibrated_od_cell)."""
    exp = root / name
    exp.mkdir(parents=True, exist_ok=True)
    lines = ["timestamp,elapsed_hours,raw_adc,calibrated_od"]
    for ts, eh, raw, od in rows:
        lines.append(f"{ts},{eh:.4f},{raw},{od}")
    (exp / f"vial{vial:02d}_OD.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_pump_csv(root: Path, name: str, vial: int, rows: list[tuple[str, float, str, str, str]]) -> None:
    exp = root / name
    exp.mkdir(parents=True, exist_ok=True)
    lines = ["timestamp,elapsed_hours,direction,duration_seconds,od_at_pump"]
    for ts, eh, direction, dur, od in rows:
        lines.append(f"{ts},{eh:.4f},{direction},{dur},{od}")
    (exp / f"vial{vial:02d}_pump_log.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_od_rows(n: int, interval_s: int = 10):
    """n evenly spaced OD rows, value = 0.5 baseline."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        ts = (base + timedelta(seconds=i * interval_s)).isoformat(timespec="seconds")
        eh = i * interval_s / 3600.0
        rows.append((ts, eh, 50000 + i, f"{0.5:.4f}"))
    return rows


# ---------------------------------------------------------------------------
# _downsample_minmax (pure staticmethod)
# ---------------------------------------------------------------------------

def test_downsample_noop_when_small() -> None:
    ts = [f"t{i}" for i in range(10)]
    v = [float(i) for i in range(10)]
    out_ts, out_v = ExperimentEngine._downsample_minmax(ts, v, max_points=100)
    assert out_ts == ts and out_v == v
    print("PASS  downsample is a no-op when n <= max_points")


def test_downsample_caps_length() -> None:
    n = 5000
    ts = [f"t{i}" for i in range(n)]
    v = [float(i % 7) for i in range(n)]
    out_ts, out_v = ExperimentEngine._downsample_minmax(ts, v, max_points=500)
    assert len(out_v) <= 500, len(out_v)
    assert len(out_ts) == len(out_v)
    print(f"PASS  downsample caps length ({n} -> {len(out_v)} <= 500)")


def test_downsample_preserves_spikes() -> None:
    n = 1000
    ts = [f"t{i}" for i in range(n)]
    v = [0.5] * n
    v[500] = 9.0   # global max
    v[200] = 0.01  # global min
    _, out_v = ExperimentEngine._downsample_minmax(ts, v, max_points=50)
    present = [x for x in out_v if x is not None]
    assert max(present) == 9.0, "max spike lost"
    assert min(present) == 0.01, "min dip lost"
    print("PASS  downsample preserves global min and max spikes")


def test_downsample_all_none_yields_gaps() -> None:
    n = 100
    ts = [f"t{i}" for i in range(n)]
    v: list = [None] * n
    out_ts, out_v = ExperimentEngine._downsample_minmax(ts, v, max_points=10)
    assert all(x is None for x in out_v), out_v
    assert len(out_v) == 5, f"expected 5 gap buckets, got {len(out_v)}"
    print("PASS  fully-None buckets collapse to single None gap markers")


def test_downsample_chronological_order() -> None:
    n = 600
    ts = list(range(n))  # numeric timestamps to check ordering
    v = [float((i * 37) % 13) for i in range(n)]
    out_ts, _ = ExperimentEngine._downsample_minmax(ts, v, max_points=60)
    assert out_ts == sorted(out_ts), "timestamps not in chronological order"
    print("PASS  downsample output stays in chronological order")


# ---------------------------------------------------------------------------
# get_data — hours window + downsampling integration
# ---------------------------------------------------------------------------

def test_get_data_hours_window() -> None:
    with TmpRoot() as root:
        eng = _engine(root)
        # 3601 rows at 10s spacing -> spans ~10h (last elapsed = 10.0h).
        rows = _make_od_rows(3601)
        _write_od_csv(root, "exp", 0, rows)
        data = eng.get_data("exp", vial=0, parameter="od", hours=2.0, max_points=None)
        elapsed_kept = [
            (datetime.fromisoformat(t) - datetime.fromisoformat(rows[0][0])).total_seconds() / 3600.0
            for t in data["timestamps"]
        ]
        assert elapsed_kept, "no rows kept"
        # All kept rows are within the last 2h of the 10h series (>= ~8.0h).
        assert min(elapsed_kept) >= 8.0 - 1e-6, min(elapsed_kept)
        assert max(elapsed_kept) <= 10.0 + 1e-6
        # Window of 2h at 10s spacing ~ 720 rows (+/-1).
        assert 700 <= len(data["values"]) <= 740, len(data["values"])
    print("PASS  get_data hours= keeps only the last N hours of data")


def test_get_data_downsample_and_gaps() -> None:
    with TmpRoot() as root:
        eng = _engine(root)
        rows = _make_od_rows(4000)
        # Inject a spike and a contiguous gap (empty calibrated_od cells).
        rows[1000] = (rows[1000][0], rows[1000][1], rows[1000][2], f"{5.0:.4f}")
        for i in range(2000, 2100):
            rows[i] = (rows[i][0], rows[i][1], rows[i][2], "")  # NaN -> empty
        _write_od_csv(root, "exp", 0, rows)
        data = eng.get_data("exp", vial=0, parameter="od", max_points=500)
        assert len(data["values"]) <= 500, len(data["values"])
        present = [x for x in data["values"] if x is not None]
        assert max(present) == 5.0, "spike lost through downsampling"
        assert any(x is None for x in data["values"]), "gap not preserved"
    print("PASS  get_data downsamples, preserves spikes, and keeps NaN gaps")


def test_get_data_pump_not_downsampled() -> None:
    with TmpRoot() as root:
        eng = _engine(root)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        prows = []
        for i in range(50):
            ts = (base + timedelta(minutes=i)).isoformat(timespec="seconds")
            eh = i / 60.0  # hours
            prows.append((ts, eh, "influx", "5.00", f"{0.8:.4f}"))
        _write_pump_csv(root, "exp", 0, prows)
        # Whole series (~0.82h) with a tiny max_points must NOT be downsampled.
        data = eng.get_data("exp", vial=0, parameter="pump", max_points=10)
        assert "rows" in data and "header" in data
        assert len(data["rows"]) == 50, len(data["rows"])
        assert data["header"][0] == "timestamp"
        # hours window still applies to pump events.
        windowed = eng.get_data("exp", vial=0, parameter="pump", hours=0.25)
        assert len(windowed["rows"]) < 50, len(windowed["rows"])
        assert all(r["direction"] == "influx" for r in windowed["rows"])
    print("PASS  get_data pump events: never downsampled, hours window applies")


def test_get_data_validation() -> None:
    with TmpRoot() as root:
        eng = _engine(root)
        _write_od_csv(root, "exp", 0, _make_od_rows(5))
        for kwargs in ({"hours": 0}, {"hours": -1}, {"max_points": 0}, {"max_points": -5}):
            try:
                eng.get_data("exp", vial=0, parameter="od", **kwargs)
            except ValueError:
                pass
            else:
                raise AssertionError(f"accepted bad input: {kwargs}")
    print("PASS  get_data rejects non-positive hours/max_points")


def main() -> int:
    test_downsample_noop_when_small()
    test_downsample_caps_length()
    test_downsample_preserves_spikes()
    test_downsample_all_none_yields_gaps()
    test_downsample_chronological_order()
    test_get_data_hours_window()
    test_get_data_downsample_and_gaps()
    test_get_data_pump_not_downsampled()
    test_get_data_validation()
    print("\nAll get_data tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# ===========================================================================
# Bounded tail reads  (see data_export.read_tail_rows)
# ===========================================================================
#
# The optimisation's whole safety argument is that the tail read is a
# PREFILTER: it returns a superset and `filter_rows_by_hours` still trims
# exactly. These tests assert that equivalence directly against a reference
# that reads the whole file, so a future change to the chunking cannot quietly
# start dropping rows.


def _reference_get_data(path, parameter, hours=None, last_n=None,
                        max_points=None):
    """What get_data did before the tail read: read everything, then filter."""
    with path.open("r", encoding="utf-8", newline="") as f:
        lines = f.read().splitlines()
    if not lines:
        return {"timestamps": [], "values": []}
    header = lines[0].split(",")
    rows = lines[1:]
    try:
        elapsed_col = header.index("elapsed_hours")
    except ValueError:
        elapsed_col = 1
    if hours is not None and rows:
        rows = dx.filter_rows_by_hours(rows, elapsed_col, hours)
    if last_n is not None:
        rows = rows[-last_n:]
    col = header.index("calibrated_od" if parameter == "od"
                       else "calibrated_temp_c")
    ts, vals = [], []
    for line in rows:
        parts = line.split(",")
        ts.append(parts[0] if parts else "")
        cell = parts[col] if len(parts) > col else ""
        try:
            vals.append(float(cell) if cell != "" else None)
        except ValueError:
            vals.append(None)
    if max_points is not None:
        ts, vals = ExperimentEngine._downsample_minmax(ts, vals, max_points)
    return {"timestamps": ts, "values": vals}


def _write_big_od(root: Path, name: str, vial: int, n_rows: int,
                  crlf: bool = True, gaps=()) -> Path:
    exp = root / name
    exp.mkdir(parents=True, exist_ok=True)
    path = exp / f"vial{vial:02d}_OD.csv"
    eol = "\r\n" if crlf else "\n"
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write("timestamp,elapsed_hours,raw_adc,calibrated_od,n_valid,flag,dark" + eol)
        for i in range(n_rows):
            od = "" if i in gaps else f"{0.2 + (i % 500) * 0.001:.4f}"
            f.write(f"2026-05-12T10:00:{i % 60:02d}+00:00,{i * 10 / 3600:.4f},"
                    f"52000,{od},5,ok," + eol)
    return path


@pytest.mark.parametrize("crlf", [True, False])
@pytest.mark.parametrize("hours", [0.5, 1.0, 6.0, 24.0, 1000.0])
def test_tail_read_matches_a_full_read(crlf, hours) -> None:
    """Byte-identical results to the read-everything reference, across windows
    that fall inside, on, and outside the file's span, for both line endings."""
    with TmpRoot() as root:
        path = _write_big_od(root, "Big", 0, 20000, crlf=crlf, gaps=(5, 6, 7))
        eng = _engine(root)
        for max_points in (None, 500):
            got = eng.get_data("Big", 0, "od", hours=hours, max_points=max_points)
            want = _reference_get_data(path, "od", hours=hours,
                                       max_points=max_points)
            assert got == want, f"hours={hours} crlf={crlf} mp={max_points}"


@pytest.mark.parametrize("last_n", [1, 5, 500, 19999, 20000, 999999])
def test_tail_read_last_n_matches_a_full_read(last_n) -> None:
    with TmpRoot() as root:
        path = _write_big_od(root, "Big", 0, 20000)
        eng = _engine(root)
        got = eng.get_data("Big", 0, "od", last_n=last_n)
        want = _reference_get_data(path, "od", last_n=last_n)
        assert got == want


def test_tail_read_hours_and_last_n_together() -> None:
    with TmpRoot() as root:
        path = _write_big_od(root, "Big", 0, 20000)
        eng = _engine(root)
        got = eng.get_data("Big", 0, "od", hours=2.0, last_n=100)
        want = _reference_get_data(path, "od", hours=2.0, last_n=100)
        assert got == want


def test_tail_read_degenerate_files() -> None:
    """Smaller than one chunk, header-only, and empty."""
    with TmpRoot() as root:
        eng = _engine(root)
        small = _write_big_od(root, "Small", 0, 3)
        assert eng.get_data("Small", 0, "od", hours=1.0) == \
            _reference_get_data(small, "od", hours=1.0)

        exp = root / "HeaderOnly"
        exp.mkdir()
        (exp / "vial00_OD.csv").write_text(
            "timestamp,elapsed_hours,raw_adc,calibrated_od\n", encoding="utf-8")
        assert eng.get_data("HeaderOnly", 0, "od", hours=1.0)["values"] == []
        assert eng.get_data("HeaderOnly", 0, "od", last_n=5)["values"] == []

        exp2 = root / "Empty"
        exp2.mkdir()
        (exp2 / "vial00_OD.csv").write_text("", encoding="utf-8")
        assert eng.get_data("Empty", 0, "od", hours=1.0)["values"] == []


def test_tail_read_is_bounded_in_bytes() -> None:
    """The point of the exercise, asserted deterministically.

    A byte count, not a wall-clock timing, so it cannot flake: a 1 h window on
    a 7-day file must not read anything like the whole file.
    """
    with TmpRoot() as root:
        path = _write_big_od(root, "Week", 0, 60480)      # 7 days at 10 s
        total = path.stat().st_size
        assert total > 3_000_000, "fixture should be a few MB"

        read_bytes = {"n": 0}
        real_open = Path.open

        def counting_open(self, *a, **kw):
            fh = real_open(self, *a, **kw)
            if self == path:
                real_read = fh.read

                def read(*ra, **rkw):
                    data = real_read(*ra, **rkw)
                    read_bytes["n"] += len(data)
                    return data
                fh.read = read
            return fh

        eng = _engine(root)
        Path.open = counting_open
        try:
            got = eng.get_data("Week", 0, "od", hours=1.0, max_points=500)
        finally:
            Path.open = real_open

        want = _reference_get_data(path, "od", hours=1.0, max_points=500)
        assert got == want
        assert read_bytes["n"] < total // 4, (
            f"read {read_bytes['n']} of {total} bytes for a 1 h window"
        )
