"""Would slowing the OD lane change the reported growth rate?

Written when the sensor loop gained a decimated OD lane (app.py
OD_EVERY_N_TICKS). The growth-rate estimator's span constants were measured at
the 10 s OD cadence (GROWTH_RATE_METHOD.md §2 #6), and a 6x sparser series is a
real change to a regression's variance, so "does accuracy survive" needed an
answer against ground truth rather than an argument.

    python3 server/verify_cadence_growth.py

Standalone; numpy not required. Drives the REAL TurbidostatController and the
REAL estimator, so what it measures is the shipped path.

**The OD lane is NOT currently decimated** -- app.py reads OD on the same 10 s
tick as temperature, and `current` below is the shipped configuration. This
script is kept as the record of what decimating it would cost, and of which
constants have to move if it is decimated again.

Three configurations, because two questions are tangled together -- what the
cadence does, and what the constant changes do about it:

    current  dt=10 s, MIN_FIT_SPAN=600, PREFERRED=1800, MIN_SAMPLES=30, hist 3 h
    naive    dt=60 s, the SAME constants  (the cadence change alone)
    tuned    dt=60 s, MIN_FIT_SPAN=600, PREFERRED=3600, MIN_SAMPLES=10, hist 6 h

`naive` is the interesting row: it is what ships if the constants are left
alone when the cadence is slowed.
"""
from __future__ import annotations

import math
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import growth_rate as g                                        # noqa: E402
from control_modes.turbidostat import TurbidostatController    # noqa: E402

SEED = 11
V = 25.0           # vial working volume, mL
F = 1.0            # influx flow rate, mL/s

# Stirred-vial OD noise, the level GROWTH_RATE_METHOD.md's tables use and the
# level verify_growth_rate.py simulates at.
OD_NOISE = 0.004
# What a settled (stir-stopped) read should look like. The 60 s lane exists to
# make room for that, so it is the operating point the change is aiming at --
# reported separately rather than folded into the headline numbers, because
# nothing has measured it on the bench yet.
OD_NOISE_SETTLED = 0.002


class Config:
    def __init__(self, name, dt, min_span, preferred, min_samples, history_h):
        self.name = name
        self.dt = dt
        self.min_span = min_span
        self.preferred = preferred
        self.min_samples = min_samples
        self.history_s = history_h * 3600.0

    def __enter__(self):
        self._saved = (
            g.MIN_FIT_SPAN_SECONDS,
            g.PREFERRED_FIT_SPAN_SECONDS,
            g.MIN_SAMPLES,
        )
        g.MIN_FIT_SPAN_SECONDS = self.min_span
        g.PREFERRED_FIT_SPAN_SECONDS = self.preferred
        g.MIN_SAMPLES = self.min_samples
        return self

    def __exit__(self, *exc):
        (g.MIN_FIT_SPAN_SECONDS,
         g.PREFERRED_FIT_SPAN_SECONDS,
         g.MIN_SAMPLES) = self._saved
        return False


BEFORE = Config("current (10 s, shipped constants)", 10.0, 600.0, 1800.0, 30, 3)
NAIVE = Config("naive   (60 s, same constants)", 60.0, 600.0, 1800.0, 30, 3)
AFTER = Config("tuned   (60 s, re-derived)", 60.0, 600.0, 3600.0, 10, 6)


def simulate_turbidostat(mu, dt, hours=8.0, od0=0.25, lo=0.2, hi=0.6,
                         pump_wait_min=15.0, od_noise=OD_NOISE, seed=SEED):
    """Drive the REAL TurbidostatController against an exponential culture.

    Same culture model as test_control_loop.py and verify_growth_rate.py:
    ``od *= exp(mu*dt)`` between ticks and ``od *= exp(-F*t/V)`` on each
    PumpAction. The controller is stepped at `dt`, so a slower lane genuinely
    changes when dilutions land -- which is the point: this measures the whole
    closed loop at each cadence, not just a resampled series.
    """
    rng = random.Random(seed)
    c = TurbidostatController(
        0, od_lower=lo, od_upper=hi,
        pump_wait_seconds=pump_wait_min * 60.0,
        flow_rate_ml_s=F, volume_ml=V, efflux_extra_seconds=5.0,
    )
    od, t = od0, 0.0
    samples, events = [], []
    for _ in range(int(hours * 3600 / dt)):
        od *= math.exp(mu * dt / 3600.0)
        samples.append((t, od + rng.gauss(0.0, od_noise)))
        c.push_od(od)
        a = c.decide(t)
        if a is not None:
            od *= math.exp(-F * a.pump_time / V)
            events.append(g.DilutionEvent(t, t + a.efflux_seconds, a.pump_time * F))
        t += dt
    return samples, events, t


def report_mu(cfg, samples, events, now, lo, hi, mode="turbidostat", D=None):
    rep = g.estimate(
        samples=samples, now=now, dilution_events=events,
        mode=mode, volume_ml=V,
        od_range=(lo * 0.5, hi * 1.5),
        dilution_rate_per_hour=D,
        samples_seen=len(samples),
        history_window_seconds=cfg.history_s,
    )
    return rep.growth


def run_turbidostat(out, od_noise, seeds=(11, 12, 13, 14, 15)):
    lo, hi = 0.2, 0.6
    out(f"{'config':<34} {'mu true':>8} {'reported':>9} {'err %':>8} "
        f"{'sd %':>7} {'R2':>7} {'n':>5} {'flags'}")
    summary = {}
    for cfg in (BEFORE, NAIVE, AFTER):
        errs_all = []
        for mu in (0.35, 0.70, 1.20):
            errs, r2s, npts, flagset, reported = [], [], [], set(), []
            with cfg:
                for sd in seeds:
                    samples, events, now = simulate_turbidostat(
                        mu, cfg.dt, lo=lo, hi=hi, od_noise=od_noise, seed=sd,
                    )
                    est = report_mu(cfg, samples, events, now, lo, hi)
                    flagset.update(est.flags or ())
                    if est.mu_per_hour is None:
                        continue
                    reported.append(est.mu_per_hour)
                    errs.append(100.0 * (est.mu_per_hour - mu) / mu)
                    if est.r_squared is not None:
                        r2s.append(est.r_squared)
                    npts.append(est.n_points)
            if not errs:
                out(f"{cfg.name:<34} {mu:8.2f} {'NO ESTIMATE':>9} "
                    f"{'-':>8} {'-':>7} {'-':>7} {'-':>5} "
                    f"{','.join(sorted(flagset)) or '-'}")
                errs_all.append(float('nan'))
                continue
            sd_pct = statistics.stdev(errs) if len(errs) > 1 else 0.0
            errs_all.extend(errs)
            out(f"{cfg.name:<34} {mu:8.2f} "
                f"{statistics.mean(reported):9.4f} "
                f"{statistics.mean(errs):+8.2f} {sd_pct:7.2f} "
                f"{(statistics.mean(r2s) if r2s else float('nan')):7.4f} "
                f"{int(statistics.mean(npts)):5d} "
                f"{','.join(sorted(flagset)) or '-'}")
        finite = [e for e in errs_all if e == e]
        summary[cfg.name] = (
            statistics.mean(finite) if finite else float('nan'),
            statistics.mean([abs(e) for e in finite]) if finite else float('nan'),
        )
        out("")
    return summary


def run_batch(out, od_noise, seeds=(11, 12, 13)):
    """Regime A. Inoculation to the first dilution -- a plain exponential rise
    with no segmentation, which is where a sparser series has the least to
    hide behind."""
    out(f"{'config':<34} {'mu true':>8} {'reported':>9} {'err %':>8} "
        f"{'sd %':>7} {'R2':>7}")
    for cfg in (BEFORE, NAIVE, AFTER):
        for mu in (0.35, 0.70, 1.20):
            errs, r2s, reported = [], [], []
            with cfg:
                for sd in seeds:
                    rng = random.Random(sd)
                    n = int(6.0 * 3600 / cfg.dt)
                    samples = []
                    od = 0.05
                    for i in range(n):
                        t = i * cfg.dt
                        od = 0.05 * math.exp(mu * t / 3600.0)
                        if od > 1.4:
                            break
                        samples.append((t, od + rng.gauss(0.0, od_noise)))
                    now = samples[-1][0]
                    est = report_mu(cfg, samples, [], now, 0.1, 1.0)
                    if est.mu_per_hour is None:
                        continue
                    reported.append(est.mu_per_hour)
                    errs.append(100.0 * (est.mu_per_hour - mu) / mu)
                    if est.r_squared is not None:
                        r2s.append(est.r_squared)
            if not errs:
                out(f"{cfg.name:<34} {mu:8.2f} {'NO ESTIMATE':>9}")
                continue
            sd_pct = statistics.stdev(errs) if len(errs) > 1 else 0.0
            out(f"{cfg.name:<34} {mu:8.2f} {statistics.mean(reported):9.4f} "
                f"{statistics.mean(errs):+8.2f} {sd_pct:7.2f} "
                f"{(statistics.mean(r2s) if r2s else float('nan')):7.4f}")
        out("")


def main() -> int:
    lines: list[str] = []

    def out(s=""):
        print(s)
        lines.append(s)

    out("=" * 84)
    out("DOES THE 60 s OD LANE CHANGE THE REPORTED GROWTH RATE?")
    out("=" * 84)
    out("Real TurbidostatController + real growth_rate.estimate, 8 h runs,")
    out("band 0.2-0.6, pump_wait 15 min, 5 seeds per mu.")
    out("'err' is bias (mean signed error); 'sd' is run-to-run spread.")

    out("")
    out("-" * 84)
    out(f"1. TURBIDOSTAT (segmented), stirred-vial OD noise sd={OD_NOISE}")
    out("-" * 84)
    summary = run_turbidostat(out, OD_NOISE)

    out("-" * 84)
    out(f"2. TURBIDOSTAT (segmented), settled-read OD noise sd={OD_NOISE_SETTLED}")
    out("   The operating point stopping the stirrers is meant to reach.")
    out("-" * 84)
    summary_quiet = run_turbidostat(out, OD_NOISE_SETTLED)

    out("-" * 84)
    out(f"3. BATCH / startup regime, OD noise sd={OD_NOISE}")
    out("-" * 84)
    run_batch(out, OD_NOISE)

    out("=" * 84)
    out("SUMMARY  (mean signed bias / mean absolute error, pooled over mu)")
    out("=" * 84)
    out(f"{'config':<34} {'stirred noise':>22} {'settled noise':>22}")
    for name in summary:
        b1, a1 = summary[name]
        b2, a2 = summary_quiet[name]
        out(f"{name:<34} {b1:+9.2f}% / {a1:6.2f}%   {b2:+9.2f}% / {a2:6.2f}%")
    out("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
