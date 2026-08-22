#!/usr/bin/env python3
"""Closed-loop verification harness for the chemostat and turbidostat modes.

Companion to CONTROL_MODE_AUDIT.md. Unlike test_turbidostat.py /
test_chemostat.py — which check the controllers' arithmetic against itself —
this drives the REAL controller classes against a simulated culture and asks
whether the resulting OD trajectory and dilution rate are what the user asked
for.

Originally (2026-08-20) this printed CURRENT beside FIXED, where FIXED was a
subclass overriding ``decide()`` with the audit's proposed repairs. Those
subclasses are gone: the repository classes now *are* the fixed ones, so the
harness prints one column and exits non-zero if any check regresses.

``test_control_loop.py`` asserts the same properties under pytest. This script
stays because the numbers are worth reading, not just passing.

Run from the server/ directory:      python3 verify_control_modes.py
"""

from __future__ import annotations

import math
import random
import sys

from control_modes.chemostat import ChemostatController
from control_modes.turbidostat import TurbidostatController

DT = 10.0                          # sensor loop tick (SPEC §9)
V = 25.0                           # vial working volume, mL
F = 1.0                            # influx flow rate, mL/s
MU = math.log(2) / (1.5 * 3600)    # 90 min doubling, per second


# ----------------------------------------------------------------------
# Simulations
# ----------------------------------------------------------------------

def run_turbidostat(od_lower, od_upper, pump_wait_min, hours=48, od0=0.2):
    c = TurbidostatController(
        0, od_lower=od_lower, od_upper=od_upper,
        pump_wait_seconds=pump_wait_min * 60.0,
        flow_rate_ml_s=F, volume_ml=V, efflux_extra_seconds=5.0,
    )
    od, t, ods, first_fire = od0, 0.0, [], None
    for i in range(int(hours * 3600 / DT)):
        od *= math.exp(MU * DT)
        c.push_od(od)
        a = c.decide(t)
        if a is not None:
            if first_fire is None:
                first_fire = i
            od *= math.exp(-F * a.pump_time / V)
        ods.append(od)
        t += DT
    # Settle from the first dilution rather than a fixed wall-clock offset.
    # The original harness discarded exactly one hour, which for a high band
    # leaves the inoculum still climbing: od0=0.2 at mu=0.462/h is only 0.317
    # after an hour, and that reads as a 9% "breach" of a 0.35 floor the
    # culture has not yet reached. That artifact is what the audit's T-4 row
    # was measuring.
    settled = ods if first_fire is None else ods[first_fire:]
    return min(settled), max(settled)


def run_chemostat(hours=12, overrun=0.0, jitter=0.0, dropout=0.0,
                  D=0.5, interval=DT, seed=3):
    rng = random.Random(seed)
    c = ChemostatController(
        0, dilution_rate_per_hour=D, bolus_interval_seconds=interval,
        volume_ml=V, flow_rate_ml_s=F,
    )
    t, delivered, end = 0.0, 0.0, hours * 3600
    while t < end:
        period = max(interval, interval + overrun + rng.uniform(-jitter, jitter))
        if rng.random() >= dropout:          # dropout => decide() never called
            a = c.decide(t)
            if a is not None:
                delivered += a.pump_time * F
        t += period
    return delivered / V / hours, c.total_volume_ml, delivered


# ----------------------------------------------------------------------

def check_band_adherence() -> bool:
    print("T-1/T-2/T-3/T-4  Does the turbidostat keep OD inside its band?")
    print("    'breach' = how far below od_lower the culture was driven;")
    print("    0% means the floor was respected.")
    print(f"  {'band':<16}{'wait':>9}{'floor':>10}{'breach':>9}{'ceiling':>10}")
    ok = True
    for lo, hi in ((0.35, 0.40), (0.30, 0.40), (0.20, 0.40), (0.20, 0.60)):
        for pw in (15, 30):
            floor, ceiling = run_turbidostat(lo, hi, pw)
            breach = max(0.0, 100 * (lo - floor) / lo)
            flag = "" if breach <= 3 else "  <-- floor breached"
            if breach > 3:
                ok = False
            band = f"[{lo:.2f}, {hi:.2f}]"
            print(f"  {band:<16}{pw:>5} min{floor:>10.3f}{breach:>8.0f}%"
                  f"{ceiling:>10.3f}{flag}")
    return ok


def check_chemostat_rate() -> bool:
    print("\nC-1/C-2  Does the chemostat deliver the requested dilution rate?")
    print(f"  {'condition':<34}{'D':>9}{'err':>9}")
    ok = True
    cases = (("ideal 10.000 s period", {}),
             ("jitter +/-0.5 s", {"jitter": 0.5}),
             ("loop work overrun +2 s", {"overrun": 2.0}),
             ("loop work overrun +5 s", {"overrun": 5.0}),
             ("10% dropped OD samples", {"dropout": 0.10}),
             ("30% dropped OD samples", {"dropout": 0.30}))
    for label, kw in cases:
        got, _, _ = run_chemostat(**kw)
        err = 100 * (got - 0.5) / 0.5
        flag = "" if abs(err) <= 3 else "   <-- off spec"
        if abs(err) > 3:
            ok = False
        print(f"  {label:<34}{got:>9.3f}{err:>8.1f}%{flag}")
    return ok


def check_silent_no_dilution() -> bool:
    print("\nC-3      Is a bolus interval too short to dilute rejected?")
    ok = True
    for interval in (1.0, 1.5, 2.0, 3.0):
        try:
            D_act, booked, delivered = run_chemostat(hours=2, interval=interval)
        except ValueError as exc:
            print(f"  bolus_interval={interval:<5}  rejected at construction: "
                  f"{str(exc).split(':')[0]}")
            continue
        flag = ""
        if delivered == 0.0:
            flag = "   <-- ZERO media delivered, booked as full"
            ok = False
        print(f"  bolus_interval={interval:<5}  delivered {delivered:>6.1f} mL   "
              f"booked {booked:>6.1f} mL{flag}")
    return ok


def check_booked_equals_delivered() -> bool:
    print("\nC-4      Does the volume counter match what was actually delivered?")
    _, booked, delivered = run_chemostat(hours=6, D=5.0, interval=600.0)
    drift = booked - delivered
    ok = abs(drift) <= 1.0
    flag = "" if ok else f"   <-- overstates by {drift:.0f} mL"
    print(f"  booked {booked:>7.1f} mL   delivered {delivered:>7.1f} mL{flag}")
    return ok


def main() -> int:
    print(f"Closed-loop control-mode verification "
          f"(mu={MU*3600:.3f}/h, V={V} mL, F={F} mL/s, tick={DT} s)\n")
    results = [check_band_adherence(), check_chemostat_rate(),
               check_silent_no_dilution(), check_booked_equals_delivered()]
    failed = results.count(False)
    print(f"\n{'-'*72}")
    if failed:
        print(f"{failed} of {len(results)} checks FAILED against the current code.")
        print("See CONTROL_MODE_AUDIT.md for the analysis.")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
