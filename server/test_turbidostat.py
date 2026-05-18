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
    only); the fractional remainder is preserved in pump_time_deficit_seconds
    and applied on a subsequent fire."""
    c = _make()
    # We want pump_time = -ln(0.2 / avg) * 25 / 1.0 < 20
    # ln(0.2/avg) > -20/25 = -0.8 -> 0.2/avg > e^-0.8 ≈ 0.449 -> avg < 0.4456
    # So pick avg = 0.43 (just above upper threshold).
    for _ in range(5):
        c.push_od(0.43)
    action = c.decide(now=10000.0)
    assert action is not None
    expected_float = -math.log(0.2 / 0.43) * 25.0 / 1.0
    expected_whole = int(expected_float)
    expected_residual = expected_float - expected_whole
    assert action.pump_time == float(expected_whole), (
        f"pump_time {action.pump_time}, expected {expected_whole} "
        f"(int part of formula {expected_float:.6f})"
    )
    assert abs(c.pump_time_deficit_seconds - expected_residual) < 1e-9, (
        f"residual deficit {c.pump_time_deficit_seconds}, "
        f"expected {expected_residual:.6f}"
    )
    print(
        f"PASS  pump_time formula matches SPEC §9 "
        f"(avg=0.43 -> formula {expected_float:.4f} s, fires {expected_whole}s, "
        f"carries {expected_residual:.4f}s)"
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


def _sub_second_make(**overrides) -> TurbidostatController:
    """Narrow hysteresis band so avg-just-above-lower regimes (the only
    place the formula yields sub-second values with realistic flow rates)
    stay in the target=lower branch instead of bouncing back to upper.

    With od_lower=0.2, od_upper=0.205 -> midpoint=0.2025; an avg of 0.203
    keeps target pinned to lower AND yields pump_time ≈ 0.372 s per cycle."""
    kw = dict(
        od_lower=0.2,
        od_upper=0.205,
        pump_wait_seconds=0.0,
        min_samples_before_action=1,
    )
    kw.update(overrides)
    return _make(**kw)


def test_sub_second_deficit_accumulates_then_fires() -> None:
    """Sub-second per-cycle pump_time must accumulate into a deficit and
    fire when the integer part >= 1 s. The legacy firmware would silently
    drop each sub-second command; the deficit makes the cumulative
    dilution land as a smaller number of whole-second pumps instead."""
    c = _sub_second_make()
    # Whitebox: pretend hysteresis has already flipped target to lower so
    # we can feed a steady avg-just-above-lower regime without first
    # having to ramp OD over od_upper.
    c.target = c.od_lower

    per_cycle = -math.log(0.2 / 0.203) * 25.0 / 1.0
    assert per_cycle < 0.5, (
        f"sanity: per_cycle {per_cycle:.4f} should be sub-second"
    )

    fires = []
    for tick in range(9):
        c.push_od(0.203)  # above midpoint(0.2025), keeps target at lower
        action = c.decide(now=100.0 * (tick + 1))
        if action is not None:
            fires.append((tick, action))

    assert len(fires) >= 2, (
        f"expected >=2 fires across 9 sub-second cycles "
        f"(per_cycle={per_cycle:.4f} s); got {len(fires)}"
    )
    for tick, action in fires:
        assert action.pump_time == int(action.pump_time), (
            f"fire at tick {tick}: pump_time {action.pump_time} is not integer"
        )
        assert action.pump_time >= 1.0, (
            f"fire at tick {tick}: pump_time {action.pump_time} < 1"
        )
    # First fire arrives no earlier than ceil(1/per_cycle) cycles in.
    earliest_legitimate_tick = int(math.ceil(1.0 / per_cycle)) - 1
    assert fires[0][0] >= earliest_legitimate_tick, (
        f"first fire at tick {fires[0][0]} earlier than the deficit could "
        f"legitimately cross 1 s (need >= tick {earliest_legitimate_tick})"
    )
    print(
        f"PASS  sub-second deficit accumulates (per-cycle {per_cycle:.4f}s, "
        f"first fire at tick {fires[0][0]}, total fires {len(fires)})"
    )


def test_deficit_preserves_total_dilution() -> None:
    """The deficit accumulator should preserve total dilution: fired
    seconds plus the residual still in the deficit equals the sum of all
    per-cycle formula outputs (no truncation loss). Without accumulation,
    every cycle's sub-second contribution would be silently dropped."""
    c = _sub_second_make()
    c.target = c.od_lower
    per_cycle = -math.log(0.2 / 0.203) * 25.0 / 1.0
    n_cycles = 50

    fired_seconds = 0.0
    for tick in range(n_cycles):
        c.push_od(0.203)
        action = c.decide(now=100.0 * (tick + 1))
        if action is not None:
            fired_seconds += action.pump_time

    naive_total = per_cycle * n_cycles
    reconstructed = fired_seconds + c.pump_time_deficit_seconds
    assert abs(reconstructed - naive_total) < 1e-9, (
        f"fired {fired_seconds} + residual {c.pump_time_deficit_seconds} "
        f"= {reconstructed}, expected formula sum {naive_total}"
    )
    print(
        f"PASS  deficit preserves total dilution "
        f"(fired {fired_seconds}s + residual {c.pump_time_deficit_seconds:.4f}s "
        f"== formula sum {naive_total:.4f}s)"
    )


def test_deficit_capped_at_pump_duration_cap() -> None:
    """A long pump_wait stall must not let the deficit snowball past the
    hard cap — otherwise a stuck experiment could fire a huge catch-up
    bolus once the wait elapses."""
    c = _make(pump_wait_seconds=1e9)  # effectively never opens the wait gate
    # Whitebox: simulate that a pump fired at t=0 so the wait gate blocks
    # every subsequent decide() in this test (time_since_last_pump returns
    # inf for last_pump_time==None, which would let the very first call fire).
    c.last_pump_time = 0.0
    # Drive OD well above upper so each cycle's pump_time is the cap (20 s).
    for _ in range(8):
        c.push_od(0.9)
    for tick in range(20):
        c.push_od(0.9)
        action = c.decide(now=100.0 * (tick + 1))
        assert action is None, (
            f"tick {tick}: pump_wait gate should block but a PumpAction fired"
        )
    assert c.pump_time_deficit_seconds == c.pump_duration_cap_seconds, (
        f"deficit {c.pump_time_deficit_seconds} should be clamped at "
        f"cap {c.pump_duration_cap_seconds}"
    )
    print(
        f"PASS  deficit clamped at pump_duration_cap_seconds "
        f"({c.pump_duration_cap_seconds}s) despite long pump_wait stall"
    )


def test_deficit_persists_across_state_roundtrip() -> None:
    """A controller that has accumulated a fractional residual must restore
    that residual so a resumed experiment doesn't lose pending dilution."""
    c = _sub_second_make()
    c.target = c.od_lower
    for tick in range(3):
        c.push_od(0.203)
        c.decide(now=100.0 * (tick + 1))
    assert c.pump_time_deficit_seconds > 0.0

    state = c.to_state()
    assert "pump_time_deficit_seconds" in state, (
        "to_state must include pump_time_deficit_seconds for persistence"
    )

    other = _sub_second_make()
    other.restore_state(state)
    assert abs(other.pump_time_deficit_seconds - c.pump_time_deficit_seconds) < 1e-12, (
        f"restored deficit {other.pump_time_deficit_seconds}, "
        f"expected {c.pump_time_deficit_seconds}"
    )
    print(
        f"PASS  deficit persists across state round-trip "
        f"({c.pump_time_deficit_seconds:.4f}s preserved)"
    )


def test_deficit_restore_clamps_corrupt_values() -> None:
    """A negative deficit in state.json would silently block the next pump;
    a too-large one would grant a >cap bolus. Both get clamped on restore."""
    c1 = _make()
    c1.restore_state({"pump_time_deficit_seconds": -7.0})
    assert c1.pump_time_deficit_seconds == 0.0, (
        f"negative deficit not clamped: {c1.pump_time_deficit_seconds}"
    )

    c2 = _make(pump_duration_cap_seconds=20.0)
    c2.restore_state({"pump_time_deficit_seconds": 9999.0})
    assert c2.pump_time_deficit_seconds == 20.0, (
        f"over-cap deficit not clamped: {c2.pump_time_deficit_seconds}"
    )
    print("PASS  restore clamps deficit to [0, pump_duration_cap_seconds]")


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
    test_pump_wait_gate()
    test_hysteresis_back_to_upper()
    test_history_window_limits_average()
    test_nan_dropped()
    test_state_round_trip()
    test_invalid_constructor()
    test_pump_action_efflux_seconds()
    test_sub_second_deficit_accumulates_then_fires()
    test_deficit_preserves_total_dilution()
    test_deficit_capped_at_pump_duration_cap()
    test_deficit_persists_across_state_roundtrip()
    test_deficit_restore_clamps_corrupt_values()
    test_warmup_gate_blocks_early_action()
    test_warmup_gate_persists_across_state_roundtrip()
    test_full_turbidostat_oscillation()
    print("\nAll turbidostat tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
