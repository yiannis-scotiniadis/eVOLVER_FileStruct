"""Tests for server/data_export.py — wide CSV pivot, pump-event merge, bundle
assembly (single CSV vs zip), the shared hours-window filter, and storage
reporting.

Run from the project root:
    python server/test_data_export.py
or under pytest:
    pytest server/test_data_export.py
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import data_export as dx  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class TmpRoot:
    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="evolver-export-"))
        return self.path

    def __exit__(self, *args) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _ts(i: int, interval_s: int = 10) -> str:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return (base + timedelta(seconds=i * interval_s)).isoformat(timespec="seconds")


def _write_od_csv(exp: Path, vial: int, rows: list[tuple[str, float, str]]) -> None:
    """rows: (timestamp, elapsed_hours, calibrated_od_cell). Uses the real
    7-column OD header so header.index() lookups are exercised."""
    exp.mkdir(parents=True, exist_ok=True)
    lines = ["timestamp,elapsed_hours,raw_adc,calibrated_od,n_valid,flag,dark"]
    for ts, eh, od in rows:
        lines.append(f"{ts},{eh:.4f},50000,{od},5,ok,100")
    (exp / f"vial{vial:02d}_OD.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_temp_csv(exp: Path, vial: int, rows: list[tuple[str, float, str]]) -> None:
    exp.mkdir(parents=True, exist_ok=True)
    lines = ["timestamp,elapsed_hours,raw_adc,calibrated_temp_c"]
    for ts, eh, tc in rows:
        lines.append(f"{ts},{eh:.4f},430,{tc}")
    (exp / f"vial{vial:02d}_temp.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_pump_csv(exp: Path, vial: int, rows: list[tuple[str, float, str, str, str]]) -> None:
    exp.mkdir(parents=True, exist_ok=True)
    lines = ["timestamp,elapsed_hours,direction,duration_seconds,od_at_pump"]
    for ts, eh, direction, dur, od in rows:
        lines.append(f"{ts},{eh:.4f},{direction},{dur},{od}")
    (exp / f"vial{vial:02d}_pump_log.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_csv(text: str) -> tuple[list[str], list[list[str]]]:
    rows = list(csv_reader(text))
    return rows[0], rows[1:]


def csv_reader(text: str):
    import csv
    return csv.reader(io.StringIO(text))


# ---------------------------------------------------------------------------
# filter_rows_by_hours (shared helper)
# ---------------------------------------------------------------------------

def test_filter_passthrough_when_hours_none() -> None:
    rows = ["a,0.0", "b,1.0"]
    assert dx.filter_rows_by_hours(rows, 1, None) is rows
    assert dx.filter_rows_by_hours([], 1, 2.0) == []
    print("PASS  filter_rows_by_hours passes through on hours=None / empty")


def test_filter_keeps_last_window() -> None:
    rows = [f"t{i},{float(i):.4f}" for i in range(11)]  # elapsed_hours 0..10
    kept = dx.filter_rows_by_hours(rows, 1, 2.0)  # last 2h of a 10h series -> >= 8.0
    elapsed = [float(r.split(",")[1]) for r in kept]
    assert min(elapsed) >= 8.0 - 1e-9, elapsed
    assert max(elapsed) == 10.0
    print("PASS  filter_rows_by_hours keeps only the last N hours")


def test_filter_keeps_unparseable_rows() -> None:
    rows = ["t0,0.0", "t1,oops", "t2,9.0", "t3,10.0"]
    kept = dx.filter_rows_by_hours(rows, 1, 2.0)
    assert "t1,oops" in kept, "unparseable row was dropped"
    assert "t0,0.0" not in kept, "old row was kept"
    print("PASS  filter_rows_by_hours keeps unparseable rows, drops old ones")


# ---------------------------------------------------------------------------
# wide_csv
# ---------------------------------------------------------------------------

def test_wide_csv_alignment_and_gaps() -> None:
    with TmpRoot() as root:
        exp = root / "exp"
        # vials 0 and 5 have all three cycles; vial 1 is missing cycle 1 and has
        # a blank (dropped) read at cycle 2.
        _write_od_csv(exp, 0, [(_ts(0), 0.0, "0.100"), (_ts(1), 0.0028, "0.110"), (_ts(2), 0.0056, "0.120")])
        _write_od_csv(exp, 1, [(_ts(0), 0.0, "0.200"), (_ts(2), 0.0056, "")])
        _write_od_csv(exp, 5, [(_ts(0), 0.0, "0.500"), (_ts(1), 0.0028, "0.510"), (_ts(2), 0.0056, "0.520")])

        text = dx.wide_csv(exp, "od", [0, 1, 5])
        header, data = _parse_csv(text)
        assert header == ["timestamp", "elapsed_hours", "vial00", "vial01", "vial05"], header
        assert len(data) == 3, data  # three distinct timestamps unioned
        # row order is by elapsed_hours
        assert [r[0] for r in data] == [_ts(0), _ts(1), _ts(2)]
        # cycle 1: vial01 missing entirely -> blank
        assert data[1] == [_ts(1), "0.0028", "0.110", "", "0.510"], data[1]
        # cycle 2: vial01 dropped read -> blank
        assert data[2] == [_ts(2), "0.0056", "0.120", "", "0.520"], data[2]
    print("PASS  wide_csv joins vials on timestamp, preserves gaps as blanks")


def test_wide_csv_temp_value_column() -> None:
    with TmpRoot() as root:
        exp = root / "exp"
        _write_temp_csv(exp, 2, [(_ts(0), 0.0, "37.0"), (_ts(1), 0.0028, "37.1")])
        text = dx.wide_csv(exp, "temp", [2])
        header, data = _parse_csv(text)
        assert header == ["timestamp", "elapsed_hours", "vial02"], header
        assert [r[2] for r in data] == ["37.0", "37.1"]
    print("PASS  wide_csv uses calibrated_temp_c for temperature")


def test_wide_csv_hours_window() -> None:
    with TmpRoot() as root:
        exp = root / "exp"
        rows = [(_ts(i), i * 10 / 3600.0, f"{0.5:.4f}") for i in range(3601)]  # ~10h
        _write_od_csv(exp, 0, rows)
        text = dx.wide_csv(exp, "od", [0], hours=2.0)
        _, data = _parse_csv(text)
        elapsed = [float(r[1]) for r in data]
        assert min(elapsed) >= 8.0 - 1e-6, min(elapsed)
        assert 700 <= len(data) <= 740, len(data)
    print("PASS  wide_csv honors the hours window")


# ---------------------------------------------------------------------------
# pump_events_csv
# ---------------------------------------------------------------------------

def test_pump_events_merge_sorted_with_vial_col() -> None:
    with TmpRoot() as root:
        exp = root / "exp"
        # interleaved timestamps across two vials
        _write_pump_csv(exp, 0, [(_ts(0), 0.0, "influx", "5.00", "0.40"),
                                 (_ts(2), 0.0056, "efflux", "6.00", "0.41")])
        _write_pump_csv(exp, 3, [(_ts(1), 0.0028, "influx", "4.00", "0.30")])
        text = dx.pump_events_csv(exp, [0, 3])
        header, data = _parse_csv(text)
        assert header == list(dx.PUMP_EXPORT_HEADER), header
        assert [r[0] for r in data] == [_ts(0), _ts(1), _ts(2)], "not time-sorted"
        assert [r[2] for r in data] == ["0", "3", "0"], "vial column wrong"
        assert data[1][3] == "influx" and data[1][4] == "4.00"
    print("PASS  pump_events_csv merges vials, sorts by time, adds vial column")


# ---------------------------------------------------------------------------
# build_bundle
# ---------------------------------------------------------------------------

def test_bundle_single_param_returns_bare_csv() -> None:
    with TmpRoot() as root:
        exp = root / "exp"
        _write_od_csv(exp, 0, [(_ts(0), 0.0, "0.10")])
        fname, blob = dx.build_bundle(exp, name="exp", vials=[0], parameters=["od"])
        assert fname == "exp_OD.csv", fname
        assert not fname.endswith(".zip")
        header = blob.decode("utf-8").splitlines()[0]
        assert header == "timestamp,elapsed_hours,vial00", header
    print("PASS  build_bundle returns a bare CSV for a single parameter")


def test_bundle_multi_param_returns_zip() -> None:
    with TmpRoot() as root:
        exp = root / "exp"
        (exp).mkdir(parents=True, exist_ok=True)
        (exp / "config.json").write_text(json.dumps({"name": "exp", "vials": [0]}), encoding="utf-8")
        _write_od_csv(exp, 0, [(_ts(0), 0.0, "0.10"), (_ts(1), 0.0028, "0.11")])
        _write_temp_csv(exp, 0, [(_ts(0), 0.0, "37.0"), (_ts(1), 0.0028, "37.1")])
        _write_pump_csv(exp, 0, [(_ts(1), 0.0028, "influx", "5.00", "0.11")])

        fname, blob = dx.build_bundle(
            exp, name="exp", vials=[0], parameters=["od", "temp", "pump"]
        )
        assert fname == "exp_export.zip", fname
        zf = zipfile.ZipFile(io.BytesIO(blob))
        names = set(zf.namelist())
        assert {"exp_OD.csv", "exp_temp.csv", "exp_pump_events.csv",
                "config.json", "export_manifest.json"} <= names, names
        manifest = json.loads(zf.read("export_manifest.json"))
        assert manifest["filters"]["parameters"] == ["od", "temp", "pump"]
        assert manifest["files"]["exp_OD.csv"]["data_rows"] == 2, manifest["files"]
        assert manifest["files"]["exp_pump_events.csv"]["data_rows"] == 1
    print("PASS  build_bundle zips CSVs + config + manifest for multiple params")


def test_bundle_validation() -> None:
    with TmpRoot() as root:
        exp = root / "exp"
        _write_od_csv(exp, 0, [(_ts(0), 0.0, "0.10")])
        for params in ([], ["bogus"]):
            try:
                dx.build_bundle(exp, name="exp", vials=[0], parameters=params)
            except ValueError:
                pass
            else:
                raise AssertionError(f"accepted bad parameters: {params}")
        try:
            dx.build_bundle(exp, name="exp", vials=[99], parameters=["od"])
        except ValueError:
            pass
        else:
            raise AssertionError("accepted out-of-range vial")
    print("PASS  build_bundle rejects empty/unknown parameters and bad vials")


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def test_storage_report_shapes() -> None:
    with TmpRoot() as root:
        exp_root = root / "experiments"
        exp_root.mkdir()
        e1 = exp_root / "exp1"
        _write_od_csv(e1, 0, [(_ts(0), 0.0, "0.10")])
        exports = root / "exports"
        exports.mkdir()
        (exports / "exp1_export.zip").write_bytes(b"x" * 123)

        rep = dx.storage_report(exp_root, exports)
        fs = rep["filesystem"]
        assert {"total_bytes", "used_bytes", "free_bytes", "free_pct"} <= set(fs)
        assert fs["free_bytes"] > 0
        names = {e["name"] for e in rep["experiments"]}
        assert names == {"exp1"}, names
        assert rep["experiments"][0]["files"] == 1
        assert rep["exports"]["bytes"] == 123, rep["exports"]
    print("PASS  storage_report reports filesystem + per-experiment + exports sizes")


def test_experiment_disk_usage() -> None:
    with TmpRoot() as root:
        exp = root / "exp"
        _write_od_csv(exp, 0, [(_ts(0), 0.0, "0.10")])
        _write_temp_csv(exp, 0, [(_ts(0), 0.0, "37.0")])
        u = dx.experiment_disk_usage(exp)
        assert u["files"] == 2, u
        assert u["bytes"] > 0
        assert dx.experiment_disk_usage(root / "missing") == {"bytes": 0, "files": 0}
    print("PASS  experiment_disk_usage sums sizes and counts files")


# ---------------------------------------------------------------------------
# low-disk monitor decision (app._disk_alert_decision) — pure + edge-triggered
# ---------------------------------------------------------------------------

def test_disk_alert_decision_edges() -> None:
    import app  # lazy: pulls in flask; only needed for this test
    GB = 1024 ** 3
    MB = 1024 ** 2
    total = 10 * GB
    # plenty free -> no alert, latches clear
    assert app._disk_alert_decision(5 * GB, total, False, False) == (None, False, False)
    # cross into warning band (800 MB, 7.8%) from ok
    assert app._disk_alert_decision(800 * MB, total, False, False) == ("warning", True, False)
    # still in warning, already warned -> no repeat alert
    assert app._disk_alert_decision(800 * MB, total, True, False) == (None, True, False)
    # cross into critical (100 MB, ~1%)
    assert app._disk_alert_decision(100 * MB, total, True, False) == ("critical", True, True)
    # still critical -> no repeat
    assert app._disk_alert_decision(100 * MB, total, True, True) == (None, True, True)
    # drop critical -> warning: clears critical latch silently (no new warning)
    assert app._disk_alert_decision(800 * MB, total, True, True) == (None, True, False)
    # recover to ok -> latches clear so a later re-crossing re-alerts
    assert app._disk_alert_decision(5 * GB, total, True, False) == (None, False, False)
    print("PASS  _disk_alert_decision edge-triggers warning/critical with hysteresis")


def main() -> int:
    test_filter_passthrough_when_hours_none()
    test_filter_keeps_last_window()
    test_filter_keeps_unparseable_rows()
    test_wide_csv_alignment_and_gaps()
    test_wide_csv_temp_value_column()
    test_wide_csv_hours_window()
    test_pump_events_merge_sorted_with_vial_col()
    test_bundle_single_param_returns_bare_csv()
    test_bundle_multi_param_returns_zip()
    test_bundle_validation()
    test_storage_report_shapes()
    test_experiment_disk_usage()
    test_disk_alert_decision_edges()
    print("\nAll data_export tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
