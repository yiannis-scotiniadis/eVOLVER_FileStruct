"""Unit tests for calibration_service.py (ROADMAP Session O / SPEC §19).

Covers the four §15 correctness assertions for the blank re-anchor, the
versioned store's immutability + legacy-view regeneration, the resumable
pump session, the §13 guards, and the O4 reconciliation math.

Run from the project root:
    python -m pytest server/test_calibration_service.py
"""

from __future__ import annotations

import csv
import json
import math
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from calibration_service import (  # noqa: E402
    CalibrationConflict,
    CalibrationService,
    CalibrationStore,
    N_PUMPS,
    N_VIALS,
    PumpCalSession,
    QCRefusal,
    TempStabilityTracker,
    _parse_version,
    make_envelope,
    reanchor_od_calibration,
)
from serial_manager import SerialManager  # noqa: E402

REPO_CAL = Path(__file__).resolve().parent.parent / "calibration"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _synthetic_od_cal() -> np.ndarray:
    """A well-behaved 4x16 logistic: a=10k, b=100k, c=0.5, d=-0.7."""
    return np.vstack([
        np.full(N_VIALS, 10_000.0),
        np.full(N_VIALS, 100_000.0),
        np.full(N_VIALS, 0.5),
        np.full(N_VIALS, -0.7),
    ])


def _od_of(od_cal: np.ndarray, raw: float, vial: int) -> float:
    a, b, c, d = (od_cal[r, vial] for r in range(4))
    return c - math.log10((b - a) / (raw - a) - 1.0) / d


class FakeClock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeManager:
    """collect_od_raw / pump_command stub for service-level tests."""

    def __init__(self, dark: float = 1200.0, blank: float = 58_000.0) -> None:
        self.dark_counts = dark
        self.blank_counts = blank
        self.pump_calls: list[tuple[int, str, float]] = []

    def collect_od_raw(self, led_power: int, n_samples: int = 5) -> dict:
        val = self.dark_counts if led_power == 0 else self.blank_counts
        return {
            "median": [val] * N_VIALS,
            "sd": [10.0] * N_VIALS,
            "n_valid": [n_samples] * N_VIALS,
        }

    def pump_command(self, vial: int, direction: str, seconds: float) -> None:
        self.pump_calls.append((vial, direction, seconds))


def _service(tmp: Path, manager=None, clock=None) -> CalibrationService:
    cal_root = tmp / "calibration"
    exp_root = tmp / "experiments"
    cal_root.mkdir(parents=True, exist_ok=True)
    exp_root.mkdir(parents=True, exist_ok=True)
    svc = CalibrationService(
        cal_root, exp_root, manager or FakeManager(),
        clock=clock or FakeClock(),
    )
    return svc


def _install_synthetic_od(svc: CalibrationService, version: str = None) -> np.ndarray:
    od = _synthetic_od_cal()
    env = make_envelope(
        "od", operator="test", source="test-fixture",
        conditions={"note": "synthetic"},
        data={"rows": od.tolist(), "dark_subtracted": False},
    )
    if version:
        env["version"] = version
    svc.store.save_version("od", env)
    return od


def _warm_tracker(svc: CalibrationService, clock: FakeClock, vials, target=37.0,
                  minutes=12, value=None):
    """Feed the stability tracker `minutes` of on-target reads."""
    value = target if value is None else value
    for i in range(minutes * 2):  # every 30 s
        clock.t += 30.0
        temps = [float("nan")] * N_VIALS
        for v in vials:
            temps[v] = value
        svc.note_temperatures(temps)


def _blank_session(svc, clock, name="exp1", stir=8, led=2125):
    (svc.experiments_root / name).mkdir(exist_ok=True)
    config = {
        "vials": [0, 1],
        "parameters": {"stir_rate": stir, "temperature_c": 37.0},
    }
    _warm_tracker(svc, clock, [0, 1])
    return svc.blank_start(
        experiment=name, config=config, engine_status="created",
        led_power=led, stir_pwm=stir, expected_led_power=2125,
    )


# ---------------------------------------------------------------------------
# reanchor_od_calibration — the four §15 correctness assertions
# ---------------------------------------------------------------------------

def test_reanchor_blank_reads_zero_on_all_16():
    od_cal = _synthetic_od_cal()
    blank = np.full(N_VIALS, 58_000.0)
    out = reanchor_od_calibration(od_cal, blank)
    for v in range(N_VIALS):
        assert abs(_od_of(out, 58_000.0, v)) < 1e-12, v


def test_reanchor_leaves_rows_0_1_3_bitwise_unchanged():
    od_cal = _synthetic_od_cal()
    out = reanchor_od_calibration(od_cal, np.full(N_VIALS, 58_000.0))
    for row in (0, 1, 3):
        assert np.array_equal(out[row], od_cal[row]), f"row {row} changed"
    assert not np.array_equal(out[2], od_cal[2])


def test_reanchor_is_pure_vertical_shift():
    """OD_new(S) - OD_old(S) must be constant in S per vial (shape preserved)."""
    od_cal = _synthetic_od_cal()
    out = reanchor_od_calibration(od_cal, np.full(N_VIALS, 58_000.0))
    for v in (0, 7, 15):
        shifts = [
            _od_of(out, s, v) - _od_of(od_cal, s, v)
            for s in (15_000.0, 30_000.0, 58_000.0, 90_000.0)
        ]
        assert max(shifts) - min(shifts) < 1e-9, shifts


def test_reanchor_rejects_out_of_domain_blank():
    od_cal = _synthetic_od_cal()
    for bad in (9_000.0, 100_001.0):
        blank = np.full(N_VIALS, 58_000.0)
        blank[3] = bad
        try:
            reanchor_od_calibration(od_cal, blank)
        except ValueError as exc:
            assert "domain" in str(exc) and "3" in str(exc), exc
        else:
            raise AssertionError(f"blank {bad} did not raise")


def test_reanchor_on_committed_repo_calibration():
    """The assertions also hold on the actual committed OD_cal.txt."""
    od_cal = np.genfromtxt(REPO_CAL / "OD_cal.txt", delimiter=",")
    blank = np.full(N_VIALS, 58_000.0)
    out = reanchor_od_calibration(od_cal, blank)
    for v in range(N_VIALS):
        assert abs(_od_of(out, 58_000.0, v)) < 1e-9, v
    for row in (0, 1, 3):
        assert np.array_equal(out[row], od_cal[row])


# ---------------------------------------------------------------------------
# Envelope + store (O1)
# ---------------------------------------------------------------------------

def test_envelope_rejects_empty_conditions():
    for bad in ({}, None, "x"):
        try:
            make_envelope("od", operator="y", source="s",
                          conditions=bad, data={})
        except ValueError:
            pass
        else:
            raise AssertionError(f"conditions={bad!r} accepted")


def test_bootstrap_imports_legacy_and_flags_audit_findings():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cal = tmp / "calibration"
        cal.mkdir()
        for f in ("OD_cal.txt", "temp_calibration.txt"):
            (cal / f).write_bytes((REPO_CAL / f).read_bytes())
        store = CalibrationStore(cal, tmp / "experiments")
        created = store.bootstrap_from_legacy()
        assert sorted(created) == ["od", "temperature"]
        # Second call is a no-op.
        assert store.bootstrap_from_legacy() == []
        # Audit findings surfaced as qc warnings (vial 0 temp, vial 1 od).
        temp = store.get_current("temperature")
        assert any("vial 0" in w for w in temp["qc"]["warnings"]), \
            temp["qc"]["warnings"]
        od = store.get_current("od")
        assert any("vial 1" in w for w in od["qc"]["warnings"]), \
            od["qc"]["warnings"]
        # The .txt files were NOT rewritten by the import.
        assert (cal / "OD_cal.txt").read_bytes() == \
            (REPO_CAL / "OD_cal.txt").read_bytes()
        # Values round-trip through the envelope.
        assert np.allclose(
            np.asarray(od["data"]["rows"]),
            np.genfromtxt(REPO_CAL / "OD_cal.txt", delimiter=","),
        )


def test_version_files_are_immutable():
    with tempfile.TemporaryDirectory() as tmp:
        store = CalibrationStore(Path(tmp) / "calibration")
        env = make_envelope("od", operator="t", source="s",
                            conditions={"n": 1},
                            data={"rows": _synthetic_od_cal().tolist()},
                            version="2026-01-01T000000Z")
        store.save_version("od", env)
        try:
            store.save_version("od", dict(env))
        except CalibrationConflict:
            pass
        else:
            raise AssertionError("overwriting a version file did not raise")


def test_legacy_views_regenerate_and_load_in_serial_manager():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        store = CalibrationStore(tmp / "calibration")
        od = _synthetic_od_cal()
        temp_slope = [-0.11] * N_VIALS
        temp_intercept = [82.0] * N_VIALS
        store.save_version("od", make_envelope(
            "od", operator="t", source="dilution-series",
            conditions={"n": 1},
            data={"rows": od.tolist(), "dark_subtracted": True},
            version="2026-01-01T000000Z",
        ))
        store.save_version("temperature", make_envelope(
            "temperature", operator="t", source="two-point",
            conditions={"n": 1},
            data={"slope": temp_slope, "intercept": temp_intercept},
            version="2026-01-01T000001Z",
        ))
        # Derived views exist and carry the same values.
        od_txt = np.genfromtxt(tmp / "calibration" / "OD_cal.txt", delimiter=",")
        assert np.allclose(od_txt, od)
        meta = json.loads(
            (tmp / "calibration" / "OD_cal.meta.json").read_text()
        )
        assert meta["dark_subtracted"] is True
        # And SerialManager loads them unchanged (needs a fake serial).
        class _NullSer:
            def flushInput(self): pass
        sm = SerialManager(ser=_NullSer())
        sm.load_calibration(
            str(tmp / "calibration" / "temp_calibration.txt"),
            str(tmp / "calibration" / "OD_cal.txt"),
        )
        assert np.allclose(sm.od_cal, od)
        assert np.allclose(sm.temp_cal[0], temp_slope)
        assert sm.od_cal_dark_subtracted is True


def test_current_pump_rates_requires_complete_32():
    with tempfile.TemporaryDirectory() as tmp:
        store = CalibrationStore(Path(tmp) / "calibration")
        rates = [1.0] * N_PUMPS
        rates[31] = None  # partial calibration
        store.save_version("pump", make_envelope(
            "pump", operator="t", source="gravimetric",
            conditions={"fluid": "water"},
            data={}, fit={"flow_rates_ml_s": rates},
            version="2026-01-01T000000Z",
        ))
        assert store.current_pump_rates() is None
        rates2 = [1.0] * N_PUMPS
        store.save_version("pump", make_envelope(
            "pump", operator="t", source="gravimetric",
            conditions={"fluid": "water"},
            data={}, fit={"flow_rates_ml_s": rates2},
            version="2026-01-01T000001Z",
        ))
        assert store.current_pump_rates() == rates2


# ---------------------------------------------------------------------------
# Thermal-settling tracker (§13 guard)
# ---------------------------------------------------------------------------

def test_tracker_requires_full_window_on_target():
    clock = FakeClock()
    tr = TempStabilityTracker(clock=clock)
    # 5 minutes of on-target reads: not settled (window not spanned).
    for _ in range(10):
        clock.t += 30.0
        tr.note([37.0] * N_VIALS)
    assert not tr.settled([0], {0: 37.0})["settled"]
    # Another 6 minutes: settled.
    for _ in range(12):
        clock.t += 30.0
        tr.note([37.0] * N_VIALS)
    assert tr.settled([0], {0: 37.0})["settled"]
    # One excursion beyond +-0.3 C inside the window: not settled.
    clock.t += 30.0
    temps = [37.0] * N_VIALS
    temps[0] = 37.8
    tr.note(temps)
    r = tr.settled([0], {0: 37.0})
    assert not r["settled"], r


# ---------------------------------------------------------------------------
# OD blank session (O2)
# ---------------------------------------------------------------------------

def test_blank_start_guards():
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock()
        svc = _service(Path(tmp), clock=clock)
        _install_synthetic_od(svc)
        config = {"vials": [0], "parameters": {"stir_rate": 8,
                                               "temperature_c": 37.0}}
        # Wrong engine status -> 409.
        try:
            svc.blank_start(experiment="e", config=config,
                            engine_status="running", led_power=2125,
                            stir_pwm=8, expected_led_power=2125)
        except CalibrationConflict:
            pass
        else:
            raise AssertionError("blank_start in RUNNING did not raise")
        # Condition mismatch: stir differs from the run's -> ValueError.
        try:
            svc.blank_start(experiment="e", config=config,
                            engine_status="created", led_power=2125,
                            stir_pwm=10, expected_led_power=2125)
        except ValueError as exc:
            assert "stir" in str(exc)
        else:
            raise AssertionError("stir mismatch did not raise")
        # LED mismatch -> ValueError.
        try:
            svc.blank_start(experiment="e", config=config,
                            engine_status="created", led_power=2000,
                            stir_pwm=8, expected_led_power=2125)
        except ValueError as exc:
            assert "LED" in str(exc) or "led" in str(exc)
        else:
            raise AssertionError("LED mismatch did not raise")


def test_blank_flow_commit_and_response_shape():
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock()
        manager = FakeManager(dark=1200.0, blank=58_000.0)
        svc = _service(Path(tmp), manager=manager, clock=clock)
        od = _install_synthetic_od(svc)
        start = _blank_session(svc, clock)
        sid = start["session"]
        # Order guard: blank before dark -> 409.
        try:
            svc.blank_measure(sid)
        except CalibrationConflict:
            pass
        else:
            raise AssertionError("measure before dark did not raise")
        dark = svc.blank_dark(sid)
        assert dark["median"][0] == 1200.0
        blank = svc.blank_measure(sid)
        assert blank["median"][0] == 58_000.0
        result = svc.blank_commit(sid, operator="tester")
        assert result["updated_rows"] == [2]
        assert sorted(result["c_run"]) == ["0", "1"]
        # c_run makes the blank read OD 0 on the committed vials.
        reanchored = od.copy()
        reanchored[2, 0] = result["c_run"]["0"]
        assert abs(_od_of(reanchored, 58_000.0, 0)) < 1e-9
        # od_offset_removed = old OD at blank (c was 0.5 => offset present).
        assert abs(
            result["od_offset_removed"]["0"]
            - _od_of(od, 58_000.0, 0)
        ) < 1e-3
        # Envelope written into the experiment dir with the full record.
        env = json.loads(
            (svc.experiments_root / "exp1" / "od_blank.json").read_text()
        )
        assert env["subsystem"] == "od_blank"
        assert env["fit"]["updated_rows"] == [2]
        assert env["conditions"]["led_power"] == 2125
        assert env["conditions"]["parent_od_cal"]
        # Session is consumed.
        try:
            svc.blank_dark(sid)
        except CalibrationConflict:
            pass
        else:
            raise AssertionError("session survived commit")


def test_blank_commit_blocked_until_thermally_settled():
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock()
        svc = _service(Path(tmp), clock=clock)
        _install_synthetic_od(svc)
        (svc.experiments_root / "exp1").mkdir()
        config = {"vials": [0], "parameters": {"stir_rate": 8,
                                               "temperature_c": 37.0}}
        # No tracker warm-up at all.
        start = svc.blank_start(
            experiment="exp1", config=config, engine_status="created",
            led_power=2125, stir_pwm=8, expected_led_power=2125,
        )
        assert start["thermal"]["settled"] is False
        sid = start["session"]
        svc.blank_dark(sid)
        svc.blank_measure(sid)
        try:
            svc.blank_commit(sid)
        except CalibrationConflict as exc:
            assert "equilibr" in str(exc) or "settle" in str(exc).lower() or \
                "±" in str(exc)
        else:
            raise AssertionError("unsettled commit did not raise")


def test_blank_commit_domain_guard_and_exclusion():
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock()
        manager = FakeManager(blank=5_000.0)  # below a=10k on every vial
        svc = _service(Path(tmp), manager=manager, clock=clock)
        _install_synthetic_od(svc)
        start = _blank_session(svc, clock)
        sid = start["session"]
        svc.blank_dark(sid)
        svc.blank_measure(sid)
        try:
            svc.blank_commit(sid)
        except ValueError as exc:
            assert "domain" in str(exc), exc
        else:
            raise AssertionError("out-of-domain blank did not raise")
        # Excluding every bad vial (all of them here) -> nothing to commit.
        try:
            svc.blank_commit(sid, exclude_vials=[0, 1])
        except ValueError as exc:
            assert "excluded" in str(exc)
        else:
            raise AssertionError("all-excluded commit did not raise")


def test_blank_qc_failure_needs_override_reason():
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock()

        class NoisyManager(FakeManager):
            def collect_od_raw(self, led_power, n_samples=5):
                out = super().collect_od_raw(led_power, n_samples)
                out["sd"] = [500.0] * N_VIALS  # over both SD limits
                return out

        svc = _service(Path(tmp), manager=NoisyManager(), clock=clock)
        _install_synthetic_od(svc)
        start = _blank_session(svc, clock)
        sid = start["session"]
        svc.blank_dark(sid)
        svc.blank_measure(sid)
        try:
            svc.blank_commit(sid)
        except QCRefusal as exc:
            assert exc.qc["failures"], exc.qc
        else:
            raise AssertionError("QC failure committed without override")
        result = svc.blank_commit(sid, override_reason="bench accepts noise")
        assert result["qc"]["overridden_by"] == "bench accepts noise"


# ---------------------------------------------------------------------------
# Pump session (O3)
# ---------------------------------------------------------------------------

def test_pump_session_full_flow_and_install():
    with tempfile.TemporaryDirectory() as tmp:
        manager = FakeManager()
        svc = _service(Path(tmp), manager=manager)
        r = svc.pump_start({"pumps": [0, 16], "fire_seconds": 20,
                            "replicates": 3, "fluid_density_g_ml": 1.0,
                            "operator": "tester"})
        assert r["status"] == "started"
        fired = svc.pump_fire(16)
        assert fired["vial"] == 0 and fired["direction"] == "efflux"
        assert manager.pump_calls[-1] == (0, "efflux", 20.0)
        for pump, masses in ((0, [20.0, 20.2, 19.8]), (16, [22.0, 22.1, 21.9])):
            for i, m in enumerate(masses):
                svc.pump_record(pump, i, m)
        result = svc.pump_finish(operator="tester")
        assert result["status"] == "installed"
        # Influx 0: 20 g / 1.0 / 20 s = 1.0 mL/s; efflux 0: 1.1 mL/s.
        rates = result["flow_rates_ml_s"]
        assert abs(rates[0] - 1.0) < 1e-9
        assert abs(rates[16] - 1.1) < 1e-6
        assert result["flow_rates_complete"] is False  # only 2 of 32 measured
        # Session file removed; store has the version.
        assert not (svc.store.sessions_dir / "pump.json").exists()
        assert svc.store.get_current("pump")["fit"]["measured_pumps"] == [0, 16]
        # Incomplete calibration must NOT feed the engine.
        assert svc.store.current_pump_rates() is None


def test_pump_session_survives_restart_and_resumes():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        svc = _service(tmp)
        svc.pump_start({"pumps": [2, 5], "replicates": 2,
                        "fluid_density_g_ml": 1.0})
        svc.pump_record(2, 0, 19.0)
        # Simulate a server restart: new service over the same roots.
        svc2 = _service(tmp)
        svc2.bootstrap()
        session = svc2.pump_session()
        assert session["active"] is True
        assert session["remaining"] == [2, 5]
        assert session["per_pump"]["2"]["recorded"] == 1
        # start without resume -> 409; with resume -> continues.
        try:
            svc2.pump_start({"pumps": [0]})
        except CalibrationConflict:
            pass
        else:
            raise AssertionError("second start did not raise")
        r = svc2.pump_start({"resume": True})
        assert r["status"] == "resumed"


def test_pump_abort_leaves_no_partial_file():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        svc = _service(tmp)
        svc.pump_start({"pumps": [0], "replicates": 1,
                        "fluid_density_g_ml": 1.0})
        assert (svc.store.sessions_dir / "pump.json").is_file()
        svc.pump_abort()
        assert not (svc.store.sessions_dir / "pump.json").exists()
        assert svc.store.get_current("pump") is None
        assert list((tmp / "calibration").glob("pump/*.json")) == []


def test_pump_finish_qc_refusal_and_override():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(Path(tmp))
        svc.pump_start({"pumps": [0], "replicates": 3,
                        "fluid_density_g_ml": 1.0})
        # Wildly scattered replicates -> CV >> 5 %.
        for i, m in enumerate([10.0, 20.0, 30.0]):
            svc.pump_record(0, i, m)
        try:
            svc.pump_finish()
        except QCRefusal as exc:
            assert any("CV" in f for f in exc.qc["failures"]), exc.qc
        else:
            raise AssertionError("bad CV installed without override")
        result = svc.pump_finish(override_reason="known-wobbly line")
        assert result["qc"]["overridden_by"] == "known-wobbly line"
        assert svc.store.get_current("pump") is not None


def test_pump_finish_requires_all_replicates():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(Path(tmp))
        svc.pump_start({"pumps": [0, 1], "replicates": 2,
                        "fluid_density_g_ml": 1.0})
        svc.pump_record(0, 0, 20.0)
        svc.pump_record(0, 1, 20.1)
        try:
            svc.pump_finish()
        except ValueError as exc:
            assert "1" in str(exc), exc
        else:
            raise AssertionError("incomplete session finished")


def test_pump_merge_carries_previous_rates_forward():
    """A spot-check subset overwrites only its own pumps; the rest keep the
    previous calibration's rates, so a partial recalibration cannot punch
    holes in a complete one."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(Path(tmp))
        svc.store.save_version("pump", make_envelope(
            "pump", operator="t", source="gravimetric",
            conditions={"fluid": "water"},
            data={}, fit={"flow_rates_ml_s": [1.0] * N_PUMPS},
            version="2026-01-01T000000Z",
        ))
        svc.pump_start({"pumps": [3], "replicates": 1, "fire_seconds": 20,
                        "fluid_density_g_ml": 1.0})
        svc.pump_record(3, 0, 21.0)  # 1.05 mL/s, within 15 % of 1.0
        result = svc.pump_finish()
        rates = result["flow_rates_ml_s"]
        assert abs(rates[3] - 1.05) < 1e-9
        assert rates[4] == 1.0 and rates[31] == 1.0
        assert result["flow_rates_complete"] is True
        assert svc.store.current_pump_rates()[3] == rates[3]
        # Review carries previous + delta for the operator (§13 review screens).
        assert result["review"]["3"]["previous_rate_ml_s"] == 1.0
        assert abs(result["review"]["3"]["delta_pct"] - 5.0) < 0.01


# ---------------------------------------------------------------------------
# Reconciliation (O4)
# ---------------------------------------------------------------------------

def test_reconcile_math_and_stale_marking():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(Path(tmp))
        (svc.experiments_root / "run1").mkdir()
        state = {"media_state": {
            "bottles": {"b1": {"consumed_ml": 100.0}},
            "waste": {"filled_ml": 100.0},
        }}
        # Media: 1000g -> 899g at density 1.0 => 101 mL vs 100 inferred: pass.
        # Waste: 0 -> 150g => 150 mL vs 100 inferred: fail (ratio 1.5).
        record = svc.reconcile("run1", state, {
            "media_start_g": 1000.0, "media_end_g": 899.0,
            "waste_start_g": 0.0, "waste_end_g": 150.0,
            "density_g_ml": 1.0,
        })
        assert record["media"]["within_tolerance"] is True
        assert abs(record["media"]["ratio"] - 1.01) < 1e-6
        assert record["waste"]["within_tolerance"] is False
        assert record["within_tolerance"] is False
        assert (svc.experiments_root / "run1" / "reconciliation.json").is_file()
        # The failed reconciliation marks the pump calibration stale (§13).
        svc.store.save_version("pump", make_envelope(
            "pump", operator="t", source="gravimetric",
            conditions={"fluid": "water"},
            data={}, fit={"flow_rates_ml_s": [1.0] * N_PUMPS},
        ))
        stale = svc.store.staleness()
        assert stale["pump"]["stale"] is True
        assert any("reconciliation" in r for r in stale["pump"]["reasons"])


def test_reconcile_requires_density_and_one_side():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(Path(tmp))
        (svc.experiments_root / "run1").mkdir()
        try:
            svc.reconcile("run1", {}, {"media_start_g": 1, "media_end_g": 0})
        except ValueError as exc:
            assert "density" in str(exc)
        else:
            raise AssertionError("missing density accepted")
        try:
            svc.reconcile("run1", {}, {"density_g_ml": 1.0})
        except ValueError as exc:
            assert "media" in str(exc) or "waste" in str(exc)
        else:
            raise AssertionError("no-side reconcile accepted")


# ---------------------------------------------------------------------------
# Staleness (§13)
# ---------------------------------------------------------------------------

def test_staleness_reports_missing_and_blank_hard_block():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(Path(tmp))
        stale = svc.store.staleness(loaded_experiment="expX",
                                    loaded_status="created")
        assert stale["pump"]["stale"] is True          # never calibrated
        assert stale["od_blank"]["stale"] is True      # no blank taken
        assert stale["stir"]["stale"] is True          # absent
        assert stale["any_stale"] is True
        # A committed blank clears the od_blank flag.
        exp = svc.experiments_root / "expX"
        exp.mkdir()
        (exp / "od_blank.json").write_text("{}", encoding="utf-8")
        stale = svc.store.staleness(loaded_experiment="expX",
                                    loaded_status="created")
        assert stale["od_blank"]["stale"] is False


def test_staleness_pump_seconds_accumulate_from_pump_logs():
    """§13: cumulative pump-on seconds since the calibration version drive
    the wear-based staleness trigger (>40 h)."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(Path(tmp))
        svc.store.save_version("pump", make_envelope(
            "pump", operator="t", source="gravimetric",
            conditions={"fluid": "water"},
            data={}, fit={"flow_rates_ml_s": [1.0] * N_PUMPS},
            version="2026-01-01T000000Z",
        ))
        exp = svc.experiments_root / "run1"
        exp.mkdir()
        # 15,000 rows x 10 s = 41.7 h of pump time, all after the version.
        rows = ["timestamp,elapsed_hours,direction,duration_seconds,od_at_pump"]
        rows += ["2026-02-01T00:00:00+00:00,1.0,influx,10.0,0.3"] * 15000
        (exp / "vial00_pump_log.csv").write_text("\n".join(rows) + "\n",
                                                 encoding="utf-8")
        seconds = svc.store.pump_seconds_since("2026-01-01T000000Z")
        assert abs(seconds - 150_000.0) < 1e-6
        stale = svc.store.staleness()
        assert any("cumulative pump time" in r for r in stale["pump"]["reasons"])
        # Rows BEFORE the version don't count.
        assert svc.store.pump_seconds_since("2026-03-01T000000Z") == 0.0


def test_staleness_legacy_import_never_verified():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cal = tmp / "calibration"
        cal.mkdir()
        for f in ("OD_cal.txt", "temp_calibration.txt"):
            (cal / f).write_bytes((REPO_CAL / f).read_bytes())
        store = CalibrationStore(cal, tmp / "experiments")
        store.bootstrap_from_legacy()
        stale = store.staleness()
        assert stale["od"]["stale"] is True
        assert any("spectrophotometer" in r for r in stale["od"]["reasons"])
        assert stale["temperature"]["stale"] is True
        assert any("outlier" in r for r in stale["temperature"]["reasons"])


# ---------------------------------------------------------------------------
# pump_seconds_since: incremental, mtime-keyed cache
# ---------------------------------------------------------------------------
#
# The uncached scan re-parsed every pump event ever logged on every call --
# 12.3 s over a 480 000-row campaign, growing without bound. It is dormant
# today only because current.json has "pump": null, and goes live on the
# dashboard's staleness banner as soon as Tier 2 calibration lands. These
# tests pin the equivalence, not the speed.

_PUMP_HEADER = ["timestamp", "elapsed_hours", "direction",
                "duration_seconds", "od_at_pump"]
_BASE = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _write_pump_log(path: Path, rows, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if mode == "w":
            w.writerow(_PUMP_HEADER)
        for ts, dur in rows:
            w.writerow([ts, "0.0", "influx", dur, "0.5"])


def _pump_rows(n, start=0, minutes=15):
    return [((_BASE + timedelta(minutes=minutes * (start + i))).isoformat(
        timespec="seconds"), "12.00") for i in range(n)]


def _reference_pump_seconds(root: Path, since):
    """The pre-cache implementation, verbatim in behaviour."""
    total = 0.0
    for p in root.glob("*/vial*_pump_log.csv"):
        with p.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if since is not None:
                    try:
                        dt = datetime.fromisoformat(row.get("timestamp", ""))
                    except ValueError:
                        continue
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < since:
                        continue
                try:
                    total += float(row.get("duration_seconds") or 0.0)
                except (TypeError, ValueError):
                    continue
    return total


def _pump_store(tmp: Path) -> CalibrationStore:
    return CalibrationStore(REPO_CAL, tmp / "experiments")


def test_pump_cache_matches_the_uncached_scan() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        exps = tmp / "experiments"
        _write_pump_log(exps / "e1" / "vial00_pump_log.csv", _pump_rows(50))
        _write_pump_log(exps / "e1" / "vial03_pump_log.csv", _pump_rows(30, start=5))
        _write_pump_log(exps / "e2" / "vial00_pump_log.csv", _pump_rows(20, start=60))
        store = _pump_store(tmp)
        for version in (None, "2026-07-01T060000Z", "2026-07-15T000000Z",
                        "2020-01-01T000000Z"):
            since = None if version is None else _parse_version(version)
            want = _reference_pump_seconds(exps, since)
            assert store.pump_seconds_since(version) == want, version
            # ...and again, this time from the warm cache.
            assert store.pump_seconds_since(version) == want, version


def test_pump_cache_picks_up_appended_rows() -> None:
    """The point of keying on size+mtime: an active run keeps appending."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        exps = tmp / "experiments"
        path = exps / "e1" / "vial00_pump_log.csv"
        _write_pump_log(path, _pump_rows(10))
        store = _pump_store(tmp)
        assert store.pump_seconds_since(None) == 120.0          # 10 x 12 s
        _write_pump_log(path, _pump_rows(5, start=10), mode="a")
        assert store.pump_seconds_since(None) == 180.0          # 15 x 12 s
        assert store.pump_seconds_since(None) == _reference_pump_seconds(exps, None)


def test_pump_cache_reparses_a_truncated_or_replaced_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        exps = tmp / "experiments"
        path = exps / "e1" / "vial00_pump_log.csv"
        _write_pump_log(path, _pump_rows(40))
        store = _pump_store(tmp)
        assert store.pump_seconds_since(None) == 480.0
        _write_pump_log(path, _pump_rows(3))                    # rewritten shorter
        assert store.pump_seconds_since(None) == 36.0


def test_pump_cache_preserves_the_unparseable_timestamp_asymmetry() -> None:
    """Pins behaviour that predates the cache: a row whose timestamp does not
    parse IS counted by a since=None query and is NOT counted by a windowed
    one. Both readings survive the rewrite."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        exps = tmp / "experiments"
        path = exps / "e1" / "vial00_pump_log.csv"
        _write_pump_log(path, _pump_rows(4) + [("not-a-timestamp", "7.00")])
        store = _pump_store(tmp)
        assert store.pump_seconds_since(None) == 55.0           # 4x12 + 7
        windowed = store.pump_seconds_since("2020-01-01T000000Z")
        assert windowed == 48.0                                 # the 7 s is skipped
        assert windowed == _reference_pump_seconds(
            exps, _parse_version("2020-01-01T000000Z")
        )


def test_pump_cache_ignores_a_half_written_final_row() -> None:
    """`_append_row` writes one row at a time, but a read can still land
    mid-flush. A partial tail must be left for the next call, not parsed as a
    truncated record."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        exps = tmp / "experiments"
        path = exps / "e1" / "vial00_pump_log.csv"
        _write_pump_log(path, _pump_rows(5))
        store = _pump_store(tmp)
        assert store.pump_seconds_since(None) == 60.0
        with path.open("a", encoding="utf-8", newline="") as f:
            f.write("2026-07-02T00:00:00+00:00,0.0,influx,99")   # no line ending
        assert store.pump_seconds_since(None) == 60.0            # still ignored
        with path.open("a", encoding="utf-8", newline="") as f:
            f.write(".00,0.5" + chr(13) + chr(10))
        assert store.pump_seconds_since(None) == 159.0           # now counted


def test_pump_cache_drops_entries_for_deleted_experiments() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        exps = tmp / "experiments"
        _write_pump_log(exps / "e1" / "vial00_pump_log.csv", _pump_rows(5))
        _write_pump_log(exps / "e2" / "vial00_pump_log.csv", _pump_rows(5))
        store = _pump_store(tmp)
        assert store.pump_seconds_since(None) == 120.0
        assert len(store._pump_log_cache) == 2
        shutil.rmtree(exps / "e2")
        assert store.pump_seconds_since(None) == 60.0
        assert len(store._pump_log_cache) == 1


def test_pump_cache_handles_timestamps_that_go_backwards() -> None:
    """A clock step would break the bisect, so the index flags itself unordered
    and falls back to a linear sum rather than returning a wrong number."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        exps = tmp / "experiments"
        path = exps / "e1" / "vial00_pump_log.csv"
        _write_pump_log(path, _pump_rows(5, start=10) + _pump_rows(5))
        store = _pump_store(tmp)
        entry = store._pump_log_index(path)
        assert entry["ordered"] is False
        version = "2026-07-01T020000Z"
        assert store.pump_seconds_since(version) == _reference_pump_seconds(
            exps, _parse_version(version)
        )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
