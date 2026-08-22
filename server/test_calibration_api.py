"""Route-level tests for the /api/calibration/* surface (ROADMAP Session O).

Runs against a --mock server rooted in a temp directory, with a temp COPY of
the calibration files so the store bootstrap and wizard writes never touch
the repo's calibration/ directory.

Run from the project root:
    python -m pytest server/test_calibration_api.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO_CAL = Path(__file__).resolve().parent.parent / "calibration"


def _make_app(tmp: Path):
    import app as A
    A.EXPERIMENTS_DIR = tmp / "experiments"
    A.EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    A.EXPORTS_DIR = tmp / "exports"
    A.LOGS_DIR = tmp / "logs"
    # Temp copy of the calibration dir: the manager loads the .txt files and
    # the store bootstraps its versioned envelopes here, not in the repo.
    # The three calibration globals are only read inside create_app, so they
    # are restored immediately afterwards — other test modules build their
    # own apps against the repo calibration and must not inherit a deleted
    # temp path.
    saved = (A.CAL_DIR, A.TEMP_CAL_PATH, A.OD_CAL_PATH)
    A.CAL_DIR = tmp / "calibration"
    A.CAL_DIR.mkdir(parents=True, exist_ok=True)
    for f in ("OD_cal.txt", "temp_calibration.txt"):
        shutil.copy(REPO_CAL / f, A.CAL_DIR / f)
    A.TEMP_CAL_PATH = A.CAL_DIR / "temp_calibration.txt"
    A.OD_CAL_PATH = A.CAL_DIR / "OD_cal.txt"
    try:
        flask_app, _socketio = A.create_app(use_mock=True)
    finally:
        A.CAL_DIR, A.TEMP_CAL_PATH, A.OD_CAL_PATH = saved
    return flask_app


def _closure(fn, name):
    return fn.__closure__[fn.__code__.co_freevars.index(name)].cell_contents


def _state(flask_app):
    return _closure(flask_app.view_functions["api_health"], "state")


def _experiment_body(name: str, with_media: bool = True) -> dict:
    body = {
        "name": name, "mode": "turbidostat", "vials": [0],
        "parameters": {"temperature_c": 37, "stir_rate": 10,
                       "od_lower_thresh": 0.2, "od_upper_thresh": 0.4},
    }
    if with_media:
        body["media"] = {
            "bottles": [{"id": "b1", "name": "LB", "initial_volume_ml": 1000}],
            "vial_to_bottle": {"0": "b1"},
            "waste": {"capacity_ml": 2000},
        }
    return body


def _warm_thermal_tracker(state, vials, target=37.0):
    """White-box: fabricate 12 minutes of on-target samples so the §13
    thermal-settling guard passes without waiting wall-clock time."""
    tr = state.cal_service.tracker
    now = time.time()
    with tr._lock:
        tr._samples.clear()
        for i in range(26):
            temps = [float(target)] * 16
            tr._samples.append((now - 760 + i * 30.0, temps))


# ---------------------------------------------------------------------------
# Index / staleness / raw routes
# ---------------------------------------------------------------------------

def test_index_reports_bootstrap_and_staleness():
    with tempfile.TemporaryDirectory() as tmp:
        c = _make_app(Path(tmp)).test_client()
        d = c.get("/api/calibration/").get_json()
        assert d["subsystems"]["od"]["source"] == "legacy-import-2016"
        assert d["subsystems"]["temperature"]["version"]
        assert d["subsystems"]["pump"] is None
        stale = d["staleness"]
        assert stale["pump"]["stale"] is True
        assert stale["any_stale"] is True
        # The temperature legacy import carries the vial 0 audit warning.
        env = c.get("/api/calibration/temperature").get_json()
        assert any("vial 0" in w for w in env["qc"]["warnings"])
        # History lists the imported versions.
        h = c.get("/api/calibration/history").get_json()
        assert len(h["od"]) == 1 and len(h["temperature"]) == 1
        assert c.get("/api/calibration/bogus").status_code == 400


def test_raw_routes_moved_off_the_actuator_surface():
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        c = flask_app.test_client()
        # Old route is gone.
        r = c.post("/api/actuators/temperature/raw",
                   json={"setpoints": [4095] * 16})
        assert r.status_code in (404, 405), r.status_code
        # New calibration-only route works while idle.
        r = c.post("/api/calibration/raw/temperature",
                   json={"setpoints": [4095] * 16})
        assert r.status_code == 200, r.get_json()
        # Raw OD LED read returns the collect_od_raw shape.
        r = c.post("/api/calibration/raw/od_led", json={"power": 0})
        assert r.status_code == 200
        d = r.get_json()
        assert len(d["median"]) == 16 and len(d["sd"]) == 16
        assert c.post("/api/calibration/raw/od_led",
                      json={"power": 5000}).status_code == 400


def test_mutating_calibration_routes_409_while_running():
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        c = flask_app.test_client()
        c.post("/api/experiments/create", json=_experiment_body("run"))
        assert c.post("/api/experiments/run/start",
                      json={"allow_missing_od_blank": True}).status_code == 200
        for route in (
            "/api/calibration/od/blank/start",
            "/api/calibration/pump/start",
            "/api/calibration/raw/temperature",
            "/api/calibration/raw/od_led",
        ):
            r = c.post(route, json={})
            assert r.status_code == 409, (route, r.status_code)
            assert r.get_json()["code"] == "experiment_running"
        # Read-only routes stay available.
        assert c.get("/api/calibration/").status_code == 200


# ---------------------------------------------------------------------------
# Missing-blank hard block
# ---------------------------------------------------------------------------

def test_start_blocked_without_blank_and_override_records_warning():
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        c = flask_app.test_client()
        c.post("/api/experiments/create", json=_experiment_body("noblank"))
        r = c.post("/api/experiments/noblank/start")
        assert r.status_code == 409
        assert r.get_json()["code"] == "missing_od_blank"
        # Explicit override starts and leaves a warning in the ring.
        r = c.post("/api/experiments/noblank/start",
                   json={"allow_missing_od_blank": True})
        assert r.status_code == 200
        events = c.get(
            "/api/events/recent?level=warning&category=calibration"
        ).get_json()["events"]
        assert any("WITHOUT a per-run OD blank" in e["message"] for e in events)


# ---------------------------------------------------------------------------
# OD blank wizard end-to-end (mock hardware)
# ---------------------------------------------------------------------------

def test_blank_wizard_end_to_end_then_start_without_override():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        flask_app = _make_app(tmp)
        c = flask_app.test_client()
        state = _state(flask_app)
        c.post("/api/experiments/create", json=_experiment_body("blanked"))

        _warm_thermal_tracker(state, [0])
        r = c.post("/api/calibration/od/blank/start", json={})
        assert r.status_code == 200, r.get_json()
        sid = r.get_json()["session"]
        assert r.get_json()["thermal"]["settled"] is True

        r = c.post("/api/calibration/od/blank/dark", json={"session": sid})
        assert r.status_code == 200
        r = c.post("/api/calibration/od/blank/measure", json={"session": sid})
        assert r.status_code == 200

        _warm_thermal_tracker(state, [0])  # sensor loop may have added reads
        r = c.post("/api/calibration/od/blank/commit",
                   json={"session": sid, "operator": "tester"})
        assert r.status_code == 200, r.get_json()
        d = r.get_json()
        assert d["updated_rows"] == [2]
        assert "0" in d["c_run"]

        # The blank landed in the experiment dir + config provenance.
        assert (tmp / "experiments" / "blanked" / "od_blank.json").is_file()
        config = json.loads(
            (tmp / "experiments" / "blanked" / "config.json").read_text()
        )
        assert config["calibration"]["od_blank"] == \
            "experiments/blanked/od_blank.json"
        # The live manager's row 2 now carries c_run for vial 0.
        assert abs(
            float(state.manager.od_cal[2, 0]) - d["c_run"]["0"]
        ) < 1e-9
        # Staleness no longer flags the blank.
        stale = c.get("/api/calibration/staleness").get_json()
        assert stale["od_blank"]["stale"] is False

        # Start now succeeds WITHOUT the override.
        assert c.post("/api/experiments/blanked/start").status_code == 200
        # Stopping restores the pristine calibration (blank is run-scoped).
        od_version_file = next((tmp / "calibration" / "od").glob("*.json"))
        base_c0 = json.loads(od_version_file.read_text())["data"]["rows"][2][0]
        c.post("/api/experiments/blanked/stop")
        assert abs(float(state.manager.od_cal[2, 0]) - base_c0) < 1e-9


def test_blank_start_requires_created_experiment():
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        c = flask_app.test_client()
        r = c.post("/api/calibration/od/blank/start", json={})
        assert r.status_code == 409  # nothing loaded


# ---------------------------------------------------------------------------
# Pump wizard end-to-end + engine consumption
# ---------------------------------------------------------------------------

def test_pump_wizard_full_32_flow_feeds_new_experiments():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        flask_app = _make_app(tmp)
        c = flask_app.test_client()
        state = _state(flask_app)

        r = c.post("/api/calibration/pump/start", json={
            "fire_seconds": 20, "replicates": 1,
            "fluid_density_g_ml": 1.0, "operator": "tester",
        })
        assert r.status_code == 200
        assert r.get_json()["pumps"] == list(range(32))

        # Fire one pump to prove the wire path; record masses for all 32.
        r = c.post("/api/calibration/pump/fire", json={"pump_id": 17})
        assert r.status_code == 200
        d = r.get_json()
        assert d["vial"] == 1 and d["direction"] == "efflux"
        assert state.manager.pump_log[-1]["vial"] == 1
        for p in range(32):
            r = c.post("/api/calibration/pump/record",
                       json={"pump_id": p, "replicate": 0, "mass_g": 21.0})
            assert r.status_code == 200, r.get_json()

        # Session is visible and resumable via GET.
        s = c.get("/api/calibration/pump/session").get_json()
        assert s["active"] is True and s["remaining"] == []

        r = c.post("/api/calibration/pump/finish", json={"operator": "tester"})
        assert r.status_code == 200, r.get_json()
        d = r.get_json()
        assert d["flow_rates_complete"] is True
        assert abs(d["flow_rates_ml_s"][0] - 1.05) < 1e-9  # 21 g / 20 s
        version = d["version"]
        # Installed and visible.
        env = c.get("/api/calibration/pump").get_json()
        assert env["version"] == version
        assert (tmp / "calibration" / "pump" / f"{version}.json").is_file()
        assert not (tmp / "calibration" / "_sessions" / "pump.json").exists()

        # A NEW experiment now records the measured rates and reports
        # media estimates as calibrated (the Session K label's exit).
        c.post("/api/experiments/create", json=_experiment_body("cal"))
        config = json.loads(
            (tmp / "experiments" / "cal" / "config.json").read_text()
        )
        assert config["calibration"]["pump"] == version
        assert len(config["calibration"]["pump_flow_rates"]) == 32
        c.post("/api/experiments/cal/start",
               json={"allow_missing_od_blank": True})
        media = state.engine.status()["media"]
        assert media["bottles"][0]["estimate_quality"] == "calibrated"
        c.post("/api/experiments/cal/stop")


def test_pump_abort_via_api_removes_session():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        c = _make_app(tmp).test_client()
        c.post("/api/calibration/pump/start", json={"pumps": [0]})
        assert (tmp / "calibration" / "_sessions" / "pump.json").is_file()
        assert c.post("/api/calibration/pump/abort").status_code == 200
        assert not (tmp / "calibration" / "_sessions" / "pump.json").exists()
        assert c.get("/api/calibration/pump/session").get_json()["active"] is False


# ---------------------------------------------------------------------------
# Reconciliation (O4)
# ---------------------------------------------------------------------------

def test_reconcile_round_trip_against_recorded_consumption():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        flask_app = _make_app(tmp)
        c = flask_app.test_client()
        c.post("/api/experiments/create", json=_experiment_body("recon"))
        c.post("/api/experiments/recon/start",
               json={"allow_missing_od_blank": True})
        # Running -> reconcile refused.
        r = c.post("/api/experiments/recon/reconcile",
                   json={"density_g_ml": 1.0, "media_start_g": 1,
                         "media_end_g": 0})
        assert r.status_code == 409
        c.post("/api/experiments/recon/stop")
        # A manual pump on the stopped experiment's vial debits its bottle.
        r = c.post("/api/actuators/pump",
                   json={"vial": 0, "direction": "influx", "seconds": 4})
        assert r.status_code == 200
        st = json.loads(
            (tmp / "experiments" / "recon" / "state.json").read_text()
        )
        consumed = st["media_state"]["bottles"]["b1"]["consumed_ml"]
        assert consumed > 0
        # Masses that agree exactly with the inferred volume -> ratio 1.0.
        r = c.post("/api/experiments/recon/reconcile", json={
            "density_g_ml": 1.0,
            "media_start_g": 1000.0,
            "media_end_g": 1000.0 - consumed,
        })
        assert r.status_code == 200, r.get_json()
        d = r.get_json()
        assert d["media"]["within_tolerance"] is True
        assert abs(d["media"]["ratio"] - 1.0) < 1e-6
        assert d["waste"] is None
        assert (tmp / "experiments" / "recon" / "reconciliation.json").is_file()

        # A badly-off reconciliation flags the pump calibration stale and
        # raises a warning through the funnel.
        r = c.post("/api/experiments/recon/reconcile", json={
            "density_g_ml": 1.0,
            "media_start_g": 1000.0,
            "media_end_g": 1000.0 - 3 * consumed,
        })
        assert r.get_json()["within_tolerance"] is False
        events = c.get(
            "/api/events/recent?level=warning&category=calibration"
        ).get_json()["events"]
        assert any("reconciliation" in e["message"].lower() for e in events)


# ---------------------------------------------------------------------------
# Dark-subtract coherence at the API boundary
# ---------------------------------------------------------------------------

def test_create_rejects_dark_subtract_without_sidecar_over_http():
    with tempfile.TemporaryDirectory() as tmp:
        c = _make_app(Path(tmp)).test_client()
        body = _experiment_body("ds", with_media=False)
        body["parameters"]["od_acquisition"] = {"dark_subtract": True}
        r = c.post("/api/experiments/create", json=body)
        assert r.status_code == 400
        assert "dark_subtract" in r.get_json()["error"]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
