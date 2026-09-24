"""Parallel experiments (PARALLEL_EXPERIMENTS.md / supervisor.py).

Two operators, one machine: independent experiments with their own vials,
modes, lifecycles, directories and operators, on one sensor thread. Driven
two ways, like the vial-group tests:

* ``ExperimentSupervisor`` directly, with a real DataLogger and
  MockSerialManager and a hand-advanced clock;
* the HTTP API, through ``create_app(use_mock=True)``.

What each test pins is independence -- one experiment's lifecycle, fault or
stop never reaching into the other's vials -- and the few places the
machine is deliberately shared: one tick, one stir write, shared vessels,
the machine hold and the emergency stop.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_logger import DataLogger  # noqa: E402
from experiment_engine import (  # noqa: E402
    ConflictError,
    ExperimentStatus,
    N_VIALS,
)
from mock_serial_manager import MockSerialManager  # noqa: E402
from serial_manager import HEATER_OFF_SETPOINT  # noqa: E402
from supervisor import (  # noqa: E402
    AmbiguousExperimentError,
    ExperimentSupervisor,
)

CAL_DIR = Path(__file__).resolve().parent.parent / "calibration"
TEMP_CAL = str(CAL_DIR / "temp_calibration.txt")
OD_CAL = str(CAL_DIR / "OD_cal.txt")


class TmpRoot:
    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="evolver-parallel-test-"))
        return self.path

    def __exit__(self, *args) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _sup(root: Path, manager=None):
    manager = manager or MockSerialManager(seed=7)
    manager.load_calibration(TEMP_CAL, OD_CAL)
    events: list = []
    alerts: list = []
    clock = {"t": 1_000_000.0}
    sup = ExperimentSupervisor(
        manager, DataLogger(root / "experiments"), root / "experiments",
        on_event=events.append, on_alert=alerts.append,
        temp_cal=np.genfromtxt(TEMP_CAL, delimiter=","),
        clock=lambda: clock["t"],
        machine_dir=root / "machine",
    )
    return sup, manager, events, alerts, clock


TURB = {
    "temperature_c": 37, "stir_rate": 8,
    "od_lower_thresh": 0.2, "od_upper_thresh": 0.4,
    "min_samples_before_action": 1, "pump_wait_minutes": 0.0,
    "volume_ml": 25, "efflux_extra_seconds": 2, "pump_flow_rates": [1.0] * 16,
}
CHEM = {
    "temperature_c": 30, "stir_rate": 5,
    "dilution_rate_per_hour": 30.0, "bolus_interval_seconds": 60.0,
    "volume_ml": 25, "efflux_extra_seconds": 2, "pump_flow_rates": [1.0] * 16,
}


def _media(bottle_id: str, vials, *, waste=None, capacity=4000.0, bottle_vessel=None):
    bottle = {"id": bottle_id, "name": bottle_id.upper(), "initial_volume_ml": 1000.0}
    if bottle_vessel:
        bottle = {"id": bottle_id, "vessel": bottle_vessel}
    return {
        "bottles": [bottle],
        "vial_to_bottle": {str(v): bottle_id for v in vials},
        "waste": waste if waste is not None else {"name": "Carboy", "capacity_ml": capacity},
    }


def _two(sup, *, a_media=None, b_media=None):
    sup.create_experiment(name="alice", mode="turbidostat", vials=[0, 1, 2, 3],
                          parameters=dict(TURB), media=a_media, operator="Alice")
    sup.create_experiment(name="bob", mode="chemostat", vials=[8, 9],
                          parameters=dict(CHEM), media=b_media, operator="Bob")


def _temps(value=37.0):
    return [float(value)] * N_VIALS


# ---------------------------------------------------------------------------
# Ownership and independent lifecycles
# ---------------------------------------------------------------------------

def test_two_experiments_on_disjoint_vials_and_overlap_is_refused() -> None:
    with TmpRoot() as root:
        sup, *_ = _sup(root)
        _two(sup)
        assert sup.loaded_names() == ["alice", "bob"]
        assert sup.owner_of(2) == "alice" and sup.owner_of(9) == "bob"
        assert sup.owner_of(5) is None
        try:
            sup.create_experiment(name="carol", mode="turbidostat", vials=[3, 4],
                                  parameters=dict(TURB))
        except ConflictError as exc:
            assert "alice" in str(exc) and "Alice" in str(exc) and "[3]" in str(exc)
        else:
            raise AssertionError("a vial another experiment holds must be refused")
        assert not (root / "experiments" / "carol").exists()


def test_staggered_start_stop_leaves_the_other_experiment_untouched() -> None:
    with TmpRoot() as root:
        sup, manager, _events, _alerts, clock = _sup(root)
        sup.create_experiment(name="alice", mode="turbidostat", vials=[0, 1, 2, 3],
                              parameters=dict(TURB), operator="Alice")
        sup.start_experiment("alice")
        for i in range(3):
            sup.run_cycle(f"2026-05-14T10:00:0{i}+00:00", _temps(), [0.1] * 16)
            clock["t"] += 10.0
        # Bob arrives later, with a different mode.
        sup.create_experiment(name="bob", mode="chemostat", vials=[8, 9],
                              parameters=dict(CHEM), operator="Bob")
        sup.start_experiment("bob")
        assert sup.running_names() == ["alice", "bob"]
        fired: set[int] = set()
        # Alice's turbidostat decides on a 5-sample mean, so it needs a few
        # high readings to push the mean past her band after the low ones.
        for i in range(6):
            actions = sup.run_cycle(f"2026-05-14T10:01:0{i}+00:00", _temps(), [0.5] * 16)
            fired |= {v for v, _a in actions}
            clock["t"] += 60.0
        assert {0, 1, 2, 3} <= fired and {8, 9} <= fired, fired

        bob_raw = [int(manager.temp_setpoint_raw[v]) for v in (8, 9)]
        sup.stop_experiment("alice")
        # Alice's vials parked; Bob's heaters untouched, Bob still running.
        raw = [int(x) for x in manager.temp_setpoint_raw]
        assert raw[0] == HEATER_OFF_SETPOINT and raw[3] == HEATER_OFF_SETPOINT
        assert [raw[8], raw[9]] == bob_raw and raw[8] < HEATER_OFF_SETPOINT
        assert sup.running_names() == ["bob"]
        assert sup.owner_of(0) is None
        # Bob keeps getting his tick, and his stir is still re-asserted.
        manager.stir_speed = np.zeros(N_VIALS, dtype=int)
        clock["t"] += 60.0
        actions = sup.run_cycle("2026-05-14T10:05:00+00:00", _temps(), [0.5] * 16)
        assert {v for v, _a in actions} <= {8, 9}
        assert int(manager.stir_speed[8]) == 5 and int(manager.stir_speed[0]) == 0
        # Each run wrote its own directory, with its own elapsed clock.
        assert (root / "experiments" / "bob" / "vial08_OD.csv").is_file()
        assert not (root / "experiments" / "bob" / "vial00_OD.csv").exists()
        sup.stop_experiment("bob")


def test_one_stir_write_per_tick_for_every_experiment() -> None:
    with TmpRoot() as root:
        sup, manager, *_ = _sup(root)
        _two(sup)
        sup.start_experiment("alice")
        sup.start_experiment("bob")
        writes = []
        original = manager.set_stir
        manager.set_stir = lambda values: (writes.append(list(values)), original(values))
        sup.run_cycle("2026-05-14T10:00:00+00:00", _temps(), [0.1] * 16)
        assert len(writes) == 1, f"{len(writes)} zv writes in one tick"
        assert writes[0][0] == 8 and writes[0][8] == 5 and writes[0][5] == 0


def test_one_experiment_failing_does_not_cost_the_other_its_tick() -> None:
    with TmpRoot() as root:
        sup, _manager, _events, alerts, _clock = _sup(root)
        _two(sup)
        sup.start_experiment("alice")
        sup.start_experiment("bob")

        def boom(*_a, **_k):
            raise RuntimeError("controller exploded")

        sup.get_run("alice").run_cycle = boom
        actions = sup.run_cycle("2026-05-14T10:00:00+00:00", _temps(), [0.5] * 16)
        assert {v for v, _a in actions} == {8, 9}
        assert any("alice" in a["message"] and a["level"] == "critical" for a in alerts)


def test_unnamed_calls_are_ambiguous_with_two_loaded() -> None:
    with TmpRoot() as root:
        sup, *_ = _sup(root)
        _two(sup)
        for call in (lambda: sup.status(), lambda: sup.enter_maintenance(),
                     lambda: sup.run()):
            try:
                call()
            except AmbiguousExperimentError as exc:
                assert exc.names == ["alice", "bob"]
            else:
                raise AssertionError("an unnamed call with two loaded must be ambiguous")
        assert sup.status("bob")["operator"] == "Bob"


def test_od_acquisition_must_agree_across_experiments() -> None:
    with TmpRoot() as root:
        sup, *_ = _sup(root)
        sup.create_experiment(name="alice", mode="turbidostat", vials=[0],
                              parameters=dict(TURB, od_acquisition={"agg": "median"}))
        try:
            sup.create_experiment(name="bob", mode="turbidostat", vials=[1],
                                  parameters=dict(TURB, od_acquisition={"agg": "mean"}))
        except ConflictError as exc:
            assert "agg" in str(exc)
        else:
            raise AssertionError("mismatched OD aggregation must be refused")
        # n_samples may differ; the machine reads at the larger.
        sup.create_experiment(name="carol", mode="turbidostat", vials=[2],
                              parameters=dict(TURB, od_acquisition={"agg": "median", "n_samples": 9}))
        sup.start_experiment("alice")
        sup.start_experiment("carol")
        assert sup.od_acquisition_params()["n_samples"] == 9


# ---------------------------------------------------------------------------
# Shared consumables
# ---------------------------------------------------------------------------

def test_a_shared_carboy_has_one_level_and_blocks_both() -> None:
    with TmpRoot() as root:
        sup, _manager, _events, alerts, clock = _sup(root)
        sup.create_experiment(name="alice", mode="turbidostat", vials=[0, 1],
                              parameters=dict(TURB),
                              media=_media("lb", [0, 1], capacity=500.0))
        # Bob shares Alice's carboy, created but not yet started.
        sup.create_experiment(name="bob", mode="chemostat", vials=[8],
                              parameters=dict(CHEM),
                              media=_media("m9", [8], waste={"vessel": "alice.waste"}))
        vessels = {v["id"]: v for v in sup.vessels()}
        assert vessels["alice.waste"]["users"] == ["alice", "bob"]
        sup.start_experiment("alice")
        sup.start_experiment("bob")
        for i in range(4):
            sup.run_cycle(f"2026-05-14T10:0{i}:00+00:00", _temps(), [0.5] * 16)
            clock["t"] += 60.0
        rec = {v["id"]: v for v in sup.vessels()}["alice.waste"]
        assert set(rec["attribution"]) == {"alice", "bob"}
        assert abs(rec["level_ml"] - sum(rec["attribution"].values())) < 1e-6
        # Both runs see the same level.
        a = sup.status("alice")["media"]["waste"]
        b = sup.status("bob")["media"]["waste"]
        assert a["filled_ml"] == b["filled_ml"] and b["shared"] is True
        # Fill it to the reserve: BOTH experiments' dilutions are suppressed.
        sup.get_run("alice")._waste_filled_ml = 450.0
        clock["t"] += 60.0
        actions = sup.run_cycle("2026-05-14T10:10:00+00:00", _temps(), [0.5] * 16)
        assert actions == [], actions
        # Every vial of both is blocked, so BOTH auto-enter consumables
        # maintenance -- the cross-experiment interlock (SPEC §15).
        for name in ("alice", "bob"):
            m = sup.status(name)["maintenance"]
            assert m["active"] and m["reason"] == "consumables", (name, m)
        # Emptying it through ONE experiment clears the block for both; each
        # then resumes explicitly, as consumables maintenance always has.
        sup.refill_media("alice", waste_filled_ml=0.0)
        assert not sup.status("bob")["media"]["waste"]["blocked"]
        sup.exit_maintenance("alice")
        sup.exit_maintenance("bob")
        clock["t"] += 60.0
        actions = sup.run_cycle("2026-05-14T10:11:00+00:00", _temps(), [0.5] * 16)
        assert 8 in {v for v, _a in actions}, actions
        # The registry file carries the level across a restart.
        on_disk = json.loads((root / "machine" / "vessels.json").read_text())
        assert any(v["id"] == "alice.waste" for v in on_disk["vessels"])


def test_unknown_or_wrong_kind_vessel_reference_is_refused() -> None:
    with TmpRoot() as root:
        sup, *_ = _sup(root)
        sup.create_experiment(name="alice", mode="turbidostat", vials=[0],
                              parameters=dict(TURB), media=_media("lb", [0]))
        for waste, match in (({"vessel": "nope"}, "not a known vessel"),
                             ({"vessel": "alice.lb"}, "not a waste carboy")):
            try:
                sup.create_experiment(name="bob", mode="turbidostat", vials=[1],
                                      parameters=dict(TURB),
                                      media=_media("m9", [1], waste=waste))
            except ValueError as exc:
                assert match in str(exc), str(exc)
            else:
                raise AssertionError(f"waste {waste} must be refused")


def test_a_morbidostat_drug_bottle_is_never_shared() -> None:
    with TmpRoot() as root:
        sup, *_ = _sup(root)
        morb = dict(TURB, target_od=0.4, od_lower=0.2)
        sup.create_experiment(name="alice", mode="morbidostat", vials=[0],
                              parameters=morb, media=_media("drug", [0]))
        try:
            sup.create_experiment(name="bob", mode="turbidostat", vials=[1],
                                  parameters=dict(TURB),
                                  media=_media("lb", [1], bottle_vessel="alice.drug"))
        except ConflictError as exc:
            assert "drug bottle" in str(exc)
        else:
            raise AssertionError("sharing a morbidostat drug bottle must be refused")


# ---------------------------------------------------------------------------
# Machine hold
# ---------------------------------------------------------------------------

def test_machine_hold_holds_every_experiment_and_releases_in_one_batch() -> None:
    with TmpRoot() as root:
        sup, _manager, events, alerts, clock = _sup(root)
        _two(sup)
        sup.start_experiment("alice")
        sup.start_experiment("bob")
        sup.enter_machine_hold()
        assert (root / "machine" / "hold.json").is_file()
        actions = sup.run_cycle("2026-05-14T10:00:00+00:00", _temps(), [0.5] * 16)
        assert actions == []
        assert sup.machine_hold["queued_pump_count"] == 6
        released = sup.exit_machine_hold()
        assert {v for v, _a, _ts in released} == {0, 1, 2, 3, 8, 9}
        assert sup.machine_hold is None
        assert not (root / "machine" / "hold.json").exists()
        assert any(e.get("type") == "machine_hold_entered" for e in events)
        assert any("Machine hold" in a["message"] for a in alerts)


def test_machine_hold_auto_resumes_after_30_minutes() -> None:
    with TmpRoot() as root:
        sup, *_ , clock = _sup(root)
        _two(sup)
        sup.start_experiment("alice")
        sup.enter_machine_hold()
        sup.run_cycle("2026-05-14T10:00:00+00:00", _temps(), [0.5] * 16)
        assert sup.check_maintenance_timeout() == []
        sup._machine_hold["entered_at"] -= timedelta(minutes=31)
        released = sup.check_maintenance_timeout()
        assert {v for v, _a, _ts in released} == {0, 1, 2, 3}
        assert sup.machine_hold is None


def test_held_dilutions_of_a_run_in_its_own_maintenance_wait_for_it() -> None:
    with TmpRoot() as root:
        sup, *_ = _sup(root)
        _two(sup)
        sup.start_experiment("alice")
        sup.start_experiment("bob")
        sup.enter_machine_hold()
        sup.run_cycle("2026-05-14T10:00:00+00:00", _temps(), [0.5] * 16)
        sup.enter_maintenance("alice")
        released = sup.exit_machine_hold()
        assert {v for v, _a, _ts in released} == {8, 9}
        assert sup.status("alice")["maintenance"]["queued_pump_count"] == 4
        # ...and fire when Alice resumes, as her own maintenance would.
        queued = sup.exit_maintenance("alice")
        assert {v for v, _a, _ts in queued} == {0, 1, 2, 3}


def test_machine_hold_survives_a_restart() -> None:
    with TmpRoot() as root:
        sup, *_ = _sup(root)
        sup.enter_machine_hold(reason="lid off")
        sup2, *_ = _sup(root)
        assert sup2.machine_hold is not None and sup2.machine_hold["reason"] == "lid off"


# ---------------------------------------------------------------------------
# Emergency stop, resume
# ---------------------------------------------------------------------------

def test_emergency_stop_ends_every_experiment() -> None:
    with TmpRoot() as root:
        sup, _manager, events, alerts, _clock = _sup(root)
        _two(sup)
        sup.start_experiment("alice")
        sup.start_experiment("bob")
        stopped = sup.handle_emergency_stop()
        assert sorted(stopped) == ["alice", "bob"]
        assert sup.loaded_names() == []
        stops = [e for e in events if e.get("type") == "stopped"]
        assert {e["experiment"] for e in stops} == {"alice", "bob"}
        assert {e["reason"] for e in stops} == {"emergency_stop"}
        crit = [a for a in alerts if a["level"] == "critical"]
        assert {a.get("experiment") for a in crit} >= {"alice", "bob"}


def test_every_running_experiment_resumes() -> None:
    with TmpRoot() as root:
        sup, *_ = _sup(root)
        _two(sup, a_media=_media("lb", [0, 1, 2, 3]))
        sup.start_experiment("alice")
        sup.start_experiment("bob")
        sup.run_cycle("2026-05-14T10:00:00+00:00", _temps(), [0.5] * 16)
        consumed = sup.status("alice")["media"]["bottles"][0]["consumed_ml"]
        assert consumed > 0
        # "restart": a fresh supervisor over the same directories
        sup2, manager2, *_ = _sup(root)
        assert sup2.resume_on_startup() == ["alice", "bob"]
        assert sup2.running_names() == ["alice", "bob"]
        assert sup2.status("alice")["media"]["bottles"][0]["consumed_ml"] == consumed
        assert sup2.status("bob")["operator"] == "Bob"
        stir = [int(x) for x in manager2.stir_speed]
        assert stir[0] == 8 and stir[8] == 5


def test_resume_refuses_both_experiments_claiming_one_vial() -> None:
    with TmpRoot() as root:
        sup, *_ = _sup(root)
        _two(sup)
        sup.start_experiment("alice")
        sup.start_experiment("bob")
        # Corrupt: Bob's state now also claims vial 0.
        path = root / "experiments" / "bob" / "state.json"
        state = json.loads(path.read_text())
        state["vials"] = [0, 8, 9]
        path.write_text(json.dumps(state))
        sup2, _m, _e, alerts2, _c = _sup(root)
        assert sup2.resume_on_startup() == []
        for name in ("alice", "bob"):
            st = json.loads((root / "experiments" / name / "state.json").read_text())
            assert st["status"] == ExperimentStatus.ERROR
            assert st["stop_reason"] == "resume_ownership_conflict"
        assert sum(a["level"] == "critical" for a in alerts2) == 2


# ---------------------------------------------------------------------------
# Records: events route to their own run
# ---------------------------------------------------------------------------

def test_events_land_in_their_own_experiments_log() -> None:
    import event_log as evlog

    with TmpRoot() as root:
        manager = MockSerialManager(seed=7)
        manager.load_calibration(TEMP_CAL, OD_CAL)
        dl = DataLogger(root / "experiments")
        ring = evlog.EventLog(dl)
        sup = ExperimentSupervisor(
            manager, dl, root / "experiments",
            on_event=lambda p: ring.record(message=str(p.get("type")),
                                           data={"experiment": p.get("experiment")}),
            on_alert=ring.record_alert,
            temp_cal=np.genfromtxt(TEMP_CAL, delimiter=","),
        )
        _two(sup)
        sup.start_experiment("alice")
        sup.start_experiment("bob")
        # Alice's run-scoped alert: hers only. A machine-wide one: both.
        sup.get_run("alice")._broadcast_alert(level="warning", message="alice-only",
                                              category="media")
        ring.record(level="critical", category="serial", message="bus down")
        a = (root / "experiments" / "alice" / "events.csv").read_text()
        b = (root / "experiments" / "bob" / "events.csv").read_text()
        assert "alice-only" in a and "alice-only" not in b
        assert "bus down" in a and "bus down" in b
        # Identical alert text from two runs is two drawer rows, not one.
        for name in ("alice", "bob"):
            sup.get_run(name)._broadcast_alert(level="warning", message="same text",
                                               category="media")
        assert sum(e["message"] == "same text" for e in ring.recent()) == 2


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

def _api_create(c, name, vials, operator, mode="turbidostat", params=None):
    return c.post("/api/experiments/create", json={
        "name": name, "mode": mode, "vials": vials, "operator": operator,
        "params": params or dict(TURB),
    })


def test_api_two_operators() -> None:
    from test_calibration_api import _make_app, _state  # noqa: E402

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        flask_app = _make_app(tmp)
        c = flask_app.test_client()
        state = _state(flask_app)

        assert _api_create(c, "alice", [0, 1], "Alice").status_code == 200
        r = _api_create(c, "bob", [1, 2], "Bob")
        assert r.status_code == 409 and "Alice" in r.get_json()["error"]
        assert _api_create(c, "bob", [8, 9], "Bob", mode="chemostat",
                           params=dict(CHEM)).status_code == 200
        for n in ("alice", "bob"):
            assert c.post(f"/api/experiments/{n}/start",
                          json={"allow_missing_od_blank": True}).status_code == 200

        # Unscoped maintenance is ambiguous; scoped works.
        r = c.post("/api/maintenance/enter")
        assert r.status_code == 409 and r.get_json()["code"] == "ambiguous_experiment"
        assert c.post("/api/experiments/bob/maintenance/enter").status_code == 200
        assert c.post("/api/experiments/bob/maintenance/exit").status_code == 200

        # Manual control of an owned vial names its experiment and operator.
        r = c.post("/api/actuators/pump", json={"vial": 8, "direction": "efflux", "seconds": 1})
        assert r.status_code == 409 and "bob" in r.get_json()["error"]
        assert "Bob" in r.get_json()["error"]
        # A free vial stays manual.
        assert c.post("/api/actuators/pump",
                      json={"vial": 5, "direction": "efflux", "seconds": 1}).status_code == 200

        payload = state.sensor_update_payload("2026-05-14T10:00:00+00:00")
        assert [e["name"] for e in payload["experiments"]] == ["alice", "bob"]
        assert payload["experiments"][1]["operator"] == "Bob"
        machine = c.get("/api/machine").get_json()
        assert machine["owners"]["8"] == "bob" and machine["owners"]["0"] == "alice"

        # Calibration that needs the whole machine names who is running.
        r = c.post("/api/calibration/pump/start", json={})
        assert r.status_code == 409 and set(r.get_json()["experiments"]) == {"alice", "bob"}

        # Machine hold round trip.
        assert c.post("/api/machine/hold").status_code == 200
        assert c.get("/api/machine").get_json()["machine_hold"]["active"] is True
        assert c.post("/api/machine/release").status_code == 200

        # Stopping Alice clears ONLY her vials' per-run OD blank.
        base = np.asarray(state.manager.od_cal).copy()
        state.manager.apply_od_blank({0: 1.23, 8: 4.56})
        assert c.post("/api/experiments/alice/stop").status_code == 200
        od_cal = np.asarray(state.manager.od_cal)
        assert od_cal[2, 0] == base[2, 0]
        assert od_cal[2, 8] == 4.56

        # Emergency stop ends everyone left.
        r = c.post("/api/actuators/emergency_stop")
        assert r.get_json()["experiments_stopped"] == ["bob"]


def test_api_blank_while_another_experiment_runs_darkens_only_its_vials() -> None:
    from test_calibration_api import _make_app, _state  # noqa: E402

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        flask_app = _make_app(tmp)
        c = flask_app.test_client()
        state = _state(flask_app)
        assert _api_create(c, "alice", [0, 1], "Alice").status_code == 200
        assert c.post("/api/experiments/alice/start",
                      json={"allow_missing_od_blank": True}).status_code == 200
        assert _api_create(c, "bob", [8, 9], "Bob").status_code == 200

        r = c.post("/api/calibration/od/blank/start", json={"experiment": "bob"})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["vials"] == [8, 9]
        r = c.post("/api/calibration/od/blank/dark", json={"session": r.get_json()["session"]})
        assert r.status_code == 200, r.get_json()
        leds = state.manager.last_collect_led_powers
        assert leds[8] == 0 and leds[9] == 0
        assert leds[0] > 0 and leds[1] > 0, "Alice's running vials were darkened"
