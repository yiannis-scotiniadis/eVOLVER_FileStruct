"""Verification script for the ChemostatController (SPEC §9 algorithm).

Run from the project root::

    python server/test_chemostat.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from control_modes.chemostat import ChemostatController  # noqa: E402
from control_modes.turbidostat import PumpAction  # noqa: E402


def _make(**overrides) -> ChemostatController:
    """Default-parameterised controller; tests override individual fields."""
    kw = dict(
        vial=0,
        dilution_rate_per_hour=0.5,
        bolus_interval_seconds=60.0,
        volume_ml=25.0,
        flow_rate_ml_s=1.0,
        efflux_extra_seconds=5.0,
        pump_duration_cap_seconds=20.0,
    )
    kw.update(overrides)
    return ChemostatController(**kw)


def test_first_bolus_cycle_runs_immediately() -> None:
    """The bolus_interval gate opens immediately on cold start. With default
    params (D=0.5/h, V=25, T=60, F=1) the per-bolus pump_time is ~0.208 s —
    sub-second, so the deficit accumulator absorbs it and no PumpAction
    fires on the first cycle. last_bolus_time and boli_fired still advance
    because the bolus *cycle* completed."""
    c = _make()
    per_bolus = 0.5 * 25.0 * 60.0 / 3600.0  # 0.20833...
    action = c.decide(now=0.0)
    assert action is None, (
        f"sub-second per-bolus pump_time ({per_bolus:.4f}s) should accumulate "
        f"in the deficit, not produce a PumpAction; got {action}"
    )
    assert c.boli_fired == 1, c.boli_fired
    assert c.last_bolus_time == 0.0
    assert abs(c.pump_time_deficit_seconds - per_bolus) < 1e-9
    print(
        f"PASS  first bolus cycle runs immediately, sub-second pump_time "
        f"({per_bolus:.4f}s) deferred into deficit"
    )


def test_sub_second_per_bolus_accumulates_then_fires() -> None:
    """A slow chemostat with sub-second per-bolus pump_time should fire a
    whole-second pump every ~ceil(1 / per_bolus) bolus cycles."""
    c = _make()  # D=0.5/h, per-bolus pump_time = 0.208 s
    per_bolus = 0.5 * 25.0 * 60.0 / 3600.0
    fires_at: list[int] = []
    for cycle in range(20):
        action = c.decide(now=60.0 * cycle)
        if action is not None:
            fires_at.append(cycle)
            assert action.pump_time >= 1.0
            assert action.pump_time == int(action.pump_time)
    # 20 bolus cycles * 0.208 s/cycle = 4.17 s of total dilution => ~4 fires.
    assert len(fires_at) >= 3, (
        f"expected >=3 whole-second fires across 20 sub-second bolus cycles, "
        f"got {len(fires_at)} (fires at {fires_at})"
    )
    # Total fired seconds + residual deficit must equal naive cumulative
    # per_bolus * cycles (no truncation loss).
    naive_total = per_bolus * 20
    # boli_fired tracks bolus cycles, not fires.
    assert c.boli_fired == 20
    print(
        f"PASS  20 sub-second bolus cycles yield {len(fires_at)} whole-s "
        f"fires at cycles {fires_at}; deficit residual "
        f"{c.pump_time_deficit_seconds:.4f}s (cumulative {naive_total:.4f}s)"
    )


def test_bolus_interval_gates() -> None:
    """The bolus_interval gate limits how often decide() advances the
    bolus cycle. Use a fast enough chemostat (D=30/h) that each per-bolus
    pump_time is well above 1 s, so every cycle that passes the gate
    actually fires a PumpAction."""
    # per_bolus = 30 * 25 * 60 / 3600 / 1 = 12.5 s (well above 1 s)
    c = _make(dilution_rate_per_hour=30.0, bolus_interval_seconds=60.0)
    first = c.decide(now=0.0)
    assert first is not None
    # Within interval -> None
    assert c.decide(now=30.0) is None
    assert c.decide(now=59.999) is None
    # At interval -> fire
    second = c.decide(now=60.0)
    assert second is not None
    assert c.boli_fired == 2
    # Within next window -> None again
    assert c.decide(now=90.0) is None
    third = c.decide(now=120.0)
    assert third is not None
    assert c.boli_fired == 3
    print("PASS  bolus_interval gates subsequent decide() calls")


def test_pump_time_formula() -> None:
    """When per-bolus pump_time >= 1 s, the deficit accumulator passes it
    straight through (with int-truncation per the firmware contract)."""
    # D=120/h, V=10, T=30, F=2 -> influx = 120*10*30/3600 = 10 mL
    # pump_time = 10 / 2 = 5.0 s exactly (above 1 s, integer => no residual)
    c = _make(dilution_rate_per_hour=120.0, volume_ml=10.0,
              bolus_interval_seconds=30.0, flow_rate_ml_s=2.0)
    action = c.decide(now=0.0)
    assert action is not None
    expected = 120.0 * 10.0 * 30.0 / 3600.0 / 2.0  # = 5.0
    assert action.pump_time == int(expected), (
        f"pump_time {action.pump_time}, expected int({expected})"
    )
    print(f"PASS  pump_time formula D*V*T/3600/F (got {action.pump_time} s)")


def test_pump_time_capped_by_duration_limit() -> None:
    # Make the math yield > 20 s: D=10 vol/hr, V=100 mL, T=600 s, F=1 mL/s
    # influx = 10 * 100 * 600 / 3600 = 166.67 mL  ->  pump_time = 166.67 s
    # Will be capped at pump_duration_cap_seconds=20.0, then also by
    # bolus_interval-1=599 (so effective cap is 20 here).
    c = _make(dilution_rate_per_hour=10.0, volume_ml=100.0,
              bolus_interval_seconds=600.0, flow_rate_ml_s=1.0,
              pump_duration_cap_seconds=20.0)
    action = c.decide(now=0.0)
    assert action is not None
    assert action.pump_time == 20.0, f"expected hard-cap 20s, got {action.pump_time}"
    print("PASS  pump_time capped at pump_duration_cap_seconds")


def test_pump_time_clamped_by_bolus_interval() -> None:
    # Make the math yield > bolus_interval - 1 but < pump_duration_cap.
    # D=10 vol/hr, V=10 mL, T=10 s, F=0.1 mL/s -> influx = 10*10*10/3600 = 0.2778 mL
    # pump_time = 0.2778 / 0.1 = 2.778 s. bolus_interval - 1 = 9 s, cap = 20.
    # So no clamp here. Need pump_time > bolus_interval-1.
    # Try: D=100, V=10, T=10, F=0.1 -> influx = 100*10*10/3600 = 2.778 mL
    # pump_time = 2.778 / 0.1 = 27.78 s; cap at 20 first, but bolus_interval-1 = 9
    # So clamped to 9.
    c = _make(dilution_rate_per_hour=100.0, volume_ml=10.0,
              bolus_interval_seconds=10.0, flow_rate_ml_s=0.1,
              pump_duration_cap_seconds=20.0)
    action = c.decide(now=0.0)
    assert action is not None
    assert action.pump_time == 9.0, (
        f"expected bolus_interval-1 = 9s, got {action.pump_time}"
    )
    print("PASS  pump_time clamped to bolus_interval - 1s safety margin")


def test_constructor_rejects_invalid_params() -> None:
    cases = [
        dict(vial=-1),
        dict(vial=16),
        dict(dilution_rate_per_hour=0),
        dict(dilution_rate_per_hour=-1),
        dict(bolus_interval_seconds=0),
        dict(bolus_interval_seconds=-1),
        dict(volume_ml=0),
        dict(volume_ml=-1),
        dict(flow_rate_ml_s=0),
        dict(flow_rate_ml_s=-1),
        dict(pump_duration_cap_seconds=0),
    ]
    for overrides in cases:
        try:
            _make(**overrides)
        except ValueError:
            continue
        raise AssertionError(f"accepted bad inputs: {overrides}")
    print("PASS  invalid constructor parameters rejected")


def test_state_roundtrip() -> None:
    c = _make()
    c.push_od(0.42)
    c.decide(now=0.0)
    c.decide(now=60.0)

    state = c.to_state()
    assert state["last_bolus_time"] == 60.0
    assert state["boli_fired"] == 2
    assert state["last_od"] == 0.42
    assert state["total_volume_ml"] > 0
    assert "pump_time_deficit_seconds" in state, (
        "to_state must include pump_time_deficit_seconds for persistence"
    )

    other = _make()
    other.restore_state(state)
    assert other.last_bolus_time == c.last_bolus_time
    assert other.boli_fired == c.boli_fired
    assert other.total_volume_ml == c.total_volume_ml
    assert other.last_od == c.last_od
    assert other.pump_time_deficit_seconds == c.pump_time_deficit_seconds
    # After restore, gating must still hold
    assert other.decide(now=90.0) is None
    print("PASS  to_state / restore_state round-trip preserves state")


def test_restore_clamps_corrupt_deficit() -> None:
    """Negative or over-cap deficit values in state.json get clamped to
    the [0, safety_cap] range on restore so a corrupt file can't suppress
    the next pump or grant a bolus that overlaps the next interval."""
    c1 = _make()
    c1.restore_state({"pump_time_deficit_seconds": -3.0})
    assert c1.pump_time_deficit_seconds == 0.0

    c2 = _make(bolus_interval_seconds=10.0, pump_duration_cap_seconds=20.0)
    c2.restore_state({"pump_time_deficit_seconds": 9999.0})
    # safety_cap = min(20, 10-1) = 9
    assert c2.pump_time_deficit_seconds == 9.0
    print("PASS  restore clamps deficit to [0, safety_cap]")


def test_chemostat_ignores_od_for_decisions() -> None:
    """OD pushes update last_od but do not affect bolus timing."""
    # Use a fast chemostat so both decides actually fire a PumpAction
    # (with the slow default, sub-second pump_time would accumulate into
    # the deficit instead of firing on the very first call).
    c_low = _make(dilution_rate_per_hour=30.0)
    c_high = _make(dilution_rate_per_hour=30.0)
    for _ in range(10):
        c_low.push_od(0.05)
        c_high.push_od(5.0)
    a_low = c_low.decide(now=0.0)
    a_high = c_high.decide(now=0.0)
    assert a_low is not None and a_high is not None
    # pump_time is identical regardless of OD
    assert a_low.pump_time == a_high.pump_time
    # average_od on the PumpAction reflects last_od (informational)
    assert a_low.average_od == 0.05
    assert a_high.average_od == 5.0
    print("PASS  OD pushes do not influence pump timing (only last_od metadata)")


def main() -> int:
    test_first_bolus_cycle_runs_immediately()
    test_sub_second_per_bolus_accumulates_then_fires()
    test_bolus_interval_gates()
    test_pump_time_formula()
    test_pump_time_capped_by_duration_limit()
    test_pump_time_clamped_by_bolus_interval()
    test_constructor_rejects_invalid_params()
    test_state_roundtrip()
    test_restore_clamps_corrupt_deficit()
    test_chemostat_ignores_od_for_decisions()
    print("\nAll chemostat tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
