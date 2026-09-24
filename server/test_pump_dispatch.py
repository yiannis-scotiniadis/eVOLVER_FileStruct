"""app.py's automatic-dilution dispatch: one concurrent pump schedule per cycle.

Drives the real ``_dispatch_dilutions`` (reached through the maintenance-exit
route's closure) against ``MockSerialManager``, which queues frames like the
firmware (FLUIDICS_FIRMWARE_AUDIT.md FW-1). Checks the three things the
dispatcher owns:

* every diluting vial's influx and efflux start together, all vials in
  parallel, and the efflux overrun survives;
* ``pump_log.csv`` keeps exactly two rows per dilution -- it is parsed
  positionally, so frame detail goes to events.csv as one ``pump_batch``;
* a write failure part-way is logged as what actually reached the wire.

Run from the project root:  python -m pytest server/test_pump_dispatch.py
"""

from __future__ import annotations

import csv
import logging
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from control_modes.turbidostat import PumpAction  # noqa: E402
from fluidics import FluidicsDispatchError  # noqa: E402
from test_event_log_api import _closure, _make_app  # noqa: E402

NAME = "dispatch"


def _body() -> dict:
    return {
        "name": NAME, "mode": "turbidostat", "vials": [0, 1],
        "parameters": {
            "lower_thresh": [0.2] * 16, "upper_thresh": [0.4] * 16,
            "efflux_extra_seconds": 2.0,
        },
        "media": {
            "bottles": [{"id": "b1", "name": "LB", "initial_volume_ml": 1000}],
            "vial_to_bottle": {"0": "b1", "1": "b1"},
            "waste": {"capacity_ml": 2000},
        },
    }


def _start(root: Path):
    flask_app = _make_app(root)
    c = flask_app.test_client()
    assert c.post("/api/experiments/create", json=_body()).status_code == 200
    assert c.post(f"/api/experiments/{NAME}/start",
                  json={"allow_missing_od_blank": True}).status_code == 200
    state = _closure(flask_app.view_functions["api_health"], "state")
    dispatch = _closure(
        flask_app.view_functions["api_maintenance_exit"],
        "_execute_queued_pump_actions",
    )
    return c, state, dispatch


def _rows(root: Path, vial: int) -> list[tuple[str, float]]:
    path = root / "experiments" / NAME / f"vial{vial:02d}_pump_log.csv"
    with path.open(newline="", encoding="utf-8") as fh:
        return [(r["direction"], float(r["duration_seconds"]))
                for r in csv.DictReader(fh)]


def _entries(ts: str) -> list:
    # vial 0: 3 s bolus, vial 1: 5 s bolus; both with the 2 s overrun.
    return [
        (0, PumpAction(pump_time=3.0, efflux_extra_seconds=2.0, average_od=0.45), ts),
        (1, PumpAction(pump_time=5.0, efflux_extra_seconds=2.0, average_od=0.47), ts),
    ]


def test_cycle_fires_as_one_concurrent_schedule():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        c, state, dispatch = _start(root)
        n_before = len(state.manager.pump_log)
        dispatch(_entries(datetime.now(timezone.utc).isoformat(timespec="seconds")))

        log = state.manager.pump_log[n_before:]
        starts: dict = {}
        ends: dict = {}
        for e in log:
            key = (e["vial"], e["direction"])
            starts.setdefault(key, e["sim_time"])
            ends[key] = e["sim_time"] + e["seconds"]
        t0 = starts[(0, "influx")]
        # Influx and efflux of both vials all start in the first frame ...
        assert set(starts.values()) == {t0}, starts
        # ... each pump runs its full dose, and the overrun survives.
        assert {k: v - t0 for k, v in ends.items()} == {
            (0, "influx"): 3, (0, "efflux"): 5,
            (1, "influx"): 5, (1, "efflux"): 7,
        }

        # Exactly two CSV rows per dilution, totals not frames.
        assert _rows(root, 0) == [("influx", 3.0), ("efflux", 5.0)]
        assert _rows(root, 1) == [("influx", 5.0), ("efflux", 7.0)]

        batches = [e for e in state.event_log.recent(category="pump", limit=500)
                   if (e.get("data") or {}).get("frames")]
        assert len(batches) == 1, batches
        data = batches[0]["data"]
        assert data["vials"] == [0, 1]
        assert data["span_seconds"] == 7
        assert [f["seconds"] for f in data["frames"]] == [3, 2, 2]


def test_partial_failure_logs_only_what_reached_the_wire():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        c, state, dispatch = _start(root)

        def fails_after_first_frame(frames):
            raise FluidicsDispatchError("RS485 I/O error", frames_sent=1)

        state.manager.pump_frames = fails_after_first_frame
        logging.disable(logging.CRITICAL)
        try:
            dispatch(_entries(datetime.now(timezone.utc).isoformat(timespec="seconds")))
        finally:
            logging.disable(logging.NOTSET)

        # Frame 0 ran every pump for 3 s; nothing after it was sent.
        assert _rows(root, 0) == [("influx", 3.0), ("efflux", 3.0)]
        assert _rows(root, 1) == [("influx", 3.0), ("efflux", 3.0)]
        crit = c.get("/api/events/recent?level=critical").get_json()["events"]
        assert len(crit) == 1
        assert crit[0]["category"] == "pump"
        assert "1/3" in crit[0]["message"]


def test_failure_before_any_frame_logs_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        c, state, dispatch = _start(root)

        def boom(frames):
            raise RuntimeError("port closed")

        state.manager.pump_frames = boom
        logging.disable(logging.CRITICAL)
        try:
            dispatch(_entries(datetime.now(timezone.utc).isoformat(timespec="seconds")))
        finally:
            logging.disable(logging.NOTSET)

        assert _rows(root, 0) == []
        assert _rows(root, 1) == []
        crit = c.get("/api/events/recent?level=critical").get_json()["events"]
        assert len(crit) == 1 and crit[0]["category"] == "pump"
