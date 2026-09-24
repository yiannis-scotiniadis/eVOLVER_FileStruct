"""Vial groups within one experiment (ROADMAP Session Y / run_config.py).

Two layers, tested separately:

* ``run_config`` -- pure normalisation and validation, no engine.
* ``ExperimentEngine`` with a real DataLogger and MockSerialManager, driven by
  direct ``run_cycle()`` calls, in the style of ``test_experiment_engine.py``.

The load-bearing property is that each vial behaves exactly as it would in a
single-mode experiment of its group's mode: a chemostat group keeps diluting
through unusable OD while the turbidostat group beside it stands down, each
group's heater and stir targets reach the wire, and a restart rebuilds the
same controllers.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import data_export  # noqa: E402
import run_config  # noqa: E402
from calibration_service import CalibrationService  # noqa: E402
from control_modes.chemostat import ChemostatController  # noqa: E402
from control_modes.morbidostat import MorbidostatController  # noqa: E402
from control_modes.turbidostat import TurbidostatController  # noqa: E402
from data_logger import DataLogger  # noqa: E402
from experiment_engine import (  # noqa: E402
    CONTROL_MODES,
    ExperimentEngine,
    ExperimentStatus,
    InvalidExperimentStateError,
    N_VIALS,
    supported_modes,
    validate_control_parameters,
)
from mock_serial_manager import MockSerialManager  # noqa: E402
from serial_manager import HEATER_OFF_SETPOINT  # noqa: E402

CAL_DIR = Path(__file__).resolve().parent.parent / "calibration"
TEMP_CAL = str(CAL_DIR / "temp_calibration.txt")
OD_CAL = str(CAL_DIR / "OD_cal.txt")

MODES = supported_modes()


class TmpRoot:
    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="evolver-groups-test-"))
        return self.path

    def __exit__(self, *args) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _fresh(root: Path):
    manager = MockSerialManager(seed=42)
    manager.load_calibration(TEMP_CAL, OD_CAL)
    events: list = []
    alerts: list = []
    clock_state = {"t": 1_000_000.0}

    def clock():
        return clock_state["t"]

    clock.state = clock_state  # type: ignore[attr-defined]
    engine = ExperimentEngine(
        serial_manager=manager,
        data_logger=DataLogger(root),
        experiments_root=root,
        on_event=events.append,
        on_alert=alerts.append,
        temp_cal=np.genfromtxt(TEMP_CAL, delimiter=","),
        clock=clock,
    )
    return engine, manager, events, alerts


RUN_PARAMS = {
    "volume_ml": 25,
    "efflux_extra_seconds": 2,
    "temperature_c": 37,
    "stir_rate": 8,
    "pump_flow_rates": [1.0] * 16,
}


def _mixed_groups() -> list[dict]:
    return [
        {"name": "ctrl", "vials": [0, 1, 2, 3], "mode": "turbidostat",
         "parameters": {"od_lower_thresh": 0.2, "od_upper_thresh": 0.4,
                        "pump_wait_minutes": 0.0,
                        "min_samples_before_action": 1}},
        {"name": "sel", "vials": [8, 9], "mode": "chemostat",
         "parameters": {"dilution_rate_per_hour": 30.0,
                        "bolus_interval_seconds": 60.0,
                        "temperature_c": 30.0, "stir_rate": 5}},
    ]


def _create_mixed(engine, name="mixed", **kw):
    return engine.create_experiment(
        name=name, parameters=dict(RUN_PARAMS), groups=_mixed_groups(), **kw,
    )


def _temps(value=37.0):
    return [float(value)] * N_VIALS


# ---------------------------------------------------------------------------
# run_config -- pure
# ---------------------------------------------------------------------------

def test_legacy_config_is_one_implicit_group() -> None:
    groups = run_config.normalize_groups(
        mode="turbidostat", vials=[3, 1, 2], parameters={"stir_rate": 9},
        groups=None, supported_modes=MODES,
    )
    assert len(groups) == 1
    g = groups[0]
    assert g.implicit and g.name == run_config.IMPLICIT_GROUP_NAME
    assert g.mode == "turbidostat" and g.vials == (1, 2, 3)
    assert g.parameters == {"stir_rate": 9}
    assert not run_config.is_grouped(groups)
    assert run_config.run_mode(groups) == "turbidostat"


def test_group_parameters_override_run_defaults() -> None:
    groups = run_config.normalize_groups(
        mode=None, vials=None, parameters=RUN_PARAMS,
        groups=_mixed_groups(), supported_modes=MODES,
    )
    by_name = {g.name: g for g in groups}
    assert by_name["sel"].parameters["temperature_c"] == 30.0   # override
    assert by_name["ctrl"].parameters["temperature_c"] == 37     # inherited
    assert by_name["sel"].parameters["volume_ml"] == 25          # run-wide
    # overrides are what round-trips to config.json, never the merged view
    assert "volume_ml" not in by_name["sel"].to_config()["parameters"]
    assert run_config.run_mode(groups) == run_config.MIXED_MODE
    assert run_config.stir_by_vial(groups) == {0: 8, 1: 8, 2: 8, 3: 8, 8: 5, 9: 5}
    temps = run_config.temperature_c_by_vial(groups)
    assert temps[0] == 37.0 and temps[9] == 30.0


def test_alias_override_drops_the_run_default_spelling() -> None:
    """A group writing `od_lower` must win over a run-level `od_lower_thresh`
    -- the builder prefers `od_lower_thresh`, so leaving the run default in
    place would silently ignore the group's value."""
    merged = run_config.merge_parameters(
        {"od_lower_thresh": 0.2, "temperature": 37},
        {"od_lower": 0.1, "temperature_c": 30},
    )
    assert "od_lower_thresh" not in merged and merged["od_lower"] == 0.1
    assert "temperature" not in merged and merged["temperature_c"] == 30


def test_group_validation_errors() -> None:
    def bad(groups, vials=None, match=""):
        try:
            run_config.normalize_groups(
                mode=None, vials=vials, parameters={}, groups=groups,
                supported_modes=MODES,
            )
        except ValueError as exc:
            assert match in str(exc), str(exc)
            return
        raise AssertionError(f"expected ValueError containing {match!r}")

    ok = {"name": "a", "vials": [0], "mode": "turbidostat"}
    bad([ok, {"name": "b", "vials": [0], "mode": "chemostat"}],
        match="in both group")
    bad([ok, {"name": "a", "vials": [1], "mode": "chemostat"}],
        match="duplicate group name")
    bad([{"name": "Bad Name", "vials": [0], "mode": "turbidostat"}], match="name")
    bad([{"name": "a", "vials": [], "mode": "turbidostat"}], match="non-empty")
    bad([{"name": "a", "vials": [16], "mode": "turbidostat"}], match="invalid vial")
    bad([{"name": "a", "vials": [0], "mode": "nope"}], match="unsupported mode")
    bad([{"name": "a", "vials": [0], "mode": "turbidostat",
          "parameters": {"volume_ml": 20}}], match="run-wide")
    bad([ok], vials=[0, 1], match="union")


def test_reader_mode_resolves_targets_without_a_mode() -> None:
    """The calibration layer reads configs that may carry no `mode` at all;
    it only needs heater and stir targets."""
    config = {"vials": [0, 1], "parameters": {"stir_rate": 6, "temperature_c": 33}}
    assert run_config.config_stir_by_vial(config) == {0: 6, 1: 6}
    assert run_config.config_temperature_c_by_vial(config) == {0: 33.0, 1: 33.0}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def test_every_mode_is_registered_once() -> None:
    assert set(CONTROL_MODES) == {"turbidostat", "chemostat", "morbidostat"}
    try:
        validate_control_parameters("no_such_mode", {}, [1.0] * 32, [0])
    except ValueError as exc:
        assert "unsupported mode" in str(exc)
    else:
        raise AssertionError("an unknown mode must not silently validate")


def test_create_writes_groups_and_mixed_mode() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        config = _create_mixed(engine)
        assert config["mode"] == "mixed"
        assert config["vials"] == [0, 1, 2, 3, 8, 9]
        saved = json.loads((root / "mixed" / "config.json").read_text())
        assert [g["name"] for g in saved["groups"]] == ["ctrl", "sel"]
        assert saved["groups"][1]["parameters"]["temperature_c"] == 30.0
        status = engine.status()
        assert status["grouped"] is True
        assert status["per_vial"]["8"]["group"] == "sel"
        assert status["per_vial"]["8"]["mode"] == "chemostat"
        # CREATED: nothing is stirring yet, so the commanded stir is a
        # uniform 0. Once the groups' targets apply, they differ -> None.
        assert status["setpoint_stir"] == 0
        engine.start_experiment("mixed")
        assert engine.status()["setpoint_stir"] is None
        engine.stop_experiment(reason="cleanup")


def test_legacy_create_leaves_config_unchanged() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        engine.create_experiment(
            name="plain", mode="turbidostat", vials=[0, 1],
            parameters={"temperature_c": 37, "stir_rate": 10,
                        "pump_flow_rates": [1.0] * 16},
        )
        saved = json.loads((root / "plain" / "config.json").read_text())
        assert "groups" not in saved and saved["mode"] == "turbidostat"
        status = engine.status()
        assert status["grouped"] is False
        assert status["groups"][0]["name"] == run_config.IMPLICIT_GROUP_NAME


def test_mode_conflicting_with_groups_is_rejected() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        try:
            engine.create_experiment(
                name="x", mode="turbidostat", parameters=dict(RUN_PARAMS),
                groups=_mixed_groups(),
            )
        except ValueError as exc:
            assert "conflicts with the groups" in str(exc)
        else:
            raise AssertionError("mode=turbidostat with a mixed group list must fail")


def test_per_group_validation_names_the_group() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        groups = _mixed_groups()
        groups[1]["parameters"]["bolus_interval_seconds"] = 1.0
        try:
            engine.create_experiment(
                name="x", parameters=dict(RUN_PARAMS), groups=groups,
            )
        except ValueError as exc:
            assert "bolus_interval_seconds" in str(exc)
        else:
            raise AssertionError("a bad chemostat group must fail create")


def test_run_wide_warning_reported_once_unprefixed() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        params = dict(RUN_PARAMS, efflux_extra_seconds=0)
        config = engine.create_experiment(
            name="x", parameters=params, groups=_mixed_groups(),
        )
        overrun = [w for w in config["warnings"] if "efflux_extra_seconds is 0" in w]
        assert len(overrun) == 1, config["warnings"]
        assert not overrun[0].startswith("group ")


def test_morbidostat_bottle_may_not_feed_another_group() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        groups = [
            {"name": "drug", "vials": [0], "mode": "morbidostat",
             "parameters": {"target_od": 0.4, "od_lower": 0.2}},
            {"name": "ctrl", "vials": [1], "mode": "turbidostat",
             "parameters": {"od_lower_thresh": 0.2, "od_upper_thresh": 0.4}},
        ]
        media = {
            "bottles": [{"id": "shared", "name": "Drug", "initial_volume_ml": 500}],
            "vial_to_bottle": {"0": "shared", "1": "shared"},
            "waste": {"capacity_ml": 4000},
        }
        try:
            engine.create_experiment(
                name="x", parameters=dict(RUN_PARAMS), groups=groups, media=media,
            )
        except ValueError as exc:
            assert "morbidostat" in str(exc)
        else:
            raise AssertionError("a shared morbidostat drug bottle must be refused")


def test_start_builds_each_vial_from_its_group() -> None:
    with TmpRoot() as root:
        engine, manager, *_ = _fresh(root)
        _create_mixed(engine)
        engine.start_experiment("mixed")
        for v in (0, 1, 2, 3):
            assert isinstance(engine._controllers[v], TurbidostatController)
        for v in (8, 9):
            assert isinstance(engine._controllers[v], ChemostatController)
        # Heater + stir targets per group reached the wire.
        stir = [int(x) for x in manager.stir_speed]
        assert stir[0] == 8 and stir[8] == 5 and stir[4] == 0
        raw = [int(x) for x in manager.temp_setpoint_raw]
        assert raw[0] == engine._C_to_raw(37.0, 0)
        assert raw[8] == engine._C_to_raw(30.0, 8)
        assert raw[4] == HEATER_OFF_SETPOINT
        # Stir is re-sent every tick at each group's own rate.
        manager.stir_speed = np.zeros(N_VIALS, dtype=int)
        engine.run_cycle("2026-05-14T10:00:00+00:00", _temps(), [0.1] * N_VIALS)
        stir = [int(x) for x in manager.stir_speed]
        assert stir[0] == 8 and stir[9] == 5
        engine.stop_experiment(reason="cleanup")


def test_chemostat_group_dilutes_while_turbidostat_group_stands_down() -> None:
    """CONTROL_MODE_AUDIT.md C-2, per group: `requires_od` is read per
    controller, so an unusable OD suspends only the modes that close the
    loop on it."""
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        clock = engine._clock.state
        _create_mixed(engine)
        engine.start_experiment("mixed")
        nan = float("nan")
        fired: set[int] = set()
        for tick in range(4):
            actions = engine.run_cycle(
                f"2026-05-14T10:0{tick}:00+00:00", _temps(), [nan] * N_VIALS,
                od_flags=["out_of_range"] * N_VIALS,
            )
            fired |= {v for v, _a in actions}
            clock["t"] += 60.0
        assert fired == {8, 9}, fired
        engine.stop_experiment(reason="cleanup")


def test_turbidostat_group_dilutes_on_high_od() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        _create_mixed(engine)
        engine.start_experiment("mixed")
        actions = engine.run_cycle("2026-05-14T10:00:00+00:00", _temps(), [0.5] * N_VIALS)
        turb = {v for v, _a in actions if v < 8}
        assert turb == {0, 1, 2, 3}, actions
        engine.stop_experiment(reason="cleanup")


def test_growth_regime_follows_each_vials_group() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        clock = engine._clock.state
        _create_mixed(engine)
        engine.start_experiment("mixed")
        for tick in range(12):
            engine.run_cycle(f"2026-05-14T10:00:{tick:02d}+00:00", _temps(), [0.1] * N_VIALS)
            clock["t"] += 10.0
        reports = engine._growth_reports
        assert reports[8].regime == "chemostat"
        assert reports[0].regime != "chemostat"
        engine.stop_experiment(reason="cleanup")


def test_resume_rebuilds_groups_and_per_vial_stir() -> None:
    with TmpRoot() as root:
        engine_a, *_ = _fresh(root)
        _create_mixed(engine_a)
        engine_a.start_experiment("mixed")
        engine_a.run_cycle("2026-05-14T10:00:00+00:00", _temps(), [0.1] * N_VIALS)
        # no stop: state.json still says RUNNING

        engine_b, manager_b, *_ = _fresh(root)
        assert engine_b.resume_on_startup() == "mixed"
        assert isinstance(engine_b._controllers[8], ChemostatController)
        assert isinstance(engine_b._controllers[0], TurbidostatController)
        stir = [int(x) for x in manager_b.stir_speed]
        assert stir[0] == 8 and stir[8] == 5
        assert engine_b.status()["per_vial"]["9"]["group"] == "sel"
        engine_b.stop_experiment(reason="cleanup")


def test_legacy_state_without_groups_resumes_as_implicit_group() -> None:
    with TmpRoot() as root:
        engine_a, *_ = _fresh(root)
        engine_a.create_experiment(
            name="old", mode="turbidostat", vials=[0, 1],
            parameters={"temperature_c": 37, "stir_rate": 7,
                        "pump_flow_rates": [1.0] * 16},
        )
        engine_a.start_experiment("old")
        state_path = root / "old" / "state.json"
        state = json.loads(state_path.read_text())
        # Rewrite as a pre-groups state file: no groups, one scalar stir.
        state.pop("groups", None)
        state.pop("stir_by_vial", None)
        state["setpoint_stir"] = 7
        state_path.write_text(json.dumps(state))

        engine_b, manager_b, *_ = _fresh(root)
        assert engine_b.resume_on_startup() == "old"
        assert [int(x) for x in manager_b.stir_speed][:2] == [7, 7]
        assert engine_b.status()["setpoint_stir"] == 7
        engine_b.stop_experiment(reason="cleanup")


def test_escalation_confined_to_the_morbidostat_group() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        groups = [
            {"name": "drug", "vials": [0], "mode": "morbidostat",
             "parameters": {"target_od": 0.4, "od_lower": 0.2,
                            "min_samples_before_action": 1}},
            {"name": "ctrl", "vials": [1], "mode": "turbidostat",
             "parameters": {"od_lower_thresh": 0.2, "od_upper_thresh": 0.4}},
        ]
        engine.create_experiment(name="m", parameters=dict(RUN_PARAMS), groups=groups)
        engine.start_experiment("m")
        assert isinstance(engine._controllers[0], MorbidostatController)
        status = engine.status()
        assert set(status["morbidostat"]["per_vial"]) == {"0"}
        try:
            engine.confirm_escalation("m", 1, new_drug_conc=2.0)
        except ValueError as exc:
            assert "not a morbidostat group" in str(exc)
        else:
            raise AssertionError("escalating a turbidostat vial must fail")
        engine.stop_experiment(reason="cleanup")


def test_precondition_holds_targets_and_stop_parks_them() -> None:
    with TmpRoot() as root:
        engine, manager, events, _ = _fresh(root)
        _create_mixed(engine)
        assert not engine.controls_actuators
        result = engine.precondition_experiment("mixed")
        assert result["stir"]["8"] == 5 and result["temperature_c"]["8"] == 30.0
        assert engine.controls_actuators
        assert engine.status_string == ExperimentStatus.CREATED
        assert engine.status()["preconditioned"] is True
        assert any(e.get("type") == "preconditioned" for e in events)
        raw = [int(x) for x in manager.temp_setpoint_raw]
        assert raw[8] == engine._C_to_raw(30.0, 8)
        # CREATED + preconditioned: stir re-sent, no pumps, no CSV rows.
        manager.stir_speed = np.zeros(N_VIALS, dtype=int)
        actions = engine.run_cycle("2026-05-14T10:00:00+00:00", _temps(), [0.9] * N_VIALS)
        assert actions == []
        assert int(manager.stir_speed[8]) == 5
        assert (root / "mixed" / "vial00_OD.csv").read_text().count("\n") == 1

        engine.stop_experiment(reason="manual")
        raw = [int(x) for x in manager.temp_setpoint_raw]
        stir = [int(x) for x in manager.stir_speed]
        assert raw[0] == HEATER_OFF_SETPOINT and raw[8] == HEATER_OFF_SETPOINT
        assert stir[0] == 0 and stir[8] == 0
        assert not engine.controls_actuators


def test_precondition_runs_heater_safety_and_start_keeps_the_park() -> None:
    """A preconditioned CREATED run drives heaters, so the Pi-side overtemp
    watch must run for it -- and a vial it latches must not be re-heated by
    start_experiment."""
    with TmpRoot() as root:
        engine, manager, *_ = _fresh(root)
        _create_mixed(engine)
        engine.precondition_experiment("mixed")
        hot = _temps(37.0)
        hot[2] = 60.0
        for _ in range(3):
            engine.run_cycle("2026-05-14T10:00:00+00:00", hot, None)
        assert engine._vial_faults[2] == "overtemp"
        assert int(manager.temp_setpoint_raw[2]) == HEATER_OFF_SETPOINT
        engine.start_experiment("mixed")
        assert int(manager.temp_setpoint_raw[2]) == HEATER_OFF_SETPOINT
        assert int(manager.stir_speed[2]) == 0
        assert int(manager.temp_setpoint_raw[0]) < HEATER_OFF_SETPOINT
        engine.stop_experiment(reason="cleanup")


def test_precondition_only_from_created() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        _create_mixed(engine)
        engine.start_experiment("mixed")
        try:
            engine.precondition_experiment("mixed")
        except InvalidExperimentStateError:
            pass
        else:
            raise AssertionError("precondition while RUNNING must fail")
        engine.stop_experiment(reason="cleanup")


def test_blank_expects_each_vial_at_its_group_stir() -> None:
    with TmpRoot() as root:
        engine, manager, *_ = _fresh(root)
        _create_mixed(engine)
        config = json.loads((root / "mixed" / "config.json").read_text())
        svc = CalibrationService(root / "cal", root, manager)
        # A single claimed PWM cannot match groups that stir differently.
        try:
            svc.blank_start(
                experiment="mixed", config=config, engine_status="created",
                led_power=2125, stir_pwm=8, expected_led_power=2125,
            )
        except ValueError as exc:
            assert "different rates" in str(exc)
        else:
            raise AssertionError("a scalar stir claim must fail for mixed stir")
        # Without a claim: per-vial expectation, and the stir actually being
        # sent (nothing yet) is reported as a mismatch warning.
        started = svc.blank_start(
            experiment="mixed", config=config, engine_status="created",
            led_power=2125, stir_pwm=None, expected_led_power=2125,
        )
        assert started["stir_mismatch"]["8"] == {"expected": 5, "actual": 0}
        engine.precondition_experiment("mixed")
        started = svc.blank_start(
            experiment="mixed", config=config, engine_status="created",
            led_power=2125, stir_pwm=None, expected_led_power=2125,
        )
        assert started["stir_mismatch"] == {}
        engine.stop_experiment(reason="cleanup")


def test_export_bundle_carries_vial_groups_csv() -> None:
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        _create_mixed(engine)
        fn, blob = data_export.build_bundle(
            root / "mixed", name="mixed", vials=[0, 8], parameters=["od", "temp"],
        )
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            text = zf.read("vial_groups.csv").decode()
            manifest = json.loads(zf.read("export_manifest.json"))
        assert text.splitlines() == ["vial,group,mode", "0,ctrl,turbidostat", "8,sel,chemostat"]
        assert manifest["files"]["vial_groups.csv"]["data_rows"] == 2

        engine.stop_experiment(reason="cleanup")
        engine.create_experiment(
            name="plain", mode="turbidostat", vials=[0],
            parameters={"pump_flow_rates": [1.0] * 16},
        )
        _fn, blob = data_export.build_bundle(
            root / "plain", name="plain", vials=[0], parameters=["od", "temp"],
        )
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            assert "vial_groups.csv" not in zf.namelist()


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

def _api_body(name: str) -> dict:
    return {"name": name, "parameters": dict(RUN_PARAMS), "groups": _mixed_groups()}


def test_api_create_precondition_and_manual_lock() -> None:
    from test_calibration_api import _make_app, _state  # noqa: E402

    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        c = flask_app.test_client()
        state = _state(flask_app)

        bad = _api_body("bad")
        bad["groups"][1]["parameters"]["volume_ml"] = 20
        r = c.post("/api/experiments/create", json=bad)
        assert r.status_code == 400 and "run-wide" in r.get_json()["error"]

        r = c.post("/api/experiments/create", json=_api_body("g1"))
        assert r.status_code == 200, r.get_json()
        status = c.get("/api/experiments/g1/status").get_json()
        assert status["mode"] == "mixed"
        assert [g["name"] for g in status["groups"]] == ["ctrl", "sel"]

        # CREATED, not preconditioned: the run's vials are still manual.
        stir = [int(x) for x in state.manager.stir_speed]
        stir[8] = 3
        assert c.post("/api/actuators/stir", json={"values": stir}).status_code == 200

        r = c.post("/api/experiments/g1/precondition")
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["stir"]["8"] == 5

        # The dashboard's per-tick summary carries the groups and the
        # preconditioned flag (it colours cards and locks from these).
        summary = state.sensor_update_payload("2026-05-14T10:00:00+00:00")["experiment"]
        assert summary["grouped"] is True and summary["preconditioned"] is True
        assert summary["groups"] == [
            {"name": "ctrl", "mode": "turbidostat", "vials": [0, 1, 2, 3]},
            {"name": "sel", "mode": "chemostat", "vials": [8, 9]},
        ]

        # Preconditioned: the engine owns vial 8; a free vial stays manual.
        stir = [int(x) for x in state.manager.stir_speed]
        stir[8] = 3
        r = c.post("/api/actuators/stir", json={"values": stir})
        assert r.status_code == 409 and "g1" in r.get_json()["error"]
        stir = [int(x) for x in state.manager.stir_speed]
        stir[5] = 4
        assert c.post("/api/actuators/stir", json={"values": stir}).status_code == 200
        r = c.post("/api/actuators/pump", json={"vial": 9, "direction": "efflux", "seconds": 1})
        assert r.status_code == 409

        # The blank takes each vial at its own group's stir; no scalar claim.
        r = c.post("/api/calibration/od/blank/start", json={})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["stir_mismatch"] == {}

        r = c.post("/api/experiments/g1/stop")
        assert r.status_code == 200
        assert int(state.manager.stir_speed[8]) == 0
