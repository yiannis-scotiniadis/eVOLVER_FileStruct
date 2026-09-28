"""Tests for the Tier 3 calibration work (ROADMAP Session AA / SPEC §19.5).

Service- and serial-level: the derived-view integrity check, structured
staleness, the machine-reserving Tier 3 sessions and their safety tick, the
two-point thermistor wizard and the OD dilution-series wizard. The HTTP
surface is in test_calibration_tier3_api.py; the pure 4-parameter fit is in
test_od_curve_fit.py.

Run from the project root:
    python -m pytest server/test_calibration_tier3.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from calibration_service import (  # noqa: E402
    CalibrationStore,
    N_VIALS,
    TempStabilityTracker,
    make_envelope,
    per_vial_sources,
)

REPO_CAL = Path(__file__).resolve().parent.parent / "calibration"


def _legacy_store(tmp: Path) -> CalibrationStore:
    """A store bootstrapped from copies of the committed 2016 files."""
    cal = tmp / "calibration"
    cal.mkdir(parents=True, exist_ok=True)
    for f in ("OD_cal.txt", "temp_calibration.txt"):
        (cal / f).write_bytes((REPO_CAL / f).read_bytes())
    store = CalibrationStore(cal, tmp / "experiments")
    store.bootstrap_from_legacy()
    return store


# ---------------------------------------------------------------------------
# Phase 0: derived-view integrity + structured staleness
# ---------------------------------------------------------------------------

def test_ensure_views_leaves_consistent_legacy_files_byte_identical():
    """The views are tracked in git; a boot must not reformat them."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _legacy_store(Path(tmp))
        before = {
            f: (store.root / f).read_bytes()
            for f in ("OD_cal.txt", "temp_calibration.txt")
        }
        assert store.ensure_legacy_views() == []
        for f, data in before.items():
            assert (store.root / f).read_bytes() == data
        assert not (store.root / "OD_cal.meta.json").exists()


def test_ensure_views_repairs_truncated_and_out_of_date_views():
    with tempfile.TemporaryDirectory() as tmp:
        store = _legacy_store(Path(tmp))
        rows = np.genfromtxt(store.root / "OD_cal.txt", delimiter=",")
        # A half-written view (the pre-atomic failure mode).
        text = (store.root / "OD_cal.txt").read_text()
        (store.root / "OD_cal.txt").write_text(text[: len(text) // 2])
        assert store.ensure_legacy_views() == ["od"]
        assert np.allclose(
            np.genfromtxt(store.root / "OD_cal.txt", delimiter=","), rows,
        )
        # current.json names a version whose view was never written (a crash
        # between save_version's pointer update and its view regeneration).
        new = make_envelope(
            "temperature", operator="t", source="two-point-reference",
            conditions={"n": 1},
            data={"slope": [-0.1] * N_VIALS, "intercept": [80.0] * N_VIALS},
            version="2026-09-01T000000Z",
        )
        store._write_version_locked("temperature", new)
        current = store._read_current()
        current["temperature"] = new["version"]
        store._write_current(current)
        assert store.ensure_legacy_views() == ["temperature"]
        temp = np.genfromtxt(store.root / "temp_calibration.txt", delimiter=",")
        assert np.allclose(temp[0], -0.1) and np.allclose(temp[1], 80.0)


def test_ensure_views_repairs_a_sidecar_that_disagrees():
    with tempfile.TemporaryDirectory() as tmp:
        store = _legacy_store(Path(tmp))
        (store.root / "OD_cal.meta.json").write_text(
            json.dumps({"dark_subtracted": True})
        )
        assert store.ensure_legacy_views() == ["od"]
        meta = json.loads((store.root / "OD_cal.meta.json").read_text())
        assert meta["dark_subtracted"] is False


def test_per_vial_sources_falls_back_to_the_envelope_source():
    env = {"source": "legacy-import-2016", "fit": {}}
    assert per_vial_sources(env) == ["legacy-import-2016"] * N_VIALS
    env["fit"]["per_vial_source"] = ["two-point-reference"] * N_VIALS
    assert per_vial_sources(env)[0] == "two-point-reference"


def _tier3_temperature(version, *, flagged=None, verified=(), legacy_vials=()):
    sources = [
        "legacy-import-2016" if v in legacy_vials else "two-point-reference"
        for v in range(N_VIALS)
    ]
    return make_envelope(
        "temperature", operator="t", source="two-point-reference",
        conditions={"n": 1},
        data={"slope": [-0.11] * N_VIALS, "intercept": [82.0] * N_VIALS},
        fit={
            "per_vial_source": sources,
            "per_vial_version": [version] * N_VIALS,
            "flagged_vials": {str(v): "outside pack" for v in (flagged or [])},
        },
        qc={
            "passed": True,
            # A missing vial map warns on every write until P1 is done.
            "warnings": ["vial_map.json is missing (prerequisite P1)"],
            "failures": [],
            "overridden_by": None,
            "verification": {"passed_vials": list(verified)},
        },
        version=version,
    )


def test_staleness_vial_map_warning_alone_is_not_stale():
    """Before Session AA any qc warning marked temperature stale, so every
    new version would have read 'stale' forever (the vial-map warning)."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _legacy_store(Path(tmp))
        store.save_version("temperature", _tier3_temperature("2026-09-01T000000Z"))
        stale = store.staleness()["temperature"]
        assert stale["stale"] is False, stale


def test_staleness_unverified_flagged_vials_and_partial_legacy():
    with tempfile.TemporaryDirectory() as tmp:
        store = _legacy_store(Path(tmp))
        store.save_version("temperature", _tier3_temperature(
            "2026-09-01T000000Z", flagged=[0, 3], verified=[3],
            legacy_vials=[7, 8],
        ))
        reasons = store.staleness()["temperature"]["reasons"]
        assert any("[0]" in r and "not independently verified" in r
                   for r in reasons), reasons
        assert not any("[0, 3]" in r for r in reasons)
        assert any("[7, 8]" in r and "2016" in r for r in reasons), reasons


def test_staleness_legacy_import_keeps_its_audit_message():
    with tempfile.TemporaryDirectory() as tmp:
        store = _legacy_store(Path(tmp))
        reasons = store.staleness()["temperature"]["reasons"]
        assert any("never verified" in r for r in reasons)
        assert any("vial 0 is the prime suspect" in r for r in reasons)


def test_tracker_reset_forgets_old_scale_history():
    tr = TempStabilityTracker(clock=lambda: 1000.0)
    tr.note([37.0] * N_VIALS)
    tr.reset()
    assert tr.settled([0], {0: 37.0})["per_vial"]["0"]["n_samples"] == 0


# ---------------------------------------------------------------------------
# Phase 1: machine reservation, safety tick, serial primitives, mock truth
# ---------------------------------------------------------------------------

import calibration_service as cs  # noqa: E402
from calibration_service import (  # noqa: E402
    CAL_HEATER_CRITICAL_C,
    CalibrationConflict,
    CalibrationService,
    RawTempTracker,
    Tier3Session,
)
from mock_serial_manager import MockSerialManager  # noqa: E402
from serial_manager import (  # noqa: E402
    DEFAULT_RAW_FLOOR,
    HEATER_OFF_SETPOINT,
    SerialManager,
)
from test_serial_manager import FakeSerial, fresh, fresh_dark_marked  # noqa: E402


class _Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _ActuatorManager:
    """Records the actuator writes a Tier 3 session makes."""

    def __init__(self) -> None:
        self.stir_calls: list[list[int]] = []
        self.temp_calls: list[list[int]] = []
        self.temp_setpoint_raw = np.full(N_VIALS, HEATER_OFF_SETPOINT, dtype=int)
        self.stir_speed = np.zeros(N_VIALS, dtype=int)

    def set_stir(self, values) -> None:
        self.stir_calls.append(list(values))
        self.stir_speed = np.asarray(values, dtype=int)

    def set_temperature_raw(self, setpoints, floor_override=None):
        self.temp_calls.append([int(x) for x in setpoints])
        self.temp_setpoint_raw = np.asarray(setpoints, dtype=int)
        return [22.0] * N_VIALS


class _ProbeSession(Tier3Session):
    kind = "probe"


def _probe_service(tmp: Path, manager=None, clock=None) -> CalibrationService:
    cal_root = tmp / "calibration"
    cal_root.mkdir(parents=True, exist_ok=True)
    (tmp / "experiments").mkdir(exist_ok=True)
    return CalibrationService(cal_root, tmp / "experiments",
                              manager or _ActuatorManager(), clock=clock or _Clock())


def _open_probe(svc, vials=(0, 1, 2), stir=8, armed=True) -> _ProbeSession:
    s = _ProbeSession(operator="t", vials=list(vials), stir_pwm=stir)
    svc._begin_tier3(s)
    if armed:
        svc._arm_tier3(s, "probe")
    return s


def test_tier3_begin_refuses_second_session_pump_and_blank():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _probe_service(Path(tmp))
        svc.pump_start({"pumps": [0]})
        try:
            _open_probe(svc)
        except CalibrationConflict as exc:
            assert "pump" in str(exc)
        else:
            raise AssertionError("Tier 3 opened over a pump session")
        svc.pump_abort()
        _open_probe(svc)
        for fn in (lambda: _open_probe(svc),
                   lambda: svc.pump_start({"pumps": [0]})):
            try:
                fn()
            except CalibrationConflict:
                pass
            else:
                raise AssertionError("second machine-wide session allowed")


def test_tick_resends_stir_only_while_armed_and_fault_stops_it():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _ActuatorManager()
        svc = _probe_service(Path(tmp), manager=mgr)
        _open_probe(svc, vials=(0, 1), stir=9)
        svc.tick([22.0] * N_VIALS, [540.0] * N_VIALS)
        assert mgr.stir_calls[-1] == [9, 9] + [0] * (N_VIALS - 2)
        # Emergency stop: the stop zeroes stir; the tick must not undo it.
        svc.fault("emergency stop")
        n = len(mgr.stir_calls)
        svc.tick([22.0] * N_VIALS, [540.0] * N_VIALS)
        assert len(mgr.stir_calls) == n
        assert svc.tier3_status()["state"] == "faulted"
        assert svc.tier3_status()["state_reason"] == "emergency stop"


def test_heater_watchdog_parks_after_three_reads_once():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _ActuatorManager()
        svc = _probe_service(Path(tmp), manager=mgr)
        _open_probe(svc)
        hot = [30.0] * N_VIALS
        # Vial 9 is NOT in the session: a stuck heater anywhere matters.
        hot[9] = CAL_HEATER_CRITICAL_C + 2.0
        alerts = []
        for i in range(2):
            alerts += svc.tick(hot, [500.0] * N_VIALS)
        assert alerts == [] and mgr.temp_calls == []
        # A dropped read neither extends nor resets the streak.
        dropped = list(hot)
        dropped[9] = float("nan")
        alerts += svc.tick(dropped, [500.0] * N_VIALS)
        assert alerts == []
        alerts += svc.tick(hot, [500.0] * N_VIALS)
        assert len(alerts) == 1 and alerts[0]["vial"] == 9
        assert alerts[0]["dedup_key"] == ("cal_heater_critical", 9)
        assert mgr.temp_calls[-1][9] == HEATER_OFF_SETPOINT
        assert svc.tier3_status()["vial_faults"] == {"9": "overtemp"}
        # Latched: no second park or alert while it stays hot.
        n = len(mgr.temp_calls)
        assert svc.tick(hot, [500.0] * N_VIALS) == []
        assert len(mgr.temp_calls) == n


def test_tier3_session_reloads_suspended_after_restart():
    with tempfile.TemporaryDirectory() as tmp:
        cs.TIER3_SESSION_CLASSES["probe"] = _ProbeSession
        try:
            svc = _probe_service(Path(tmp))
            s = _open_probe(svc)
            s.commanded_raw[0] = 482
            svc._persist_tier3()
            assert svc.tier3_status()["state"] == "armed"
            # A fresh process on the same calibration dir.
            mgr = _ActuatorManager()
            svc2 = _probe_service(Path(tmp), manager=mgr)
            svc2.bootstrap()
            status = svc2.tier3_status()
            assert status["state"] == "suspended"
            assert "restarted" in status["state_reason"]
            assert svc2._tier3.commanded_raw == {0: 482}
            # Suspended drives nothing.
            svc2.tick([22.0] * N_VIALS, [540.0] * N_VIALS)
            assert mgr.stir_calls == []
        finally:
            cs.TIER3_SESSION_CLASSES.pop("probe", None)


def test_end_tier3_parks_heaters_and_leaves_no_file():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _ActuatorManager()
        svc = _probe_service(Path(tmp), manager=mgr)
        _open_probe(svc)
        path = svc._tier3_path("probe")
        assert path.is_file()
        svc._end_tier3()
        assert not path.exists()
        assert mgr.temp_calls[-1] == [HEATER_OFF_SETPOINT] * N_VIALS
        assert svc.tier3_status() is None
        assert list((svc.store.root).glob("*/*.json")) == []


def test_raw_tracker_pack_relative_cancels_common_drift():
    clock = _Clock()
    tr = RawTempTracker(clock=clock)
    for i in range(10 * 6):  # 10 min at 10 s
        clock.t += 10.0
        raw = np.full(N_VIALS, 540.0 + 0.5 * i)  # the whole room warms
        raw[3] = 600.0 - 0.3 * i                 # vial 3 heats on its own
        tr.note(raw)
    abs_med, coverage = tr.minute_medians(10)
    rel_med, _ = tr.minute_medians(10, relative_to=list(range(N_VIALS)))
    assert coverage > 9.5
    assert np.ptp(abs_med[:, 0]) > 20            # absolute drift is large
    assert np.ptp(rel_med[:, 0]) < 1e-9          # shared drift cancels
    assert np.ptp(rel_med[:, 3]) > 40            # a lone mover does not


def test_collect_temp_raw_frames_and_stats():
    sm, fake = fresh()
    from test_serial_manager import _temp_response
    fake.read_queue = [_temp_response([500 + i] * N_VIALS) for i in range(3)]
    out = sm.collect_temp_raw(3)
    assert out["median"][0] == 501.0 and out["n_valid"] == [3] * N_VIALS
    # Every read re-sends the CURRENT setpoints: capturing changes nothing.
    assert len(fake.write_log) == 3
    assert all(w.startswith(b"xr4095,") for w in fake.write_log)
    assert list(sm.last_temperature_raw) == [502.0] * N_VIALS


def test_read_temperature_with_raw_is_one_transaction():
    sm, fake = fresh()
    from test_serial_manager import _temp_response
    fake.read_queue = [_temp_response([500] * N_VIALS)]
    c, raw = sm.read_temperature_with_raw()
    assert len(fake.write_log) == 1
    assert raw == [500.0] * N_VIALS
    assert np.allclose(c, 500.0 * sm.temp_cal[0] + sm.temp_cal[1])


def test_floor_override_is_per_vial_and_never_below_default():
    sm, fake = fresh()
    from test_serial_manager import _temp_response
    floor = sm._raw_floor_per_vial()
    fake.read_queue = [_temp_response([500] * N_VIALS)]
    sm.set_temperature_raw([0] * N_VIALS, floor_override={0: 376, 1: 12})
    sent = sm.temp_setpoint_raw
    assert sent[0] == 376                      # below vial 0's installed floor
    assert floor[0] > 376
    assert sent[1] == DEFAULT_RAW_FLOOR        # an override can't go hotter
    assert list(sent[2:]) == list(floor[2:])   # others keep theirs


def test_dropped_dark_read_falls_back_to_last_good_dark():
    with tempfile.TemporaryDirectory() as tmp:
        sm, fake = fresh_dark_marked(Path(tmp))

        def turb(v):
            return ("turb" + ",".join(str(x) for x in [v] * N_VIALS) + ",end\n").encode()
        # Cycle 1: good dark 1200, signal 58000.
        fake.read_queue = [turb(1200), turb(58000)]
        r1 = sm.read_od_enhanced(2125, n_samples=1, dark_subtract=True, n_dark=1)
        # Cycle 2: the dark read drops (timeout), signal 58000.
        fake.read_queue = [b"", turb(58000)]
        r2 = sm.read_od_enhanced(2125, n_samples=1, dark_subtract=True, n_dark=1)
        assert r1.raw[0] == 58000 - 1200
        assert r2.raw[0] == 58000 - 1200, "dropped dark subtracted 0"


def test_mock_truth_is_pinned_and_independent_of_installed_cal():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        m = MockSerialManager(seed=1)
        m.load_calibration(str(REPO_CAL / "temp_calibration.txt"),
                           str(REPO_CAL / "OD_cal.txt"))
        truth = m._true_temp_cal.copy()
        # Install a different curve: the physics must not follow it.
        other = tmp / "temp_calibration.txt"
        np.savetxt(other, np.vstack([np.full(N_VIALS, -0.1), np.full(N_VIALS, 79.0)]),
                   delimiter=",")
        m.load_calibration(str(other), str(REPO_CAL / "OD_cal.txt"))
        assert np.array_equal(m._true_temp_cal, truth)
        # Heaters off: the vial relaxes to ambient; the raw counts are the
        # TRUE response's, and reported °C is the installed curve applied
        # to them (so it is wrong by exactly the curve difference).
        for _ in range(20):
            c, raw = m.read_temperature_with_raw()
        want_raw = (np.asarray(m.temp_C) - truth[1]) / truth[0]
        assert np.allclose(raw, want_raw)
        assert np.allclose(c, np.asarray(raw) * -0.1 + 79.0)
        stats = m.collect_temp_raw(5)
        assert stats["n_valid"] == [5] * N_VIALS


def test_mock_true_od_freeze_and_curve():
    m = MockSerialManager(seed=2)
    m.load_calibration(str(REPO_CAL / "temp_calibration.txt"),
                       str(REPO_CAL / "OD_cal.txt"))
    rows = np.vstack([np.full(N_VIALS, 500.0), np.full(N_VIALS, 90000.0),
                      np.full(N_VIALS, -0.3), np.full(N_VIALS, -0.8)])
    m.set_true_od_curve(rows, dark_counts=1500.0)
    m.set_true_od([0, 1], 2.0)      # above the growth model's 1.575 clip
    m.set_stir([8] * N_VIALS)
    lit = m.collect_od_raw(2125, 5)
    dark = m.collect_od_raw(0, 5)
    a, b, c, d = rows[:, 0]
    want = 1500.0 + a + (b - a) / (1 + 10 ** (d * (c - 2.0)))
    assert abs(lit["median"][0] - want) < 400
    assert abs(dark["median"][0] - 1500.0) < 100
    assert m.od_abs[0] == 2.0
    m.set_true_od([0], None)
    assert 0 not in m._frozen_od


# ---------------------------------------------------------------------------
# Phase 3: two-point thermistor calibration (CALIBRATION_PROTOCOL §8.1)
# ---------------------------------------------------------------------------

from calibration_service import (  # noqa: E402
    QCRefusal,
    TEMP_CAL_SOURCE,
    TempCalSession,
)
from serial_manager import MAX_SAFE_TEMP_C, apply_floor_override  # noqa: E402

LEGACY_TEMP = np.genfromtxt(REPO_CAL / "temp_calibration.txt", delimiter=",")
# The pack excluding vial 0 (CALIBRATION_PROTOCOL Appendix A.2): the audit's
# hypothesis is that vial 0's sleeve really behaves like this.
PACK_SLOPE, PACK_INTERCEPT = -0.11130, 81.897


class ThermalRig:
    """Deterministic stand-in for 16 sleeves under the firmware's loop.

    Each vial's thermistor truly reads ``raw = (T - i_true) / s_true``. The
    firmware drives that raw reading to the ``xr`` setpoint, so a vial
    settles at ``T = s_true * xr + i_true`` -- never below ambient (there is
    no cooling). The INSTALLED curve (``temp_cal``) only converts counts to
    reported °C and sets the safety floor, as on the real manager."""

    def __init__(self, true_slope=PACK_SLOPE, true_intercept=PACK_INTERCEPT,
                 installed=LEGACY_TEMP, ambient=22.0):
        self.true = np.vstack([np.broadcast_to(true_slope, (N_VIALS,)),
                               np.broadcast_to(true_intercept, (N_VIALS,))]).astype(float)
        self.temp_cal = np.array(installed, dtype=float)
        self.ambient = float(ambient)
        self.temp_setpoint_raw = np.full(N_VIALS, HEATER_OFF_SETPOINT, dtype=int)
        self.stir_speed = np.zeros(N_VIALS, dtype=int)
        self.stuck_heating_c_per_read: dict[int, float] = {}
        self._stuck_extra = np.zeros(N_VIALS)
        # Optional curvature: T_true(raw) gains quad * (raw - raw0)^2.
        self.quad = np.zeros(N_VIALS)
        self.stir_calls = 0

    def _floor(self):
        floor = np.rint((MAX_SAFE_TEMP_C - self.temp_cal[1]) / self.temp_cal[0]).astype(int)
        return floor

    def set_temperature_raw(self, setpoints, floor_override=None):
        floor = apply_floor_override(self._floor(), floor_override)
        raw = np.clip(np.maximum(np.asarray(setpoints, dtype=int), floor), None,
                      HEATER_OFF_SETPOINT)
        self.temp_setpoint_raw = raw
        return self.reported()

    def set_stir(self, values):
        self.stir_calls += 1
        self.stir_speed = np.asarray(values, dtype=int)

    def true_temps(self) -> np.ndarray:
        target = self.true[0] * self.temp_setpoint_raw + self.true[1]
        return np.maximum(target, self.ambient) + self._stuck_extra

    def raw(self) -> np.ndarray:
        T = self.true_temps()
        raw = (T - self.true[1]) / self.true[0]
        # Curvature in the thermistor itself: the same T reads differently.
        return raw + self.quad * (T - 30.0) ** 2

    def reported(self) -> list:
        return (self.raw() * self.temp_cal[0] + self.temp_cal[1]).tolist()

    def step(self):
        for v, rate in self.stuck_heating_c_per_read.items():
            self._stuck_extra[v] += rate

    def read_temperature_with_raw(self):
        self.step()
        return self.reported(), self.raw().tolist()

    def collect_temp_raw(self, n_samples=5):
        raw = self.raw()
        return {"median": raw.tolist(), "sd": [0.5] * N_VIALS,
                "n_valid": [n_samples] * N_VIALS}


def _temp_service(tmp: Path, rig: ThermalRig, clock=None) -> CalibrationService:
    """A bootstrapped (legacy-import) store + service driving ``rig``."""
    _legacy_store(tmp)          # writes the 2016 files and imports them
    svc = CalibrationService(tmp / "calibration", tmp / "experiments", rig,
                             clock=clock or _Clock())
    return svc


def _ticks(svc, rig, clock, minutes: float):
    for _ in range(int(minutes * 6)):
        clock.t += 10.0
        c, raw = rig.read_temperature_with_raw()
        svc.tick(c, raw)


def _probe(rig: ThermalRig, vial: int) -> float:
    """The reference thermometer: the vial's TRUE temperature."""
    return float(rig.true_temps()[vial])


def _cold_point(svc, rig, clock, reference_vial=3):
    svc.temp_apply("cold")
    _ticks(svc, rig, clock, 11)
    return svc.temp_point("cold", _probe(rig, reference_vial),
                          reference_vial=reference_vial)


def _hot_points(svc, rig, clock, vials=None):
    svc.temp_apply("hot")
    _ticks(svc, rig, clock, 6)
    for v in (vials if vials is not None else range(N_VIALS)):
        svc.temp_point("hot", _probe(rig, v), vials=[v])


def test_thermistor_wizard_recovers_vial_0_and_reports_what_it_delivered():
    """The headline scenario the session exists for. Vial 0's installed curve
    is the 5-sigma outlier; its sleeve truly behaves like the pack. The
    wizard must land it at 40 °C (retarget), recover the true line, and say
    what the old curve actually delivered when the UI said 37 °C (~28 °C)."""
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        rig = ThermalRig()
        svc = _temp_service(Path(tmp), rig, clock)
        started = svc.temp_start({"operator": "t", "reference_thermometer": "Fluke 51",
                                  "bench_temp_c": 21.0})
        assert started["state"] == "suspended"      # drives nothing until apply
        _cold_point(svc, rig, clock)
        status = svc.temp_session()
        assert all(status["per_vial"][str(v)]["p2"]["status"] == "ok" for v in range(N_VIALS))
        # Vial 0 reads ~9 °C hot at ambient on its installed curve: advisory.
        assert status["per_vial"]["0"]["advisory"]
        assert not status["per_vial"]["1"]["advisory"]

        _hot_points(svc, rig, clock)
        v0 = svc.temp_session()["per_vial"]["0"]
        assert v0["span_c"] < 15.0 and v0["retarget_suggested"], v0
        assert not svc.temp_session()["per_vial"]["5"]["retarget_suggested"]

        # Retarget vial 0 through its own provisional line. Its xr lands
        # BELOW the installed curve's 45 °C floor -- the override is needed.
        installed_floor = int(rig._floor()[0])
        svc.temp_retarget([0])
        assert rig.temp_setpoint_raw[0] < installed_floor
        _ticks(svc, rig, clock, 6)
        # The installed curve now reads vial 0 near 48 °C; the watchdog must
        # judge it by its own line and leave it alone.
        assert svc.tier3_status()["vial_faults"] == {}
        assert abs(_probe(rig, 0) - 40.0) < 0.3
        svc.temp_point("hot", _probe(rig, 0), vials=[0])
        assert svc.temp_session()["per_vial"]["0"]["span_c"] > 15.0

        review = svc.temp_fit()
        # Unverified: the review says so, the finish would refuse.
        assert any("verification incomplete" in f for f in review["qc"]["failures"])
        r0 = review["fit"]["review"]["0"]
        assert abs(r0["slope"] - PACK_SLOPE) < 1e-9
        assert abs(r0["intercept"] - PACK_INTERCEPT) < 1e-6
        assert r0["xr_37_previous"] == 482
        assert abs(r0["previous_delivered_at_37_c"] - 28.25) < 0.05, r0

        svc.temp_apply("verify")
        _ticks(svc, rig, clock, 6)
        for v in (0, 1, 7, 12):
            svc.temp_point("verify", _probe(rig, v), vials=[v])
        result = svc.temp_finish()
        assert result["status"] == "installed" and result["qc"]["passed"], result["qc"]
        env = svc.store.get_current("temperature")
        assert np.allclose(env["data"]["slope"], PACK_SLOPE)
        assert np.allclose(env["data"]["intercept"], PACK_INTERCEPT)
        assert env["fit"]["per_vial_source"] == [TEMP_CAL_SOURCE] * N_VIALS
        assert env["fit"]["flagged_vials"] == {}
        assert env["conditions"]["reference_thermometer"] == "Fluke 51"
        assert env["qc"]["verification"]["passed_vials"] == [0, 1, 7, 12]
        # Session closed: heaters parked, file gone, staleness clean.
        assert svc.tier3_status() is None
        assert list(rig.temp_setpoint_raw) == [HEATER_OFF_SETPOINT] * N_VIALS
        stale = svc.store.staleness()["temperature"]
        assert stale["stale"] is False, stale


def test_cold_point_waits_for_equilibrium_unless_overridden():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        rig = ThermalRig()
        svc = _temp_service(Path(tmp), rig, clock)
        svc.temp_start({"operator": "t"})
        svc.temp_apply("cold")
        _ticks(svc, rig, clock, 4)
        try:
            svc.temp_point("cold", 22.0, reference_vial=0)
        except CalibrationConflict as exc:
            assert "equilibrated" in str(exc)
        else:
            raise AssertionError("captured before a full window of stability")
        out = svc.temp_point("cold", 22.0, reference_vial=0, settle_check=False)
        assert all(p["settle_checked"] is False for p in out["captured"])
        assert svc.temp_session()["per_vial"]["0"]["points"][0]["reference_vial"] == 0


def test_p2_heater_fault_excludes_a_vial_heating_while_off():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        rig = ThermalRig()
        rig.stuck_heating_c_per_read[6] = 0.05     # ~27 counts / 10 min
        svc = _temp_service(Path(tmp), rig, clock)
        svc.temp_start({"operator": "t"})
        svc.temp_apply("cold")
        alerts = []
        for _ in range(11 * 6):
            clock.t += 10.0
            c, raw = rig.read_temperature_with_raw()
            alerts += svc.tick(c, raw)
        assert [a["vial"] for a in alerts] == [6], alerts
        assert alerts[0]["dedup_key"] == ("cal_p2_heater_fault", 6)
        status = svc.temp_session()
        assert status["per_vial"]["6"]["p2"]["status"] == "heater_fault"
        assert "P2" in status["vial_faults"]["6"]
        # The cold point proceeds without it.
        out = svc.temp_point("cold", 22.0, reference_vial=0)
        assert 6 not in [p["vial"] for p in out["captured"]]


def test_room_warming_is_not_a_p2_fault():
    """Every vial drifts warmer together: absolute heating, no relative."""
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        rig = ThermalRig()
        for v in range(N_VIALS):
            rig.stuck_heating_c_per_read[v] = 0.05
        svc = _temp_service(Path(tmp), rig, clock)
        svc.temp_start({"operator": "t"})
        svc.temp_apply("cold")
        alerts = []
        for _ in range(11 * 6):
            clock.t += 10.0
            c, raw = rig.read_temperature_with_raw()
            alerts += svc.tick(c, raw)
        assert alerts == []
        assert svc.tier3_status()["vial_faults"] == {}
        # And the pack-relative cold gate is satisfied despite the drift.
        assert svc.temp_session()["cold_ready"] is True


def test_nonlinear_thermistor_fails_verification_and_override_is_recorded():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        rig = ThermalRig(installed=np.vstack([np.full(N_VIALS, PACK_SLOPE),
                                              np.full(N_VIALS, PACK_INTERCEPT)]))
        rig.quad[2] = 0.08                          # a curved response
        svc = _temp_service(Path(tmp), rig, clock)
        svc.temp_start({"operator": "t"})
        _cold_point(svc, rig, clock)
        _hot_points(svc, rig, clock)
        svc.temp_apply("verify")
        _ticks(svc, rig, clock, 6)
        for v in (1, 2, 3, 4):
            svc.temp_point("verify", _probe(rig, v), vials=[v])
        try:
            svc.temp_finish()
        except QCRefusal as exc:
            assert any("vial 2" in f and "not linear" in f for f in exc.qc["failures"])
        else:
            raise AssertionError("a curved thermistor passed verification")
        out = svc.temp_finish(override_reason="bench: accept, re-run with a third point")
        assert out["qc"]["overridden_by"].startswith("bench")


def test_outlier_sleeve_is_flagged_not_refused():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        intercepts = np.full(N_VIALS, PACK_INTERCEPT)
        intercepts[9] += 4.0                         # genuinely different sleeve
        rig = ThermalRig(true_intercept=intercepts,
                         installed=np.vstack([np.full(N_VIALS, PACK_SLOPE), intercepts]))
        svc = _temp_service(Path(tmp), rig, clock)
        svc.temp_start({"operator": "t"})
        _cold_point(svc, rig, clock)
        _hot_points(svc, rig, clock)
        svc.temp_apply("verify")
        _ticks(svc, rig, clock, 6)
        for v in (0, 1, 2, 3):                        # vial 9 NOT verified
            svc.temp_point("verify", _probe(rig, v), vials=[v])
        out = svc.temp_finish()
        assert out["qc"]["passed"]
        env = svc.store.get_current("temperature")
        assert "9" in env["fit"]["flagged_vials"]
        reasons = svc.store.staleness()["temperature"]["reasons"]
        assert any("[9]" in r and "not independently verified" in r for r in reasons)


def test_partial_session_merges_and_needs_every_measured_vial_verified():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        rig = ThermalRig()
        svc = _temp_service(Path(tmp), rig, clock)
        svc.temp_start({"operator": "t", "vials": [0]})
        svc.temp_apply("cold")
        _ticks(svc, rig, clock, 11)
        svc.temp_point("cold", _probe(rig, 0), reference_vial=0)
        _hot_points(svc, rig, clock, vials=[0])
        svc.temp_retarget([0])
        _ticks(svc, rig, clock, 6)
        svc.temp_point("hot", _probe(rig, 0), vials=[0])
        fit = svc.temp_fit()
        assert fit["qc"]["verification"]["required"] == 1
        svc.temp_apply("verify")
        _ticks(svc, rig, clock, 6)
        svc.temp_point("verify", _probe(rig, 0), vials=[0])
        out = svc.temp_finish()
        env = svc.store.get_current("temperature")
        assert out["measured_vials"] == [0]
        assert env["data"]["slope"][5] == LEGACY_TEMP[0][5]      # carried
        assert env["fit"]["per_vial_source"][0] == TEMP_CAL_SOURCE
        assert env["fit"]["per_vial_source"][5] == "legacy-import-2016"
        assert any("keep their 2016 constants" in w for w in env["qc"]["warnings"])
        reasons = svc.store.staleness()["temperature"]["reasons"]
        assert any("[1, 2, 3" in r and "2016" in r for r in reasons), reasons


def test_thermistor_session_survives_a_restart_suspended():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        rig = ThermalRig()
        svc = _temp_service(Path(tmp), rig, clock)
        svc.temp_start({"operator": "t"})
        _cold_point(svc, rig, clock)
        rig2 = ThermalRig()
        svc2 = CalibrationService(Path(tmp) / "calibration", Path(tmp) / "experiments",
                                  rig2, clock=clock)
        svc2.bootstrap()
        st = svc2.temp_session()
        assert st["state"] == "suspended"
        assert len([p for p in st["per_vial"]["4"]["points"] if p["label"] == "cold"]) == 1
        try:
            svc2.temp_point("cold", 22.0, reference_vial=0)
        except CalibrationConflict as exc:
            assert "re-apply" in str(exc)
        else:
            raise AssertionError("captured on a suspended session")
        _hot_points(svc2, rig2, clock)          # apply re-arms and carries on
        assert svc2.temp_session()["per_vial"]["4"]["span_c"] > 15


def test_thermistor_abort_parks_heaters_and_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        rig = ThermalRig()
        svc = _temp_service(Path(tmp), rig, clock)
        before = svc.store.current_versions()["temperature"]
        svc.temp_start({"operator": "t"})
        svc.temp_apply("cold")
        _ticks(svc, rig, clock, 11)
        svc.temp_point("cold", 22.0, reference_vial=0)
        svc.temp_apply("hot")
        svc.temp_abort()
        assert list(rig.temp_setpoint_raw) == [HEATER_OFF_SETPOINT] * N_VIALS
        assert svc.store.current_versions()["temperature"] == before
        assert not (svc.store.sessions_dir / "temperature.json").exists()
        assert len(list((svc.store.root / "temperature").glob("*.json"))) == 1


def test_thermistor_start_warns_when_the_span_is_unreachable():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _temp_service(Path(tmp), ThermalRig())
        out = svc.temp_start({"operator": "t", "bench_temp_c": 27.0})
        assert any("cannot reach" in w for w in out["warnings"])
        assert any("vial_map" in w for w in out["warnings"])
