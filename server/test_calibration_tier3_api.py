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


def test_a_failing_calibration_tick_is_contained_and_alerted():
    """The sensor loop calls _calibration_tick inside its tick; it must never
    raise, or that tick loses its broadcast and its watchdog pet."""
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        try:
            state = _state(flask_app)

            def boom(*_a, **_k):
                raise RuntimeError("tick exploded")
            state.cal_service.tick = boom
            state.calibration_tick({"calibrated": [22.0] * N_VIALS, "adc": None})
            messages = [e.get("message", "") for e in state.event_log.recent()]
            assert any("Calibration session tick failed" in m for m in messages)
        finally:
            _stop(flask_app)


def test_calibration_heater_alerts_go_through_the_funnel():
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        try:
            state = _state(flask_app)
            _open_probe(state)
            hot = {"calibrated": [30.0] * N_VIALS, "adc": [500.0] * N_VIALS}
            hot["calibrated"][4] = 48.0
            for _ in range(3):
                state.calibration_tick(hot)
            entries = [e for e in state.event_log.recent()
                       if "heater watchdog" in e.get("message", "")]
            assert len(entries) == 1 and entries[0].get("vial") == 4
            assert int(state.manager.temp_setpoint_raw[4]) == 4095
        finally:
            _stop(flask_app)


# ---------------------------------------------------------------------------
# Phase 3: the thermistor wizard over HTTP, against the mock's ground truth
# ---------------------------------------------------------------------------

from test_calibration_api import _experiment_body  # noqa: E402

PACK_SLOPE, PACK_INTERCEPT = -0.11130, 81.897


def _settle(manager, n=80):
    """Advance the mock until the vials sit at their setpoints."""
    for _ in range(n):
        manager.read_temperature()


def _post(client, url, body=None, code=200):
    r = client.post(url, json=body or {})
    assert r.status_code == code, (url, r.status_code, r.get_json())
    return r.get_json()


def test_tier3_start_refuses_a_machine_that_is_not_idle():
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        try:
            client = flask_app.test_client()
            _post(client, "/api/experiments/create", _experiment_body("busy"))
            r = client.post("/api/calibration/temperature/start", json={})
            assert r.status_code == 409
            assert r.get_json()["code"] == "machine_not_idle"
            assert "busy" in r.get_json()["error"]
        finally:
            _stop(flask_app)


def test_thermistor_wizard_over_http_installs_the_truth_and_goes_live():
    """The HTTP flow and the live reload, on a NOISELESS mock: this app's
    own sensor thread may tick mid-test and advance the simulation between
    the reference reading and the capture. Noise and the equilibrium gates
    are covered deterministically in test_calibration_tier3.py."""
    import mock_serial_manager as M
    saved_noise = M.TEMP_NOISE_SIGMA
    M.TEMP_NOISE_SIGMA = 0.0
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        try:
            state = _state(flask_app)
            mgr = state.manager
            mgr.time_multiplier = 10.0
            mgr.mock_temp_raw_noise_sd = 0.0
            mgr.set_true_temp_cal(PACK_SLOPE, PACK_INTERCEPT)
            client = flask_app.test_client()
            _post(client, "/api/calibration/temperature/start",
                  {"operator": "t", "reference_thermometer": "ref-1"})
            # The raw path is reserved while the session is open.
            assert client.post("/api/calibration/raw/temperature",
                               json={"setpoints": [4095] * N_VIALS}).status_code == 409

            _post(client, "/api/calibration/temperature/apply", {"phase": "cold"})
            _settle(mgr)
            # Real clock: no 10-min window in a unit test -- record the override.
            _post(client, "/api/calibration/temperature/point",
                  {"label": "cold", "reference_c": float(mgr.temp_C[2]),
                   "reference_vial": 2, "settle_check": False})

            _post(client, "/api/calibration/temperature/apply", {"phase": "hot"})
            _settle(mgr)
            for v in range(N_VIALS):
                _post(client, "/api/calibration/temperature/point",
                      {"label": "hot", "vials": [v], "reference_c": float(mgr.temp_C[v]),
                       "settle_check": False})
            sess = client.get("/api/calibration/temperature/session").get_json()
            suggested = [int(v) for v, d in sess["per_vial"].items()
                         if d["retarget_suggested"]]
            assert 0 in suggested, suggested
            _post(client, "/api/calibration/temperature/retarget", {"vials": suggested})
            _settle(mgr)
            for v in suggested:
                _post(client, "/api/calibration/temperature/point",
                      {"label": "hot", "vials": [v], "reference_c": float(mgr.temp_C[v]),
                       "settle_check": False})

            review = _post(client, "/api/calibration/temperature/fit")
            assert review["fit"]["review"]["0"]["previous_delivered_at_37_c"] < 30.0

            _post(client, "/api/calibration/temperature/apply", {"phase": "verify"})
            _settle(mgr)
            for v in (0, 3, 8, 11):
                _post(client, "/api/calibration/temperature/point",
                      {"label": "verify", "vials": [v], "reference_c": float(mgr.temp_C[v]),
                       "settle_check": False})
            out = _post(client, "/api/calibration/temperature/finish")
            assert out["qc"]["passed"], out["qc"]

            # Live without a restart: manager, AppState, the sliders' curve.
            want = np.vstack([np.full(N_VIALS, PACK_SLOPE), np.full(N_VIALS, PACK_INTERCEPT)])
            assert np.allclose(mgr.temp_cal, want, rtol=1e-3, atol=0.05)
            assert np.allclose(state.temp_cal, mgr.temp_cal)
            cal = client.get("/api/actuators/state").get_json()["temp_calibration"]
            assert np.allclose(cal["slope"], mgr.temp_cal[0])
            idx = client.get("/api/calibration/").get_json()
            assert idx["tier3_session"] is None
            assert idx["subsystems"]["temperature"]["version"] == out["version"]
            assert not any("never verified" in r
                           for r in idx["staleness"]["temperature"]["reasons"])
            # The machine is released.
            assert client.post("/api/calibration/raw/temperature",
                               json={"setpoints": [4095] * N_VIALS}).status_code == 200
        finally:
            M.TEMP_NOISE_SIGMA = saved_noise
            _stop(flask_app)


def test_thermistor_finish_refusal_is_422_with_the_qc_block():
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        try:
            mgr = _state(flask_app).manager
            mgr.time_multiplier = 10.0
            client = flask_app.test_client()
            _post(client, "/api/calibration/temperature/start", {"operator": "t"})
            _post(client, "/api/calibration/temperature/apply", {"phase": "cold"})
            _settle(mgr)
            _post(client, "/api/calibration/temperature/point",
                  {"label": "cold", "reference_c": 22.0, "reference_vial": 0,
                   "settle_check": False})
            r = client.post("/api/calibration/temperature/finish", json={})
            assert r.status_code == 422
            body = r.get_json()
            assert body["code"] == "qc_refused" and body["qc"]["failures"]
            _post(client, "/api/calibration/temperature/abort")
            assert client.get("/api/calibration/temperature/session").get_json() == {"active": False}
        finally:
            _stop(flask_app)


# ---------------------------------------------------------------------------
# Phase 4: the OD dilution series over HTTP + the dark-subtraction switch
# ---------------------------------------------------------------------------

import json  # noqa: E402

from test_calibration_tier3 import SERIES_LEVELS, _true_ds_rows  # noqa: E402


def _run_http_series(client, mgr, rows, vials):
    mgr.set_true_od_curve(rows, dark_counts=1200.0)
    mgr.mock_signal_noise_sd = 0.0
    mgr.mock_dark_noise_sd = 0.0
    _post(client, "/api/calibration/od/series/start",
          {"operator": "t", "vials": vials, "standard": "beads",
           "spectrophotometer": "Genesys 30", "new_organism": "Pichia pastoris"})
    _post(client, "/api/calibration/od/series/apply")
    for od in SERIES_LEVELS:
        mgr.set_true_od(vials, od)
        _post(client, "/api/calibration/od/series/point",
              {"spec_reading": od, "vials": vials})


def test_od_series_over_http_creates_an_organism_profile():
    """A series introduces an organism: a new profile, dark-subtracted, with
    the machine's default curve -- and so the live reader -- untouched."""
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        try:
            state = _state(flask_app)
            mgr = state.manager
            default_rows = np.array(mgr.od_cal)
            client = flask_app.test_client()
            rows = _true_ds_rows()
            _run_http_series(client, mgr, rows, list(range(N_VIALS)))
            sess = client.get("/api/calibration/od/series/session?preview=1").get_json()
            assert sess["organism"] == "Pichia pastoris" and sess["new_profile"] is True
            assert sess["per_vial"]["0"]["n_levels"] == len(SERIES_LEVELS)
            assert sess["per_vial"]["0"]["preview"]["r2"] > 0.9999
            review = _post(client, "/api/calibration/od/series/fit")
            assert review["qc"]["passed"], review["qc"]
            out = _post(client, "/api/calibration/od/series/finish")
            assert out["profile_id"] == "pichia-pastoris"
            env = state.cal_service.store.get_od_profile("pichia-pastoris")
            assert np.allclose(env["data"]["rows"], rows, rtol=1e-6)
            assert env["data"]["dark_subtracted"] is True
            # The default curve is not an organism: unchanged, live and on disk.
            assert np.array_equal(mgr.od_cal, default_rows)
            assert mgr.od_cal_dark_subtracted is False
            idx = client.get("/api/calibration/").get_json()
            ids = [p["id"] for p in idx["od_profiles"]]
            assert ids == ["legacy-2016", "pichia-pastoris"], ids
            # The install event names the organism.
            events = [e.get("data") or {} for e in state.event_log.recent()]
            assert any(d.get("organism") == "Pichia pastoris" and d.get("new_profile")
                       for d in events), events
            # A second series may recalibrate it, but not re-introduce it.
            r = client.post("/api/calibration/od/series/start",
                            json={"new_organism": "pichia  PASTORIS"})
            assert r.status_code == 400 and "already exists" in r.get_json()["error"]
        finally:
            _stop(flask_app)


def test_a_blank_taken_on_a_replaced_od_curve_is_refused_at_start():
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        try:
            client = flask_app.test_client()
            _post(client, "/api/experiments/create", _experiment_body("old"))
            (Path(tmp) / "experiments" / "old" / "od_blank.json").write_text(json.dumps({
                "conditions": {"parent_od_cal": "2020-01-01T000000Z"},
                "fit": {"c_run": {"0": -0.5}},
            }))
            r = client.post("/api/experiments/old/start", json={})
            assert r.status_code == 409 and r.get_json()["code"] == "stale_od_blank"
            # The recorded escape hatch starts it WITHOUT applying the blank.
            state = _state(flask_app)
            row2_before = float(state.manager.od_cal[2, 0])
            _post(client, "/api/experiments/old/start", {"allow_missing_od_blank": True})
            assert float(state.manager.od_cal[2, 0]) == row2_before
            messages = [e.get("message", "") for e in state.event_log.recent()]
            assert any("WITHOUT a per-run OD blank" in m for m in messages)
        finally:
            _stop(flask_app)
