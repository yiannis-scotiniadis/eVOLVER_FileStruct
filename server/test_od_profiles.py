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


# ---------------------------------------------------------------------------
# Phase 3: organism profiles in the store; the series creates / recalibrates
# ---------------------------------------------------------------------------

from calibration_service import (  # noqa: E402
    CalibrationService,
    LEGACY_OD_ORGANISM,
    od_profile_id_of,
    od_profile_slug,
)
from test_calibration_tier3 import (  # noqa: E402
    SERIES_LEVELS,
    OdRig,
    _Clock,
    _legacy_store,
)


def _series_svc(tmp: Path, rig=None):
    _legacy_store(tmp)
    (tmp / "experiments").mkdir(exist_ok=True)
    return CalibrationService(tmp / "calibration", tmp / "experiments",
                              rig or OdRig(), clock=_Clock())


def _series(svc, rig, target: dict, vials=(0, 1, 2)):
    svc.series_start({"operator": "t", "vials": list(vials), **target})
    svc.series_apply()
    for od in SERIES_LEVELS:
        rig.set_od(vials, od)
        svc.series_point(spec_reading=od, vials=list(vials))
    return svc.series_finish()


def test_the_default_slot_is_derived_without_writing_current_json():
    with tempfile.TemporaryDirectory() as tmp:
        store = _legacy_store(Path(tmp))
        before = (store.root / "current.json").read_bytes()
        profiles = store.od_profiles()
        assert profiles == {"legacy-2016": {
            "version": store.current_versions()["od"],
            "organism": LEGACY_OD_ORGANISM, "is_default": True}}
        assert store.od_default_profile() == "legacy-2016"
        assert (store.root / "current.json").read_bytes() == before


def test_profile_less_curves_are_assigned_by_source_not_all_to_legacy():
    assert od_profile_id_of({"source": "legacy-import-2016"}) == "legacy-2016"
    assert od_profile_id_of({"source": "dilution-series"}) == "machine-default"
    assert od_profile_id_of({"source": "x", "profile": {"id": "e-coli"}}) == "e-coli"
    assert od_profile_slug("Pichia  pastoris GS115") == "pichia-pastoris-gs115"
    assert od_profile_slug("Cupriavidus necátor H16") == "cupriavidus-necator-h16"


def test_a_profile_install_leaves_the_default_and_its_view_alone():
    with tempfile.TemporaryDirectory() as tmp:
        store = _legacy_store(Path(tmp))
        view = (store.root / "OD_cal.txt").read_bytes()
        default = store.current_versions()["od"]
        v1 = store.save_od_profile_version("e-coli", "E. coli", _od_env(store.fresh_version("od")))
        v2 = store.save_od_profile_version("e-coli", "E. coli", _od_env(store.fresh_version("od"), c=0.1))
        assert store.current_versions()["od"] == default
        assert (store.root / "OD_cal.txt").read_bytes() == view
        assert store.get_od_profile("e-coli")["version"] == v2
        assert store.get_od_profile("e-coli")["supersedes"] == v1
        assert store.od_profile_versions("e-coli") == [v2, v1]
        assert store.history()["od_profile_of"][v1] == "e-coli"
        # A pinned version must belong to the profile asked for.
        try:
            store.get_od_profile("legacy-2016", v2)
        except ValueError:
            pass
        else:
            raise AssertionError("another profile's version was accepted")
        for bad in ("legacy-2016", "machine-default", "!!"):
            try:
                store.save_od_profile_version(bad, "x", _od_env(store.fresh_version("od")))
            except ValueError:
                pass
            else:
                raise AssertionError(f"installed into {bad!r}")


def test_a_series_target_is_exactly_one_known_profile_or_one_new_organism():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _series_svc(Path(tmp))
        for body, why in (
            ({}, "exactly one"),
            ({"profile": "legacy-2016", "new_organism": "E. coli"}, "exactly one"),
            ({"profile": "legacy-2016"}, "default curve"),
            ({"profile": "nope"}, "unknown OD profile"),
            ({"new_organism": "x"}, "characters"),
            ({"new_organism": "Legacy 2016"}, "already exists"),
        ):
            try:
                svc.series_start({"operator": "t", **body})
            except ValueError as exc:
                assert why in str(exc), (body, str(exc))
            else:
                raise AssertionError(f"series started for {body}")


def test_introduce_then_recalibrate_an_organism():
    with tempfile.TemporaryDirectory() as tmp:
        rig = OdRig()
        svc = _series_svc(Path(tmp), rig)
        first = _series(svc, rig, {"new_organism": "Pichia pastoris"}, vials=(0, 1, 2))
        assert first["new_profile"] and first["profile_id"] == "pichia-pastoris"
        try:
            svc.series_start({"operator": "t", "new_organism": "PICHIA pastoris"})
        except ValueError as exc:
            assert "recalibrate it instead" in str(exc)
        else:
            raise AssertionError("an existing organism was re-introduced")
        second = _series(svc, rig, {"profile": "pichia-pastoris"}, vials=(2, 3))
        assert second["new_profile"] is False and second["organism"] == "Pichia pastoris"
        env = svc.store.get_od_profile("pichia-pastoris")
        assert env["supersedes"] == first["version"]
        # Vials 0-1 measured by the first series, 2-3 by the second: all are
        # this organism's own; the rest are still the default curve.
        assert svc.store.od_vials_measured("pichia-pastoris", env) == [0, 1, 2, 3]
        summary = {p["id"]: p for p in svc.store.od_profile_summaries()}["pichia-pastoris"]
        assert summary["vials_measured"] == [0, 1, 2, 3]
        assert summary["dark_subtracted"] is False
        assert summary["versions"] == [second["version"], first["version"]]
        assert any("2016" in r for r in summary["reasons"])     # 12 vials carried


def test_the_series_target_survives_a_restart():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _series_svc(Path(tmp))
        svc.series_start({"operator": "t", "vials": [0], "new_organism": "C. necator H16"})
        svc2 = CalibrationService(Path(tmp) / "calibration", Path(tmp) / "experiments",
                                  OdRig(), clock=_Clock())
        svc2.bootstrap()
        cfg = svc2.series_session()["config"]
        assert cfg["organism"] == "C. necator H16" and cfg["profile_id"] == "c-necator-h16"
        assert cfg["new_profile"] is True


def test_staleness_follows_the_loaded_experiments_pinned_profile():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        rig = OdRig()
        svc = _series_svc(tmp, rig)
        out = _series(svc, rig, {"new_organism": "E. coli"}, vials=list(range(N_VIALS)))
        exp = tmp / "experiments" / "run1"
        exp.mkdir()
        (exp / "config.json").write_text(json.dumps({"calibration": {
            "od": out["version"], "od_profile": "e-coli"}}))
        od = svc.store.staleness(loaded_experiment="run1", loaded_status="created")["od"]
        assert od["profile"] == "e-coli" and od["stale"] is False, od
        default = svc.store.staleness()["od"]
        assert default["profile"] == "legacy-2016" and default["stale"] is True


# ---------------------------------------------------------------------------
# Phase 4: a run reads on its organism's pinned curve -- create, start,
# stop, resume
# ---------------------------------------------------------------------------

import threading  # noqa: E402
import time  # noqa: E402

from test_calibration_api import (  # noqa: E402
    _experiment_body,
    _make_app,
    _state,
    _warm_thermal_tracker,
)

# Two organisms whose curves differ from the default and from each other:
# A is dark-subtracted, B reads the raw lit signal (the same truth, with its
# asymptotes carried up by the dark floor -- an exact conversion).
TRUTH_A = _ds_shift(LEGACY_OD)
TRUTH_A[2] -= 0.4
TRUTH_B = _ds_shift(LEGACY_OD)
TRUTH_B[2] += 0.3
RAW_B = TRUTH_B.copy()
RAW_B[0] += DARK
RAW_B[1] += DARK


def _install(store, pid, organism, rows, ds, version):
    env = make_envelope(
        "od", operator="t", source="od-dilution-series", conditions={"n": 1},
        data={"rows": np.asarray(rows).tolist(), "dark_subtracted": ds},
        fit={"per_vial_profile": [pid] * N_VIALS}, version=version,
    )
    return store.save_od_profile_version(pid, organism, env)


def _body(name, vials, profile=None):
    body = _experiment_body(name)
    body["vials"] = list(vials)
    body["media"]["vial_to_bottle"] = {str(v): "b1" for v in vials}
    if profile is not None:
        body["od_profile"] = profile
    return body


def _stop_app(flask_app, threads_before):
    _state(flask_app).sensor_thread_stop.set()
    for t in set(threading.enumerate()) - threads_before:
        if t.name == "sensor-loop":
            t.join(timeout=5)


def _alerts(state, key_part):
    return [e for e in state.event_log.recent() if key_part in (e.get("message") or "")]


def test_two_organisms_read_their_own_curves_in_one_acquisition():
    threads_before = set(threading.enumerate())
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        try:
            state = _state(flask_app)
            store = state.cal_service.store
            _install(store, "org-a", "Organism A", TRUTH_A, True, "2026-09-01T000000Z")
            _install(store, "org-b", "Organism B", RAW_B, False, "2026-09-02T000000Z")
            c = flask_app.test_client()
            r = c.post("/api/experiments/create", json=_body("ra", range(0, 4), "org-a"))
            assert r.status_code == 200, r.get_json()
            assert r.get_json()["organism"] == "Organism A"
            r = c.post("/api/experiments/create", json=_body("rb", range(4, 8), "org-b"))
            assert r.status_code == 200, r.get_json()

            # The photodiodes see each organism's own truth on its vials.
            truth = _ds_shift(LEGACY_OD)
            truth[:, 0:4] = TRUTH_A[:, 0:4]
            truth[:, 4:8] = TRUTH_B[:, 4:8]
            state.manager.set_true_od_curve(truth, dark_counts=DARK)
            state.manager.set_true_od(range(N_VIALS), 0.8)
            rd = state.manager.read_od_enhanced(2125, n_samples=5, dark_subtract=None,
                                                record_dark=True)
            for v in range(8):
                assert abs(rd.calibrated[v] - 0.8) < 1e-9, (v, rd.calibrated[v])
            assert rd.dark_subtracted[:8] == [True] * 4 + [False] * 4
            # The default curve still serves the idle vials.
            assert np.allclose(state.manager.od_cal[:, 12], LEGACY_OD[:, 12])

            cfg = json.loads((Path(tmp) / "experiments" / "ra" / "config.json").read_text())
            cal = cfg["calibration"]
            assert cal["od"] == "2026-09-01T000000Z" and cal["od_profile"] == "org-a"
            assert cal["od_organism"] == "Organism A" and cal["od_dark_subtracted"] is True
            snap = json.loads((Path(tmp) / "experiments" / "ra" / "od_calibration.json").read_text())
            assert snap["version"] == "2026-09-01T000000Z"
            created = [e for e in state.event_log.recent()
                       if (e.get("data") or {}).get("od_profile") == "org-b"]
            assert created, "the created event carries the organism"
            summary = {x["name"]: x for x in state.engine.experiments_summary()}
            assert summary["rb"]["organism"] == "Organism B"
        finally:
            _stop_app(flask_app, threads_before)


def test_stop_and_emergency_stop_return_the_vials_to_the_default_curve():
    threads_before = set(threading.enumerate())
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        try:
            state = _state(flask_app)
            _install(state.cal_service.store, "org-a", "A", TRUTH_A, True, "2026-09-01T000000Z")
            c = flask_app.test_client()
            assert c.post("/api/experiments/create",
                          json=_body("r1", [0], "org-a")).status_code == 200
            assert c.post("/api/experiments/create",
                          json=_body("r2", [1], "org-a")).status_code == 200
            assert np.allclose(state.manager.od_cal[:, :2], TRUTH_A[:, :2])
            assert state.manager.od_dark_subtracted_by_vial[:2] == [True, True]

            c.post("/api/experiments/r1/stop")
            assert np.allclose(state.manager.od_cal[:, 0], LEGACY_OD[:, 0])
            assert np.allclose(state.manager.od_cal[:, 1], TRUTH_A[:, 1])   # r2's own

            assert c.post("/api/experiments/r2/start",
                          json={"allow_missing_od_blank": True}).status_code == 200
            assert c.post("/api/actuators/emergency_stop").status_code == 200
            assert np.allclose(state.manager.od_cal, LEGACY_OD)
            assert not any(state.manager.od_dark_subtracted_by_vial)
        finally:
            _stop_app(flask_app, threads_before)


def test_the_designer_must_name_a_known_organism_and_owns_no_od_keys():
    threads_before = set(threading.enumerate())
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        try:
            state = _state(flask_app)
            _install(state.cal_service.store, "org-a", "A", TRUTH_A, True, "2026-09-01T000000Z")
            c = flask_app.test_client()
            r = c.post("/api/experiments/create", json=_body("x", [0], "pichia"))
            assert r.status_code == 400 and r.get_json()["code"] == "unknown_od_profile"
            assert "org-a" in r.get_json()["profiles"]
            # An organism cannot be created by typing it into the designer.
            assert "pichia" not in state.cal_service.store.od_profiles()
            for key in ("od", "od_profile", "od_dark_subtracted"):
                body = _body("x", [0], "org-a")
                body["calibration"] = {key: "2020-01-01T000000Z"}
                r = c.post("/api/experiments/create", json=body)
                assert r.status_code == 400, (key, r.get_json())
            assert not (Path(tmp) / "experiments" / "x").exists()

            # Omitted: the default slot, labelled and warned about.
            r = c.post("/api/experiments/create", json=_body("y", [0]))
            assert r.status_code == 200, r.get_json()
            d = r.get_json()
            assert d["od_profile"] == "legacy-2016"
            assert d["organism"] == LEGACY_OD_ORGANISM
            assert any("never verified" in w for w in d["warnings"])
        finally:
            _stop_app(flask_app, threads_before)


def test_a_partly_measured_profile_warns_about_the_carried_vials():
    threads_before = set(threading.enumerate())
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        try:
            store = _state(flask_app).cal_service.store
            env = make_envelope(
                "od", operator="t", source="od-dilution-series", conditions={"n": 1},
                data={"rows": TRUTH_A.tolist(), "dark_subtracted": True},
                fit={"per_vial_profile": ["org-a"] * 2 + ["legacy-2016"] * 14},
                version="2026-09-01T000000Z",
            )
            store.save_od_profile_version("org-a", "A", env)
            r = flask_app.test_client().post(
                "/api/experiments/create", json=_body("x", [0, 1, 2, 3], "org-a"))
            assert r.status_code == 200, r.get_json()
            assert any("vials [2, 3] were never measured" in w
                       for w in r.get_json()["warnings"]), r.get_json()["warnings"]
        finally:
            _stop_app(flask_app, threads_before)


def test_the_blank_and_growth_context_follow_the_runs_profile():
    threads_before = set(threading.enumerate())
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        flask_app = _make_app(tmp)
        try:
            state = _state(flask_app)
            # Profile rows the legacy QC would call suspect on no vial.
            rows = TRUTH_A.copy()
            rows[0] = 1000.0
            _install(state.cal_service.store, "org-a", "A", rows, True, "2026-09-01T000000Z")
            c = flask_app.test_client()
            assert c.post("/api/experiments/create",
                          json=_body("b1", [0], "org-a")).status_code == 200
            _warm_thermal_tracker(state, [0])
            sid = c.post("/api/calibration/od/blank/start", json={}).get_json()["session"]
            assert c.post("/api/calibration/od/blank/dark", json={"session": sid}).status_code == 200
            assert c.post("/api/calibration/od/blank/measure", json={"session": sid}).status_code == 200
            _warm_thermal_tracker(state, [0])
            r = c.post("/api/calibration/od/blank/commit",
                       json={"session": sid, "operator": "t"})
            assert r.status_code == 200, r.get_json()
            blank = json.loads((tmp / "experiments" / "b1" / "od_blank.json").read_text())
            assert blank["conditions"]["parent_od_cal"] == "2026-09-01T000000Z"
            assert blank["conditions"]["od_profile"] == "org-a"
            # c_run re-anchors the ORGANISM's row 2, on the organism's signal.
            c_run = float(blank["fit"]["c_run"]["0"])
            signal = float(blank["data"]["blank_signal"][0])
            assert abs(F.od_from_signal(signal, rows[0, 0], rows[1, 0], c_run, rows[3, 0])) < 1e-9
            assert abs(float(state.manager.od_cal[2, 0]) - c_run) < 1e-9

            ctx = state.cal_service.growth_context("b1")
            assert ctx["suspect_vials"] == []
            legacy_suspect = [v for v in range(N_VIALS) if LEGACY_OD[0, v] > 40000.0]
            assert state.cal_service.growth_context(None)["suspect_vials"] == legacy_suspect
            assert c.post("/api/experiments/b1/start").status_code == 200
        finally:
            _stop_app(flask_app, threads_before)


def _od_row(path: Path, after: int, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _rows(path)
        if len(rows) > after:
            return dict(zip(rows[0], rows[after]))
        time.sleep(0.05)
    raise AssertionError(f"no OD row {after} in {path}")


def test_a_resumed_run_reads_its_first_row_on_its_pinned_curve_and_blank():
    import app as A
    saved = (A.SENSOR_LOOP_INTERVAL_SECONDS, A.OD_INTERVAL_SECONDS)
    threads_before = set(threading.enumerate())
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        flask_app = _make_app(tmp)
        try:
            state = _state(flask_app)
            _install(state.cal_service.store, "org-a", "A", TRUTH_A, True, "2026-09-01T000000Z")
            c = flask_app.test_client()
            assert c.post("/api/experiments/create",
                          json=_body("res", [0], "org-a")).status_code == 200
            _warm_thermal_tracker(state, [0])
            sid = c.post("/api/calibration/od/blank/start", json={}).get_json()["session"]
            c.post("/api/calibration/od/blank/dark", json={"session": sid})
            c.post("/api/calibration/od/blank/measure", json={"session": sid})
            _warm_thermal_tracker(state, [0])
            c_run = c.post("/api/calibration/od/blank/commit",
                           json={"session": sid, "operator": "t"}).get_json()["c_run"]["0"]
            assert c.post("/api/experiments/res/start").status_code == 200
            # A newer version of the profile lands while the run is down.
            _install(state.cal_service.store, "org-a", "A", TRUTH_B, True, "2026-09-03T000000Z")
        finally:
            _stop_app(flask_app, threads_before)

        # "Restart": a fresh app and manager resume the run from state.json.
        A.SENSOR_LOOP_INTERVAL_SECONDS = A.OD_INTERVAL_SECONDS = 0.05
        path = tmp / "experiments" / "res" / "vial00_OD.csv"
        before = len(_rows(path)) if path.exists() else 1
        app2 = _make_app(tmp)
        try:
            state2 = _state(app2)
            assert state2.engine.get_run("res") is not None, "the run resumed"
            want = TRUTH_A[:, 0].copy()
            want[2] = c_run
            assert np.allclose(state2.manager.od_cal[:, 0], want)
            row = _od_row(path, before)
            assert row["dark_subtracted"] == "1"
            signal = float(row["lit_adc"]) - float(row["dark_used"])
            od = F.od_from_signal(signal, *want)
            assert abs(od - float(row["calibrated_od"])) < 1e-3, (od, row)
            # It stays on its pinned version; the newer one is only mentioned.
            infos = _alerts(state2, "2026-09-03T000000Z")
            assert infos and infos[0]["level"] == "info", infos
            assert not _alerts(state2, "is missing")
        finally:
            _stop_app(app2, threads_before)
            A.SENSOR_LOOP_INTERVAL_SECONDS, A.OD_INTERVAL_SECONDS = saved


def test_pre_profile_configs_resume_and_a_lost_pin_is_quarantined():
    threads_before = set(threading.enumerate())
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        flask_app = _make_app(tmp)
        try:
            c = flask_app.test_client()
            for name, vial in (("old", 0), ("older", 1), ("lost", 2)):
                assert c.post("/api/experiments/create",
                              json=_body(name, [vial])).status_code == 200
                assert c.post(f"/api/experiments/{name}/start",
                              json={"allow_missing_od_blank": True}).status_code == 200
        finally:
            _stop_app(flask_app, threads_before)

        def edit(name, fn):
            p = tmp / "experiments" / name / "config.json"
            cfg = json.loads(p.read_text())
            fn(cfg)
            p.write_text(json.dumps(cfg))

        # Session O config: a version, no profile. Pre-Session-O: nothing.
        edit("old", lambda cfg: [cfg["calibration"].pop(k) for k in list(cfg["calibration"])
                                 if k.startswith("od") and k != "od"])
        edit("older", lambda cfg: cfg.pop("calibration"))
        edit("lost", lambda cfg: cfg["calibration"].update(
            od="2020-01-01T000000Z", od_profile="gone"))

        app2 = _make_app(tmp)
        try:
            state2 = _state(app2)
            assert np.allclose(state2.manager.od_cal[:, :2], LEGACY_OD[:, :2])
            assert np.all(np.isnan(state2.manager.od_cal[:, 2]))
            crit = _alerts(state2, "is missing")
            assert len(crit) == 1 and "'lost'" in crit[0]["message"]
            assert crit[0]["level"] == "critical"
            # Stopping the quarantined run hands vial 2 back its default curve.
            assert app2.test_client().post("/api/experiments/lost/stop").status_code == 200
            assert np.allclose(state2.manager.od_cal[:, 2], LEGACY_OD[:, 2])
        finally:
            _stop_app(app2, threads_before)


def test_start_refuses_a_run_whose_pinned_curve_is_gone():
    threads_before = set(threading.enumerate())
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        flask_app = _make_app(tmp)
        try:
            c = flask_app.test_client()
            assert c.post("/api/experiments/create", json=_body("s", [3])).status_code == 200
            p = tmp / "experiments" / "s" / "config.json"
            cfg = json.loads(p.read_text())
            cfg["calibration"].update(od="2020-01-01T000000Z", od_profile="gone")
            p.write_text(json.dumps(cfg))
            r = c.post("/api/experiments/s/start", json={"allow_missing_od_blank": True})
            assert r.status_code == 409 and r.get_json()["code"] == "od_pin_missing"
            assert _state(flask_app).engine.get_run("s").status()["status"] == "created"
        finally:
            _stop_app(flask_app, threads_before)
