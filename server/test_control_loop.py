"""Closed-loop control-mode tests (CONTROL_MODE_AUDIT.md, "Tests the fixes need").

``test_turbidostat.py`` and ``test_chemostat.py`` check each controller's
arithmetic against itself. These tests drive the real controller classes
against a simulated culture and ask a different question: **is the OD
trajectory, and the dilution rate, what the user actually asked for?**

That distinction is the whole of audit finding T-5. The pre-fix suite passed
in full while the turbidostat breached its OD floor by up to 47 % and the
chemostat under-delivered by up to 34 %, because
``test_deficit_preserves_total_dilution`` asserted the windup was conserved --
the assertion *was* the bug.

Culture model: ``od *= exp(mu * dt)`` between ticks, ``od *= exp(-F * t / V)``
on each PumpAction. That washout model is the exact inverse of the formula the
turbidostat uses, and it is the right one because ``_execute_pump_actions``
fires influx and efflux concurrently on separate pump bits.

Run from the project root::

    python -m pytest server/test_control_loop.py
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from control_modes.chemostat import ChemostatController  # noqa: E402
from control_modes.turbidostat import TurbidostatController  # noqa: E402
from experiment_engine import validate_control_parameters  # noqa: E402

DT = 10.0                          # sensor loop tick (SPEC §9)
V = 25.0                           # vial working volume, mL
F = 1.0                            # influx flow rate, mL/s
MU = math.log(2) / (1.5 * 3600)    # 90 min doubling, per second


# ----------------------------------------------------------------------
# Simulations
# ----------------------------------------------------------------------

def run_turbidostat(od_lower, od_upper, pump_wait_min, hours=48, od0=0.2):
    """Return (min_od, max_od, fires) over the settled portion of the run.

    ``fires`` is a list of ``(sizing_od, pump_time)`` so a caller can check
    bolus proportionality without re-deriving the controller's state.
    """
    c = TurbidostatController(
        0, od_lower=od_lower, od_upper=od_upper,
        pump_wait_seconds=pump_wait_min * 60.0,
        flow_rate_ml_s=F, volume_ml=V, efflux_extra_seconds=5.0,
    )
    od, t, ods, fires = od0, 0.0, [], []
    first_fire = None
    for i in range(int(hours * 3600 / DT)):
        od *= math.exp(MU * DT)
        c.push_od(od)
        a = c.decide(t)
        if a is not None:
            if first_fire is None:
                first_fire = i
            fires.append((a.sizing_od, a.pump_time))
            od *= math.exp(-F * a.pump_time / V)
        ods.append(od)
        t += DT
    # Settle from the first dilution, not from a fixed wall-clock offset.
    # A fixed offset (the audit harness discarded exactly one hour) leaves
    # the inoculum still climbing toward a high band: od0=0.2 at mu=0.462/h
    # is only 0.317 after an hour, which then reads as a 9% "breach" of a
    # 0.35 floor the culture has not yet reached. That artifact is what the
    # audit attributed to T-4.
    if first_fire is None:
        return min(ods), max(ods), fires
    settled = ods[first_fire:]
    return min(settled), max(settled), fires


def run_chemostat(hours=12, overrun=0.0, jitter=0.0, dropout=0.0,
                  D=0.5, interval=DT, seed=3, push_od=None):
    """Return (delivered_D, booked_ml, delivered_ml).

    ``overrun`` models the sensor loop's real period: ``app.py`` sleeps
    ``interval - work``, so the period is ``max(interval, work)`` and can only
    ever run slower than nominal. ``dropout`` models a cycle on which the
    engine never reaches ``decide()`` at all.
    """
    rng = random.Random(seed)
    c = ChemostatController(
        0, dilution_rate_per_hour=D, bolus_interval_seconds=interval,
        volume_ml=V, flow_rate_ml_s=F,
    )
    t, delivered, end = 0.0, 0.0, hours * 3600
    while t < end:
        period = max(interval, interval + overrun + rng.uniform(-jitter, jitter))
        if rng.random() >= dropout:          # dropout => decide() never called
            if push_od is not None:
                c.push_od(push_od)
            a = c.decide(t)
            if a is not None:
                delivered += a.pump_time * F
        t += period
    return delivered / V / hours, c.total_volume_ml, delivered


# ----------------------------------------------------------------------
# 1. Band adherence  (T-1, T-2, T-3, T-4)
# ----------------------------------------------------------------------

def test_turbidostat_holds_its_band() -> None:
    """The audit's headline check. With the accumulator in place the floor
    was breached by 10-47 % depending on band width; a wide band masked it
    entirely because the required bolus already exceeded the 20 s cap, so
    capped and wound-up behaviour coincided."""
    for lo, hi in ((0.35, 0.40), (0.30, 0.40), (0.20, 0.40), (0.20, 0.60)):
        for pw in (15, 30):
            floor, ceiling, fires = run_turbidostat(lo, hi, pw)
            assert fires, f"band [{lo}, {hi}] @ {pw} min never diluted"
            assert floor >= lo * 0.97, (
                f"band [{lo}, {hi}] @ {pw} min: OD driven to {floor:.4f}, "
                f"{100 * (lo - floor) / lo:.0f}% below the {lo} floor"
            )
            # The ceiling is only meaningful where the refractory window is
            # short relative to the time the culture needs to traverse the
            # band. Where it is not, overshoot is the pump_wait the operator
            # asked for, not a controller defect -- so assert it only there.
            traverse_s = math.log(hi / lo) / MU
            if pw * 60.0 < 0.5 * traverse_s:
                assert ceiling <= hi * 1.03, (
                    f"band [{lo}, {hi}] @ {pw} min: OD reached {ceiling:.4f} "
                    f"above the {hi} ceiling with pump_wait well inside the "
                    f"{traverse_s / 60:.0f} min band traverse"
                )


def test_turbidostat_bolus_is_proportional_not_wound_up() -> None:
    """Model-independent windup check: every fired bolus must match the
    formula for the OD it was sized from, to within the whole-second
    truncation. A wound-up accumulator shows up here as a bolus larger than
    the current OD can justify."""
    for lo, hi in ((0.35, 0.40), (0.30, 0.40), (0.20, 0.40)):
        _, _, fires = run_turbidostat(lo, hi, 15)
        for sizing_od, pump_time in fires:
            wanted = min(math.log(sizing_od / lo) * V / F, 20.0)
            assert pump_time <= wanted + 1e-9, (
                f"band [{lo}, {hi}]: fired {pump_time}s where OD "
                f"{sizing_od:.4f} justifies at most {wanted:.3f}s -- windup"
            )
            assert wanted - pump_time < 1.0, (
                f"band [{lo}, {hi}]: fired {pump_time}s against a needed "
                f"{wanted:.3f}s -- more than one second of truncation loss"
            )


# ----------------------------------------------------------------------
# 2. Chemostat rate under adverse timing  (C-1, C-2)
# ----------------------------------------------------------------------

def test_chemostat_rate_survives_loop_overrun() -> None:
    """C-1. The sensor loop's real period is max(10 s, work), so sizing each
    bolus from the nominal interval biases D negative -- systematically, and
    only ever in that direction."""
    for label, kwargs in (
        ("ideal 10 s period", {}),
        ("jitter +/-0.5 s", {"jitter": 0.5}),
        ("overrun +2 s", {"overrun": 2.0}),
        ("overrun +5 s", {"overrun": 5.0}),
        ("12 s tick", {"interval": 12.0}),
        ("15 s tick", {"interval": 15.0}),
    ):
        delivered_d, _, _ = run_chemostat(D=0.5, **kwargs)
        err = abs(delivered_d - 0.5) / 0.5
        assert err <= 0.02, (
            f"{label}: delivered D={delivered_d:.4f}, "
            f"{100 * err:.1f}% off the requested 0.500"
        )


def test_chemostat_rate_survives_dropped_cycles() -> None:
    """C-2. A dropped cycle costs nothing once boli are sized from elapsed
    time: the next one covers the gap. Before the fix, 30 % dropped samples
    cost 29 % of the dilution rate."""
    for dropout in (0.10, 0.30):
        delivered_d, _, _ = run_chemostat(D=0.5, dropout=dropout, hours=48)
        err = abs(delivered_d - 0.5) / 0.5
        assert err <= 0.02, (
            f"{100 * dropout:.0f}% dropped cycles: delivered D="
            f"{delivered_d:.4f}, {100 * err:.1f}% off the requested 0.500"
        )


def test_chemostat_dilutes_through_a_dead_od_sensor() -> None:
    """The sharpest form of C-2. A chemostat is open-loop; OD reading
    out of range means the culture is DENSER than the calibration covers,
    i.e. exactly when dilution must not stop."""
    c = ChemostatController(
        0, dilution_rate_per_hour=30.0, bolus_interval_seconds=60.0,
        volume_ml=V, flow_rate_ml_s=F,
    )
    assert c.requires_od is False, (
        "an open-loop mode must not declare an OD dependency -- the engine "
        "gates on this flag"
    )
    c.push_od(float("nan"))
    assert c.decide(now=0.0) is not None
    assert c.decide(now=60.0) is not None


# ----------------------------------------------------------------------
# 3. Delivered equals booked  (C-3, C-4)
# ----------------------------------------------------------------------

def test_chemostat_books_what_it_delivered() -> None:
    """C-4. In a cap-binding config the pre-fix counter recorded 750 mL
    against 720 mL actually pumped -- implying D=5.00 where the truth was
    4.80."""
    _, booked, delivered = run_chemostat(hours=6, D=5.0, interval=600.0)
    assert abs(booked - delivered) < 1e-6, (
        f"booked {booked:.1f} mL against {delivered:.1f} mL delivered"
    )
    assert delivered > 0.0


def test_short_bolus_interval_is_rejected_not_silently_dead() -> None:
    """C-3. Below a 2 s interval the safety cap falls under the firmware's
    1 s resolution, so `int(deficit) >= 1` was never true: zero media
    delivered, no warning, and the full volume still booked."""
    for interval in (1.0, 1.5):
        try:
            ChemostatController(
                0, dilution_rate_per_hour=0.5,
                bolus_interval_seconds=interval,
                volume_ml=V, flow_rate_ml_s=F,
            )
        except ValueError:
            continue
        raise AssertionError(
            f"bolus_interval_seconds={interval} accepted; it delivers nothing"
        )
    # And a workable interval still runs.
    _, _, delivered = run_chemostat(hours=2, interval=3.0)
    assert delivered > 0.0


# ----------------------------------------------------------------------
# 4. Create-time control-parameter validation
# ----------------------------------------------------------------------

_RATES_32 = [1.0] * 32


def test_validation_rejects_an_undilutable_band() -> None:
    """The precondition that makes dropping the accumulator safe: a band too
    narrow to ever call for a whole second of pumping must fail at creation,
    not run for a week delivering nothing."""
    try:
        validate_control_parameters(
            "turbidostat",
            {"od_lower_thresh": 0.200, "od_upper_thresh": 0.203,
             "volume_ml": 25.0},
            _RATES_32, [0],
        )
    except ValueError as exc:
        assert "too narrow" in str(exc), exc
        assert "0.20" in str(exc), "the error must name a workable threshold"
    else:
        raise AssertionError("a sub-second band was accepted")


def test_validation_accepts_a_normal_band() -> None:
    warnings = validate_control_parameters(
        "turbidostat",
        {"od_lower_thresh": 0.2, "od_upper_thresh": 0.4, "volume_ml": 25.0,
         "efflux_extra_seconds": 5.0},
        _RATES_32, [0, 1, 2],
    )
    assert warnings == [], warnings


def test_validation_rejects_a_sub_two_second_bolus_interval() -> None:
    try:
        validate_control_parameters(
            "chemostat",
            {"dilution_rate_per_hour": 0.5, "bolus_interval_seconds": 1.5},
            _RATES_32, [0],
        )
    except ValueError as exc:
        assert "bolus_interval_seconds" in str(exc), exc
    else:
        raise AssertionError("a 1.5 s bolus interval was accepted")


def test_validation_warns_when_the_duration_cap_binds() -> None:
    """Runs, but cannot reach the requested D. That has to be said out loud
    -- it is the difference between D=5.00 and D=4.80 in the record."""
    warnings = validate_control_parameters(
        "chemostat",
        {"dilution_rate_per_hour": 5.0, "bolus_interval_seconds": 600.0,
         "volume_ml": 25.0, "efflux_extra_seconds": 5.0},
        _RATES_32, [0],
    )
    assert any("clipped" in w for w in warnings), warnings


def test_validation_warns_when_efflux_overrun_is_disabled() -> None:
    """X-1. efflux_extra_seconds=0 disengages the only volume-regulation
    loop the machine has, and no sensor can detect the drift."""
    warnings = validate_control_parameters(
        "turbidostat",
        {"od_lower_thresh": 0.2, "od_upper_thresh": 0.4, "volume_ml": 25.0,
         "efflux_extra_seconds": 0.0},
        _RATES_32, [0],
    )
    assert any("efflux_extra_seconds" in w for w in warnings), warnings


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll closed-loop control tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
