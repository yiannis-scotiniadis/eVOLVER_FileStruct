"""Supervised per-vial override (SPEC §21, ROADMAP Session S, vial_override.py).

Driven three ways, like the parallel-run tests:

* the pure pieces -- ``plan_override_pump``, ``fluidics.plan_pump_schedule``,
  ``run_config``'s ``vial_targets`` layer, the pump-log schema;
* ``ExperimentSupervisor`` directly, with a real DataLogger, MockSerialManager
  and a hand-advanced clock -- where controller coherence is checked;
* the HTTP API through ``create_app(use_mock=True)`` -- where the schedule on
  the wire, the CSV record and the plot-marker endpoint are checked.

What each test pins is the ROADMAP checklist: an unlock is required and
expires; a manual dilution is pushed into controller state so the controller
does not immediately re-dilute; media and waste move; every action is
recorded with operator and reason; a restart revokes in a defined way.
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fluidics  # noqa: E402
import run_config  # noqa: E402
from data_logger import PUMP_HEADER, PUMP_HEADER_V1, DataLogger  # noqa: E402
from experiment_engine import N_VIALS, compute_pump_quantization  # noqa: E402
from mock_serial_manager import MockSerialManager  # noqa: E402
from serial_manager import HEATER_OFF_SETPOINT  # noqa: E402
from supervisor import ExperimentSupervisor  # noqa: E402
from vial_override import (  # noqa: E402
    OverrideError,
    OverridePumpAction,
    override_history,
    plan_override_pump,
)

CAL_DIR = Path(__file__).resolve().parent.parent / "calibration"
TEMP_CAL = str(CAL_DIR / "temp_calibration.txt")
OD_CAL = str(CAL_DIR / "OD_cal.txt")

TURB = {
    "temperature_c": 37, "stir_rate": 8,
    "od_lower_thresh": 0.2, "od_upper_thresh": 0.4,
    "min_samples_before_action": 1, "pump_wait_minutes": 5.0,
    "volume_ml": 25, "efflux_extra_seconds": 2, "pump_flow_rates": [1.0] * 16,
}
CHEM = {
    "temperature_c": 30, "stir_rate": 5,
    "dilution_rate_per_hour": 30.0, "bolus_interval_seconds": 60.0,
    "volume_ml": 25, "efflux_extra_seconds": 2, "pump_flow_rates": [1.0] * 16,
}


class TmpRoot:
    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="evolver-override-test-"))
        return self.path

    def __exit__(self, *args) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _sup(root: Path):
    manager = MockSerialManager(seed=7)
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


def _media(bottle_id: str, vials, capacity=4000.0):
    return {
        "bottles": [{"id": bottle_id, "name": bottle_id.upper(),
                     "initial_volume_ml": 1000.0}],
        "vial_to_bottle": {str(v): bottle_id for v in vials},
        "waste": {"name": "Carboy", "capacity_ml": capacity},
    }


def _alice(sup, media=True):
    sup.create_experiment(
        name="alice", mode="turbidostat", vials=[0, 1, 2, 3],
        parameters=dict(TURB), operator="Alice",
        media=_media("lb", [0, 1, 2, 3]) if media else None,
    )
    sup.start_experiment("alice")


def _bob(sup):
    sup.create_experiment(name="bob", mode="chemostat", vials=[8, 9],
                          parameters=dict(CHEM), operator="Bob")
    sup.start_experiment("bob")


_TICK = {"n": 0}


def _tick(sup, clock, od=0.5, temps=None, advance=10.0):
    """One sensor tick at the current clock, then advance it."""
    _TICK["n"] += 1
    ts = f"2026-10-11T10:{_TICK['n'] // 60 % 60:02d}:{_TICK['n'] % 60:02d}+00:00"
    actions = sup.run_cycle(ts, temps or [37.0] * N_VIALS, [od] * N_VIALS)
    clock["t"] += advance
    return actions


def _events(events, kind):
    return [e for e in events if e.get("type") == kind]


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------

def _plan(action, ml, **kw):
    args = dict(influx_rate_ml_s=1.0, efflux_rate_ml_s=1.0, efflux_extra_seconds=2.0,
                headroom_ml=13.0, cap_seconds=30.0, quantise=compute_pump_quantization)
    args.update(kw)
    return plan_override_pump(action, ml, **args)


def test_plan_dilute_keeps_the_overrun_tail() -> None:
    p = _plan("dilute", 5.0)
    assert (p["influx_s"], p["efflux_s"]) == (5, 7)
    assert p["influx_ml"] == 5.0


def test_plan_single_sided_actions() -> None:
    assert (_plan("influx", 4.0)["influx_s"], _plan("influx", 4.0)["efflux_s"]) == (4, 0)
    p = _plan("efflux", 3.0, efflux_rate_ml_s=0.5)
    assert (p["influx_s"], p["efflux_s"]) == (0, 6)


@pytest.mark.parametrize("action,ml,match", [
    ("dilute", 0.5, "minimum"),              # under one second: never truncated
    ("dilute", 31.0, "cap"),                 # the cap is the exposure window
    ("influx", 14.0, "headroom"),            # 40 - 25 - 2 = 13 mL above the straw
    ("bogus", 1.0, "action"),
    ("dilute", -1.0, "> 0"),
])
def test_plan_refuses(action, ml, match) -> None:
    with pytest.raises(ValueError, match=match):
        _plan(action, ml)


def test_schedule_runs_manual_and_automatic_together() -> None:
    frames = fluidics.plan_pump_schedule(
        [fluidics.Dilution(0, 3, 5)], [fluidics.ManualRun(1, 0, 4)],
    )
    # Every pump starts in frame 0, and each runs exactly its own total.
    assert set(frames[0].pumps()) == {0, 16, 17}
    assert fluidics.seconds_per_pump(frames) == {0: 3, 16: 5, 17: 4}
    with pytest.raises(ValueError, match="twice"):
        fluidics.plan_pump_schedule([fluidics.Dilution(0, 3, 5)],
                                    [fluidics.ManualRun(0, 1, 0)])
    with pytest.raises(ValueError):
        fluidics.ManualRun(2, 0, 0)


def test_override_action_schedule_entry() -> None:
    dil = OverridePumpAction(pump_time=5.0, efflux_extra_seconds=2.0, average_od=None,
                             kind="dilute", influx_s=5, efflux_s=7)
    assert dil.schedule_entry(3) == fluidics.Dilution(3, 5, 7)
    spike = OverridePumpAction(pump_time=4.0, efflux_extra_seconds=0.0, average_od=None,
                               kind="influx", influx_s=4, efflux_s=0)
    assert spike.schedule_entry(3) == fluidics.ManualRun(3, 4, 0)


def test_vial_targets_layer_over_groups() -> None:
    groups = run_config.groups_from_config(
        {"vials": [0, 1], "mode": "turbidostat",
         "parameters": {"temperature_c": 37, "stir_rate": 8}},
        ("turbidostat",),
    )
    targets = run_config.normalise_vial_targets(
        {"1": {"temperature_c": 30, "stir_rate": 12, "junk": 1}, "x": {}, "5": "bad"}
    )
    assert targets == {1: {"temperature_c": 30, "stir_rate": 12}}
    assert run_config.temperature_c_by_vial(groups, targets) == {0: 37.0, 1: 30.0}
    assert run_config.stir_by_vial(groups, targets) == {0: 8, 1: 12}
    # A target for a vial outside the run is ignored, never invented.
    assert 5 not in run_config.stir_by_vial(groups, {5: {"stir_rate": 3}})


def test_pump_log_schema_two_and_a_schema_one_file_keeps_five_columns() -> None:
    with TmpRoot() as root:
        dl = DataLogger(root / "experiments")
        cfg = dl.create_experiment(name="s", mode="turbidostat", vials=[0, 1])
        assert cfg["pump_csv_schema"] == 2
        d = root / "experiments" / "s"
        # vial 1 is a run created before schema 2
        (d / "vial01_pump_log.csv").write_text(",".join(PUMP_HEADER_V1) + "\n")
        dl.activate_experiment("s")
        ts = "2026-10-11T10:00:00+00:00"
        dl.log_pump_event(ts, 0, "influx", 3, 0.4, source="override")
        dl.log_pump_event(ts, 1, "influx", 3, 0.4, source="override")
        with (d / "vial00_pump_log.csv").open() as f:
            rows = list(csv.reader(f))
        assert rows[0] == list(PUMP_HEADER) and rows[1][-1] == "override"
        with (d / "vial01_pump_log.csv").open() as f:
            rows = list(csv.reader(f))
        assert all(len(r) == len(PUMP_HEADER_V1) for r in rows)
        with pytest.raises(ValueError):
            dl.log_pump_event(ts, 0, "influx", 3, source="robot")


# ---------------------------------------------------------------------------
# Grant, expiry, refusal
# ---------------------------------------------------------------------------

def test_unlock_is_required_and_only_a_running_vial_can_be_unlocked() -> None:
    with TmpRoot() as root:
        sup, *_ = _sup(root)
        sup.create_experiment(name="alice", mode="turbidostat", vials=[0, 1],
                              parameters=dict(TURB), operator="Alice")
        with pytest.raises(OverrideError) as e:
            sup.grant_override(0, "Ana", "sample", 10)
        assert e.value.code == "not_running"
        sup.start_experiment("alice")

        for call in (lambda: sup.override_pump(0, "dilute", 3.0),
                     lambda: sup.override_temperature(0, 30.0),
                     lambda: sup.override_stir(0, 5)):
            with pytest.raises(OverrideError) as e:
                call()
            assert e.value.code == "override_required"

        with pytest.raises(OverrideError) as e:          # a free vial
            sup.grant_override(7, "Ana", "sample", 10)
        assert e.value.code == "not_owner" and e.value.status == 404
        for bad in (("", "why", 10), ("Ana", " ", 10), ("Ana", "why", 0),
                    ("Ana", "why", 31)):
            with pytest.raises(ValueError):
                sup.grant_override(0, *bad)

        out = sup.grant_override(0, "Ana", "pull a sample", 10)
        assert out["experiment"] == "alice" and out["operator"] == "Ana"
        assert out["expires_in_seconds"] == 600
        assert sup.status("alice")["overrides"]["0"]["reason"] == "pull a sample"


def test_override_expires_and_reverts_a_transient_change() -> None:
    with TmpRoot() as root:
        sup, manager, events, _alerts, clock = _sup(root)
        _alice(sup)
        run = sup.get_run("alice")
        raw_37 = int(manager.temp_setpoint_raw[0])
        sup.grant_override(0, "Ana", "warm briefly", 1)
        sup.override_temperature(0, 30.0, keep=False)
        sup.override_stir(0, 3, keep=False)
        assert int(manager.temp_setpoint_raw[0]) != raw_37
        _tick(sup, clock)
        assert int(manager.stir_speed[0]) == 3

        clock["t"] += 61.0
        _tick(sup, clock)
        assert sup.overrides_status() == {}
        assert int(manager.temp_setpoint_raw[0]) == raw_37
        assert int(manager.stir_speed[0]) == 8
        ended = _events(events, "override_expired")
        assert len(ended) == 1 and ended[0]["reverted"] == {
            "temperature_c": 37.0, "stir_rate": 8,
        }
        with pytest.raises(OverrideError) as e:
            sup.override_stir(0, 4)
        assert e.value.code == "override_required"
        assert run.status()["overrides"] == {}


def test_regrant_extends_and_is_recorded() -> None:
    with TmpRoot() as root:
        sup, _m, events, _a, clock = _sup(root)
        _alice(sup)
        sup.grant_override(0, "Ana", "sample", 1)
        clock["t"] += 50
        out = sup.grant_override(0, "Ana", "need longer", 5)
        assert out["expires_in_seconds"] == 300
        assert len(_events(events, "override_extended")) == 1
        clock["t"] += 100            # past the ORIGINAL expiry
        _tick(sup, clock)
        assert "0" in sup.overrides_status()


# ---------------------------------------------------------------------------
# Controller coherence -- the correctness requirement
# ---------------------------------------------------------------------------

def test_manual_dilution_is_pushed_into_the_turbidostat() -> None:
    """The double-dilution bug: a manual dilution invisible to the controller
    is followed at once by an automatic one. With the override it is not --
    pump_wait counts from the manual dilution."""
    with TmpRoot() as root:
        sup, _m, events, _a, clock = _sup(root)
        _alice(sup)
        sup.grant_override(0, "Ana", "rescue", 30)

        # Control is paused on vial 0; its neighbours dilute as usual.
        actions = _tick(sup, clock, od=0.5)
        assert 0 not in {v for v, _ in actions} and 1 in {v for v, _ in actions}

        q = sup.override_pump(0, "dilute", 5.0)
        assert q["status"] == "queued" and (q["influx_s"], q["efflux_s"]) == (5, 7)
        with pytest.raises(OverrideError) as e:
            sup.override_pump(0, "dilute", 2.0)
        assert e.value.code == "override_pump_pending"

        fired_at = clock["t"]
        actions = _tick(sup, clock, od=0.5)
        mine = [a for v, a in actions if v == 0]
        assert len(mine) == 1 and isinstance(mine[0], OverridePumpAction)
        assert (mine[0].influx_s, mine[0].efflux_s) == (5, 7)
        controller = sup.get_run("alice")._controllers[0]
        assert controller.last_pump_time == fired_at
        assert _events(events, "override_pump")[0]["operator"] == "Ana"

        sup.release_override(0, "Ana")
        # OD still above the band, but pump_wait (5 min) runs from the manual
        # dilution: no automatic dilution follows it.
        for _ in range(5):
            actions = _tick(sup, clock, od=0.5, advance=30.0)
            assert 0 not in {v for v, _ in actions}
        clock["t"] = fired_at + 5 * 60 + 1
        actions = _tick(sup, clock, od=0.5)
        assert 0 in {v for v, a in actions if not isinstance(a, OverridePumpAction)}


def test_efflux_only_does_not_restart_the_refractory_clock() -> None:
    with TmpRoot() as root:
        sup, _m, _e, _a, clock = _sup(root)
        _alice(sup)
        controller = sup.get_run("alice")._controllers[0]
        before = controller.last_pump_time
        sup.grant_override(0, "Ana", "drain spike", 10)
        sup.override_pump(0, "efflux", 3.0)
        _tick(sup, clock, od=0.1)
        assert controller.last_pump_time == before


def test_chemostat_manual_dilution_is_additive_and_no_catch_up_after_release() -> None:
    with TmpRoot() as root:
        sup, *_rest = _sup(root)
        _e, _a, clock = _rest[1], _rest[2], _rest[3]
        _bob(sup)
        _tick(sup, clock, advance=60.0)         # first boluses (cold start)
        c8 = sup.get_run("bob")._controllers[8]
        scheduled = c8.total_volume_ml
        sup.grant_override(8, "Bo", "spike", 30)
        fired = set()
        for _ in range(10):                      # 10 min of paused control
            fired |= {v for v, _ in _tick(sup, clock, advance=60.0)}
        assert 8 not in fired and 9 in fired
        sup.override_pump(8, "dilute", 3.0)
        _tick(sup, clock, advance=1.0)
        assert c8.manual_volume_ml == pytest.approx(3.0)
        assert c8.total_volume_ml == scheduled   # the schedule did not move

        sup.release_override(8)
        clock["t"] += 60.0
        actions = dict(_tick(sup, clock))
        # Sized from the 60 s since release, not the 10 paused minutes
        # (which would clamp at 4 intervals = 50 s of pump).
        assert actions[8].pump_time == pytest.approx(12.0, abs=1.0)


# ---------------------------------------------------------------------------
# Accounting, interlocks, holds
# ---------------------------------------------------------------------------

def test_manual_actions_debit_media_and_credit_waste() -> None:
    with TmpRoot() as root:
        sup, _m, _e, _a, clock = _sup(root)
        _alice(sup)
        def books():
            m = sup.status("alice")["media"]
            return m["bottles"][0]["consumed_ml"], m["waste"]["filled_ml"]
        sup.grant_override(0, "Ana", "dilute by hand", 30)
        b0, w0 = books()
        sup.override_pump(0, "dilute", 5.0)
        _tick(sup, clock, od=0.1)               # OD below band: no auto pumps
        b1, w1 = books()
        assert b1 - b0 == pytest.approx(5.0)
        assert w1 - w0 == pytest.approx(7.0)    # booked like an automatic dilution
        sup.override_pump(0, "influx", 4.0)
        _tick(sup, clock, od=0.1)
        b2, w2 = books()
        assert (b2 - b1, w2 - w1) == (pytest.approx(4.0), pytest.approx(0.0))
        sup.override_pump(0, "efflux", 3.0)
        _tick(sup, clock, od=0.1)
        b3, w3 = books()
        assert (b3 - b2, w3 - w2) == (pytest.approx(0.0), pytest.approx(3.0))
        with pytest.raises(ValueError, match="headroom"):
            sup.override_pump(0, "influx", 14.0)


def test_consumables_interlock_blocks_at_request_and_at_the_tick() -> None:
    with TmpRoot() as root:
        sup, _m, events, _a, clock = _sup(root)
        _alice(sup)
        run = sup.get_run("alice")
        sup.grant_override(0, "Ana", "dilute", 30)
        sup.override_pump(0, "dilute", 3.0)
        # The carboy fills between the request and the tick.
        run._vessels.add_level(run._waste_vessel, 3900.0, experiment="alice")
        actions = _tick(sup, clock, od=0.1)
        assert not any(isinstance(a, OverridePumpAction) for _v, a in actions)
        assert _events(events, "override_pump_cancelled")[0]["cause"] == "waste_full"
        assert _events(events, "pump_suppressed")
        with pytest.raises(OverrideError) as e:
            sup.override_pump(0, "efflux", 3.0)
        assert e.value.code == "consumables_blocked"


def test_maintenance_and_machine_hold_refuse_and_cancel_override_pumps() -> None:
    with TmpRoot() as root:
        sup, _m, events, _a, clock = _sup(root)
        _alice(sup)
        sup.grant_override(0, "Ana", "dilute", 30)
        sup.enter_maintenance("alice")
        with pytest.raises(OverrideError) as e:
            sup.override_pump(0, "dilute", 3.0)
        assert e.value.code == "maintenance_active"
        sup.exit_maintenance("alice")

        sup.enter_machine_hold()
        with pytest.raises(OverrideError) as e:
            sup.override_pump(0, "dilute", 3.0)
        assert e.value.code == "machine_hold"
        sup.exit_machine_hold()

        # Queued, then the hold arrives before the tick: cancelled, not held.
        sup.override_pump(0, "dilute", 3.0)
        sup.enter_machine_hold()
        _tick(sup, clock, od=0.1)
        assert _events(events, "override_pump_cancelled")[-1]["cause"] == "machine_hold"
        assert sup.exit_machine_hold() == []
        # Temperature and stir stay available under a hold.
        sup.override_stir(0, 4)


def test_grant_drops_an_automatic_dilution_queued_by_maintenance() -> None:
    with TmpRoot() as root:
        sup, _m, events, _a, clock = _sup(root)
        _alice(sup)
        sup.enter_maintenance("alice")
        _tick(sup, clock, od=0.5)                # vials 0..3 decide; queued
        sup.grant_override(0, "Ana", "hands in", 10)
        queued = sup.exit_maintenance("alice")
        assert 0 not in {v for v, _a, _t in queued} and 1 in {v for v, _a, _t in queued}
        assert _events(events, "override_pump_cancelled")


# ---------------------------------------------------------------------------
# Revocation and restart
# ---------------------------------------------------------------------------

def test_a_heater_fault_revokes_and_keeps_the_heater_parked() -> None:
    with TmpRoot() as root:
        sup, manager, events, _a, clock = _sup(root)
        _alice(sup)
        sup.grant_override(0, "Ana", "warm", 10)
        sup.override_temperature(0, 40.0)
        hot = [37.0] * N_VIALS
        hot[0] = 60.0
        for _ in range(3):
            _tick(sup, clock, od=0.1, temps=hot)
        assert sup.overrides_status() == {}
        assert int(manager.temp_setpoint_raw[0]) == HEATER_OFF_SETPOINT
        assert _events(events, "override_revoked")[0]["cause"] == "fault:overtemp"


def test_stop_revokes_every_override() -> None:
    with TmpRoot() as root:
        sup, _m, events, *_ = _sup(root)
        _alice(sup)
        sup.grant_override(0, "Ana", "x", 10)
        sup.grant_override(1, "Ana", "y", 10)
        sup.stop_experiment("alice")
        assert sup.overrides_status() == {}
        assert sorted(e["vial"] for e in _events(events, "override_revoked")) == [0, 1]


def test_restart_revokes_reverts_transient_and_keeps_kept_targets() -> None:
    with TmpRoot() as root:
        sup, manager, _e, _a, clock = _sup(root)
        _alice(sup)
        raw_37 = int(manager.temp_setpoint_raw[0])
        sup.grant_override(0, "Ana", "tune", 30)
        sup.override_temperature(0, 30.0, keep=False)
        sup.override_stir(0, 12, keep=True)
        sup.grant_override(1, "Ana", "tune", 30)
        sup.override_temperature(1, 33.0, keep=True)
        _tick(sup, clock, od=0.1)
        raw_33 = int(manager.temp_setpoint_raw[1])
        cfg = json.loads((root / "experiments" / "alice" / "config.json").read_text())
        assert cfg["vial_targets"] == {"0": {"stir_rate": 12}, "1": {"temperature_c": 33.0}}

        sup2, manager2, events2, *_ = _sup(root)
        assert sup2.resume_on_startup() == ["alice"]
        assert sup2.overrides_status() == {}
        assert int(manager2.temp_setpoint_raw[0]) == raw_37      # transient reverted
        assert int(manager2.stir_speed[0]) == 12                 # kept
        assert int(manager2.temp_setpoint_raw[1]) == raw_33      # kept
        revoked = _events(events2, "override_revoked")
        assert {e["vial"] for e in revoked} == {0, 1}
        assert all(e["cause"] == "restart" for e in revoked)
        # A recalibration re-derivation honours the kept target too.
        assert 1 not in sup2.get_run("alice").reapply_temperature_targets()


# ---------------------------------------------------------------------------
# Parallel runs
# ---------------------------------------------------------------------------

def test_an_override_in_one_run_leaves_the_other_alone() -> None:
    with TmpRoot() as root:
        sup, *_rest = _sup(root)
        clock = _rest[3]
        _alice(sup)
        _bob(sup)
        sup.grant_override(0, "Ana", "rescue", 10)
        with pytest.raises(OverrideError):
            sup.override_pump(8, "dilute", 3.0)     # Bob's vial is not unlocked
        sup.override_pump(0, "dilute", 3.0)
        actions = _tick(sup, clock, od=0.5)
        by_vial = {v: a for v, a in actions}
        # One list = one schedule: Alice's override pump and Bob's boluses.
        assert isinstance(by_vial[0], OverridePumpAction)
        assert {8, 9} <= set(by_vial)
        assert set(sup.overrides_status()) == {"0"}
        assert sup.overrides_status()["0"]["experiment"] == "alice"


# ---------------------------------------------------------------------------
# HTTP: the wire, the CSV record, the plot markers
# ---------------------------------------------------------------------------

def test_api_end_to_end() -> None:
    from test_event_log_api import _closure, _make_app  # noqa: E402

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        flask_app = _make_app(root)
        c = flask_app.test_client()
        body = {
            "name": "ov", "mode": "turbidostat", "vials": [0, 1], "operator": "Alice",
            "parameters": {"lower_thresh": [0.2] * 16, "upper_thresh": [0.4] * 16,
                           "efflux_extra_seconds": 2.0, "pump_flow_rates": [1.0] * 16},
            "media": {"bottles": [{"id": "b1", "name": "LB", "initial_volume_ml": 1000}],
                      "vial_to_bottle": {"0": "b1", "1": "b1"},
                      "waste": {"capacity_ml": 2000}},
        }
        assert c.post("/api/experiments/create", json=body).status_code == 200
        assert c.post("/api/experiments/ov/start",
                      json={"allow_missing_od_blank": True}).status_code == 200
        state = _closure(flask_app.view_functions["api_health"], "state")
        dispatch = _closure(flask_app.view_functions["api_maintenance_exit"],
                            "_execute_queued_pump_actions")

        # The plain actuator route stays shut; the override door needs a key.
        r = c.post("/api/actuators/pump", json={"vial": 0, "direction": "efflux", "seconds": 1})
        assert r.status_code == 409
        r = c.post("/api/vials/0/override/pump", json={"action": "dilute", "volume_ml": 3})
        assert r.status_code == 409 and r.get_json()["code"] == "override_required"
        r = c.post("/api/vials/5/override", json={"operator": "A", "reason": "r"})
        assert r.status_code == 404 and r.get_json()["code"] == "not_owner"
        r = c.post("/api/vials/0/override", json={"operator": "Ana"})
        assert r.status_code == 400

        r = c.post("/api/vials/0/override",
                   json={"operator": "Ana", "reason": "rescue stalled culture"})
        assert r.status_code == 200 and r.get_json()["expires_in_seconds"] == 600
        r = c.post("/api/vials/0/override/pump/preview",
                   json={"action": "influx", "volume_ml": 20})
        assert r.status_code == 400 and "headroom" in r.get_json()["error"]
        r = c.post("/api/vials/0/override/pump", json={"action": "dilute", "volume_ml": 4})
        assert r.status_code == 200 and r.get_json()["status"] == "queued"
        r = c.post("/api/vials/0/override/stir", json={"value": 5, "keep": "yes"})
        assert r.status_code == 400
        assert c.post("/api/vials/0/override/stir", json={"value": 5}).status_code == 200

        payload = state.sensor_update_payload("2026-10-11T10:00:00+00:00")
        assert payload["experiments"][0]["overrides"]["0"]["operator"] == "Ana"

        # One tick: the engine books the pump, the dispatcher puts it on the
        # wire inside the tick's single schedule.
        ts = "2026-10-11T10:00:10+00:00"
        n_before = len(state.manager.pump_log)
        actions = state.engine.run_cycle(ts, [37.0] * N_VIALS, [0.1] * N_VIALS)
        dispatch([(v, a, ts) for v, a in actions])
        log = state.manager.pump_log[n_before:]
        starts, ends = {}, {}
        for e in log:
            key = (e["vial"], e["direction"])
            starts.setdefault(key, e["sim_time"])
            ends[key] = e["sim_time"] + e["seconds"]
        t0 = starts[(0, "influx")]
        assert starts[(0, "efflux")] == t0                       # never serialised
        assert (ends[(0, "influx")] - t0, ends[(0, "efflux")] - t0) == (4, 6)

        with (root / "experiments" / "ov" / "vial00_pump_log.csv").open() as f:
            rows = list(csv.DictReader(f))
        assert [(r["direction"], r["source"]) for r in rows] == [
            ("influx", "override"), ("efflux", "override"),
        ]

        assert c.post("/api/vials/0/override/release",
                      json={"operator": "Ana"}).status_code == 200
        hist = c.get("/api/experiments/ov/overrides?vial=0").get_json()
        assert len(hist["windows"]) == 1
        w = hist["windows"][0]
        assert w["operator"] == "Ana" and w["ended_by"] == "released" and w["end"]
        kinds = [a["kind"] for a in hist["actions"]]
        assert kinds == ["stir", "pump"]
        assert all(a["operator"] == "Ana" for a in hist["actions"])
        assert all(a["reason"] == "rescue stalled culture" for a in hist["actions"])
        assert c.get("/api/experiments/../overrides").status_code in (400, 404)
        assert override_history(root / "nope.csv") == {"windows": [], "actions": []}
