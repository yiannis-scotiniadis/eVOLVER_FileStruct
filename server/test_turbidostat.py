"""Verification script for the TurbidostatController (SPEC §9 algorithm).

Pure function tests — no I/O, no clock, no mocks. The controller is fed
synthetic OD trajectories at a controlled cadence; we assert that pump
events fire on the right thresholds, the pump-time formula matches the
SPEC, the pump_wait gate is respected, and state survives a round-trip
through to_state/restore_state.

Run from the project root:
    python server/test_turbidostat.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from control_modes.turbidostat import (  # noqa: E402
    PumpAction,
    TurbidostatController,
)


def _make(**overrides) -> TurbidostatController:
    """Default-parameterised controller; tests override individual fields.

    Note: ``min_samples_before_action`` is set to 1 here so the existing
    algorithm-correctness tests (which push only a handful of samples)
    aren't blocked by the legacy 8-sample warmup gate. The gate itself
    is exercised by ``test_warmup_gate_blocks_early_action`` below."""
    kw = dict(
        vial=0,
        od_lower=0.2,
        od_upper=0.4,
        pump_wait_seconds=900.0,   # 15 min
        flow_rate_ml_s=1.0,
        volume_ml=25.0,
        efflux_extra_seconds=5.0,
        history_window=5,
        pump_duration_cap_seconds=20.0,
        min_samples_before_action=1,
    )
    kw.update(overrides)
    return TurbidostatController(**kw)


def test_initial_state() -> None:
    c = _make()
    assert c.target == 0.4
    assert c.last_pump_time is None
    assert c.average_od() is None
    assert c.time_since_last_pump(1.0) == float("inf")
    print("PASS  initial state (target=upper, no pumps yet)")


def test_cold_start_returns_none() -> None:
    c = _make()
    # No samples yet
    assert c.decide(now=0.0) is None
    print("PASS  cold start returns None (no history)")


def test_below_target_no_pump() -> None:
    c = _make()
    for _ in range(5):
        c.push_od(0.25)  # below upper threshold of 0.4
    # target is upper (0.4) and avg (0.25) <= target -> no pump
    action = c.decide(now=10000.0)
    assert action is None
    print("PASS  avg <= target -> no pump")


def test_above_upper_switches_target_and_pumps() -> None:
    c = _make()
    for _ in range(5):
        c.push_od(0.5)  # well above upper 0.4
    action = c.decide(now=10000.0)
    assert isinstance(action, PumpAction)
    assert c.target == 0.2, f"target should have flipped to lower, got {c.target}"
    # pump_time = -ln(0.2/0.5) * 25 / 1.0 = ln(2.5) * 25 ≈ 22.91 -> capped at 20.
    assert abs(action.pump_time - 20.0) < 1e-9, f"pump_time {action.pump_time}, expected 20 (capped)"
    assert action.efflux_extra_seconds == 5.0
    assert action.efflux_seconds == 25.0
    assert abs(action.average_od - 0.5) < 1e-9
    print(f"PASS  above upper -> target flips, pump fires (pump_time={action.pump_time}, capped)")


def test_pump_time_formula_uncapped() -> None:
    """Pick an OD that yields a sub-cap pump time so we can verify the formula.

    The controller fires whole-second pump_time (firmware accepts integer s
    only) and DISCARDS the fractional remainder -- there is no accumulator
    (CONTROL_MODE_AUDIT.md T-3). int(t) <= t_needed is what makes the OD
    floor unbreachable."""
    c = _make()
    # We want pump_time = -ln(0.2 / avg) * 25 / 1.0 < 20
    # ln(0.2/avg) > -20/25 = -0.8 -> 0.2/avg > e^-0.8 ~ 0.449 -> avg < 0.4456
    # So pick avg = 0.43 (just above upper threshold).
    for _ in range(5):
        c.push_od(0.43)
    action = c.decide(now=10000.0)
    assert action is not None
    expected_float = -math.log(0.2 / 0.43) * 25.0 / 1.0
    expected_whole = int(expected_float)
    assert action.pump_time == float(expected_whole), (
        f"pump_time {action.pump_time}, expected {expected_whole} "
        f"(int part of formula {expected_float:.6f})"
    )
    assert not hasattr(c, "pump_time_deficit_seconds"), (
        "the turbidostat must carry no deficit accumulator (T-1/T-3)"
    )
    print(
        f"PASS  pump_time formula matches SPEC section 9 "
        f"(avg=0.43 -> formula {expected_float:.4f} s, fires {expected_whole}s, "
        f"remainder discarded)"
    )


def test_bolus_sized_from_latest_sample_not_lagged_mean() -> None:
    """T-4: the rolling mean decides WHETHER to dilute; the newest sample
    sizes the bolus. During growth the mean sits below the culture's present
    density, so sizing from it under-doses."""
    c = _make(history_window=5)
    # Mean must clear od_upper (0.4) for hysteresis to flip the target,
    # and the latest sample must stay under the 20 s cap so the cap does not
    # mask which OD the bolus was sized from.
    samples = (0.39, 0.40, 0.41, 0.42, 0.44)
    for od in samples:
        c.push_od(od)
    mean = sum(samples) / len(samples)   # 0.412
    latest = samples[-1]                 # 0.44
    action = c.decide(now=10000.0)
    assert action is not None
    from_mean = int(-math.log(0.2 / mean) * 25.0)
    from_latest = int(min(-math.log(0.2 / latest) * 25.0, 20.0))
    assert from_mean != from_latest, "sanity: the two must differ for this test"
    assert action.pump_time == float(from_latest), (
        f"pump_time {action.pump_time} was sized from the lagged mean "
        f"({from_mean}s); expected sizing from the latest sample ({from_latest}s)"
    )
    assert abs(action.average_od - mean) < 1e-9, (
        "average_od must still report the rolling mean -- it is what lands "
        "in pump_log.csv od_at_pump"
    )
    assert action.sizing_od == latest
    print(
        f"PASS  bolus sized from latest sample ({latest}) not lagged mean "
        f"({mean:.3f}); average_od still reports the mean"
    )


def test_pump_wait_gate() -> None:
    """Two decide() calls within pump_wait_seconds: only the first fires."""
    c = _make(pump_wait_seconds=900.0)
    for _ in range(5):
        c.push_od(0.5)
    first = c.decide(now=10000.0)
    assert isinstance(first, PumpAction)
    # Push more high OD and check again 100 s later — under pump_wait.
    for _ in range(5):
        c.push_od(0.5)
    second = c.decide(now=10100.0)
    assert second is None, "second fire within pump_wait should be gated"
    # After pump_wait elapses, it can fire again.
    third = c.decide(now=10000.0 + 900.0 + 1.0)
    assert isinstance(third, PumpAction)
    print("PASS  pump_wait gate (no fire within window, fire after)")


def test_hysteresis_back_to_upper() -> None:
    """After a fire flips target to lower, OD must fall below midpoint
    before target flips back to upper."""
    c = _make()
    for _ in range(5):
        c.push_od(0.5)
    c.decide(now=10000.0)
    assert c.target == 0.2

    # OD drops to 0.35 — above midpoint 0.3, so target should NOT flip yet.
    for _ in range(5):
        c.push_od(0.35)
    c.decide(now=10000.0 + 1000.0)
    assert c.target == 0.2, f"target flipped too early; got {c.target}"

    # OD drops below midpoint 0.3 — now target should flip back to upper.
    for _ in range(5):
        c.push_od(0.25)
    c.decide(now=10000.0 + 2000.0)
    assert c.target == 0.4, f"target should flip back to upper; got {c.target}"
    print("PASS  hysteresis target switching (midpoint = (lower+upper)/2)")


def test_history_window_limits_average() -> None:
    """Only the last N samples count toward the average."""
    c = _make(history_window=3)
    c.push_od(10.0)  # outlier; should fall out of window
    c.push_od(0.3)
    c.push_od(0.3)
    c.push_od(0.3)
    avg = c.average_od()
    assert abs(avg - 0.3) < 1e-9, f"average {avg}, expected 0.3 (3-sample window)"
    print("PASS  history_window limits the rolling average")


def test_nan_dropped() -> None:
    c = _make(history_window=3)
    c.push_od(0.3)
    c.push_od(float("nan"))
    c.push_od(0.3)
    assert len(c.od_history) == 2
    assert abs(c.average_od() - 0.3) < 1e-9
    print("PASS  NaN samples silently dropped from history")


def test_state_round_trip() -> None:
    c = _make()
    for v in (0.30, 0.32, 0.35, 0.38, 0.42):
        c.push_od(v)
    c.decide(now=100.0)  # might fire; might not (target=0.4, avg≈0.354; below 0.4)

    state = c.to_state()
    # Build a fresh controller, restore, and verify state matches.
    other = _make()
    other.restore_state(state)
    assert other.target == c.target
    assert other.last_pump_time == c.last_pump_time
    assert list(other.od_history) == list(c.od_history)
    assert other.average_od() == c.average_od()
    print("PASS  to_state / restore_state round-trip preserves controller state")


def test_invalid_constructor() -> None:
    cases = [
        dict(vial=-1),
        dict(vial=16),
        dict(od_lower=0),
        dict(od_lower=-0.1),
        dict(od_upper=0.1, od_lower=0.2),   # upper < lower
        dict(pump_wait_seconds=-1),
        dict(flow_rate_ml_s=0),
        dict(flow_rate_ml_s=-1),
        dict(volume_ml=0),
        dict(history_window=0),
        dict(pump_duration_cap_seconds=0),
    ]
    for overrides in cases:
        try:
            _make(**overrides)
        except ValueError:
            continue
        raise AssertionError(f"accepted bad inputs: {overrides}")
    print("PASS  invalid constructor parameters rejected")


def test_pump_action_efflux_seconds() -> None:
    a = PumpAction(pump_time=8.2, efflux_extra_seconds=5.0, average_od=0.41)
    assert a.efflux_seconds == 13.2
    print("PASS  PumpAction.efflux_seconds == pump_time + efflux_extra_seconds")


def test_no_windup_across_refractory_cycles() -> None:
    """T-1, the critical one. pump_time is an ABSOLUTE correction, not a
    per-cycle increment, so accumulating it across cycles that fire nothing
    makes an integrator with no anti-windup. 50 gated cycles must leave the
    next bolus exactly as large as a single ungated decision would."""
    c = _make(pump_wait_seconds=900.0)
    for _ in range(8):
        c.push_od(0.5)
    first = c.decide(now=0.0)
    assert first is not None and first.pump_time == 20.0

    # 50 cycles inside the refractory window: hot OD, nothing may fire and
    # nothing may be stored up for later.
    for tick in range(1, 51):
        c.push_od(0.43)
        assert c.decide(now=float(tick)) is None, f"tick {tick} fired inside pump_wait"

    # Gate opens. The bolus must match the formula for the CURRENT OD alone.
    c.push_od(0.43)
    after = c.decide(now=1000.0)
    assert after is not None
    expected = int(-math.log(0.2 / 0.43) * 25.0)
    assert after.pump_time == float(expected), (
        f"after 50 gated cycles the bolus was {after.pump_time}s; a single "
        f"ungated decision at the same OD asks for {expected}s -- the "
        f"difference is accumulator windup"
    )
    print(
        f"PASS  no windup: 50 refractory cycles leave the next bolus at "
        f"{after.pump_time}s (single-decision value)"
    )


def test_dilution_never_undershoots_the_floor() -> None:
    """Truncating without carrying guarantees int(t) <= t_needed, so a
    dilution can never drive OD below od_lower. Checked directly against the
    washout model the formula inverts."""
    volume, flow = 25.0, 1.0
    for lo, hi in ((0.35, 0.40), (0.30, 0.40), (0.20, 0.40), (0.20, 0.60)):
        c = _make(od_lower=lo, od_upper=hi, pump_wait_seconds=0.0)
        for od in (hi * 1.05, hi * 1.10, hi * 1.15, hi * 1.20, hi * 1.25):
            c.push_od(od)
        action = c.decide(now=100.0)
        assert action is not None, f"band [{lo}, {hi}] failed to fire"
        landed = action.sizing_od * math.exp(-flow * action.pump_time / volume)
        assert landed >= lo - 1e-12, (
            f"band [{lo}, {hi}]: {action.pump_time}s from OD "
            f"{action.sizing_od:.3f} lands at {landed:.4f}, below the floor {lo}"
        )
    print("PASS  truncation guarantees the OD floor is never undershot")


def test_sub_second_bolus_is_dropped_not_accumulated() -> None:
    """T-3: with no accumulator a sub-second formula value fires nothing and
    carries nothing. This is reachable only in a band narrower than
    validate_control_parameters allows -- the create-time band check is what
    makes it unreachable in a real run."""
    # od_upper/od_lower = 1.015 -> max bolus ln(1.015)*25 = 0.37 s
    c = _make(od_lower=0.2, od_upper=0.203, pump_wait_seconds=0.0)
    c.target = c.od_lower  # whitebox: pretend hysteresis already flipped
    fires = 0
    for tick in range(50):
        c.push_od(0.2029)
        if c.decide(now=100.0 * (tick + 1)) is not None:
            fires += 1
    assert fires == 0, (
        f"{fires} fires from a band whose largest bolus is sub-second -- "
        "something is accumulating"
    )
    print(
        "PASS  sub-second bolus dropped, not accumulated (band validation at "
        "experiment creation is what prevents this configuration)"
    )


def test_restore_ignores_legacy_deficit_key() -> None:
    """A state.json written before T-3 removed the accumulator still carries
    pump_time_deficit_seconds. Resuming across the upgrade must ignore it
    rather than fail -- the rig can have a run in flight."""
    c = _make()
    c.restore_state({
        "target": 0.2,
        "last_pump_time": 500.0,
        "od_history": [0.41, 0.42],
        "total_samples_seen": 9,
        "pump_time_deficit_seconds": 17.5,   # legacy key
    })
    assert c.target == 0.2
    assert c.last_pump_time == 500.0
    assert c.total_samples_seen == 9
    assert not hasattr(c, "pump_time_deficit_seconds")
    assert "pump_time_deficit_seconds" not in c.to_state()
    print("PASS  legacy pump_time_deficit_seconds ignored on restore")


def test_restore_rebaselines_future_timestamp() -> None:
    """X-2: the RPi has no RTC, so a stale boot clock can put a persisted
    last_pump_time ahead of wall time. Left alone, now - last_pump_time goes
    negative and every dilution is blocked until wall time catches up."""
    c = _make(pump_wait_seconds=900.0)
    c.restore_state({"last_pump_time": 1_000_000.0}, now=1000.0)
    assert c.last_pump_time == 1000.0, (
        f"future timestamp not re-baselined: {c.last_pump_time}"
    )
    # Without now=, the value is left as-is (callers that have no clock).
    other = _make()
    other.restore_state({"last_pump_time": 1_000_000.0})
    assert other.last_pump_time == 1_000_000.0

    # And the gate opens normally after the clamp.
    for _ in range(8):
        c.push_od(0.5)
    assert c.decide(now=1000.0 + 901.0) is not None
    print("PASS  restore re-baselines a future timestamp to now (X-2)")


def test_warmup_gate_blocks_early_action() -> None:
    """Legacy custom_script.py:83 required >7 (i.e. 8+) OD samples before
    any control action. The new controller preserves this gate via
    ``min_samples_before_action`` (default 8). 1..7 samples never produce
    a PumpAction even when the OD is well above the upper threshold;
    sample 8 (and onward) can fire."""
    c = _make(min_samples_before_action=8)
    # Push 7 hot samples (well above upper threshold).
    for i in range(7):
        c.push_od(0.9)
        action = c.decide(now=100.0 + i)
        assert action is None, (
            f"sample {i + 1}: gate should block, but PumpAction was returned "
            f"(pump_time={action.pump_time})"
        )
    # 8th sample: gate opens. Avg of 5 (window) most recent = 0.9 — well
    # above upper, so a pump should fire.
    c.push_od(0.9)
    action = c.decide(now=200.0)
    assert action is not None, (
        "warmup gate should release on sample 8, but no PumpAction was returned"
    )
    print(
        "PASS  warmup gate blocks samples 1-7 (action gated until 8th OD)"
    )


def test_warmup_gate_persists_across_state_roundtrip() -> None:
    """total_samples_seen must survive to_state/restore_state so a resumed
    experiment doesn't reset the warmup period."""
    c = _make(min_samples_before_action=8)
    for _ in range(5):
        c.push_od(0.3)
    state = c.to_state()
    other = _make(min_samples_before_action=8)
    other.restore_state(state)
    assert other.total_samples_seen == 5, other.total_samples_seen
    # Resumed controller still has 3 more samples before the gate opens.
    for i in range(2):
        other.push_od(0.9)
        action = other.decide(now=300.0 + i)
        assert action is None, f"resumed sample {6 + i}: should still be gated"
    other.push_od(0.9)
    action = other.decide(now=400.0)
    assert action is not None, "resumed should fire on the cumulative 8th sample"
    print("PASS  warmup counter persists across state round-trip")


def test_full_turbidostat_oscillation() -> None:
    """Integration-style: drive a controller with a logistic-then-dilute
    OD trajectory; assert pump events fire near the upper threshold and
    target oscillates."""
    c = _make(pump_wait_seconds=10.0)  # very short for test speed
    pumps_fired: list[PumpAction] = []
    now = 0.0

    # OD climbs from 0.05 to 0.55 over 100 ticks, then dilution drops it back.
    for tick in range(200):
        # Simple sawtooth: rise, then plummet after a pump fires.
        if not pumps_fired or pumps_fired[-1] is None:
            od = 0.05 + 0.005 * (tick % 100)   # 0.05 -> 0.545
        else:
            od = 0.05 + 0.005 * (tick % 100)
        c.push_od(od)
        action = c.decide(now=now)
        if action is not None:
            pumps_fired.append(action)
            # Simulate dilution back to lower threshold
            for _ in range(5):
                c.push_od(0.18)
        now += 10.0

    assert len(pumps_fired) > 0, "expected at least one pump fire across the trajectory"
    # All pump times must be in (0, 20] (cap).
    for p in pumps_fired:
        assert 0 < p.pump_time <= 20.0, f"pump_time out of bounds: {p.pump_time}"
    print(f"PASS  full turbidostat oscillation fires {len(pumps_fired)} pump events")


def main() -> int:
    test_initial_state()
    test_cold_start_returns_none()
    test_below_target_no_pump()
    test_above_upper_switches_target_and_pumps()
    test_pump_time_formula_uncapped()
    test_bolus_sized_from_latest_sample_not_lagged_mean()
    test_pump_wait_gate()
    test_hysteresis_back_to_upper()
    test_history_window_limits_average()
    test_nan_dropped()
    test_state_round_trip()
    test_invalid_constructor()
    test_pump_action_efflux_seconds()
    test_no_windup_across_refractory_cycles()
    test_dilution_never_undershoots_the_floor()
    test_sub_second_bolus_is_dropped_not_accumulated()
    test_restore_ignores_legacy_deficit_key()
    test_restore_rebaselines_future_timestamp()
    test_warmup_gate_blocks_early_action()
    test_warmup_gate_persists_across_state_roundtrip()
    test_full_turbidostat_oscillation()
    print("\nAll turbidostat tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
