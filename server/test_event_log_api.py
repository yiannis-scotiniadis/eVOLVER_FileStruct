"""Integration tests for the observability surface: the /api/events and
/api/health routes, failure escalation, and the per-vial counters in
ExperimentEngine.status() (SPEC §20.3-§20.4, ROADMAP Sessions M and M2).

Everything here runs against a --mock server rooted in a temp directory, so no
test can touch the real experiments/ directory (doing so would resume a live
experiment on startup).

Run from the project root:
    python -m pytest server/test_event_log_api.py
"""

from __future__ import annotations

import logging
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _make_app(tmp: Path):
    import app as A
    A.EXPERIMENTS_DIR = tmp / "experiments"
    A.EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    A.EXPORTS_DIR = tmp / "exports"
    A.LOGS_DIR = tmp / "logs"
    flask_app, _socketio = A.create_app(use_mock=True)
    return flask_app


def _closure(fn, name):
    """Reach a create_app local (state, _classify_bus_reads) through the view
    function's closure -- create_app returns only (app, socketio)."""
    return fn.__closure__[fn.__code__.co_freevars.index(name)].cell_contents


def _experiment_body(name: str) -> dict:
    return {
        "name": name, "mode": "turbidostat", "vials": [0],
        "parameters": {"lower_thresh": [0.2] * 16, "upper_thresh": [0.4] * 16},
        "media": {
            "bottles": [{"id": "b1", "name": "LB", "initial_volume_ml": 1000}],
            "vial_to_bottle": {"0": "b1"},
            "waste": {"capacity_ml": 2000},
        },
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def test_events_recent_is_populated_while_idle():
    """M checklist: the ring must work with no experiment running -- serial and
    manual-control faults happen while the machine sits idle."""
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        c = flask_app.test_client()
        state = _closure(flask_app.view_functions["api_health"], "state")
        state.event_log.record(level="warning", category="serial",
                               message="idle fault")

        d = c.get("/api/events/recent").get_json()
        assert d["counts"]["unacked_warning"] == 1
        assert d["events"][0]["message"] == "idle fault"


def test_events_recent_rejects_bad_query_params():
    with tempfile.TemporaryDirectory() as tmp:
        c = _make_app(Path(tmp)).test_client()
        assert c.get("/api/events/recent?level=bogus").status_code == 400
        assert c.get("/api/events/recent?vial=99").status_code == 400
        assert c.get("/api/events/recent?vial=abc").status_code == 400
        assert c.get("/api/events/recent?limit=abc").status_code == 400


def test_ack_route_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        c = flask_app.test_client()
        state = _closure(flask_app.view_functions["api_health"], "state")
        entry = state.event_log.record(level="critical", category="serial",
                                       message="bus down")

        r = c.post("/api/events/" + str(entry["id"]) + "/ack",
                   json={"by": "yiannis"})
        assert r.status_code == 200
        assert r.get_json()["event"]["acknowledged_by"] == "yiannis"
        assert c.get("/api/health").get_json()["events"]["unacked_critical"] == 0
        assert c.post("/api/events/424242/ack").status_code == 404


def test_health_route_shape():
    with tempfile.TemporaryDirectory() as tmp:
        d = _make_app(Path(tmp)).test_client().get("/api/health").get_json()
        assert set(d) == {"bus", "vials", "file_logging", "events"}
        assert d["bus"]["temperature"]["state"] == "ok"
        assert len(d["vials"]) == 16


# ---------------------------------------------------------------------------
# Escalation of the failures SPEC §20.4 found to be log-only
# ---------------------------------------------------------------------------

def test_manual_pump_failure_reaches_the_drawer_not_just_the_journal():
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        c = flask_app.test_client()
        state = _closure(flask_app.view_functions["api_health"], "state")

        def boom(*a, **k):
            raise RuntimeError("RS485 I/O error")

        state.manager.pump_command = boom
        logging.disable(logging.CRITICAL)
        try:
            r = c.post("/api/actuators/pump",
                       json={"vial": 3, "direction": "influx", "seconds": 2})
            assert r.status_code == 500
            # 40 more identical failures must not become 41 drawer rows.
            for _ in range(40):
                c.post("/api/actuators/pump",
                       json={"vial": 3, "direction": "influx", "seconds": 2})
        finally:
            logging.disable(logging.NOTSET)

        rows = c.get("/api/events/recent?level=critical").get_json()["events"]
        assert len(rows) == 1
        assert rows[0]["category"] == "pump"
        assert rows[0]["count"] == 41
        assert rows[0]["vial"] == 3


def test_actuator_failures_escalate_with_the_right_levels():
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        c = flask_app.test_client()
        state = _closure(flask_app.view_functions["api_health"], "state")

        def boom(*a, **k):
            raise RuntimeError("RS485 I/O error")

        state.manager.set_temperature_celsius = boom
        state.manager.set_stir = boom
        logging.disable(logging.CRITICAL)
        try:
            assert c.post("/api/actuators/temperature",
                          json={"values_c": [37.0] * 16}).status_code == 500
            assert c.post("/api/actuators/stir",
                          json={"values": [8] * 16}).status_code == 500
        finally:
            logging.disable(logging.NOTSET)

        by_cat = {e["category"]: e for e in
                  c.get("/api/events/recent?level=warning").get_json()["events"]}
        # A dead heater can end a run; a dead stirrer degrades it.
        assert by_cat["heater"]["level"] == "critical"
        assert by_cat["actuator"]["level"] == "warning"


# ---------------------------------------------------------------------------
# Bus + vial classification wired into the sensor path
# ---------------------------------------------------------------------------

def test_bus_classification_drives_health_and_alerts():
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        c = flask_app.test_client()
        classify = _closure(
            flask_app.view_functions["api_health"], "state").classify_bus_reads
        nan = float("nan")
        dead_t = {"calibrated": [nan] * 16}
        dead_od = {"calibrated": [nan] * 16,
                   "flags": ["dropped"] * 16, "n_valid": [0] * 16}

        logging.disable(logging.CRITICAL)
        try:
            for _ in range(4):
                classify(dead_t, dead_od)
        finally:
            logging.disable(logging.NOTSET)

        h = c.get("/api/health").get_json()
        assert h["bus"]["temperature"]["state"] == "down"
        assert h["bus"]["od"]["state"] == "down"
        assert h["vials"][0]["state"] == "degraded"

        serial_alerts = [
            e for e in c.get("/api/events/recent?level=critical").get_json()["events"]
            if e["category"] == "serial"
        ]
        # One per subsystem, raised on the crossing edge -- not one per cycle.
        assert len(serial_alerts) == 2

        classify({"calibrated": [37.0] * 16},
                 {"calibrated": [0.2] * 16, "flags": ["ok"] * 16,
                  "n_valid": [3] * 16})
        h = c.get("/api/health").get_json()
        assert h["bus"]["temperature"]["state"] == "ok"
        assert h["vials"][0]["state"] == "ok"


def test_out_of_range_od_does_not_mark_the_bus_down():
    """A culture denser than the calibration is not a dead Arduino."""
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        c = flask_app.test_client()
        classify = _closure(
            flask_app.view_functions["api_health"], "state").classify_bus_reads
        nan = float("nan")
        for _ in range(5):
            classify({"calibrated": [37.0] * 16},
                     {"calibrated": [nan] * 16,
                      "flags": ["out_of_range"] * 16, "n_valid": [3] * 16})

        h = c.get("/api/health").get_json()
        assert h["bus"]["od"]["state"] == "ok"
        assert h["vials"][0]["state"] == "out_of_range"
        assert h["vials"][0]["dropped_streak"] == 0


# ---------------------------------------------------------------------------
# Engine surface + events.csv
# ---------------------------------------------------------------------------

def test_engine_status_exposes_per_vial_sensor_counters():
    """M checklist: streaks the engine already tracked but never surfaced."""
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        c = flask_app.test_client()
        assert c.post("/api/experiments/create",
                      json=_experiment_body("counters")).status_code == 200
        # No per-run OD blank in this test -> the Session O hard block needs
        # the explicit override (CALIBRATION_PROTOCOL §13).
        assert c.post("/api/experiments/counters/start",
                      json={"allow_missing_od_blank": True}).status_code == 200

        state = _closure(flask_app.view_functions["api_health"], "state")
        nan = float("nan")
        logging.disable(logging.CRITICAL)
        try:
            for _ in range(3):
                state.engine.run_cycle(_ts(), [nan] * 16, [nan] * 16,
                                       od_flags=["dropped"] * 16)
        finally:
            logging.disable(logging.NOTSET)

        pv = state.engine.status()["per_vial"]["0"]
        assert pv["nan_streak"] == 3
        assert pv["sensor_health"] == "degraded"
        assert pv["od_range_streak"] == 0


def test_lifecycle_events_land_in_both_the_ring_and_events_csv():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        flask_app = _make_app(root)
        c = flask_app.test_client()
        c.post("/api/experiments/create", json=_experiment_body("lifecycle"))
        c.post("/api/experiments/lifecycle/start",
               json={"allow_missing_od_blank": True})

        messages = [e["message"] for e in
                    c.get("/api/events/recent?level=info").get_json()["events"]]
        assert any("started" in m for m in messages)
        assert any("created" in m for m in messages)

        csv_text = (root / "experiments" / "lifecycle" / "events.csv").read_text(
            encoding="utf-8")
        assert "started" in csv_text
        assert "lifecycle" in csv_text


def test_suppressed_pump_is_recorded_with_its_reason():
    """M checklist: a Session K suppressed pump must appear in events.csv with
    the reason it was blocked."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        flask_app = _make_app(root)
        c = flask_app.test_client()
        state = _closure(flask_app.view_functions["api_health"], "state")
        c.post("/api/experiments/create", json=_experiment_body("suppress"))
        c.post("/api/experiments/suppress/start",
               json={"allow_missing_od_blank": True})

        # The engine emits this shape from _handle_consumables_block (§15).
        state.engine._broadcast_event({
            "type": "pump_suppressed", "vial": 0,
            "reason": "media_empty", "bottle_id": "b1",
        })

        rows = c.get("/api/events/recent?level=warning").get_json()["events"]
        assert rows[0]["category"] == "pump"
        assert "media_empty" in rows[0]["message"]

        csv_text = (root / "experiments" / "suppress" / "events.csv").read_text(
            encoding="utf-8")
        assert "media_empty" in csv_text
