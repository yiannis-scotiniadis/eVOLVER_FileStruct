"""HTTP-level tests for the Tier 3 calibration wizards (ROADMAP Session AA).

Builds a real app against MockSerialManager with a temp calibration dir, via
the helpers in test_calibration_api.py.

Run from the project root:
    python -m pytest server/test_calibration_tier3_api.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from calibration_service import N_VIALS, make_envelope  # noqa: E402
from test_calibration_api import _make_app, _state  # noqa: E402


def _stop(flask_app) -> None:
    """Stop the sensor thread so the temp dir can be removed cleanly."""
    _state(flask_app).sensor_thread_stop.set()


# ---------------------------------------------------------------------------
# Phase 0: a new calibration goes live without a restart
# ---------------------------------------------------------------------------

def test_temperature_install_reloads_every_live_cache():
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        try:
            state = _state(flask_app)
            state.cal_service.note_temperatures([37.0] * N_VIALS)
            slope = [-0.1] * N_VIALS
            intercept = [79.0] * N_VIALS
            version = state.cal_service.store.save_version(
                "temperature",
                make_envelope(
                    "temperature", operator="t", source="two-point-reference",
                    conditions={"n": 1},
                    data={"slope": slope, "intercept": intercept},
                    version="2026-09-01T000000Z",
                ),
            )
            state.reload_calibration("temperature", version)

            want = np.vstack([slope, intercept])
            assert np.allclose(state.manager.temp_cal, want)
            assert np.allclose(state.temp_cal, want)
            assert np.allclose(state.engine._engine_kwargs["temp_cal"], want)
            # Engines built from now on get the new curve.
            assert np.allclose(state.engine._new_engine()._temp_cal, want)
            # The sliders' °C<->raw conversion is served the new curve.
            client = flask_app.test_client()
            cal = client.get("/api/actuators/state").get_json()["temp_calibration"]
            assert np.allclose(cal["slope"], slope)
            assert np.allclose(cal["intercept"], intercept)
            # The settle tracker's old-scale history is gone.
            assert state.cal_service.tracker._samples == []
            # The install went through the event funnel.
            kinds = [e.get("data", {}) or {} for e in state.event_log.recent()]
            assert any(d.get("subsystem") == "temperature" for d in kinds), kinds
        finally:
            _stop(flask_app)


# ---------------------------------------------------------------------------
# Phase 1: a Tier 3 session reserves the machine
# ---------------------------------------------------------------------------

import time  # noqa: E402

import app as A  # noqa: E402
from test_calibration_tier3 import _ProbeSession  # noqa: E402


def _open_probe(state, armed=True):
    s = _ProbeSession(operator="t", vials=[0, 1], stir_pwm=8)
    state.cal_service._begin_tier3(s)
    if armed:
        state.cal_service._arm_tier3(s, "probe")
    return s


GUARDED = [
    ("/api/experiments/create", {"name": "x", "vials": [0]}),
    ("/api/experiments/x/precondition", {}),
    ("/api/experiments/x/start", {}),
    ("/api/actuators/temperature", {"values_c": [30.0] * N_VIALS}),
    ("/api/actuators/stir", {"values": [0] * N_VIALS}),
    ("/api/actuators/pump", {"vial": 0, "direction": "influx", "seconds": 1}),
    ("/api/calibration/raw/temperature", {"setpoints": [4095] * N_VIALS}),
    ("/api/calibration/raw/od_led", {"power": 0}),
    ("/api/calibration/pump/start", {"pumps": [0]}),
    ("/api/calibration/pump/fire", {"pump_id": 0}),
    ("/api/calibration/od/blank/start", {}),
]


def test_every_machine_route_409s_while_a_tier3_session_is_open():
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        try:
            state = _state(flask_app)
            _open_probe(state, armed=False)   # even suspended, it reserves
            client = flask_app.test_client()
            for url, body in GUARDED:
                r = client.post(url, json=body)
                assert r.status_code == 409, (url, r.status_code, r.get_json())
                assert r.get_json()["code"] == "calibration_in_progress", url
            # The emergency stop is never guarded.
            assert client.post("/api/actuators/emergency_stop").status_code == 200
            idx = client.get("/api/calibration/").get_json()
            assert idx["tier3_session"]["kind"] == "probe"
        finally:
            _stop(flask_app)


def test_emergency_stop_faults_the_session_so_stir_stays_off():
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        try:
            state = _state(flask_app)
            _open_probe(state)
            state.cal_service.tick([22.0] * N_VIALS, [540.0] * N_VIALS)
            assert list(state.manager.stir_speed[:2]) == [8, 8]
            client = flask_app.test_client()
            assert client.post("/api/actuators/emergency_stop").status_code == 200
            assert state.cal_service.tier3_status()["state"] == "faulted"
            state.cal_service.tick([22.0] * N_VIALS, [540.0] * N_VIALS)
            assert list(state.manager.stir_speed) == [0] * N_VIALS
        finally:
            _stop(flask_app)


def test_a_failing_calibration_tick_never_costs_the_watchdog_pet():
    with tempfile.TemporaryDirectory() as tmp:
        saved = A.SENSOR_LOOP_INTERVAL_SECONDS
        # Read by the loop on every iteration, including its first wait.
        A.SENSOR_LOOP_INTERVAL_SECONDS = 0.05
        flask_app = _make_app(Path(tmp))
        try:
            state = _state(flask_app)

            def boom(*_a, **_k):
                raise RuntimeError("tick exploded")
            state.cal_service.tick = boom
            pets = {"n": 0}
            orig_pet = state.watchdog.pet

            def counting_pet():
                pets["n"] += 1
                orig_pet()
            state.watchdog.pet = counting_pet
            deadline = time.monotonic() + 5.0
            while pets["n"] < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert pets["n"] >= 2, "sensor loop stopped petting the watchdog"
            messages = [e.get("message", "") for e in state.event_log.recent()]
            assert any("Calibration session tick failed" in m for m in messages)
        finally:
            A.SENSOR_LOOP_INTERVAL_SECONDS = saved
            _stop(flask_app)
