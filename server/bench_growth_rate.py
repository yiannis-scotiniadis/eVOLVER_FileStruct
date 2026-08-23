"""server/bench_growth_rate.py — is the §17 growth service affordable on THIS box?

The growth recompute runs on the sensor-loop thread, inside the engine lock, so
its cost is stolen directly from the 10 s tick. That is fine on a developer
laptop and emphatically not obviously fine on the pre-2016 Raspberry Pi this
suite actually ships to, which is a single-core ARM1176 at 700 MHz (Pi 1
Model B / B+) or a quad Cortex-A7 at 900 MHz (Pi 2 Model B).

Run it ON THE PI, not on a laptop::

    python3 server/bench_growth_rate.py

It reports three things:

1. A **CPython scalar calibration** — how fast this box runs the kind of
   float arithmetic the estimator is made of. Divide the developer figure by
   this box's to get the honest scaling factor, instead of guessing one.
2. **Per-vial and per-tick cost** at the worst realistic history (a full 3 h
   window at 10 s cadence, segmented by a 15 min refractory gate).
3. A **verdict** against the tick budget, including the staggering the engine
   applies (it recomputes a slice of the vials per tick rather than all
   sixteen at once).

No repo imports beyond `growth_rate` itself, so it runs on a bare checkout.
"""

from __future__ import annotations

import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import growth_rate as g  # noqa: E402

N_VIALS = 16
TICK_BUDGET_S = 10.0          # SPEC §9 sensor loop period
SEED = 1

# Measured on the development machine (x86-64, CPython 3.11) so a Pi run has
# something to divide by. Update if the reference machine changes.
DEV_CALIBRATION_OPS_PER_S = None   # filled in by --record-dev


def calibration_loop(n: int = 200_000) -> float:
    """Scalar float ops per second, in the shape the estimator uses.

    Deliberately not a general benchmark: it is the OLS inner loop, so it
    tracks the thing being predicted rather than something correlated with it.
    """
    xs = [float(i) for i in range(200)]
    ys = [math.log(0.2 + 0.001 * i) for i in range(200)]
    t0 = time.perf_counter()
    reps = max(1, n // 200)
    acc = 0.0
    for _ in range(reps):
        sx = sy = sxx = sxy = 0.0
        for x, y in zip(xs, ys):
            sx += x
            sy += y
            sxx += x * x
            sxy += x * y
        acc += sxy
    dt = time.perf_counter() - t0
    return (reps * 200) / dt if dt > 0 else float("inf")


def worst_case_history(mu: float = 1.2, hours: float = 3.0,
                       pump_wait_s: float = 900.0):
    """A full retained window: 3 h at 10 s cadence, dilutions every 15 min.

    This is the most expensive shape the engine can hand the estimator —
    maximum samples, maximum segments, every segment long enough to fit.
    """
    rng = random.Random(SEED)
    samples, events = [], []
    od, t, last = 0.25, 0.0, -1e9
    while t < hours * 3600:
        od *= math.exp(mu * 10.0 / 3600.0)
        samples.append((t, od + rng.gauss(0, 0.004)))
        if od > 0.4 and (t - last) >= pump_wait_s:
            secs = float(int(min(math.log(od / 0.2) * 25.0, 20.0)))
            od *= math.exp(-secs / 25.0)
            events.append(g.DilutionEvent(t, t + secs + 5.0, secs))
            last = t
        t += 10.0
    return samples, events, t


def time_one_vial(samples, events, now, reps: int = 20) -> float:
    t0 = time.perf_counter()
    for _ in range(reps):
        g.estimate(
            samples=samples, now=now, dilution_events=events,
            mode="turbidostat", volume_ml=25.0, od_range=(0.1, 0.9),
            samples_seen=len(samples),
        )
    return (time.perf_counter() - t0) / reps


def main() -> int:
    print("=" * 70)
    print("growth-rate service cost on this machine")
    print("=" * 70)
    print(f"  python {sys.version.split()[0]}  on  {sys.platform}")
    try:
        info = Path("/proc/cpuinfo").read_text(encoding="utf-8")
        for key in ("Model", "model name", "Hardware", "Revision"):
            for line in info.splitlines():
                if line.startswith(key):
                    print(f"  {line.strip()}")
                    break
    except Exception:
        pass

    ops = calibration_loop()
    print(f"\n  scalar OLS throughput: {ops / 1e6:.2f} M ops/s")

    samples, events, now = worst_case_history()
    segs = g.split_segments([a for a, _ in samples], [b for _, b in samples],
                            events)
    print(f"  worst-case history:    {len(samples)} samples, "
          f"{len(events)} dilutions, {len(segs)} segments")

    per_vial = time_one_vial(samples, events, now)
    all_vials = per_vial * N_VIALS
    per_tick = all_vials / g.VIALS_PER_RECOMPUTE_GROUP_DIVISOR

    print(f"\n  per vial:              {per_vial * 1000:8.1f} ms")
    print(f"  all {N_VIALS} vials at once:  {all_vials * 1000:8.1f} ms")
    print(f"  per tick (staggered):  {per_tick * 1000:8.1f} ms   "
          f"({100 * per_tick / TICK_BUDGET_S:.1f} % of the {TICK_BUDGET_S:.0f} s budget)")

    print()
    if per_tick > TICK_BUDGET_S * 0.5:
        print("  VERDICT: TOO EXPENSIVE. The recompute would eat more than half")
        print("  the sensor tick. Raise growth_rate.RECOMPUTE_INTERVAL_SECONDS,")
        print("  shorten HISTORY_WINDOW_SECONDS, or lower MAX_WINDOW_CANDIDATES.")
        return 1
    if per_tick > TICK_BUDGET_S * 0.15:
        print("  VERDICT: TIGHT. It fits, but it is a visible share of the tick.")
        print("  Watch for the loop period drifting above 10 s under load.")
        return 0
    print("  VERDICT: comfortable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
