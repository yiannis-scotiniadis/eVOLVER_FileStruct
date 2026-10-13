"""Stir pause before OD reads (od_stir_pause.py, run_config ``od_stir_pause``).

A pausing vial lowers its stir, lets the vortex settle, and accepts only the
OD read that starts after the settle; every other read of it is discarded.
The machine keeps reading all sixteen vials every tick, so nothing blocks.

What these tests pin, in roughly the order a failure would hurt:

* the schedule itself (pure, a hand-driven monotonic clock);
* that it is a GROUP parameter -- run-level default, group override, the
  implicit group of an ungrouped run -- and that two groups or two parallel
  experiments that disagree share one tick without anything to merge;
* that the engine never pushes a gated read, holds every mode while the
  stirrer is paused, still runs heater safety, and that an operator's
  override suspends the pause;
* that a pausing vial's vialNN_OD.csv only gets the reads it accepted.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_config as rc  # noqa: E402
from data_logger import DataLogger  # noqa: E402
from experiment_engine import N_VIALS, validate_control_parameters  # noqa: E402
from od_stir_pause import (  # noqa: E402
    ACCEPTED,
    STIR_SETTLING,
    STIR_UNSETTLED,
    StirPauseScheduler,
)
from test_parallel_runs import CHEM, TURB, TmpRoot, _sup, _temps  # noqa: E402

PAUSE = {"enabled": True, "settle_seconds": 20, "period_seconds": 120,
         "pause_pwm": 0, "min_mix_seconds": 30}
NAN = float("nan")


def _pause(**over) -> rc.StirPause:
    return rc.normalise_stir_pause({**PAUSE, **over}, stir_rates=[8])


def _drive(s: StirPauseScheduler, vial: int, seconds: float, *, t0=0.0, tick=10.0):
    """Ticks of the real loop's order: the OD read starts the tick, the
    composed stir write ends it. Returns [(t, label, paused_pwm_or_None)]."""
    out = []
    t = t0
    while t < t0 + seconds:
        label = s.classify(t).get(vial)
        pwm = s.stir_overrides(t + 9.5).get(vial)
        out.append((t, label, pwm))
        t += tick
    return out


# ---------------------------------------------------------------------------
# The schedule
# ---------------------------------------------------------------------------

def test_pause_settle_accept_restore_cycle() -> None:
    s = StirPauseScheduler()
    s.sync({3: _pause()}, now=0.0)
    log = _drive(s, 3, 400)
    accepted = [t for t, label, _ in log if label == ACCEPTED]
    paused = [t for t, _, pwm in log if pwm is not None]
    # Stirs min_mix first, pauses at the first stir write past 30 s, and
    # accepts the first read that STARTS >= 20 s later; one per period.
    assert accepted == [60.0, 180.0, 300.0]
    assert paused[:3] == [30.0, 40.0, 50.0]
    # Paused ~30 s of every 120 s: 25 % -- the wizard's default.
    assert 60.0 not in paused and 70.0 not in paused
    assert rc.stir_pause_paused_fraction(_pause()) == pytest.approx(0.25)


def test_settle_is_a_minimum_measured_to_the_read_start() -> None:
    s = StirPauseScheduler()
    s.sync({0: _pause(min_mix_seconds=0)}, now=0.0)
    assert s.stir_overrides(100.0) == {0: 0}
    assert s.classify(119.9)[0] == STIR_SETTLING
    assert s.classify(120.0)[0] == ACCEPTED
    # Accepting ends the settle: the next composed write restores stir.
    assert s.stir_overrides(121.0) == {}


def test_stirring_reads_are_unsettled_and_non_pausing_vials_are_absent() -> None:
    s = StirPauseScheduler()
    s.sync({5: _pause()}, now=0.0)
    labels = s.classify(1.0)
    assert labels == {5: STIR_UNSETTLED}
    assert 4 not in labels, "a vial that does not pause accepts every read"


def test_a_dilution_delays_the_next_pause_by_min_mix() -> None:
    s = StirPauseScheduler()
    s.sync({1: _pause()}, now=0.0)
    s.note_dilution(1, ends_at=25.0)
    # Pause was due at 30 s, but the vial must stir 30 s after its pumps stop.
    assert s.stir_overrides(39.0) == {}
    assert s.stir_overrides(55.0) == {1: 0}


def test_a_dilution_mid_settle_abandons_the_settle() -> None:
    s = StirPauseScheduler()
    s.sync({1: _pause(min_mix_seconds=0)}, now=0.0)
    assert s.stir_overrides(1.0) == {1: 0}
    assert s.note_dilution(1, ends_at=5.0) is True
    assert s.stir_overrides(6.0) == {}, "pumping into an unstirred vial"
    assert s.classify(30.0)[1] == STIR_UNSETTLED


def test_a_dropped_settled_read_is_retried_once_with_stir_still_paused() -> None:
    s = StirPauseScheduler()
    s.sync({2: _pause(min_mix_seconds=0)}, now=0.0)
    s.stir_overrides(0.0)
    assert s.classify(20.0, valid={2: False})[2] == STIR_SETTLING
    assert s.stir_overrides(29.0) == {2: 0}
    # Second miss: give up rather than hold the stirrer indefinitely.
    assert s.classify(30.0, valid={2: False})[2] == ACCEPTED
    assert s.stir_overrides(31.0) == {}


def test_suspended_vial_never_pauses_and_stirs_first_on_return() -> None:
    s = StirPauseScheduler()
    s.sync({0: _pause(min_mix_seconds=0)}, now=0.0)
    assert s.stir_overrides(1.0) == {0: 0}
    s.sync({0: _pause(min_mix_seconds=0)}, suspended={0}, now=2.0)
    assert s.stir_overrides(3.0) == {}, "an operator holds the vial"
    assert s.classify(50.0)[0] == STIR_UNSETTLED
    s.sync({0: _pause(min_mix_seconds=10)}, suspended=(), now=60.0)
    assert s.stir_overrides(65.0) == {}
    assert s.stir_overrides(71.0) == {0: 0}


def test_sync_drops_vials_no_longer_reported() -> None:
    s = StirPauseScheduler()
    s.sync({0: _pause(min_mix_seconds=0), 1: _pause(min_mix_seconds=0)}, now=0.0)
    assert s.stir_overrides(1.0) == {0: 0, 1: 0}
    s.sync({1: _pause(min_mix_seconds=0)}, now=2.0)  # vial 0's run stopped
    assert s.stir_overrides(3.0) == {1: 0}
    assert s.vials() == {1}


# ---------------------------------------------------------------------------
# Config and validation
# ---------------------------------------------------------------------------

def test_defaults_disabled_and_missing_all_mean_never_pause() -> None:
    assert rc.normalise_stir_pause(None) is None
    assert rc.normalise_stir_pause({}) is None
    assert rc.normalise_stir_pause({"enabled": False, "settle_seconds": 2}) is None
    p = rc.normalise_stir_pause({"enabled": True}, stir_rates=[8])
    assert (p.settle_seconds, p.period_seconds, p.pause_pwm, p.min_mix_seconds) \
        == (20.0, 120.0, 0, 30.0)


@pytest.mark.parametrize("bad, needle", [
    ({"pause_pwm": 8}, "below the group's stir rate"),
    ({"pause_pwm": 3.5}, "integer"),
    ({"settle_seconds": 2}, "5-120"),
    ({"period_seconds": 50}, "too short"),
    ({"period_seconds": 55, "min_mix_seconds": 0}, "paused 55 %"),
    ({"typo": 1}, "unknown"),
    ({"enabled": "yes"}, "true or false"),
    ({"settle_seconds": "20"}, "must be a number"),
])
def test_unrunnable_configs_are_refused(bad, needle) -> None:
    with pytest.raises(ValueError) as exc:
        rc.normalise_stir_pause({**PAUSE, **bad}, where="group 'g'", stir_rates=[8])
    assert needle in str(exc.value) and "group 'g'" in str(exc.value)


def test_create_time_warnings_name_the_mode_consequence() -> None:
    rates = [1.0] * N_VIALS
    turb = {**TURB, "pump_wait_minutes": 10, rc.STIR_PAUSE_KEY: dict(PAUSE)}
    warnings = validate_control_parameters("turbidostat", turb, rates, [0])
    assert any("settled OD read" in w for w in warnings), warnings
    chem = {**CHEM, "bolus_interval_seconds": 45,
            rc.STIR_PAUSE_KEY: {**PAUSE, "period_seconds": 60}}
    warnings = validate_control_parameters("chemostat", chem, rates, [8])
    assert any("held until the stirrer restarts" in w for w in warnings), warnings
    assert any("paused ~50 %" in w for w in warnings), warnings
    with pytest.raises(ValueError):
        validate_control_parameters(
            "turbidostat", {**TURB, rc.STIR_PAUSE_KEY: {**PAUSE, "pause_pwm": 9}},
            rates, [0],
        )


def test_it_is_a_group_parameter_with_a_run_level_default() -> None:
    """Run-level value = default; a group overrides it; an ungrouped run is
    one implicit group and gets the run-level value. Allowed inside groups,
    unlike the run-wide keys (it is NOT in RUN_WIDE_KEYS)."""
    assert rc.STIR_PAUSE_KEY not in rc.RUN_WIDE_KEYS
    groups = rc.normalize_groups(
        mode=None, vials=None,
        parameters={"stir_rate": 8, rc.STIR_PAUSE_KEY: dict(PAUSE)},
        groups=[
            {"name": "settled", "vials": [0, 1], "mode": "turbidostat", "parameters": {}},
            {"name": "stirred", "vials": [2, 3], "mode": "turbidostat",
             "parameters": {rc.STIR_PAUSE_KEY: {"enabled": False}}},
            {"name": "slow", "vials": [4], "mode": "turbidostat",
             "parameters": {rc.STIR_PAUSE_KEY: {**PAUSE, "period_seconds": 300}}},
        ],
    )
    by_vial = rc.stir_pause_by_vial(groups)
    assert sorted(by_vial) == [0, 1, 4]
    assert by_vial[4].period_seconds == 300.0
    implicit = rc.normalize_groups(
        mode="turbidostat", vials=[7], groups=None,
        parameters={"stir_rate": 8, rc.STIR_PAUSE_KEY: dict(PAUSE)},
    )
    assert sorted(rc.stir_pause_by_vial(implicit)) == [7]


# ---------------------------------------------------------------------------
# Supervisor + engine: groups and parallel experiments on one tick
# ---------------------------------------------------------------------------

def _mono_sup(root):
    sup, manager, events, alerts, clock = _sup(root)
    mono = {"t": 0.0}
    sup._monotonic = lambda: mono["t"]
    sup._stir_pause._clock = sup._monotonic
    return sup, manager, alerts, clock, mono


def _tick(sup, mono, clock, od=0.3, *, flags=None, temp=37.0):
    """One sensor-loop tick in the app's order and timing: the OD read
    starts the tick and is classified; run_cycle -- whose composed stir write
    ends the tick -- runs ~9.5 s later, after the acquisition."""
    start = mono["t"]
    labels = sup.classify_od_read(start)
    ods = [od] * N_VIALS
    fl = list(flags) if flags else ["ok"] * N_VIALS
    for v, label in labels.items():
        if label != ACCEPTED:
            ods[v], fl[v] = NAN, label
    mono["t"] = start + 9.5
    actions = sup.run_cycle("2026-05-14T10:00:00+00:00", _temps(temp), ods, od_flags=fl)
    mono["t"] = start + 10.0
    clock["t"] += 10.0
    return labels, actions


def test_two_groups_one_pausing_share_one_stir_write() -> None:
    with TmpRoot() as root:
        sup, manager, _alerts, clock, mono = _mono_sup(root)
        sup.create_experiment(
            name="g", vials=None, parameters={**TURB, "pump_wait_minutes": 15},
            groups=[
                {"name": "settled", "vials": [0, 1], "mode": "turbidostat",
                 "parameters": {rc.STIR_PAUSE_KEY: dict(PAUSE)}},
                {"name": "stirred", "vials": [2, 3], "mode": "turbidostat",
                 "parameters": {}},
            ],
        )
        sup.start_experiment("g")
        seen_paused = []
        accepted_ticks = []
        for _ in range(14):
            labels, _ = _tick(sup, mono, clock)
            if labels.get(0) == ACCEPTED:
                accepted_ticks.append(mono["t"] - 10.0)
            stir = [int(x) for x in manager.stir_speed]
            seen_paused.append((stir[0], stir[2]))
            assert 2 not in labels and 3 not in labels
        assert (0, 8) in seen_paused, "settled group never paused"
        assert all(s2 == 8 for _, s2 in seen_paused), "stirred group paused"
        assert len(accepted_ticks) == 1
        sup.stop_experiment("g", reason="cleanup")


def test_parallel_experiments_one_pausing_one_not() -> None:
    with TmpRoot() as root:
        sup, manager, _alerts, clock, mono = _mono_sup(root)
        sup.create_experiment(
            name="alice", mode="turbidostat", vials=[0, 1],
            parameters={**TURB, "pump_wait_minutes": 15, rc.STIR_PAUSE_KEY: dict(PAUSE)},
        )
        sup.create_experiment(name="bob", mode="chemostat", vials=[8, 9],
                              parameters=dict(CHEM))
        sup.start_experiment("alice")
        sup.start_experiment("bob")
        paused_alice = False
        for _ in range(8):
            labels, _ = _tick(sup, mono, clock)
            assert set(labels) == {0, 1}, labels
            stir = [int(x) for x in manager.stir_speed]
            paused_alice |= stir[0] == 0
            assert stir[8] == 5, "bob's chemostat must never pause"
        assert paused_alice
        sup.stop_experiment("alice", reason="cleanup")
        sup.stop_experiment("bob", reason="cleanup")


def test_gated_reads_are_never_pushed_and_settling_holds_a_chemostat() -> None:
    with TmpRoot() as root:
        sup, _m, _a, clock, mono = _mono_sup(root)
        sup.create_experiment(
            name="c", mode="chemostat", vials=[4],
            parameters={**CHEM, rc.STIR_PAUSE_KEY: {**PAUSE, "min_mix_seconds": 0}},
        )
        sup.start_experiment("c")
        engine = sup.get_run("c")
        controller = engine._controllers[4]
        # Stirring between pauses: an open-loop chemostat still decides
        # (its first bolus fires on its first decide), but OD is not pushed.
        labels, actions = _tick(sup, mono, clock)
        assert labels[4] == STIR_UNSETTLED
        assert [v for v, _ in actions] == [4]
        assert controller.last_od is None and not engine._od_history.get(4)
        # The composed write at the end of that tick paused vial 4. While it
        # settles, every decision is held -- even past the bolus interval.
        held = []
        for _ in range(2):
            labels, actions = _tick(sup, mono, clock)
            held.append((labels[4], actions))
        assert all(lbl == STIR_SETTLING and acts == [] for lbl, acts in held), held
        labels, actions = _tick(sup, mono, clock)
        assert labels[4] == ACCEPTED
        assert controller.last_od == pytest.approx(0.3)
        sup.stop_experiment("c", reason="cleanup")


def test_heater_safety_still_runs_while_the_stirrer_is_paused() -> None:
    with TmpRoot() as root:
        sup, _m, _a, clock, mono = _mono_sup(root)
        sup.create_experiment(
            name="h", mode="turbidostat", vials=[0],
            parameters={**TURB, rc.STIR_PAUSE_KEY: {**PAUSE, "min_mix_seconds": 0}},
        )
        sup.start_experiment("h")
        engine = sup.get_run("h")
        _tick(sup, mono, clock)  # pauses vial 0 at the end of this tick
        labels_seen = []
        for _ in range(2):
            labels, _ = _tick(sup, mono, clock, temp=99.0)
            labels_seen.append(labels[0])
        assert labels_seen == [STIR_SETTLING, STIR_SETTLING]
        # Third consecutive over-critical read lands on the accepted tick;
        # all three were read while the stirrer was paused or just restoring.
        _tick(sup, mono, clock, temp=99.0)
        assert engine._vial_faults[0] == "overtemp"
        sup.stop_experiment("h", reason="cleanup")


def test_an_override_suspends_the_pause_for_its_vial_only() -> None:
    with TmpRoot() as root:
        sup, manager, _a, clock, mono = _mono_sup(root)
        sup.create_experiment(
            name="o", mode="turbidostat", vials=[0, 1],
            parameters={**TURB, rc.STIR_PAUSE_KEY: {**PAUSE, "min_mix_seconds": 0}},
        )
        sup.start_experiment("o")
        sup.grant_override(0, "Ana", "sample", 10)
        _tick(sup, mono, clock)
        stir = [int(x) for x in manager.stir_speed]
        assert stir[0] == 8 and stir[1] == 0
        assert sup.classify_od_read(mono["t"])[0] == STIR_UNSETTLED
        sup.stop_experiment("o", reason="cleanup")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def test_a_pausing_vial_logs_only_the_reads_it_accepted() -> None:
    with TmpRoot() as root:
        dl = DataLogger(root)
        dl.create_experiment(name="x", mode="turbidostat", vials=[0, 1])
        dl.activate_experiment("x")
        cols_before = None
        for i in range(6):
            dl.log_sensor_cycle(
                timestamp_iso=f"2026-05-14T10:00:{i:02d}+00:00",
                temperature_calibrated=[30.0] * N_VIALS,
                temperature_raw=[500.0] * N_VIALS,
                od_calibrated=[0.4] * N_VIALS,
                od_raw=[30000.0] * N_VIALS,
                od_skip_vials={0} if i else set(),
            )
        dl.deactivate_experiment()
        od0 = (root / "x" / "vial00_OD.csv").read_text().strip().splitlines()
        od1 = (root / "x" / "vial01_OD.csv").read_text().strip().splitlines()
        t0 = (root / "x" / "vial00_temp.csv").read_text().strip().splitlines()
        assert len(od0) == 1 + 1 and len(od1) == 1 + 6 and len(t0) == 1 + 6
        assert od0[0] == od1[0], "columns must not change"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def test_api_create_validates_the_group_block_and_payload_reports_it() -> None:
    from test_calibration_api import _make_app, _state  # noqa: E402

    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        c = flask_app.test_client()
        state = _state(flask_app)
        body = {
            "name": "sp", "parameters": dict(TURB),
            "groups": [
                {"name": "settled", "vials": [0, 1], "mode": "turbidostat",
                 "parameters": {rc.STIR_PAUSE_KEY: {**PAUSE, "pause_pwm": 12}}},
                {"name": "stirred", "vials": [2], "mode": "turbidostat",
                 "parameters": {}},
            ],
        }
        r = c.post("/api/experiments/create", json=body)
        assert r.status_code == 400
        assert "below the group's stir rate" in r.get_json()["error"]

        body["groups"][0]["parameters"][rc.STIR_PAUSE_KEY]["pause_pwm"] = 0
        r = c.post("/api/experiments/create", json=body)
        assert r.status_code == 200, r.get_json()
        cfg = state.engine.get_run("sp").stir_pause_config()
        assert sorted(cfg) == [0, 1] and cfg[0]["period_seconds"] == 120

        # The blank is warn-only for paused vials, and records them.
        assert c.post("/api/experiments/sp/precondition").status_code == 200
        r = c.post("/api/calibration/od/blank/start", json={})
        assert r.status_code == 200, r.get_json()
        assert any("pause their stirrer" in w for w in r.get_json()["warnings"])

        payload = state.sensor_update_payload("2026-05-14T10:00:00+00:00")
        assert "vial_timestamps" in payload["od"] and "stir_pause" in payload["od"]
        assert len(payload["od"]["vial_timestamps"]) == N_VIALS
