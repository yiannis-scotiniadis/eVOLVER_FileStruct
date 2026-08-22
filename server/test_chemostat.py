"""Verification script for the ChemostatController (SPEC §9 algorithm).

Run from the project root::

    python server/test_chemostat.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from control_modes.chemostat import (  # noqa: E402
    MAX_CATCHUP_INTERVALS,
    ChemostatController,
)
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
    params (D=0.5/h, V=25, T=60, F=1) the per-bolus pump_time is ~0.208 s --
    sub-second, so the deficit accumulator absorbs it and no PumpAction
    fires on the first cycle. last_bolus_time and bolus_cycles still advance
    because the bolus *cycle* completed; boli_fired does not, because
    nothing was delivered."""
    c = _make()
    per_bolus = 0.5 * 25.0 * 60.0 / 3600.0  # 0.20833...
    action = c.decide(now=0.0)
    assert action is None, (
        f"sub-second per-bolus pump_time ({per_bolus:.4f}s) should accumulate "
        f"in the deficit, not produce a PumpAction; got {action}"
    )
    assert c.bolus_cycles == 1, c.bolus_cycles
    assert c.boli_fired == 0, "nothing was delivered, so nothing may be counted"
    assert c.total_volume_ml == 0.0, "delivered volume must stay at zero"
    assert abs(c.total_volume_intended_ml - per_bolus) < 1e-9
    assert c.last_bolus_time == 0.0
    assert abs(c.pump_time_deficit_seconds - per_bolus) < 1e-9
    print(
        f"PASS  first bolus cycle runs immediately, sub-second pump_time "
        f"({per_bolus:.4f}s) deferred into deficit"
    )


def test_sub_second_per_bolus_accumulates_then_fires() -> None:
    """A slow chemostat with sub-second per-bolus pump_time should fire a
    whole-second pump every ~ceil(1 / per_bolus) bolus cycles. The
    accumulator is CORRECT here: what it accumulates is a per-interval
    increment of prescribed dilution, not an absolute setpoint error."""
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
    assert c.bolus_cycles == 20, "bolus_cycles tracks cycles"
    assert c.boli_fired == len(fires_at), "boli_fired tracks actual deliveries"
    # Delivered volume must equal fired seconds x flow rate, exactly.
    assert abs(c.total_volume_ml - len(fires_at) * 1.0) < 1e-9
    print(
        f"PASS  20 sub-second bolus cycles yield {len(fires_at)} whole-s "
        f"fires at cycles {fires_at}; deficit residual "
        f"{c.pump_time_deficit_seconds:.4f}s (prescribed "
        f"{per_bolus * 20:.4f}s)"
    )


def test_bolus_sized_from_elapsed_not_nominal_interval() -> None:
    """C-1, the critical one. app.py sleeps `interval - work`, so the loop's
    true period is max(interval, work): it can only ever run SLOWER than
    nominal. Sizing every bolus from the nominal interval therefore
    under-doses systematically (-17% to -34% D under realistic overrun)."""
    # D=30/h, V=25, F=1 -> 0.2083 mL/s of prescribed dilution.
    c = _make(dilution_rate_per_hour=30.0, bolus_interval_seconds=60.0)
    c.decide(now=0.0)
    # Second cycle arrives 90 s later, not 60 s: a 50% longer period.
    action = c.decide(now=90.0)
    assert action is not None
    expected_ml = 30.0 * 25.0 * 90.0 / 3600.0        # 18.75 mL, from ELAPSED
    nominal_ml = 30.0 * 25.0 * 60.0 / 3600.0         # 12.5 mL, from nominal
    assert abs(c.total_volume_intended_ml - (nominal_ml + expected_ml)) < 1e-9, (
        f"second bolus was sized from the nominal interval "
        f"({nominal_ml} mL) rather than the {90 - 0} s that actually elapsed "
        f"({expected_ml} mL)"
    )
    print(
        f"PASS  bolus sized from elapsed time ({expected_ml:.2f} mL for a 90 s "
        f"period, not {nominal_ml:.2f} mL)"
    )


def test_catchup_bolus_is_clamped() -> None:
    """A resume after an outage must not fire one enormous catch-up bolus.
    Elapsed time is clamped to MAX_CATCHUP_INTERVALS nominal intervals."""
    c = _make(dilution_rate_per_hour=1.0, bolus_interval_seconds=60.0)
    c.decide(now=0.0)
    # 6 hours later (360 intervals). Only 4 may be honoured.
    c.decide(now=21600.0)
    capped_ml = 1.0 * 25.0 * (MAX_CATCHUP_INTERVALS * 60.0) / 3600.0
    first_ml = 1.0 * 25.0 * 60.0 / 3600.0
    assert abs(c.total_volume_intended_ml - (first_ml + capped_ml)) < 1e-9, (
        f"catch-up bolus not clamped: intended {c.total_volume_intended_ml} mL"
    )
    print(
        f"PASS  catch-up clamped to {MAX_CATCHUP_INTERVALS:g} intervals "
        f"({capped_ml:.3f} mL, not 6 h worth)"
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
    assert c.bolus_cycles == 2
    assert c.boli_fired == 2
    # Within next window -> None again
    assert c.decide(now=90.0) is None
    third = c.decide(now=120.0)
    assert third is not None
    assert c.bolus_cycles == 3
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
        dict(bolus_interval_seconds=1.5),   # C-3: below the firmware floor
        dict(start_od=0),
        dict(start_after_seconds=-1),
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
    assert state["bolus_cycles"] == 2
    assert state["last_od"] == 0.42
    assert state["total_volume_intended_ml"] > 0
    assert "pump_time_deficit_seconds" in state, (
        "to_state must include pump_time_deficit_seconds for persistence"
    )

    other = _make()
    other.restore_state(state)
    assert other.last_bolus_time == c.last_bolus_time
    assert other.boli_fired == c.boli_fired
    assert other.bolus_cycles == c.bolus_cycles
    assert other.total_volume_ml == c.total_volume_ml
    assert other.total_volume_intended_ml == c.total_volume_intended_ml
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


def test_delivered_equals_booked_when_cap_binds() -> None:
    """C-4: total_volume_ml must book what was DELIVERED. Booking the
    uncapped intent is how a run records D=5.00 while the truth is 4.80."""
    # D=5/h, V=25, T=600 -> 20.83 s asked per bolus, capped at 20 s.
    c = _make(dilution_rate_per_hour=5.0, volume_ml=25.0,
              bolus_interval_seconds=600.0, flow_rate_ml_s=1.0)
    fired_seconds = 0.0
    for cycle in range(36):                      # 6 h at 600 s
        action = c.decide(now=600.0 * cycle)
        if action is not None:
            fired_seconds += action.pump_time
    assert abs(c.total_volume_ml - fired_seconds * 1.0) < 1e-9, (
        f"booked {c.total_volume_ml} mL against {fired_seconds} s fired"
    )
    assert c.total_volume_intended_ml > c.total_volume_ml, (
        "the cap bound, so intent must exceed delivery and stay visible"
    )
    print(
        f"PASS  booked {c.total_volume_ml:.0f} mL == delivered "
        f"{fired_seconds:.0f} s x 1 mL/s (intended "
        f"{c.total_volume_intended_ml:.0f} mL)"
    )


def test_cap_clip_raises_an_event() -> None:
    """A silently clipped bolus is the whole of C-3/C-4. It must be visible."""
    c = _make(dilution_rate_per_hour=5.0, bolus_interval_seconds=600.0)
    c.decide(now=0.0)
    events = c.pop_events()
    assert any(e["type"] == "bolus_cap_clipped" for e in events), events
    assert c.pop_events() == [], "events must be consumed on read"
    print("PASS  cap clipping raises a bolus_cap_clipped event")


def test_start_gate_holds_until_start_od() -> None:
    """C-5: dilution begins at inoculation density unless a start gate says
    otherwise. At D above the culture's mu that washes the culture out."""
    c = _make(dilution_rate_per_hour=30.0, start_od=0.3)
    assert c.requires_od is True, (
        "an armed start_od gate is a genuine OD precondition"
    )
    for cycle in range(5):
        c.push_od(0.05 + 0.01 * cycle)          # 0.05 .. 0.09, below 0.3
        assert c.decide(now=60.0 * cycle) is None
    assert c.bolus_cycles == 0, "the cadence clock must not run while held"
    assert c.last_bolus_time is None

    c.push_od(0.31)
    action = c.decide(now=600.0)
    assert action is not None, "gate should release once start_od is reached"
    assert c.dilution_started is True
    assert c.requires_od is False, (
        "once released the chemostat is open-loop again -- otherwise C-5 "
        "quietly reintroduces the C-2 defect"
    )
    events = c.pop_events()
    assert any(e["type"] == "start_gate_released" for e in events), events
    print("PASS  start_od gate holds, releases once, then never re-arms")


def test_start_gate_timeout_is_the_escape_hatch() -> None:
    """start_after_seconds is OR'd with start_od so a sleeve whose OD never
    reads cannot hold the run at inoculation density forever."""
    c = _make(dilution_rate_per_hour=30.0, start_od=0.3,
              start_after_seconds=1800.0)
    # No OD ever pushed.
    assert c.decide(now=0.0) is None
    assert c.decide(now=1000.0) is None
    action = c.decide(now=1800.0)
    assert action is not None, "timeout must release the gate without any OD"
    reason = c.pop_events()[0]["reason"]
    assert "waited" in reason, reason
    print(f"PASS  start_after_seconds releases the gate without OD ({reason})")


def test_no_start_gate_by_default() -> None:
    c = _make(dilution_rate_per_hour=30.0)
    assert c.dilution_started is True
    assert c.requires_od is False
    assert c.decide(now=0.0) is not None
    print("PASS  no start gate configured -> dilution begins immediately")


def test_restore_rebaselines_future_timestamp() -> None:
    """X-2: a stale RPi boot clock puts last_bolus_time ahead of wall time,
    and every `now - last_bolus_time` gate then blocks."""
    c = _make(dilution_rate_per_hour=30.0)
    c.restore_state({"last_bolus_time": 1_000_000.0}, now=500.0)
    assert c.last_bolus_time == 500.0
    assert c.decide(now=500.0 + 61.0) is not None
    print("PASS  restore re-baselines a future last_bolus_time to now (X-2)")


def test_restore_migrates_legacy_counters() -> None:
    """Pre-C-4 state.json held cycle counts in boli_fired and intent in
    total_volume_ml. A run in flight must resume without losing either."""
    c = _make()
    c.restore_state({"boli_fired": 40, "total_volume_ml": 250.0})
    assert c.bolus_cycles == 40, "legacy boli_fired seeds the cycle counter"
    assert c.total_volume_intended_ml == 250.0, (
        "legacy total_volume_ml was intent, so it seeds the intent counter"
    )
    print("PASS  legacy counters migrate on restore")


def main() -> int:
    test_first_bolus_cycle_runs_immediately()
    test_sub_second_per_bolus_accumulates_then_fires()
    test_bolus_sized_from_elapsed_not_nominal_interval()
    test_catchup_bolus_is_clamped()
    test_bolus_interval_gates()
    test_pump_time_formula()
    test_pump_time_capped_by_duration_limit()
    test_pump_time_clamped_by_bolus_interval()
    test_constructor_rejects_invalid_params()
    test_state_roundtrip()
    test_restore_clamps_corrupt_deficit()
    test_chemostat_ignores_od_for_decisions()
    test_delivered_equals_booked_when_cap_binds()
    test_cap_clip_raises_an_event()
    test_start_gate_holds_until_start_od()
    test_start_gate_timeout_is_the_escape_hatch()
    test_no_start_gate_by_default()
    test_restore_rebaselines_future_timestamp()
    test_restore_migrates_legacy_counters()
    print("\nAll chemostat tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
