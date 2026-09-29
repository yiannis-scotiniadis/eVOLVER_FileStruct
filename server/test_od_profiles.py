"""Tests for per-organism OD calibration profiles, the raw-ADC OD log, and
the fixes that came with them.

Organised by the build phases in the plan: store/log fixes (phase 0), the
per-vial live curve (1), the OD CSV v2 schema (2), profiles in the store and
the series (3), and the wiring through create/start/stop/resume (4). The
post-hoc recalibration module has its own file, test_od_recalibrate.py.

Run from the project root:
    python -m pytest server/test_od_profiles.py
"""

from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from calibration_service import CalibrationStore, N_VIALS, make_envelope  # noqa: E402
from data_logger import DataLogger  # noqa: E402

REPO_CAL = Path(__file__).resolve().parent.parent / "calibration"


def _rows(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


# ---------------------------------------------------------------------------
# Phase 0: existing bugs fixed in passing
# ---------------------------------------------------------------------------

def _od_env(version: str, c: float = 0.5) -> dict:
    rows = np.vstack([np.full(N_VIALS, 10_000.0), np.full(N_VIALS, 100_000.0),
                      np.full(N_VIALS, c), np.full(N_VIALS, -0.7)])
    return make_envelope("od", operator="t", source="test", conditions={"n": 1},
                         data={"rows": rows.tolist(), "dark_subtracted": False},
                         version=version)


def test_save_version_records_what_it_supersedes():
    """make_envelope writes supersedes=None, so save_version's setdefault
    never took effect: every version claimed to supersede nothing."""
    with tempfile.TemporaryDirectory() as tmp:
        store = CalibrationStore(Path(tmp) / "calibration")
        store.save_version("od", _od_env("2026-01-01T000000Z"))
        store.save_version("od", _od_env("2026-02-01T000000Z", c=0.4))
        second = store.get_version("od", "2026-02-01T000000Z")
        assert second["supersedes"] == "2026-01-01T000000Z"
        assert store.get_version("od", "2026-01-01T000000Z")["supersedes"] is None


def test_fresh_version_bumps_past_every_stamp_on_disk():
    """Not just the current pointer: another chain's newer file, or a clock
    set back after a reboot (the Pi has no RTC), must not interleave."""
    with tempfile.TemporaryDirectory() as tmp:
        store = CalibrationStore(Path(tmp) / "calibration")
        store.save_version("od", _od_env("2026-01-01T000000Z"))
        # A newer file that is NOT the current pointer.
        store._write_version_locked("od", _od_env("2099-01-01T000000Z"))
        assert store.fresh_version("od") == "2099-01-01T000001Z"


def test_an_event_for_a_run_that_is_not_logging_goes_to_its_own_file_only():
    """Before: an event naming a CREATED (not yet logging) experiment fell
    through to 'every active run' and landed in OTHER runs' events.csv."""
    with tempfile.TemporaryDirectory() as tmp:
        dl = DataLogger(Path(tmp) / "experiments")
        dl.create_experiment(name="running", mode="turbidostat", vials=[0])
        dl.create_experiment(name="waiting", mode="turbidostat", vials=[1])
        dl.activate_experiment("running")
        ts = "2026-09-29T12:00:00+00:00"
        assert dl.log_event(ts, "info", "lifecycle", "created", experiment="waiting",
                            data={"experiment": "waiting", "organism": "E. coli"})
        waiting = _rows(Path(tmp) / "experiments" / "waiting" / "events.csv")
        running = _rows(Path(tmp) / "experiments" / "running" / "events.csv")
        assert len(waiting) == 2 and waiting[1][1] == ""     # no clock before start
        assert "E. coli" in waiting[1][6]
        assert len(running) == 1                              # header only
        # An experiment that does not exist is skipped, not broadcast.
        assert dl.log_event(ts, "info", "x", "ghost", experiment="nope") is False
        assert len(_rows(Path(tmp) / "experiments" / "running" / "events.csv")) == 1
        # Machine-wide events still reach every active run.
        assert dl.log_event(ts, "warning", "serial", "bus down")
        assert len(_rows(Path(tmp) / "experiments" / "running" / "events.csv")) == 2


# ---------------------------------------------------------------------------
# Phase 1: the live curve is composed per vial
# ---------------------------------------------------------------------------

import math  # noqa: E402

import od_curve_fit as F  # noqa: E402
from mock_serial_manager import MockSerialManager  # noqa: E402
from test_serial_manager import FakeSerial, fresh  # noqa: E402

LEGACY_OD = np.genfromtxt(REPO_CAL / "OD_cal.txt", delimiter=",")
DARK = 1200.0


def _turb(values) -> bytes:
    return ("turb" + ",".join(str(int(v)) for v in values) + ",end\n").encode()


def _ds_shift(rows: np.ndarray, dark: float = DARK) -> np.ndarray:
    """The same curve, re-expressed on dark-subtracted signal: exact."""
    out = np.array(rows, dtype=float)
    out[0] -= dark
    out[1] -= dark
    return out


def test_one_read_serves_a_dark_subtracted_profile_and_the_legacy_curve():
    """Vials 0-1 run an organism profile fit on dark-subtracted signal, the
    rest the 2016 curve: one dark read + one lit read, subtracted per vial."""
    sm, fake = fresh()
    sm.assign_od_curve([0, 1], _ds_shift(LEGACY_OD), dark_subtracted=True)
    assert sm.od_dark_subtracted_by_vial[:3] == [True, True, False]
    fake.read_queue = [_turb([DARK] * N_VIALS), _turb([50000] * N_VIALS)]
    r = sm.read_od_enhanced(2125, n_samples=1, n_dark=1, dark_subtract=None)
    assert fake.write_log[0].startswith(b"we0,")          # dark first
    assert fake.write_log[1].startswith(b"we2125,")       # LED left on
    assert r.raw[0] == 50000 - DARK and r.raw[5] == 50000
    assert r.dark_subtracted[:2] == [True, True] and r.dark_subtracted[5] is False
    assert r.dark_used[0] == DARK and math.isnan(r.dark_used[5])
    assert r.lit[0] == r.lit[5] == 50000 and r.led_power == 2125
    # Vial 0's shifted curve on lit - dark reads exactly what its own 2016
    # curve reads on lit; vial 5 is read by its 2016 curve directly.
    assert abs(r.calibrated[0] - F.od_from_signal(50000.0, *LEGACY_OD[:, 0])) < 1e-9
    assert abs(r.calibrated[5] - F.od_from_signal(50000.0, *LEGACY_OD[:, 5])) < 1e-9


def test_explicit_dark_subtraction_on_a_raw_signal_vial_is_refused():
    sm, _fake = fresh()
    sm.assign_od_curve([0], _ds_shift(LEGACY_OD), dark_subtracted=True)
    mask = [False] * N_VIALS
    mask[0] = mask[3] = True
    try:
        sm.read_od_enhanced(2125, n_samples=1, n_dark=1, dark_subtract=mask)
    except ValueError as exc:
        assert "vials [3]" in str(exc)
    else:
        raise AssertionError("dark subtraction on a raw-signal curve was allowed")


def test_a_dark_subtracted_vial_with_no_dark_at_all_is_dropped_not_zeroed():
    """Before: subtracted 0 and called it "ok" -- a ~1200-count error."""
    sm, fake = fresh()
    sm.assign_od_curve([0], _ds_shift(LEGACY_OD), dark_subtracted=True)
    fake.read_queue = [b"", _turb([50000] * N_VIALS)]     # the dark read drops
    r = sm.read_od_enhanced(2125, n_samples=1, n_dark=1, dark_subtract=None)
    assert r.flags[0] == "dropped" and math.isnan(r.calibrated[0])
    assert r.lit[0] == 50000                              # the count is kept
    assert r.flags[5] == "ok"                             # legacy vial unaffected


def test_record_dark_logs_the_dark_without_subtracting_it():
    sm, fake = fresh()
    fake.read_queue = [_turb([DARK] * N_VIALS), _turb([50000] * N_VIALS)]
    r = sm.read_od_enhanced(2125, n_samples=1, n_dark=1, dark_subtract=None,
                            record_dark=True)
    assert r.dark[0] == DARK and r.dark_n_valid[0] == 1
    assert r.raw[0] == 50000 and math.isnan(r.dark_used[0])
    assert r.dark_subtracted[0] is False
    # Without record_dark (and no dark-subtracted vial) no dark read happens.
    fake.read_queue = [_turb([50000] * N_VIALS)]
    fake.write_log.clear()
    r = sm.read_od_enhanced(2125, n_samples=1, n_dark=1, dark_subtract=None)
    assert len(fake.write_log) == 1 and math.isnan(r.dark[0])


def test_assign_release_and_blank_restore_per_vial():
    sm, _fake = fresh()
    profile = LEGACY_OD.copy()
    profile[2] -= 0.3
    sm.assign_od_curve([2], profile, dark_subtracted=False)
    sm.apply_od_blank({2: -1.5, 7: -0.9})
    # Clearing the blank restores the ASSIGNED curve's row 2, not the default.
    sm.clear_od_blank([2])
    assert sm.od_cal[2, 2] == profile[2, 2]
    assert sm.od_cal[2, 7] == -0.9                         # other run untouched
    # Assigning over a blanked vial replaces the whole column.
    sm.assign_od_curve([7], profile, dark_subtracted=True)
    assert sm.od_cal[2, 7] == profile[2, 7] and sm.od_dark_subtracted_by_vial[7]
    sm.release_od_curve([2, 7])
    assert np.array_equal(sm.od_cal[:, [2, 7]], LEGACY_OD[:, [2, 7]])
    assert sm.od_dark_subtracted_by_vial[7] is False
    try:
        sm.assign_od_curve([16], profile, dark_subtracted=False)
    except ValueError:
        pass
    else:
        raise AssertionError("vial 16 accepted")


def test_mock_truth_mode_shows_a_vial_on_the_wrong_organisms_curve():
    m = MockSerialManager(seed=3)
    m.load_calibration(str(REPO_CAL / "temp_calibration.txt"),
                       str(REPO_CAL / "OD_cal.txt"))
    truth = _ds_shift(LEGACY_OD)
    truth[2] -= 0.4                                       # this organism's curve
    m.set_true_od_curve(truth, dark_counts=DARK)
    m.assign_od_curve([0, 1, 2, 3], truth, dark_subtracted=True)
    m.set_true_od(range(N_VIALS), 0.8)
    r = m.read_od_enhanced(2125, n_samples=5, dark_subtract=None, record_dark=True)
    for v in (0, 1, 2, 3):                                # the right curve
        assert abs(r.calibrated[v] - 0.8) < 1e-9, (v, r.calibrated[v])
    assert abs(r.calibrated[8] - 0.8) > 0.05              # the wrong curve shows
    assert r.dark[8] == DARK and r.dark_subtracted[8] is False
    # Default mode (no truth): true OD reported, raw fields synthesized.
    m2 = MockSerialManager(seed=4)
    m2.load_calibration(str(REPO_CAL / "temp_calibration.txt"),
                        str(REPO_CAL / "OD_cal.txt"))
    m2.assign_od_curve([0], _ds_shift(LEGACY_OD), dark_subtracted=True)
    r2 = m2.read_od_enhanced(2125, n_samples=3, dark_subtract=None)
    assert r2.dark_subtracted[0] and r2.lit[0] == r2.raw[0] + r2.dark_used[0]
    assert abs(F.od_from_signal(r2.raw[0], *m2.od_cal[:, 0]) - r2.calibrated[0]) < 1e-6


# ---------------------------------------------------------------------------
# Phase 2: vialNN_OD.csv schema v2 -- the raw counts, appended
# ---------------------------------------------------------------------------

import io  # noqa: E402
import json  # noqa: E402
import zipfile  # noqa: E402

import data_export as dx  # noqa: E402
from data_logger import OD_HEADER, OD_HEADER_V1  # noqa: E402

TS0 = "2026-09-29T12:00:00+00:00"
TS1 = "2026-09-29T12:00:10+00:00"


def _log_od(dl, ts, *, lit=50000.5, dark=1200.25, ds=True):
    nan = float("nan")
    raw = lit - dark if ds else lit
    dl.log_sensor_cycle(
        ts, [30.0] * N_VIALS, [500.0] * N_VIALS,
        od_calibrated=[0.4123456] * N_VIALS, od_raw=[raw] * N_VIALS,
        od_n_valid=[5] * N_VIALS, od_flags=["ok"] * N_VIALS,
        od_dark=[dark] * N_VIALS, od_lit=[lit] * N_VIALS,
        od_dark_used=[dark if ds else nan] * N_VIALS,
        od_dark_n_valid=[3] * N_VIALS, od_dark_subtracted=[ds] * N_VIALS,
        od_led_power=2125,
    )


def test_new_od_files_carry_the_raw_counts_exactly():
    with tempfile.TemporaryDirectory() as tmp:
        dl = DataLogger(Path(tmp) / "experiments")
        config = dl.create_experiment(name="r", mode="turbidostat", vials=[0, 1])
        assert config["od_csv_schema"] == 2
        dl.activate_experiment("r")
        _log_od(dl, TS0)
        rows = _rows(Path(tmp) / "experiments" / "r" / "vial00_OD.csv")
        assert tuple(rows[0]) == OD_HEADER and len(rows[1]) == len(OD_HEADER)
        row = dict(zip(rows[0], rows[1]))
        # The v1 columns keep their v1 format (raw_adc and dark are integers).
        assert row["raw_adc"] == "48800" and row["dark"] == "1200"
        assert row["calibrated_od"] == "0.4123"
        # The v2 columns are exact.
        assert row["lit_adc"] == "50000.5" and row["dark_adc"] == "1200.25"
        assert row["dark_used"] == "1200.25" and row["dark_n_valid"] == "3"
        assert row["dark_subtracted"] == "1" and row["led_power"] == "2125"
        # A raw-signal row: nothing subtracted, dark still recorded.
        _log_od(dl, TS1, ds=False)
        row = dict(zip(rows[0], _rows(Path(tmp) / "experiments" / "r" / "vial00_OD.csv")[2]))
        assert row["dark_used"] == "" and row["dark_subtracted"] == "0"
        assert row["dark_adc"] == "1200.25" and row["raw_adc"] == "50000"


def test_a_pre_upgrade_od_file_keeps_seven_column_rows():
    """A run resumed across the upgrade: its rows must match ITS header."""
    with tempfile.TemporaryDirectory() as tmp:
        dl = DataLogger(Path(tmp) / "experiments")
        dl.create_experiment(name="old", mode="turbidostat", vials=[0, 1])
        path0 = Path(tmp) / "experiments" / "old" / "vial00_OD.csv"
        path0.write_text(",".join(OD_HEADER_V1) + "\n" + "t,0.0,1,0.1,5,ok,\n")
        dl.activate_experiment("old")
        _log_od(dl, TS0)
        old = _rows(path0)
        assert all(len(r) == 7 for r in old), old
        new = _rows(Path(tmp) / "experiments" / "old" / "vial01_OD.csv")
        assert all(len(r) == len(OD_HEADER) for r in new)


def test_readers_and_bundles_handle_schema_v2():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "experiments"
        dl = DataLogger(root)
        dl.create_experiment(name="r", mode="turbidostat", vials=[0, 1])
        dl.activate_experiment("r")
        _log_od(dl, TS0)
        _log_od(dl, TS1)
        exp = root / "r"
        (exp / "od_blank.json").write_text("{}")
        # By-name readers see the same calibrated values as before.
        wide = dx.wide_csv(exp, "od", [0, 1], None)
        assert wide.splitlines()[1].split(",")[-1] == "0.4123"
        from replay_growth import load_od_series
        assert [od for _t, od in load_od_series(exp, 0)] == [0.4123, 0.4123]
        name, blob = dx.build_bundle(exp, name="r", vials=[0], parameters=["od", "temp"])
        zf = zipfile.ZipFile(io.BytesIO(blob))
        assert "raw/vial00_OD.csv" in zf.namelist() and "od_blank.json" in zf.namelist()
        raw = zf.read("raw/vial00_OD.csv").decode().splitlines()
        assert raw[0] == ",".join(OD_HEADER) and len(raw) == 3
        manifest = json.loads(zf.read("export_manifest.json"))
        assert manifest["od_csv_schema"] == 2
        assert manifest["files"]["raw/vial00_OD.csv"]["data_rows"] == 2
        # The raw files are only exported with OD.
        _n, blob = dx.build_bundle(exp, name="r", vials=[0], parameters=["temp", "pump"])
        assert not any(n.startswith("raw/") for n in zipfile.ZipFile(io.BytesIO(blob)).namelist())


def test_the_live_sensor_loop_logs_the_raw_counts():
    """End to end through app.py's loop: a new experiment's config asks for
    the dark read, and every OD row carries lit/dark counts."""
    import threading
    import time
    import app as A
    from test_calibration_api import _experiment_body, _make_app, _state
    saved = (A.SENSOR_LOOP_INTERVAL_SECONDS, A.OD_INTERVAL_SECONDS)
    A.SENSOR_LOOP_INTERVAL_SECONDS = A.OD_INTERVAL_SECONDS = 0.05
    threads_before = set(threading.enumerate())
    with tempfile.TemporaryDirectory() as tmp:
        try:
            flask_app = _make_app(Path(tmp))
            client = flask_app.test_client()
            assert client.post("/api/experiments/create",
                               json=_experiment_body("live")).status_code == 200
            cfg = json.loads((Path(tmp) / "experiments" / "live" / "config.json").read_text())
            assert cfg["parameters"]["od_acquisition"]["record_dark"] is True
            assert client.post("/api/experiments/live/start",
                               json={"allow_missing_od_blank": True}).status_code == 200
            path = Path(tmp) / "experiments" / "live" / "vial00_OD.csv"
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and len(_rows(path)) < 3:
                time.sleep(0.05)
            rows = _rows(path)
            assert len(rows) >= 3, rows
            row = dict(zip(rows[0], rows[-1]))
            assert row["led_power"] == "2125" and row["dark_subtracted"] == "0"
            assert row["dark_adc"] != "" and row["dark_n_valid"] != "0"
            assert int(row["raw_adc"]) == round(float(row["lit_adc"]))
        finally:
            # Stop AND join this app's loop before the temp dir goes: at a
            # 50 ms tick it is usually mid-write when the test ends.
            _state(flask_app).sensor_thread_stop.set()
            for t in set(threading.enumerate()) - threads_before:
                if t.name == "sensor-loop":
                    t.join(timeout=5)
            A.SENSOR_LOOP_INTERVAL_SECONDS, A.OD_INTERVAL_SECONDS = saved
