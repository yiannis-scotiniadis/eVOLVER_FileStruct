"""Heater safety without the overrun step-down (SPEC §10).

The engine used to lower a vial's target by 2 °C on any single read more than
5 °C above it, against the already-lowered target, down to a 22 °C floor, with
no way back. In 20261103_longrunattempt2 thermistor glitches drove vials 4, 7
and 11 through that to room temperature for up to 58 h. The Arduino closes the
heater loop on the same thermistor, so whenever the reading is above target
its heater is already off: lowering the target removed no heat, and only ever
acted when the reading was wrong.

What these tests pin down:

  * no reading, however wild, changes a heater target -- except three
    consecutive reads above the critical limit, which park it off (unchanged);
  * the real episodes from that run raise exactly one glitch warning each and
    no overrun alert;
  * a sustained overrun raises one critical alert per episode, and a vial
    cooling toward a lowered target, or starting hot, raises none;
  * a resumed run whose state.json still carries a stepped-down setpoint goes
    back to its °C target, and says so;
  * the dashboard's VialHealth marks a suspect thermistor on a rise only.

Run from the project root:
    python -m pytest server/test_heater_safety.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from experiment_engine import (  # noqa: E402
    DEFAULT_HEATER_CRITICAL_C,
    DEFAULT_HEATER_OVERRUN_C,
    HEATER_OVERRUN_ALERT_READS,
    N_VIALS,
    ExperimentStatus,
)
import event_log as evlog  # noqa: E402
from serial_manager import HEATER_OFF_SETPOINT  # noqa: E402

from test_experiment_engine import TmpRoot, _fresh  # noqa: E402

TICK_S = 10.0
NAME = "heat"

# ---------------------------------------------------------------------------
# Real thermistor traces from 20261103_longrunattempt2 (vialNN_temp.csv,
# consecutive 10 s reads, target 30 °C). Recorded while the old step-down was
# reacting to them, so the tails include real cooling; the onsets are the
# glitches themselves.
# ---------------------------------------------------------------------------

# Vial 11, 11/05 02:52 EST: a one-read jump of +6.5 °C that decays over minutes.
# The old code took this vial from 30 to 22 °C in 30 s.
V11_STEP_AND_DECAY = [
    30.05, 30.05, 30.05, 30.05, 30.05, 30.05, 29.94, 30.05, 36.52, 35.84, 35.05,
    34.71, 34.48, 34.02, 33.68, 33.68, 33.23, 33.12, 33.00, 32.89, 32.89, 32.89,
    32.78, 32.78, 32.78, 32.66, 32.55, 32.32, 31.98, 31.87, 31.64, 31.64, 31.76,
    31.53, 31.07, 30.96,
]
# Vial 4, 11/04 14:20 EST: a +4.2 °C step to a ~35 °C plateau (the start of an
# offset that lasted ~10 h).
V4_PLATEAU = [
    30.05, 30.05, 30.05, 30.05, 30.05, 30.05, 30.05, 30.27, 34.45, 34.45, 34.34,
    34.67, 34.67, 34.89, 34.56, 34.56, 34.78, 35.11, 35.22, 35.33, 35.33, 35.22,
    35.00, 35.00, 35.00, 35.00, 34.89, 34.78, 34.78, 34.67, 34.67, 34.67, 34.67,
    34.67, 34.56,
]
# Vial 7, 11/04 03:48 EST: 25 min of chaos, -32 to +52 °C, including two reads
# over the 50 °C critical limit that are NOT consecutive.
V7_CHAOS = [
    30.05, 30.05, 30.05, 30.05, 30.16, 29.71, 29.83, 28.58, 23.06, -32.42, 46.51,
    -17.42, 45.50, 42.34, 46.06, 47.30, 52.38, -31.85, 50.91, -6.48, 21.82, 4.45,
    28.36, 34.56, 26.22, 34.11, 15.39, 5.24, -0.39, 38.96, 38.06, 39.86, 45.39,
    43.36, 29.15, 27.34, 25.77, 32.08, -22.95, 39.75, 40.88, 30.95, 25.88, 30.05,
    33.66, 31.52, 32.19, 26.10, 9.19, 2.54, 44.15, 44.93, 35.69, 28.36, 28.02,
    33.55, 30.50, 35.35, 31.85, 30.28, 32.98, 28.58, 27.68, 31.63, 30.05, 28.81,
    33.55, 28.81, 28.02, -3.21, 33.77, 33.21, 30.16, 29.49, 28.92, 30.50, 29.94,
    29.26, 29.49, 28.47, 25.65, 26.78, 28.47, 36.48, 29.04, 28.92, 27.91, 25.43,
    33.21, 31.85, 27.68, 25.88, 21.37, 13.59, 28.70, 27.80, 27.34, 21.93, 30.39,
    38.28, 30.16, 34.79, 29.04, 29.49, 33.21, 28.47, 29.04, 28.70, 28.13, 24.41,
    26.78, 29.60, 31.74, 28.36, 28.36, 28.13, 28.25, 27.23, 27.23, 29.49, 27.91,
    28.47, 28.13, 27.68, 27.68, 31.07, 26.67, 28.70, 28.13, 28.02, 27.34, 27.91,
    27.57, 25.65, 27.57, 27.34, 29.37, 28.25, 27.80, 27.80, 27.57, 27.23, 27.23,
    26.78, 26.89, 27.12, 29.49, 27.80, 27.57, 27.80, 27.23, 27.46, 27.23, 26.33,
    28.13, 26.78, 28.58, 27.80, 28.13, 27.34, 27.34, 28.47, 27.23, 26.44, 29.49,
    26.67, 27.12, 27.12, 27.80, 27.46, 27.12, 27.01,
]
# Vial 11, 11/07 20:37 EST: the longest overrun of the run -- 12 reads above
# 35 °C, under the 18 the alert needs.
V11_LONGEST_OVERRUN = [
    30.05, 30.05, 30.05, 30.05, 30.05, 30.05, 37.43, 38.90, 38.79, 38.00, 37.09,
    36.75, 36.18, 35.73, 35.50, 35.50, 35.27, 35.05, 34.93, 34.82, 34.25, 33.68,
    33.57, 33.23, 32.89, 32.66, 32.55, 32.44, 32.32, 31.76, 31.53, 31.42, 30.96,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _params(temperature_c: float = 30.0) -> dict:
    return {
        "temperature_c": temperature_c, "stir_rate": 5,
        "od_lower_thresh": 0.2, "od_upper_thresh": 0.4,
        "min_samples_before_action": 1, "pump_wait_minutes": 1,
    }


def _start(root, vials=(0, 1, 2), temperature_c: float = 30.0):
    engine, manager, _dl, events, alerts = _fresh(root)
    engine.create_experiment(
        name=NAME, mode="turbidostat", vials=list(vials),
        parameters=_params(temperature_c),
    )
    engine.start_experiment(NAME)
    return engine, manager, events, alerts


def _feed(engine, vial: int, readings, others: float = 30.0) -> None:
    """One temperature-only tick per reading for ``vial``; every other vial
    reads ``others``. NaN is a dropped read."""
    for r in readings:
        temps = [others] * N_VIALS
        temps[vial] = r
        engine.run_cycle("2026-11-05T07:53:00+00:00", temps)
        engine._clock.state["t"] += TICK_S


def _keyed(alerts, key: str, vial: int | None = None) -> list[dict]:
    """Alerts whose (namespaced) dedup key is ``(key, ...)``."""
    out = []
    for a in alerts:
        dk = a.get("dedup_key")
        inner = dk[1] if isinstance(dk, tuple) and len(dk) == 2 and isinstance(dk[1], tuple) else dk
        if isinstance(inner, tuple) and inner and inner[0] == key:
            if vial is None or a.get("vial") == vial:
                out.append(a)
    return out


def _raw(engine, celsius: float, vial: int) -> int:
    return engine._C_to_raw(celsius, vial)


# ---------------------------------------------------------------------------
# No step-down: the real episodes
# ---------------------------------------------------------------------------

def _replay_episode(trace, vial: int = 1) -> tuple[list[dict], int, int, object]:
    with TmpRoot() as root:
        engine, manager, _events, alerts = _start(root)
        before = int(manager.temp_setpoint_raw[vial])
        _feed(engine, vial, trace)
        after = int(manager.temp_setpoint_raw[vial])
        fault = engine._vial_faults.get(vial)
        engine.stop_experiment(reason="test_cleanup")
    return alerts, before, after, fault


def test_v11_step_and_decay_moves_no_setpoint() -> None:
    alerts, before, after, fault = _replay_episode(V11_STEP_AND_DECAY)
    assert after == before, f"setpoint moved {before} -> {after}"
    assert fault is None
    assert len(_keyed(alerts, "thermistor_glitch", 1)) == 1, alerts
    assert _keyed(alerts, "heater_overrun") == [], alerts


def test_v4_plateau_moves_no_setpoint() -> None:
    alerts, before, after, fault = _replay_episode(V4_PLATEAU)
    assert after == before
    assert fault is None
    assert len(_keyed(alerts, "thermistor_glitch", 1)) == 1, alerts
    assert _keyed(alerts, "heater_overrun") == [], alerts


def test_v7_chaos_moves_no_setpoint_and_latches_nothing() -> None:
    """Two reads over 50 °C, never three in a row: no park-off, and one
    warning for the whole 25-minute episode."""
    assert sum(1 for x in V7_CHAOS if x > DEFAULT_HEATER_CRITICAL_C) == 2
    alerts, before, after, fault = _replay_episode(V7_CHAOS)
    assert after == before
    assert fault is None, fault
    assert len(_keyed(alerts, "thermistor_glitch", 1)) == 1, alerts
    assert _keyed(alerts, "heater_overrun") == [], alerts
    assert not [a for a in alerts if a["level"] == "critical"], alerts


def test_longest_real_overrun_stays_under_the_alert() -> None:
    above = 0
    longest = 0
    for x in V11_LONGEST_OVERRUN:
        above = above + 1 if x > 30.0 + DEFAULT_HEATER_OVERRUN_C else 0
        longest = max(longest, above)
    assert longest < HEATER_OVERRUN_ALERT_READS
    alerts, before, after, _fault = _replay_episode(V11_LONGEST_OVERRUN)
    assert after == before
    assert _keyed(alerts, "heater_overrun") == [], alerts


def test_a_second_episode_after_the_rearm_alerts_again() -> None:
    with TmpRoot() as root:
        engine, _m, _e, alerts = _start(root)
        _feed(engine, 1, V11_STEP_AND_DECAY)
        _feed(engine, 1, [30.05] * 200)          # > 30 min quiet
        _feed(engine, 1, V11_STEP_AND_DECAY)
        assert len(_keyed(alerts, "thermistor_glitch", 1)) == 2, alerts
        engine.stop_experiment(reason="test_cleanup")


# ---------------------------------------------------------------------------
# What is and is not a glitch
# ---------------------------------------------------------------------------

def test_heater_warmup_is_not_a_glitch() -> None:
    """0.6 °C per read is faster than anything seen on the rig (0.57)."""
    with TmpRoot() as root:
        engine, _m, _e, alerts = _start(root)
        _feed(engine, 1, [22.0 + 0.6 * i for i in range(14)] + [30.0] * 5)
        assert _keyed(alerts, "thermistor_glitch") == [], alerts
        engine.stop_experiment(reason="test_cleanup")


def test_a_real_drop_is_not_a_glitch() -> None:
    """10/19: vials 5-7 fell 2.1 °C in one read and kept cooling."""
    with TmpRoot() as root:
        engine, _m, _e, alerts = _start(root)
        _feed(engine, 1, [30.1, 30.2, 30.1, 30.1, 27.9, 27.0, 26.6, 26.3, 26.1])
        assert _keyed(alerts, "thermistor_glitch") == [], alerts
        engine.stop_experiment(reason="test_cleanup")


def test_a_rise_across_dropped_reads_is_not_compared() -> None:
    with TmpRoot() as root:
        engine, _m, _e, alerts = _start(root)
        _feed(engine, 1, [30.0, 30.0, math.nan, math.nan, math.nan, math.nan, 33.0])
        assert _keyed(alerts, "thermistor_glitch") == [], alerts
        engine.stop_experiment(reason="test_cleanup")


# ---------------------------------------------------------------------------
# Sustained overrun
# ---------------------------------------------------------------------------

def test_sustained_overrun_alerts_once_and_changes_nothing() -> None:
    with TmpRoot() as root:
        engine, manager, _e, alerts = _start(root)
        before = int(manager.temp_setpoint_raw[1])
        ramp = [30.0 + 0.5 * i for i in range(14)]           # 30 -> 36.5, no glitch
        _feed(engine, 1, ramp + [36.5] * (HEATER_OVERRUN_ALERT_READS + 10))
        crit = _keyed(alerts, "heater_overrun", 1)
        assert len(crit) == 1, alerts
        assert crit[0]["level"] == "critical"
        assert crit[0]["category"] == "heater"
        assert crit[0].get("experiment") == NAME
        assert int(manager.temp_setpoint_raw[1]) == before
        assert engine._vial_faults.get(1) is None
        assert _keyed(alerts, "thermistor_glitch") == [], alerts

        # Back in band re-arms; a second episode alerts again.
        _feed(engine, 1, [30.0] * 3 + ramp + [36.5] * HEATER_OVERRUN_ALERT_READS)
        assert len(_keyed(alerts, "heater_overrun", 1)) == 2, alerts
        engine.stop_experiment(reason="test_cleanup")


def test_one_read_short_of_the_window_does_not_alert() -> None:
    with TmpRoot() as root:
        engine, _m, _e, alerts = _start(root)
        # Up to 34.5 °C: in band even though the integer raw setpoint puts the
        # "30 °C" target at 29.94 °C (so the band edge is 34.94, not 35).
        ramp = [30.0 + 0.5 * i for i in range(10)]
        _feed(engine, 1, ramp + [36.0] * (HEATER_OVERRUN_ALERT_READS - 1) + [30.0])
        assert _keyed(alerts, "heater_overrun") == [], alerts
        engine.stop_experiment(reason="test_cleanup")


def test_cooling_toward_a_lowered_target_does_not_alert() -> None:
    """A vial cooling slowly (0.02 °C/read) from 37 toward a 25 °C target
    spends far more than 3 min above target + 5 °C. That is not a heater
    failing to regulate."""
    with TmpRoot() as root:
        engine, manager, _e, alerts = _start(root, temperature_c=37.0)
        _feed(engine, 1, [37.0] * 5)
        with engine._lock:
            engine._setpoint_raw[1] = _raw(engine, 25.0, 1)
            engine._apply_temperature_locked()
        _feed(engine, 1, [37.0 - 0.02 * i for i in range(400)], others=37.0)
        assert _keyed(alerts, "heater_overrun") == [], alerts
        # Once the vial has been in band at the new target, the watch arms.
        _feed(engine, 1, [29.0] * 2 + [36.0] * HEATER_OVERRUN_ALERT_READS, others=37.0)
        assert len(_keyed(alerts, "heater_overrun", 1)) == 1, alerts
        engine.stop_experiment(reason="test_cleanup")


def test_a_vial_that_starts_hot_does_not_alert() -> None:
    with TmpRoot() as root:
        engine, _m, _e, alerts = _start(root, temperature_c=25.0)
        _feed(engine, 1, [33.0] * (HEATER_OVERRUN_ALERT_READS * 3), others=25.0)
        assert _keyed(alerts, "heater_overrun") == [], alerts
        engine.stop_experiment(reason="test_cleanup")


def test_critical_latch_still_parks_off_after_three_reads() -> None:
    with TmpRoot() as root:
        engine, manager, _e, alerts = _start(root)
        _feed(engine, 1, [30.0, 30.4, 30.8] + [51.0] * 3)
        assert engine._vial_faults.get(1) == "overtemp"
        assert int(manager.temp_setpoint_raw[1]) == HEATER_OFF_SETPOINT
        engine.stop_experiment(reason="test_cleanup")


# ---------------------------------------------------------------------------
# Resume puts a stepped-down setpoint back
# ---------------------------------------------------------------------------

def test_resume_restores_a_stepped_down_setpoint() -> None:
    with TmpRoot() as root:
        engine_a, _mgr_a, _dl_a, _ev_a, _al_a = _fresh(root)
        engine_a.create_experiment(
            name=NAME, mode="turbidostat", vials=[0, 1, 2], parameters=_params(30.0),
        )
        engine_a.start_experiment(NAME)
        target_raw_1 = _raw(engine_a, 30.0, 1)
        # Vial 0: a latched overtemp fault. Vial 1: what the retired step-down
        # left in state.json on the rig (22 °C on a 30 °C vial).
        _feed(engine_a, 0, [51.0] * 3)
        assert engine_a._vial_faults.get(0) == "overtemp"
        with engine_a._lock:
            engine_a._setpoint_raw[1] = _raw(engine_a, 22.0, 1)
            engine_a._save_state_locked()

        engine_b, mgr_b, _dl_b, _ev_b, alerts_b = _fresh(root)
        assert engine_b.resume_on_startup() == NAME
        assert engine_b.status_string == ExperimentStatus.RUNNING
        assert int(mgr_b.temp_setpoint_raw[1]) == target_raw_1
        assert int(mgr_b.temp_setpoint_raw[0]) == HEATER_OFF_SETPOINT, "fault stays parked"
        assert engine_b.resume_retargeted_vials == [1]
        restored = _keyed(alerts_b, "heater_target_restored")
        assert len(restored) == 1, alerts_b
        assert "vial 1 -> 30 °C" in restored[0]["message"], restored
        engine_b.stop_experiment(reason="test_cleanup")


def test_resume_of_a_healthy_run_changes_nothing() -> None:
    with TmpRoot() as root:
        engine_a, _m, _d, _e, _a = _fresh(root)
        engine_a.create_experiment(
            name=NAME, mode="turbidostat", vials=[0, 1], parameters=_params(30.0),
        )
        engine_a.start_experiment(NAME)
        _feed(engine_a, 1, [30.0] * 3)

        engine_b, _mgr_b, _dl_b, _ev_b, alerts_b = _fresh(root)
        assert engine_b.resume_on_startup() == NAME
        assert engine_b.resume_retargeted_vials == []
        assert _keyed(alerts_b, "heater_target_restored") == [], alerts_b
        engine_b.stop_experiment(reason="test_cleanup")


# ---------------------------------------------------------------------------
# The dashboard badge (VialHealth)
# ---------------------------------------------------------------------------

def test_vial_health_marks_a_suspect_thermistor_on_a_rise_only() -> None:
    vh = evlog.VialHealth(3)
    nan = float("nan")
    vh.record_cycle(temperature=[30.0, 30.0, 30.0], od_acquired=False)
    # vial 0 rises 6.5 °C; vial 1 drops 2.2 °C; vial 2 rises across a gap.
    vh.record_cycle(temperature=[36.5, 27.8, nan], od_acquired=False)
    vh.record_cycle(temperature=[36.0, 27.8, 33.0], od_acquired=False)
    snap = vh.snapshot()
    assert snap[0]["state"] == "temp_suspect", snap[0]
    assert snap[0]["temp_suspect"] is True
    assert snap[0]["last_temp_jump_c"] == 6.5
    assert snap[1]["state"] == "ok", snap[1]
    assert snap[2]["state"] == "ok", snap[2]


def test_vial_health_suspect_holds_then_clears() -> None:
    vh = evlog.VialHealth(1)
    vh.record_cycle(temperature=[30.0], od_acquired=False)
    vh.record_cycle(temperature=[36.0], od_acquired=False)
    for _ in range(evlog.TEMP_SUSPECT_HOLD_READS - 1):
        vh.record_cycle(temperature=[30.0], od_acquired=False)
    assert vh.snapshot()[0]["state"] == "temp_suspect"
    vh.record_cycle(temperature=[30.0], od_acquired=False)
    assert vh.snapshot()[0]["state"] == "ok"


def test_vial_health_forgets_readings_across_a_calibration_install() -> None:
    """An installed temperature curve shifts every reading at once."""
    vh = evlog.VialHealth(1)
    vh.record_cycle(temperature=[30.0], od_acquired=False)
    vh.forget_temperatures()
    vh.record_cycle(temperature=[33.0], od_acquired=False)
    assert vh.snapshot()[0]["state"] == "ok"


def test_glitch_detection_can_be_switched_off() -> None:
    """app.py switches it off under --mock, whose 100x-accelerated thermal
    model jumps several degrees per read by design."""
    vh = evlog.VialHealth(1, glitch_rise_c=None)
    vh.record_cycle(temperature=[22.0], od_acquired=False)
    vh.record_cycle(temperature=[37.0], od_acquired=False)
    assert vh.snapshot()[0]["state"] == "ok"
    assert vh.snapshot()[0]["temp_suspect"] is False
    with TmpRoot() as root:
        engine, _m, _e, alerts = _start(root)
        engine._glitch_rise_c = None
        _feed(engine, 1, V11_STEP_AND_DECAY)
        assert _keyed(alerts, "thermistor_glitch") == [], alerts
        engine.stop_experiment(reason="test_cleanup")


def test_vial_health_degraded_outranks_suspect() -> None:
    vh = evlog.VialHealth(1, degraded_threshold=3)
    nan = float("nan")
    vh.record_cycle(temperature=[30.0], od_acquired=False)
    vh.record_cycle(temperature=[36.0], od_acquired=False)
    for _ in range(3):
        vh.record_cycle(temperature=[nan], od_acquired=False)
    assert vh.snapshot()[0]["state"] == "degraded"


if __name__ == "__main__":
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"PASS  {_name}")
