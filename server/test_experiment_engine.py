"""Verification script for the ExperimentEngine (SPEC §9 + §10).

Integration tests against a real DataLogger and MockSerialManager. The
engine is driven by direct run_cycle() calls with synthetic sensor data
so we can deterministically trigger pump events, faults, and resumes.

Run from the project root:
    python server/test_experiment_engine.py
"""

from __future__ import annotations

import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_logger import DataLogger  # noqa: E402
from experiment_engine import (  # noqa: E402
    ConflictError,
    ExperimentEngine,
    ExperimentStatus,
    InvalidExperimentStateError,
    N_VIALS,
)
from control_modes.morbidostat import MorbidostatController  # noqa: E402
from mock_serial_manager import MockSerialManager  # noqa: E402
from serial_manager import HEATER_OFF_SETPOINT  # noqa: E402


# Safe per-vial raw setpoint for "non-experiment vial preset" assertions.
# Under the inverted convention any value above the per-vial safety floor
# (~340 for typical calibration) is permitted. 600 corresponds to roughly
# 15-25 °C across the vial range — well below MAX_SAFE_TEMP_C, so it
# passes through set_temperature_raw unchanged.
TEST_NON_EXP_RAW_SETPOINT = 600


CAL_DIR = Path(__file__).resolve().parent.parent / "calibration"
TEMP_CAL = str(CAL_DIR / "temp_calibration.txt")
OD_CAL = str(CAL_DIR / "OD_cal.txt")


class TmpRoot:
    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="evolver-engine-test-"))
        return self.path

    def __exit__(self, *args) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _temp_cal_array() -> np.ndarray:
    return np.genfromtxt(TEMP_CAL, delimiter=",")


def _fresh(root: Path, *, events: list = None, alerts: list = None, clock=None):
    """Return (engine, manager, data_logger, events_list, alerts_list)."""
    manager = MockSerialManager(seed=42)
    manager.load_calibration(TEMP_CAL, OD_CAL)
    data_logger = DataLogger(root)
    events_list = events if events is not None else []
    alerts_list = alerts if alerts is not None else []
    temp_cal = _temp_cal_array()
    if clock is None:
        # Default clock: a manually-advanced one so pump_wait gating is deterministic.
        clock_state = {"t": 1_000_000.0}
        def clock():  # noqa: E306
            return clock_state["t"]
        clock.state = clock_state  # type: ignore[attr-defined]
    engine = ExperimentEngine(
        serial_manager=manager,
        data_logger=data_logger,
        experiments_root=root,
        on_event=events_list.append,
        on_alert=alerts_list.append,
        temp_cal=temp_cal,
        clock=clock,
    )
    return engine, manager, data_logger, events_list, alerts_list


def _make_sensor_arrays(temp_c: float = 30.0, od: float = 0.1):
    return ([float(temp_c)] * N_VIALS, [float(od)] * N_VIALS)


def test_create_transitions_to_CREATED() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        config = engine.create_experiment(
            name="exp1", mode="turbidostat", vials=[0, 1, 2],
            parameters={"temperature_c": 37, "stir_rate": 10,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.4, "min_samples_before_action": 1,
                        "pump_wait_minutes": 1},
        )
        assert engine.status_string == ExperimentStatus.CREATED
        assert (root / "exp1" / "config.json").is_file()
        assert (root / "exp1" / "state.json").is_file()
        assert (root / "exp1" / "vial00_OD.csv").is_file()
        s = json.loads((root / "exp1" / "state.json").read_text())
        assert s["status"] == "created"
        assert s["vials"] == [0, 1, 2]
        # run_cycle in CREATED state is a no-op (no CSV rows appended).
        temps, ods = _make_sensor_arrays(od=0.5)
        engine.run_cycle("2026-05-14T10:00:00+00:00", temps, ods)
        with (root / "exp1" / "vial00_OD.csv").open() as f:
            rows = f.read().splitlines()
        assert len(rows) == 1, f"expected only header, got {len(rows)} lines"
    print("PASS  create_experiment transitions to CREATED, run_cycle is inert")


def test_start_transitions_to_RUNNING_and_applies_actuators() -> None:
    with TmpRoot() as root:
        engine, manager, dl, events, _ = _fresh(root)
        engine.create_experiment(
            name="exp1", mode="turbidostat", vials=[0, 3],
            parameters={"temperature_c": 37, "stir_rate": 8,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.4, "min_samples_before_action": 1,
                        "pump_wait_minutes": 1},
        )
        # Pre-load some non-experiment vials' setpoints to verify the engine
        # preserves them.
        manager.set_stir([0]*16)
        # Manually splice in a setpoint for a non-experiment vial:
        pre_raw = manager.temp_setpoint_raw.tolist()
        pre_raw[12] = TEST_NON_EXP_RAW_SETPOINT
        manager.set_temperature_raw(pre_raw)
        manager.set_stir([0]*16)
        # Now start the experiment.
        engine.start_experiment("exp1")
        assert engine.status_string == ExperimentStatus.RUNNING
        # Experiment vials got their setpoints; non-experiment vials preserved.
        assert manager.temp_setpoint_raw[12] == TEST_NON_EXP_RAW_SETPOINT, (
            f"non-experiment vial 12 lost its setpoint: {manager.temp_setpoint_raw[12]}"
        )
        # Experiment vial 0 was set to a heating setpoint (NOT parked off).
        # Under the inverted convention "actively heating" means
        # raw < HEATER_OFF_SETPOINT, not raw > 0.
        assert manager.temp_setpoint_raw[0] < HEATER_OFF_SETPOINT, (
            "experiment vial heater not set (still at HEATER_OFF_SETPOINT)"
        )
        assert manager.stir_speed[0] == 8
        assert manager.stir_speed[3] == 8
        assert manager.stir_speed[12] == 0
        event_types = {e.get("type") for e in events}
        assert "started" in event_types
    print("PASS  start_experiment applies initial actuators, preserves non-experiment vials")


def test_run_cycle_returns_pump_action_at_threshold() -> None:
    with TmpRoot() as root:
        engine, manager, dl, events, _ = _fresh(root)
        clock_state = engine._clock.state
        engine.create_experiment(
            name="exp1", mode="turbidostat", vials=[0],
            parameters={
                "temperature_c": 37, "stir_rate": 10,
                "od_lower_thresh": 0.2, "od_upper_thresh": 0.4, "min_samples_before_action": 1,
                "pump_wait_minutes": 15.0,   # long, so only one fires
                "volume_ml": 25,
                "pump_flow_rates": [1.0]*16,
            },
        )
        engine.start_experiment("exp1")

        # Push high OD across 5 cycles; only the first time the threshold
        # is crossed and pump_wait permits will a pump be decided.
        all_actions = []
        for tick in range(5):
            temps, ods = _make_sensor_arrays(temp_c=37.0, od=0.5)
            actions = engine.run_cycle(
                f"2026-05-14T10:00:{tick:02d}+00:00", temps, ods,
            )
            all_actions.extend(actions)
            clock_state["t"] += 10.0

        assert len(all_actions) == 1, (
            f"expected exactly one PumpAction returned, got {len(all_actions)}"
        )
        vial, action = all_actions[0]
        assert vial == 0
        # pump_time = -ln(0.2/0.5) * 25 / 1 = 22.91 -> capped at 20 by SPEC.
        assert 19.99 < action.pump_time <= 20.01
        assert action.efflux_extra_seconds == 5.0
        assert 0.49 < action.average_od < 0.51

        # Engine must not emit pump events on its own anymore.
        pump_events = [e for e in events if e.get("type") == "pump"]
        assert pump_events == [], "engine should not emit pump events directly"
    print(f"PASS  run_cycle returns pump action (pump_time={action.pump_time:.2f} s, capped)")


def test_run_cycle_respects_pump_wait_gate() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        clock_state = engine._clock.state
        engine.create_experiment(
            name="exp1", mode="turbidostat", vials=[0],
            parameters={
                "temperature_c": 37, "stir_rate": 10,
                "od_lower_thresh": 0.2, "od_upper_thresh": 0.4, "min_samples_before_action": 1,
                "pump_wait_minutes": 5.0,
                "volume_ml": 25,
                "pump_flow_rates": [1.0]*16,
            },
        )
        engine.start_experiment("exp1")
        # Fire once
        first_actions = []
        for _ in range(5):
            temps, ods = _make_sensor_arrays(temp_c=37.0, od=0.5)
            first_actions.extend(
                engine.run_cycle("2026-05-14T10:00:00+00:00", temps, ods)
            )
        assert len(first_actions) == 1, (
            f"expected one pump action on first window, got {len(first_actions)}"
        )
        # Try again 30 s later — must NOT fire (pump_wait = 5 min)
        clock_state["t"] += 30.0
        second_actions = []
        for _ in range(5):
            temps, ods = _make_sensor_arrays(temp_c=37.0, od=0.5)
            second_actions.extend(
                engine.run_cycle("2026-05-14T10:00:30+00:00", temps, ods)
            )
        assert second_actions == [], (
            f"expected no new pump actions within pump_wait; got {len(second_actions)}"
        )
    print("PASS  run_cycle gates pump actions by pump_wait")


def test_stop_zeros_experiment_vials_only() -> None:
    with TmpRoot() as root:
        engine, manager, *_ = _fresh(root)
        engine.create_experiment(
            name="exp1", mode="turbidostat", vials=[0, 5],
            parameters={"temperature_c": 37, "stir_rate": 10,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.4, "min_samples_before_action": 1,
                        "pump_wait_minutes": 1},
        )
        # Pre-set non-experiment vial 12.
        pre_raw = manager.temp_setpoint_raw.tolist()
        pre_raw[12] = TEST_NON_EXP_RAW_SETPOINT
        manager.set_temperature_raw(pre_raw)
        engine.start_experiment("exp1")

        raw_after_start = manager.temp_setpoint_raw[0]
        # Experiment vial is actively heating (not parked off).
        assert raw_after_start < HEATER_OFF_SETPOINT
        assert manager.temp_setpoint_raw[12] == TEST_NON_EXP_RAW_SETPOINT

        engine.stop_experiment(reason="test")
        assert engine.status_string == ExperimentStatus.STOPPED
        # Stop parks experiment vials' heaters OFF (HEATER_OFF_SETPOINT,
        # NOT zero — under inverted convention zero = max heat).
        assert manager.temp_setpoint_raw[0] == HEATER_OFF_SETPOINT, (
            f"experiment vial 0 heater not parked off "
            f"(got {manager.temp_setpoint_raw[0]}, expected {HEATER_OFF_SETPOINT})"
        )
        assert manager.temp_setpoint_raw[5] == HEATER_OFF_SETPOINT
        assert manager.temp_setpoint_raw[12] == TEST_NON_EXP_RAW_SETPOINT, (
            f"non-experiment vial 12 lost setpoint: {manager.temp_setpoint_raw[12]}"
        )
        assert manager.stir_speed[0] == 0
        assert manager.stir_speed[5] == 0
    print("PASS  stop_experiment parks experiment vials off, preserves others")


def test_overtemp_fault_isolates_single_vial() -> None:
    with TmpRoot() as root:
        engine, manager, dl, events, alerts = _fresh(root)
        engine.create_experiment(
            name="exp1", mode="turbidostat", vials=[0, 1, 2],
            parameters={"temperature_c": 37, "stir_rate": 10,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.4, "min_samples_before_action": 1,
                        "pump_wait_minutes": 1},
        )
        engine.start_experiment("exp1")
        before_raw = manager.temp_setpoint_raw.copy()

        # Drive run_cycle with vial 1 over critical, others normal.
        temps = [37.0] * N_VIALS
        temps[1] = 51.0
        ods = [0.1] * N_VIALS
        engine.run_cycle("2026-05-14T10:00:00+00:00", temps, ods)

        assert engine.status_string == ExperimentStatus.RUNNING, "engine should stay running"
        # Vial 1 heater parked OFF (HEATER_OFF_SETPOINT, NOT zero — under
        # the inverted convention zero would max the heater).
        assert manager.temp_setpoint_raw[1] == HEATER_OFF_SETPOINT, (
            f"vial 1 fault should park heater off, got {manager.temp_setpoint_raw[1]}"
        )
        # Vials 0 and 2 preserved
        assert manager.temp_setpoint_raw[0] == before_raw[0]
        assert manager.temp_setpoint_raw[2] == before_raw[2]
        # Critical alert emitted
        crit = [a for a in alerts if a["level"] == "critical"]
        assert any("overtemp" in a["message"] for a in crit), alerts
    print("PASS  overtemp fault parks affected vial off; others continue")


def test_sensor_failure_latches_after_threshold() -> None:
    with TmpRoot() as root:
        engine, manager, dl, events, alerts = _fresh(root)
        engine.create_experiment(
            name="exp1", mode="turbidostat", vials=[0, 1],
            parameters={"temperature_c": 37, "stir_rate": 10,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.4, "min_samples_before_action": 1,
                        "pump_wait_minutes": 1},
        )
        engine.start_experiment("exp1")
        # Drive 3 cycles with vial 0 reading NaN
        for i in range(3):
            temps = [37.0] * N_VIALS
            ods = [0.1] * N_VIALS
            ods[0] = float("nan")
            engine.run_cycle(f"2026-05-14T10:00:{i:02d}+00:00", temps, ods)
        # Vial 0 should be latched with fault=sensor_fail
        assert engine._vial_faults[0] == "sensor_fail", engine._vial_faults
        # Vial 1 unaffected
        assert engine._vial_faults[1] is None
        warns = [a for a in alerts if "sensor_fail" in a["message"]]
        assert warns, "expected warning alert for sensor_fail"
    print("PASS  sensor failure latches after threshold; other vials continue")


def test_emergency_stop_fully_stops_experiment() -> None:
    with TmpRoot() as root:
        engine, manager, dl, _, alerts = _fresh(root)
        engine.create_experiment(
            name="exp1", mode="turbidostat", vials=[0],
            parameters={"temperature_c": 37, "stir_rate": 10,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.4, "min_samples_before_action": 1,
                        "pump_wait_minutes": 1},
        )
        engine.start_experiment("exp1")
        engine.handle_emergency_stop()
        # Emergency stop now transitions all the way to STOPPED.
        assert engine.status_string == ExperimentStatus.STOPPED
        # A critical alert was broadcast.
        crit = [a for a in alerts if a.get("level") == "critical"]
        assert any("emergency" in a["message"].lower() for a in crit), alerts
        # Subsequent run_cycle returns empty action list.
        for i in range(5):
            temps, ods = _make_sensor_arrays(od=0.5)
            actions = engine.run_cycle(
                f"2026-05-14T10:00:{i:02d}+00:00", temps, ods
            )
            assert actions == [], "STOPPED engine must not return actions"
        # Calling stop_experiment again is idempotent (no error).
        again = engine.stop_experiment(reason="cleanup")
        assert again == "exp1"
        assert engine.status_string == ExperimentStatus.STOPPED
        # Heater parked off + stir zeroed for the experiment vial.
        assert manager.temp_setpoint_raw[0] == HEATER_OFF_SETPOINT
        assert manager.stir_speed[0] == 0
    print("PASS  emergency_stop fully stops experiment; heater parked off; idempotent")


def test_get_data_reads_csv_rows() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        clock_state = engine._clock.state
        engine.create_experiment(
            name="exp1", mode="turbidostat", vials=[0],
            parameters={"temperature_c": 37, "stir_rate": 10,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.4, "min_samples_before_action": 1,
                        "pump_wait_minutes": 1},
        )
        engine.start_experiment("exp1")
        # We need CSV rows; in a real run these come from sensor_loop calling
        # data_logger.log_sensor_cycle. Simulate that here.
        for i in range(20):
            engine._data_logger.log_sensor_cycle(
                f"2026-05-14T10:00:{i:02d}+00:00",
                [37.0]*N_VIALS, [400]*N_VIALS,
                [0.3]*N_VIALS, [50000]*N_VIALS,
            )
            clock_state["t"] += 10.0

        data = engine.get_data("exp1", vial=0, parameter="od")
        assert len(data["timestamps"]) == 20
        assert len(data["values"]) == 20
        assert all(abs(v - 0.3) < 1e-9 for v in data["values"])

        tail = engine.get_data("exp1", vial=0, parameter="od", last_n=5)
        assert len(tail["values"]) == 5

        temp = engine.get_data("exp1", vial=0, parameter="temp")
        assert len(temp["values"]) == 20
        assert all(abs(v - 37.0) < 1e-9 for v in temp["values"])
    print("PASS  get_data reads CSV rows for od/temp with optional last_n")


def test_list_experiments_includes_status() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        engine.create_experiment(
            name="a", mode="turbidostat", vials=[0],
            parameters={"temperature_c": 37, "stir_rate": 10},
        )
        engine.start_experiment("a")
        engine.stop_experiment()
        engine.create_experiment(
            name="b", mode="turbidostat", vials=[1],
            parameters={"temperature_c": 37, "stir_rate": 10},
        )
        engine.start_experiment("b")

        items = engine.list_experiments()
        names = {x["name"]: x for x in items}
        assert set(names) == {"a", "b"}
        assert names["a"]["status"] == "stopped"
        assert names["b"]["status"] == "running"
    print("PASS  list_experiments returns persisted status for every experiment dir")


def test_resume_on_startup_after_simulated_restart() -> None:
    with TmpRoot() as root:
        # ---- "First boot" -----------------------------------------------
        engine_a, mgr_a, dl_a, events_a, _ = _fresh(root)
        clock_state_a = engine_a._clock.state
        engine_a.create_experiment(
            name="resume1", mode="turbidostat", vials=[0, 1],
            parameters={"temperature_c": 37, "stir_rate": 10,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.4, "min_samples_before_action": 1,
                        "pump_wait_minutes": 1, "pump_flow_rates": [1.0]*16},
        )
        engine_a.start_experiment("resume1")
        # Drive a few cycles to accumulate controller state
        for i in range(6):
            temps, ods = _make_sensor_arrays(temp_c=37.0, od=0.42)
            engine_a.run_cycle(f"2026-05-14T10:00:{i:02d}+00:00", temps, ods)
            clock_state_a["t"] += 10.0
        # Controller for vial 0 should have OD history populated
        assert len(engine_a._controllers[0].od_history) == 5

        # ---- "Server restarts" (engine_a discarded) ----------------------
        # We deliberately do NOT call stop_experiment — state.json should
        # still say status=running so resume_on_startup picks it up.

        # ---- "Second boot" ------------------------------------------------
        engine_b, mgr_b, dl_b, events_b, _ = _fresh(root)
        resumed_name = engine_b.resume_on_startup()
        assert resumed_name == "resume1"
        assert engine_b.status_string == ExperimentStatus.RUNNING
        # Controllers should have inherited the OD history.
        assert len(engine_b._controllers[0].od_history) == 5
        # Heater + stir setpoints re-applied. Heater is actively heating
        # (raw < HEATER_OFF_SETPOINT under the inverted convention).
        assert mgr_b.temp_setpoint_raw[0] < HEATER_OFF_SETPOINT
        assert mgr_b.stir_speed[0] == 10
        # Resumed event emitted
        assert any(e.get("type") == "resumed" for e in events_b)

        engine_b.stop_experiment(reason="test_cleanup")
    print("PASS  resume_on_startup recovers a running experiment with full state")


def test_create_while_loaded_running_raises() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        engine.create_experiment(
            name="a", mode="turbidostat", vials=[0],
            parameters={"temperature_c": 37, "stir_rate": 10},
        )
        engine.start_experiment("a")
        try:
            engine.create_experiment(
                name="b", mode="turbidostat", vials=[1],
                parameters={"temperature_c": 37, "stir_rate": 10},
            )
        except InvalidExperimentStateError:
            print("PASS  cannot create a new experiment while one is RUNNING")
            return
        raise AssertionError("expected InvalidExperimentStateError")


def test_create_after_stop_unloads_previous() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        engine.create_experiment(name="a", mode="turbidostat", vials=[0],
                                  parameters={"temperature_c": 37, "stir_rate": 10})
        engine.start_experiment("a")
        engine.stop_experiment()
        # Creating a new one should now succeed; previous unloaded.
        engine.create_experiment(name="b", mode="turbidostat", vials=[1],
                                  parameters={"temperature_c": 37, "stir_rate": 10})
        assert engine.loaded_experiment == "b"
        assert engine.status_string == ExperimentStatus.CREATED
    print("PASS  create after stop unloads previous and creates new")


def test_delete_experiment() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        engine.create_experiment(name="a", mode="turbidostat", vials=[0],
                                  parameters={"temperature_c": 37, "stir_rate": 10})
        engine.start_experiment("a")
        engine.stop_experiment()
        engine.delete_experiment("a")
        assert not (root / "a").exists()
        assert engine.loaded_experiment is None
    print("PASS  delete_experiment removes directory")


def _basic_media(*vials: int) -> dict:
    """Helper: a 2-bottle media config covering the given vials.
    Vials with even index get bottle_a; odd-index get bottle_b."""
    v2b = {}
    for v in vials:
        v2b[str(v)] = "bottle_a" if v % 2 == 0 else "bottle_b"
    return {
        "bottles": [
            {"id": "bottle_a", "name": "LB",
             "initial_volume_ml": 1000.0, "low_volume_alert_ml": 100.0},
            {"id": "bottle_b", "name": "LB + drug",
             "initial_volume_ml": 500.0,  "low_volume_alert_ml": 50.0},
        ],
        "vial_to_bottle": v2b,
        "waste": {"name": "Carboy", "capacity_ml": 4000.0,
                  "high_fill_alert_ml": 3600.0},
    }


def test_media_config_round_trip() -> None:
    """Media block written at create_experiment is read back from config.json."""
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        media = _basic_media(0, 1, 2)
        config = engine.create_experiment(
            name="m1", mode="turbidostat",
            parameters={"temperature_c": 37, "stir_rate": 10,
                        "pump_wait_minutes": 1, "pump_flow_rates": [1.0]*16},
            media=media,
        )
        assert config["vials"] == [0, 1, 2], "vials derived from media.vial_to_bottle"
        assert "media" in config
        saved = json.loads((root / "m1" / "config.json").read_text())
        assert saved["media"]["bottles"][0]["id"] == "bottle_a"
        assert saved["media"]["bottles"][0]["initial_volume_ml"] == 1000.0
        assert saved["media"]["vial_to_bottle"] == {"0": "bottle_a", "1": "bottle_b", "2": "bottle_a"}
        assert saved["media"]["waste"]["capacity_ml"] == 4000.0
    print("PASS  media config round-trips through config.json")


def test_media_invalid_inputs_rejected() -> None:
    bad_cases = [
        # bottles missing
        ({"vial_to_bottle": {"0": "b"}, "waste": {"capacity_ml": 1000}},
         "media.bottles"),
        # bottle missing initial_volume_ml
        ({"bottles": [{"id": "x", "name": "X"}],
          "vial_to_bottle": {"0": "x"},
          "waste": {"capacity_ml": 1000}},
         "initial_volume_ml"),
        # duplicate bottle id
        ({"bottles": [{"id": "x", "name": "A", "initial_volume_ml": 100},
                      {"id": "x", "name": "B", "initial_volume_ml": 100}],
          "vial_to_bottle": {"0": "x"}, "waste": {"capacity_ml": 1000}},
         "duplicate bottle id"),
        # bottle id with bad characters
        ({"bottles": [{"id": "Bad-Id!", "name": "X", "initial_volume_ml": 100}],
          "vial_to_bottle": {"0": "Bad-Id!"}, "waste": {"capacity_ml": 1000}},
         "id"),
        # vial_to_bottle references unknown bottle
        ({"bottles": [{"id": "x", "name": "X", "initial_volume_ml": 100}],
          "vial_to_bottle": {"0": "y"}, "waste": {"capacity_ml": 1000}},
         "unknown bottle"),
        # waste capacity zero
        ({"bottles": [{"id": "x", "name": "X", "initial_volume_ml": 100}],
          "vial_to_bottle": {"0": "x"}, "waste": {"capacity_ml": 0}},
         "capacity_ml"),
        # high_fill > capacity
        ({"bottles": [{"id": "x", "name": "X", "initial_volume_ml": 100}],
          "vial_to_bottle": {"0": "x"},
          "waste": {"capacity_ml": 1000, "high_fill_alert_ml": 2000}},
         "high_fill_alert_ml"),
    ]
    for media, expected_msg in bad_cases:
        with TmpRoot() as root:
            engine, *_ = _fresh(root)
            try:
                engine.create_experiment(
                    name="m1", mode="turbidostat",
                    parameters={"temperature_c": 37, "stir_rate": 10},
                    media=media,
                )
            except ValueError as exc:
                assert expected_msg in str(exc), (
                    f"expected error containing {expected_msg!r}, got: {exc}"
                )
                continue
            raise AssertionError(f"accepted bad media: {media}")
    print("PASS  invalid media payloads rejected with descriptive errors")


def test_vials_and_media_mismatch_rejected() -> None:
    """If both `vials` and `media.vial_to_bottle` are supplied they must agree."""
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        media = _basic_media(0, 1)
        try:
            engine.create_experiment(
                name="mm", mode="turbidostat",
                vials=[0, 1, 2],   # not equal to media keys [0, 1]
                parameters={"temperature_c": 37, "stir_rate": 10},
                media=media,
            )
        except ValueError as exc:
            assert "vial_to_bottle" in str(exc), exc
            print("PASS  vials/media mismatch rejected")
            return
        raise AssertionError("expected ValueError for vials/media mismatch")


def test_run_cycle_debits_correct_bottle_and_waste() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        clock_state = engine._clock.state
        media = _basic_media(0, 1)  # vial 0 -> bottle_a, vial 1 -> bottle_b
        engine.create_experiment(
            name="m1", mode="turbidostat",
            parameters={"temperature_c": 37, "stir_rate": 10,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.4, "min_samples_before_action": 1,
                        "pump_wait_minutes": 15.0, "volume_ml": 25,
                        "pump_flow_rates": [1.0]*16},
            media=media,
        )
        engine.start_experiment("m1")

        # Drive high OD across 5 cycles. With pump_wait=15min each vial fires once.
        all_actions = []
        for tick in range(5):
            temps, ods = _make_sensor_arrays(temp_c=37.0, od=0.5)
            all_actions.extend(engine.run_cycle(
                f"2026-05-14T10:00:{tick:02d}+00:00", temps, ods,
            ))
            clock_state["t"] += 10.0

        # One PumpAction per vial — each capped at 20s influx + 5s efflux extra.
        assert len(all_actions) == 2
        # Bottle A debited by vial 0's influx: 20 s * 1 ml/s = 20 mL
        assert abs(engine._bottle_consumed_ml["bottle_a"] - 20.0) < 1e-6
        # Bottle B debited by vial 1's influx
        assert abs(engine._bottle_consumed_ml["bottle_b"] - 20.0) < 1e-6
        # Waste filled by both vials: each (20 + 5) s * 1 ml/s = 25 mL → 50 mL total
        assert abs(engine._waste_filled_ml - 50.0) < 1e-6

        s = engine.status()
        assert s["media"]["bottles"][0]["remaining_ml"] == 980.0  # 1000 - 20
        assert s["media"]["bottles"][1]["remaining_ml"] == 480.0  # 500 - 20
        assert s["media"]["waste"]["filled_ml"] == 50.0
    print("PASS  run_cycle debits correct bottle and waste per pump action")


def test_low_volume_alert_is_edge_triggered() -> None:
    """Alert fires exactly once on threshold crossing, even with repeated cycles."""
    with TmpRoot() as root:
        engine, *_, alerts = _fresh(root)
        clock_state = engine._clock.state
        # 20 mL bottle, alert at 5 mL remaining. First 20-s pump consumes the
        # whole thing in one shot, crossing the threshold.
        media = {
            "bottles": [{"id": "small_a", "name": "Small", "initial_volume_ml": 20.0,
                         "low_volume_alert_ml": 5.0}],
            "vial_to_bottle": {"0": "small_a"},
            "waste": {"name": "W", "capacity_ml": 1000.0, "high_fill_alert_ml": 900.0},
        }
        engine.create_experiment(
            name="m1", mode="turbidostat",
            parameters={"temperature_c": 37, "stir_rate": 10,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.4, "min_samples_before_action": 1,
                        "pump_wait_minutes": 0.05, "volume_ml": 25,
                        "pump_flow_rates": [1.0]*16},
            media=media,
        )
        engine.start_experiment("m1")
        for tick in range(8):
            temps, ods = _make_sensor_arrays(temp_c=37.0, od=0.5)
            engine.run_cycle(f"2026-05-14T10:00:{tick:02d}+00:00", temps, ods)
            clock_state["t"] += 10.0
        low_alerts = [a for a in alerts if "Small" in a.get("message", "")]
        assert len(low_alerts) == 1, (
            f"expected exactly one low-volume alert, got {len(low_alerts)}"
        )
        assert engine._bottle_alerted_low["small_a"] is True
    print(f"PASS  low-volume alert is edge-triggered ({len(low_alerts)} emission)")


def test_waste_alert_fires_on_threshold() -> None:
    with TmpRoot() as root:
        engine, *_, alerts = _fresh(root)
        clock_state = engine._clock.state
        media = {
            "bottles": [{"id": "x", "name": "X", "initial_volume_ml": 10000.0,
                         "low_volume_alert_ml": 100.0}],
            "vial_to_bottle": {"0": "x"},
            "waste": {"name": "Tiny", "capacity_ml": 50.0, "high_fill_alert_ml": 30.0},
        }
        engine.create_experiment(
            name="w1", mode="turbidostat",
            parameters={"temperature_c": 37, "stir_rate": 10,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.4, "min_samples_before_action": 1,
                        "pump_wait_minutes": 0.05, "volume_ml": 25,
                        "pump_flow_rates": [1.0]*16},
            media=media,
        )
        engine.start_experiment("w1")
        for tick in range(5):
            temps, ods = _make_sensor_arrays(temp_c=37.0, od=0.5)
            engine.run_cycle(f"2026-05-14T10:00:{tick:02d}+00:00", temps, ods)
            clock_state["t"] += 10.0
        waste_alerts = [a for a in alerts if "Waste" in a.get("message", "")]
        assert len(waste_alerts) == 1, waste_alerts
        assert engine._waste_alerted_high is True
    print("PASS  waste-full alert fires once on threshold cross")


def test_media_state_persists_and_resumes() -> None:
    with TmpRoot() as root:
        engine_a, *_ = _fresh(root)
        clock_state_a = engine_a._clock.state
        media = _basic_media(0, 1)
        engine_a.create_experiment(
            name="r1", mode="turbidostat",
            parameters={"temperature_c": 37, "stir_rate": 10,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.4, "min_samples_before_action": 1,
                        "pump_wait_minutes": 15.0, "volume_ml": 25,
                        "pump_flow_rates": [1.0]*16},
            media=media,
        )
        engine_a.start_experiment("r1")
        for tick in range(5):
            temps, ods = _make_sensor_arrays(temp_c=37.0, od=0.5)
            engine_a.run_cycle(f"2026-05-14T10:00:{tick:02d}+00:00", temps, ods)
            clock_state_a["t"] += 10.0
        consumed_a_before = engine_a._bottle_consumed_ml["bottle_a"]
        waste_before = engine_a._waste_filled_ml
        assert consumed_a_before > 0
        # Simulated restart: throw away engine_a, construct engine_b
        engine_b, *_ = _fresh(root)
        resumed = engine_b.resume_on_startup()
        assert resumed == "r1"
        assert engine_b._bottle_consumed_ml["bottle_a"] == consumed_a_before
        assert engine_b._waste_filled_ml == waste_before
        # And the status snapshot reports the same numbers.
        s = engine_b.status()
        assert abs(s["media"]["bottles"][0]["consumed_ml"] - consumed_a_before) < 1e-6
        engine_b.stop_experiment(reason="test_cleanup")
    print(f"PASS  media_state persists across simulated restart (consumed={consumed_a_before:.2f} mL)")


def test_status_omits_media_when_absent() -> None:
    """No media supplied at create => status().media is None (backward compatible)."""
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        engine.create_experiment(
            name="nm", mode="turbidostat", vials=[0],
            parameters={"temperature_c": 37, "stir_rate": 10},
        )
        engine.start_experiment("nm")
        s = engine.status()
        assert s["media"] is None, f"expected media=None, got {s['media']}"
    print("PASS  status() returns media=None when no media block was supplied")


def test_maintenance_queues_actions_and_resumes() -> None:
    """During maintenance: run_cycle returns []; pump actions queue (coalesced
    per vial). On exit: queued actions are returned for firing."""
    with TmpRoot() as root:
        engine, *_, events, _ = _fresh(root)
        clock_state = engine._clock.state
        engine.create_experiment(
            name="m1", mode="turbidostat", vials=[0],
            parameters={"temperature_c": 37, "stir_rate": 10,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.4, "min_samples_before_action": 1,
                        "pump_wait_minutes": 15.0, "volume_ml": 25,
                        "pump_flow_rates": [1.0] * 16},
        )
        engine.start_experiment("m1")

        # Pre-maintenance: a high-OD cycle returns an action.
        temps, ods = _make_sensor_arrays(temp_c=37.0, od=0.5)
        actions = engine.run_cycle("2026-05-15T10:00:00+00:00", temps, ods)
        assert len(actions) == 1, "expected pre-maintenance pump action"
        clock_state["t"] += 1000.0  # past pump_wait so the next decision fires

        # Enter maintenance.
        engine.enter_maintenance()
        assert engine.is_maintenance_active is True

        # Several maintenance cycles: each decision is queued, not returned.
        for tick in range(3):
            temps, ods = _make_sensor_arrays(temp_c=37.0, od=0.5)
            r = engine.run_cycle(f"2026-05-15T10:01:{tick:02d}+00:00", temps, ods)
            assert r == [], f"maintenance cycle {tick} should return [] (got {r})"
            clock_state["t"] += 1000.0  # advance past pump_wait each tick
        # Queue should be coalesced per vial (one entry max for vial 0).
        assert len(engine._pending_pump_actions) == 1

        # Exit: returns the queued action(s) for the caller to fire.
        queued = engine.exit_maintenance(reason="manual")
        assert engine.is_maintenance_active is False
        assert len(queued) == 1
        vial, action, ts_iso = queued[0]
        assert vial == 0
        assert isinstance(ts_iso, str) and ts_iso.startswith("2026-05-15T10:01:")
        evt_types = {e.get("type") for e in events}
        assert "maintenance_entered" in evt_types
        assert "maintenance_exited" in evt_types
    print("PASS  maintenance queues + coalesces pump actions; exit returns them")


def test_maintenance_only_from_running() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        # Not even created — must reject.
        try:
            engine.enter_maintenance()
        except InvalidExperimentStateError:
            pass
        else:
            raise AssertionError("expected InvalidExperimentStateError")
        engine.create_experiment(
            name="m1", mode="turbidostat", vials=[0],
            parameters={"temperature_c": 37, "stir_rate": 10},
        )
        # CREATED — also reject.
        try:
            engine.enter_maintenance()
        except InvalidExperimentStateError:
            pass
        else:
            raise AssertionError("expected InvalidExperimentStateError from CREATED")
        engine.start_experiment("m1")
        # Now allowed.
        engine.enter_maintenance()
        assert engine.is_maintenance_active is True
        # Idempotent enter.
        engine.enter_maintenance()
        assert engine.is_maintenance_active is True
        engine.exit_maintenance()
        # Idempotent exit.
        assert engine.exit_maintenance() == []
    print("PASS  enter_maintenance only allowed from RUNNING; idempotent")


def test_maintenance_auto_resume_on_timeout() -> None:
    """check_maintenance_timeout auto-exits + returns queued actions when
    the elapsed time exceeds the configured threshold."""
    with TmpRoot() as root:
        # Tight timeout (0.01 min = 0.6 s) so the test wall-clock cost is tiny.
        engine, *_, alerts = _fresh(root)
        engine._maintenance_timeout_seconds = 0.6
        clock_state = engine._clock.state
        engine.create_experiment(
            name="m1", mode="turbidostat", vials=[0],
            parameters={"temperature_c": 37, "stir_rate": 10,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.4, "min_samples_before_action": 1,
                        "pump_wait_minutes": 15.0, "volume_ml": 25,
                        "pump_flow_rates": [1.0] * 16},
        )
        engine.start_experiment("m1")
        engine.enter_maintenance()
        # Drive one cycle to queue an action.
        clock_state["t"] += 1000.0
        temps, ods = _make_sensor_arrays(temp_c=37.0, od=0.5)
        engine.run_cycle("2026-05-15T10:00:00+00:00", temps, ods)
        # Not timed out yet.
        assert engine.check_maintenance_timeout() is None
        # Wait past the timeout (real wall clock since entered_at uses now()).
        import time as _t
        _t.sleep(0.8)
        queued = engine.check_maintenance_timeout()
        assert queued is not None and len(queued) == 1
        assert engine.is_maintenance_active is False
        crit_alerts = [a for a in alerts if a.get("level") == "critical"
                       and "auto-resumed" in a.get("message", "")]
        assert crit_alerts, f"expected a critical auto-resume alert, got {alerts}"
    print(f"PASS  maintenance auto-resumes after timeout with critical alert ({len(crit_alerts)})")


def test_maintenance_refill_resets_alert_latch() -> None:
    with TmpRoot() as root:
        engine, *_, alerts = _fresh(root)
        clock_state = engine._clock.state
        media = {
            "bottles": [{"id": "small_a", "name": "Small", "initial_volume_ml": 20.0,
                         "low_volume_alert_ml": 5.0}],
            "vial_to_bottle": {"0": "small_a"},
            "waste": {"name": "W", "capacity_ml": 1000.0, "high_fill_alert_ml": 900.0},
        }
        engine.create_experiment(
            name="m1", mode="turbidostat",
            parameters={"temperature_c": 37, "stir_rate": 10,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.4, "min_samples_before_action": 1,
                        "pump_wait_minutes": 0.05, "volume_ml": 25,
                        "pump_flow_rates": [1.0] * 16},
            media=media,
        )
        engine.start_experiment("m1")
        # Consume past the low-volume threshold.
        for tick in range(5):
            temps, ods = _make_sensor_arrays(temp_c=37.0, od=0.5)
            engine.run_cycle(f"2026-05-15T10:00:{tick:02d}+00:00", temps, ods)
            clock_state["t"] += 10.0
        assert engine._bottle_alerted_low["small_a"] is True

        # Reject refill outside maintenance.
        try:
            engine.refill_media(bottles={"small_a": 20.0})
        except InvalidExperimentStateError:
            pass
        else:
            raise AssertionError("refill outside maintenance should be rejected")

        engine.enter_maintenance()
        # Reject unknown bottle id.
        try:
            engine.refill_media(bottles={"nope": 5.0})
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for unknown bottle id")
        # Reject over-capacity refill.
        try:
            engine.refill_media(bottles={"small_a": 9999.0})
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for over-capacity refill")

        # Refill clears consumption + alert latch.
        engine.refill_media(bottles={"small_a": 20.0}, waste_filled_ml=0.0)
        assert abs(engine._bottle_consumed_ml["small_a"]) < 1e-9
        assert engine._bottle_alerted_low["small_a"] is False
        assert engine._waste_filled_ml == 0.0
        engine.exit_maintenance()
    print("PASS  refill_media resets consumption + alert latches during maintenance")


def test_maintenance_persists_across_resume() -> None:
    """If the server restarts while maintenance is active, resume_on_startup
    restores the flag (but not the queued actions — they'd be stale)."""
    with TmpRoot() as root:
        engine_a, *_ = _fresh(root)
        engine_a.create_experiment(
            name="m1", mode="turbidostat", vials=[0],
            parameters={"temperature_c": 37, "stir_rate": 10},
        )
        engine_a.start_experiment("m1")
        engine_a.enter_maintenance()
        # Drop engine_a, build engine_b pointing at the same root.
        engine_b, *_ = _fresh(root)
        resumed = engine_b.resume_on_startup()
        assert resumed == "m1"
        assert engine_b.is_maintenance_active is True
        assert engine_b._pending_pump_actions == {}, (
            "pending pump actions should NOT be restored (potential stale)"
        )
        # Clean up
        engine_b.exit_maintenance()
        engine_b.stop_experiment(reason="cleanup")
    print("PASS  maintenance flag persists across resume (queued actions do not)")


def test_unsupported_mode_rejected() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        try:
            engine.create_experiment(name="x", mode="ridiculous_mode", vials=[0],
                                      parameters={"temperature_c": 37})
        except ValueError:
            print("PASS  unsupported mode rejected (whitelist enforcement)")
            return
        raise AssertionError("expected ValueError for unsupported mode")


def _chemostat_params(**overrides) -> dict:
    p = {
        "temperature_c": 37, "stir_rate": 8,
        "dilution_rate_per_hour": 1.0,
        "bolus_interval_seconds": 10.0,
        "volume_ml": 25.0,
    }
    p.update(overrides)
    return p


def _morbidostat_params(**overrides) -> dict:
    p = {
        "temperature_c": 37, "stir_rate": 8,
        "target_od": 0.4, "od_lower": 0.2,
        "pump_wait_minutes": 1,
        "volume_ml": 25.0,
        "initial_drug_conc": 1.0,
        "drug_step": 2.0,
        "adaptation_threshold_per_hour": 0.3,
        "growth_window_seconds": 1800.0,
        "growth_min_samples": 6,
        "escalation_cooldown_seconds": 300.0,
        "escalation_reminder_interval_seconds": 600.0,
        # Disable warmup gate for unit tests so the underlying turbidostat
        # logic can be exercised with a handful of synthetic samples.
        "min_samples_before_action": 1,
    }
    p.update(overrides)
    return p


def test_create_experiment_accepts_chemostat() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        config = engine.create_experiment(
            name="chem1", mode="chemostat", vials=[0, 1],
            parameters=_chemostat_params(),
        )
        assert config["mode"] == "chemostat"
        assert engine.status_string == ExperimentStatus.CREATED
        # Round-tripped through config.json
        saved = json.loads((root / "chem1" / "config.json").read_text())
        assert saved["mode"] == "chemostat"
        assert saved["parameters"]["dilution_rate_per_hour"] == 1.0
    print("PASS  create_experiment accepts mode=chemostat with valid params")


def test_create_experiment_accepts_morbidostat() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        config = engine.create_experiment(
            name="morb1", mode="morbidostat", vials=[0, 1],
            parameters=_morbidostat_params(),
        )
        assert config["mode"] == "morbidostat"
        saved = json.loads((root / "morb1" / "config.json").read_text())
        assert saved["mode"] == "morbidostat"
        assert saved["parameters"]["drug_step"] == 2.0
    print("PASS  create_experiment accepts mode=morbidostat with valid params")


def test_chemostat_run_cycle_returns_pump_action() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        # D=20/h, V=25, T=10 -> per-bolus pump_time = 1.389 s (above 1 s so
        # the deficit accumulator passes it straight through every cycle).
        # A slower chemostat (per-bolus < 1 s) would accumulate into the
        # deficit instead of firing each cycle — see test_chemostat.py
        # for that path.
        engine.create_experiment(
            name="chem1", mode="chemostat", vials=[0, 1],
            parameters=_chemostat_params(
                dilution_rate_per_hour=20.0,
                bolus_interval_seconds=10.0,
                volume_ml=25.0,
            ),
        )
        engine.start_experiment("chem1")
        # First run_cycle: each vial should fire a bolus immediately.
        temps, ods = _make_sensor_arrays(od=0.1)
        actions = engine.run_cycle("2026-05-14T10:00:00+00:00", temps, ods)
        vials_fired = sorted(v for v, _ in actions)
        assert vials_fired == [0, 1], f"expected both vials to fire, got {vials_fired}"
        for _v, action in actions:
            # Deficit accumulator fires integer seconds.
            assert action.pump_time == int(action.pump_time), action.pump_time
            assert 1 <= action.pump_time <= 20.0, action.pump_time
        # Advance clock by 5 s — under bolus_interval (10 s) — no fire expected.
        engine._clock.state["t"] += 5.0
        actions2 = engine.run_cycle("2026-05-14T10:00:05+00:00", temps, ods)
        assert actions2 == [], f"expected no fire within bolus_interval, got {actions2}"
        # Advance clock past bolus_interval — fire again.
        engine._clock.state["t"] += 10.0
        actions3 = engine.run_cycle("2026-05-14T10:00:15+00:00", temps, ods)
        assert sorted(v for v, _ in actions3) == [0, 1]
        engine.stop_experiment(reason="cleanup")
    print("PASS  chemostat run_cycle fires bolus, gates by interval, fires again")


def test_morbidostat_dilution_matches_turbidostat() -> None:
    """With slow/no growth, a morbidostat experiment dilutes the same way
    a turbidostat experiment with equivalent params would."""
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        engine.create_experiment(
            name="morb1", mode="morbidostat", vials=[0],
            parameters=_morbidostat_params(
                adaptation_threshold_per_hour=10.0,   # unreachable
                pump_wait_minutes=0.0,                # don't gate on pump_wait
            ),
        )
        engine.start_experiment("morb1")
        # OD high -> dilution should fire (target_od=0.4, od_lower=0.2,
        # average>upper triggers target flip and pump). Push a few high ODs
        # so the inner turbidostat's history_window=5 fills up.
        temps, ods = _make_sensor_arrays(od=0.5)
        actions = []
        for _ in range(5):
            actions = engine.run_cycle("2026-05-14T10:00:00+00:00", temps, ods)
            engine._clock.state["t"] += 1.0
        assert any(v == 0 for v, _ in actions), (
            f"expected morbidostat vial 0 to dilute, got {actions}"
        )
        # No escalation should have fired (threshold is unreachable).
        assert engine.escalation_pending_vials() == []
        engine.stop_experiment(reason="cleanup")
    print("PASS  morbidostat dilution matches turbidostat at low growth")


def test_morbidostat_emits_escalation_event_and_alert() -> None:
    with TmpRoot() as root:
        engine, _mgr, _dl, events, alerts = _fresh(root)
        engine.create_experiment(
            name="morb1", mode="morbidostat", vials=[0],
            parameters=_morbidostat_params(
                adaptation_threshold_per_hour=0.1,
                growth_min_samples=6,
                growth_window_seconds=1800.0,
            ),
        )
        engine.start_experiment("morb1")
        # Synthesize an exponential OD curve by manually pushing samples
        # into the controller's timestamped history before run_cycle.
        controller = engine._controllers[0]
        assert isinstance(controller, MorbidostatController)
        clock = engine._clock
        base_t = clock()
        mu_per_sec = 0.5 / 3600.0
        for i in range(20):
            t = base_t - 1800.0 + i * 100.0
            od = 0.1 * math.exp(mu_per_sec * (t - (base_t - 1800.0)))
            controller.push_od(now=t, od=od)
        temps, ods = _make_sensor_arrays(od=0.3)
        engine.run_cycle("2026-05-14T10:00:00+00:00", temps, ods)
        # Expect an experiment_event "escalation_proposed" and a warning alert.
        prop = [e for e in events if e.get("type") == "escalation_proposed"]
        assert len(prop) == 1, f"expected 1 escalation_proposed, got {events}"
        assert prop[0]["vial"] == 0
        assert prop[0]["new_drug_conc"] == 2.0
        morb_alerts = [a for a in alerts if "escalation" in a.get("message", "").lower()]
        assert len(morb_alerts) >= 1, f"expected escalation alert, got {alerts}"
        # And escalation_log.csv has the proposal row.
        log_path = root / "morb1" / "escalation_log.csv"
        assert log_path.is_file()
        rows = log_path.read_text().splitlines()
        assert len(rows) >= 2, rows  # header + at least one row
        assert engine.escalation_pending_vials() == [0]
        engine.stop_experiment(reason="cleanup")
    print("PASS  morbidostat emits escalation experiment_event + alert + CSV row")


def test_confirm_escalation_updates_state_and_config() -> None:
    with TmpRoot() as root:
        engine, _mgr, _dl, events, _alerts = _fresh(root)
        media = {
            "bottles": [
                {"id": "bottle_a", "name": "LB", "contents": "LB + drug 1x",
                 "initial_volume_ml": 1000.0, "low_volume_alert_ml": 100.0},
            ],
            "vial_to_bottle": {"0": "bottle_a"},
            "waste": {"name": "waste", "capacity_ml": 4000.0,
                      "high_fill_alert_ml": 3600.0},
        }
        engine.create_experiment(
            name="morb1", mode="morbidostat", vials=[0],
            parameters=_morbidostat_params(
                adaptation_threshold_per_hour=0.1,
            ),
            media=media,
        )
        engine.start_experiment("morb1")
        # Force escalation by feeding high-growth history.
        controller = engine._controllers[0]
        clock = engine._clock
        base_t = clock()
        mu_per_sec = 0.5 / 3600.0
        for i in range(20):
            t = base_t - 1800.0 + i * 100.0
            od = 0.1 * math.exp(mu_per_sec * (t - (base_t - 1800.0)))
            controller.push_od(now=t, od=od)
        temps, ods = _make_sensor_arrays(od=0.3)
        engine.run_cycle("2026-05-14T10:00:00+00:00", temps, ods)
        assert controller.awaiting_escalation_confirm
        # Confirm with override + new bottle contents.
        result = engine.confirm_escalation(
            name="morb1", vial=0,
            new_drug_conc=3.0,
            new_bottle_contents="LB + drug 3x",
        )
        assert result["drug_conc"] == 3.0
        assert result["bottle_contents"] == "LB + drug 3x"
        assert controller.drug_conc == 3.0
        assert not controller.awaiting_escalation_confirm
        # Config on disk has the updated bottle contents.
        saved = json.loads((root / "morb1" / "config.json").read_text())
        assert saved["media"]["bottles"][0]["contents"] == "LB + drug 3x"
        # An escalation_confirmed event was broadcast.
        confirm_events = [e for e in events if e.get("type") == "escalation_confirmed"]
        assert len(confirm_events) == 1
        # escalation_log.csv now has both the proposal and the confirmation row.
        rows = (root / "morb1" / "escalation_log.csv").read_text().splitlines()
        assert len(rows) >= 3, rows
        engine.stop_experiment(reason="cleanup")
    print("PASS  confirm_escalation updates drug_conc, bottle contents, and CSV")


def test_confirm_escalation_409_when_not_pending() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        engine.create_experiment(
            name="morb1", mode="morbidostat", vials=[0],
            parameters=_morbidostat_params(),
        )
        engine.start_experiment("morb1")
        try:
            engine.confirm_escalation(name="morb1", vial=0, new_drug_conc=2.0)
        except ConflictError:
            engine.stop_experiment(reason="cleanup")
            print("PASS  confirm_escalation raises ConflictError when not pending")
            return
        engine.stop_experiment(reason="cleanup")
        raise AssertionError("expected ConflictError when no escalation is pending")


def test_persistence_roundtrip_morbidostat() -> None:
    with TmpRoot() as root:
        engine_a, *_ = _fresh(root)
        engine_a.create_experiment(
            name="morb1", mode="morbidostat", vials=[0],
            parameters=_morbidostat_params(
                adaptation_threshold_per_hour=0.1,
            ),
        )
        engine_a.start_experiment("morb1")
        controller = engine_a._controllers[0]
        # Inject high-growth history and trigger an escalation.
        clock = engine_a._clock
        base_t = clock()
        mu_per_sec = 0.5 / 3600.0
        for i in range(20):
            t = base_t - 1800.0 + i * 100.0
            od = 0.1 * math.exp(mu_per_sec * (t - (base_t - 1800.0)))
            controller.push_od(now=t, od=od)
        temps, ods = _make_sensor_arrays(od=0.3)
        engine_a.run_cycle("2026-05-14T10:00:00+00:00", temps, ods)
        engine_a.confirm_escalation(name="morb1", vial=0, new_drug_conc=2.0)
        saved_drug_conc = controller.drug_conc
        saved_log_len = len(controller.escalation_log)

        # Drop engine_a; resume from disk.
        engine_b, *_ = _fresh(root)
        resumed = engine_b.resume_on_startup()
        assert resumed == "morb1"
        c_b = engine_b._controllers[0]
        assert isinstance(c_b, MorbidostatController)
        assert c_b.drug_conc == saved_drug_conc
        assert len(c_b.escalation_log) == saved_log_len
        assert c_b.escalation_log[-1]["confirmed_conc"] == 2.0
        engine_b.stop_experiment(reason="cleanup")
    print("PASS  morbidostat state persists and resumes (drug_conc, log preserved)")


def main() -> int:
    test_create_transitions_to_CREATED()
    test_start_transitions_to_RUNNING_and_applies_actuators()
    test_run_cycle_returns_pump_action_at_threshold()
    test_run_cycle_respects_pump_wait_gate()
    test_stop_zeros_experiment_vials_only()
    test_overtemp_fault_isolates_single_vial()
    test_sensor_failure_latches_after_threshold()
    test_emergency_stop_fully_stops_experiment()
    test_get_data_reads_csv_rows()
    test_list_experiments_includes_status()
    test_resume_on_startup_after_simulated_restart()
    test_create_while_loaded_running_raises()
    test_create_after_stop_unloads_previous()
    test_delete_experiment()
    test_media_config_round_trip()
    test_media_invalid_inputs_rejected()
    test_vials_and_media_mismatch_rejected()
    test_run_cycle_debits_correct_bottle_and_waste()
    test_low_volume_alert_is_edge_triggered()
    test_waste_alert_fires_on_threshold()
    test_media_state_persists_and_resumes()
    test_status_omits_media_when_absent()
    test_maintenance_queues_actions_and_resumes()
    test_maintenance_only_from_running()
    test_maintenance_auto_resume_on_timeout()
    test_maintenance_refill_resets_alert_latch()
    test_maintenance_persists_across_resume()
    test_unsupported_mode_rejected()
    test_create_experiment_accepts_chemostat()
    test_create_experiment_accepts_morbidostat()
    test_chemostat_run_cycle_returns_pump_action()
    test_morbidostat_dilution_matches_turbidostat()
    test_morbidostat_emits_escalation_event_and_alert()
    test_confirm_escalation_updates_state_and_config()
    test_confirm_escalation_409_when_not_pending()
    test_persistence_roundtrip_morbidostat()
    print("\nAll experiment_engine tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
