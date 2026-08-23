"""server/verify_growth_rate.py — does `growth_rate.py` recover a known μ?

The role `verify_control_modes.py` plays for `CONTROL_MODE_AUDIT.md`. Distinct
from `verify_growth_rate_method.py`, which validated the *method choice* (why
the band and the 8-sample window are not ported, why segmenting beats a naive
rolling fit). This validates the *implementation* that resulted.

    python server/verify_growth_rate.py --generate   # write the 1x-time datasets
    python server/verify_growth_rate.py              # the full report

Three sections, in decreasing order of how much they can prove:

1. **Synthetic series with known μ.** The real `TurbidostatController` drives
   the dilutions, so segment boundaries are the ones the machine would
   actually produce. This is the accuracy check.
2. **Generated 1x-time mock runs.** The real `ExperimentEngine`, `DataLogger`
   and `MockSerialManager` at `time_multiplier=1`, seeded, driven by an
   injected clock. Ground truth is exact because it comes from the mock's own
   model: `mu_max[v] * growth_rate_factor(T) * (1 - OD/K)`. This checks the
   whole pipeline, on-disk CSV included, not just the estimator.
3. **Replay of the runs already in `experiments/`.** A guard-rail check only —
   see `replay_growth.py`'s docstring for why those runs cannot test accuracy.

Section 2 needs `--generate` to have been run at least once; `experiments/` is
gitignored, so the datasets are not committed.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import growth_rate as g                                        # noqa: E402
import replay_growth                                           # noqa: E402
from control_modes.turbidostat import TurbidostatController    # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPERIMENTS_DIR = PROJECT_ROOT / "experiments"

SEED = 11
DT = 10.0          # sensor loop tick, s (SPEC §9)
V = 25.0           # vial working volume, mL
F = 1.0            # influx flow rate, mL/s
OD_NOISE = 0.004

TURBIDOSTAT_DATASET = "GrowthRateValidation1"
CHEMOSTAT_DATASET = "GrowthRateValidationChemo1"


# ===========================================================================
# Section 1 — synthetic, known μ
# ===========================================================================

def simulate_turbidostat(mu, hours=8.0, od0=0.25, lo=0.2, hi=0.6,
                         pump_wait_min=15.0, transient_s=0.0, rng=None):
    """Drive the REAL TurbidostatController against an exponential culture.

    Culture model is the same one `test_control_loop.py` uses: `od *= exp(mu*dt)`
    between ticks, `od *= exp(-F*t/V)` on each PumpAction. That washout model is
    the exact inverse of the formula the controller uses, and it is the right
    one because influx and efflux fire concurrently on separate pump bits.

    `transient_s` injects a post-dilution mixing dip of that duration — the
    contaminant the max-R² window search exists to reject (§4.1).
    """
    rng = rng or random.Random(SEED)
    c = TurbidostatController(
        0, od_lower=lo, od_upper=hi,
        pump_wait_seconds=pump_wait_min * 60.0,
        flow_rate_ml_s=F, volume_ml=V, efflux_extra_seconds=5.0,
    )
    od, t = od0, 0.0
    samples, events = [], []
    transient_until, transient_from = -1.0, 0.0
    for _ in range(int(hours * 3600 / DT)):
        od *= math.exp(mu * DT / 3600.0)
        observed = od
        if t < transient_until and transient_s > 0:
            frac = (t - transient_from) / max(transient_s, 1e-9)
            observed = od * (1.0 - 0.12 * math.exp(-3.0 * frac))
        samples.append((t, observed + rng.gauss(0.0, OD_NOISE)))
        c.push_od(od)
        a = c.decide(t)
        if a is not None:
            od *= math.exp(-F * a.pump_time / V)
            events.append(g.DilutionEvent(t, t + a.efflux_seconds, a.pump_time * F))
            transient_from = t + a.efflux_seconds
            transient_until = transient_from + transient_s
        t += DT
    return samples, events, t


def section_1(out):
    rng = random.Random(SEED)
    out("=" * 74)
    out("1. SYNTHETIC SERIES WITH KNOWN mu  (real TurbidostatController)")
    out("=" * 74)

    out("\n1a. Turbidostat, segmented vs. the naive cross-dilution fit")
    out("    GROWTH_RATE_METHOD.md §3.3 measured the naive fit at -80/-94/-99 %.")
    out("    Two configurations, because the naive fit's error depends entirely")
    out("    on whether its window straddles a dilution:")
    for label, lo, hi, pw in (
        ("narrow band 0.3-0.4, no refractory gate (dilutions every ~15-50 min)",
         0.3, 0.4, 0.0),
        ("default band 0.2-0.6, pump_wait 15 min (dilutions ~1 h apart)",
         0.2, 0.6, 15.0),
    ):
        out(f"\n    {label}")
        out(f"    {'mu true':>8} {'segmented':>10} {'err':>8} {'R2':>7} "
            f"{'segs':>5} | {'naive 30min':>11} {'err':>8}")
        for mu in (0.35, 0.70, 1.20):
            samples, events, now = simulate_turbidostat(
                mu, lo=lo, hi=hi, pump_wait_min=pw, rng=random.Random(SEED),
            )
            rep = g.estimate(samples=samples, now=now, dilution_events=events,
                             mode="turbidostat", volume_ml=V,
                             od_range=(lo * 0.5, hi * 1.5),
                             samples_seen=len(samples))
            est = rep.growth
            tail = [(t, od) for t, od in samples if t > now - 1800 and od > 0]
            naive = g.fit_log_linear([t for t, _ in tail], [o for _, o in tail])
            n_segs = len(g.split_segments(
                [t for t, _ in samples], [o for _, o in samples], events,
            ))
            out(f"    {mu:8.2f} {est.mu_per_hour:10.4f} "
                f"{100 * (est.mu_per_hour - mu) / mu:+7.1f}% "
                f"{est.r_squared:7.4f} {n_segs:5d} | "
                f"{naive.mu_per_hour:11.4f} "
                f"{100 * (naive.mu_per_hour - mu) / mu:+7.1f}%")

    out("\n1b. Batch / regime A, range gate derived from the run's own band")
    out("    Each run is simulated until the culture reaches OD 1.0. `lag h`")
    out("    is the fitted crossing of OD 0.001 measured from t=0; at mu=0.35")
    out("    the run is 11 h long, so the estimator's rolling 3 h window sees")
    out("    only its tail and the extrapolation is correspondingly long-armed.")
    out(f"    {'mu true':>8} {'batch fit':>10} {'err':>8} {'R2':>7} "
        f"{'lag h':>7} {'expect':>7} {'windows':>8}")
    for mu in (0.35, 0.70, 1.20):
        od0 = 0.02
        hours = math.log(1.0 / od0) / mu
        n = int(hours * 3600 / DT)
        samples = [
            (i * DT, od0 * math.exp(mu * i * DT / 3600.0) + rng.gauss(0, OD_NOISE))
            for i in range(n)
        ]
        rep = g.estimate(samples=samples, now=samples[-1][0], mode="turbidostat",
                         od_range=(0.1, 0.9), samples_seen=n, origin_s=0.0)
        est = rep.growth
        lag = rep.lag_time_hours
        expect = -math.log(od0 / 0.001) / mu
        out(f"    {mu:8.2f} {est.mu_per_hour:10.4f} "
            f"{100 * (est.mu_per_hour - mu) / mu:+7.1f}% {est.r_squared:7.4f} "
            f"{('n/a' if lag is None else f'{lag:7.2f}'):>7} {expect:7.2f} "
            f"{est.windows_searched:8d}")

    out("\n1c. Post-dilution mixing transient — what the window search buys")
    out("    One 25 min segment with a decaying dip at its head, matching the")
    out("    §4.1 setup: whole-segment OLS reads +7.1 % at a 120 s transient.")
    out(f"    {'transient':>10} {'max-R2 window':>14} {'err':>8} | "
        f"{'whole-segment':>14} {'err':>8}   verdict")
    mu = 0.70
    for transient in (0.0, 30.0, 60.0, 120.0, 240.0):
        win_errs, whole_errs = [], []
        for trial in range(40):
            r = random.Random(SEED + trial)
            n = int(25 * 60 / DT)
            times = [i * DT for i in range(n)]
            ods = []
            for t in times:
                od = 0.2 * math.exp(mu * t / 3600.0)
                if transient > 0 and t < transient:
                    od *= 1.0 - 0.12 * math.exp(-3.0 * t / transient)
                ods.append(max(od + r.gauss(0, OD_NOISE), 1e-6))
            w = g.best_window_fit(times, ods)
            whole = g.fit_log_linear(times, ods)
            if w.mu_per_hour is not None:
                win_errs.append(100 * (w.mu_per_hour - mu) / mu)
            if whole is not None:
                whole_errs.append(100 * (whole.mu_per_hour - mu) / mu)
        wm = sum(win_errs) / len(win_errs)
        om = sum(whole_errs) / len(whole_errs)
        verdict = "WINDOW WINS" if abs(wm) < abs(om) else "window loses"
        out(f"    {transient:9.0f}s {mu * (1 + wm / 100):14.4f} {wm:+7.2f}% | "
            f"{mu * (1 + om / 100):14.4f} {om:+7.2f}%   {verdict}")

    out("\n1d. Chemostat mass balance, mu = D + d(ln OD)/dt  (§4.6)")
    out(f"    {'D':>6} {'mu true':>8} {'estimate':>10} {'err':>8} {'flags':>34}")
    for D, mu_true in ((0.30, 0.30), (0.50, 0.50), (0.50, 0.62)):
        # Steady state when mu_true == D; a drifting OD is the informative case.
        n = int(4 * 3600 / DT)
        od = 0.4
        samples = []
        for i in range(n):
            od *= math.exp((mu_true - D) * DT / 3600.0)
            samples.append((i * DT, od + rng.gauss(0, OD_NOISE)))
        rep = g.estimate(samples=samples, now=samples[-1][0], mode="chemostat",
                         dilution_rate_per_hour=D, samples_seen=n)
        est = rep.growth
        out(f"    {D:6.2f} {mu_true:8.2f} {est.mu_per_hour:10.4f} "
            f"{100 * (est.mu_per_hour - mu_true) / mu_true:+7.1f}% "
            f"{'|'.join(est.flags):>34}")

    out("\n1e. The dilution diagnostic: v/V vs SPEC §17's ln(1+v/V)  (§4.4)")
    out("    Band 0.2-0.4 so the bolus stays under the 20 s cap -- a capped")
    out("    bolus means the culture is NOT at steady state and the identity")
    out("    mu = D does not hold in the first place.")
    mu = 0.70
    samples, events, now = simulate_turbidostat(
        mu, lo=0.2, hi=0.4, pump_wait_min=0.0, rng=random.Random(SEED),
    )
    rep = g.estimate(samples=samples, now=now, dilution_events=events,
                     mode="turbidostat", volume_ml=V, pump_calibrated=True,
                     od_range=(0.1, 0.6), samples_seen=len(samples))
    recent = sorted(
        (e for e in events if e.t_efflux_end >= now - g.HISTORY_WINDOW_SECONDS),
        key=lambda e: e.t_start,
    )
    elapsed_h = (recent[-1].t_start - recent[0].t_start) / 3600.0
    perfusion = sum(e.delivered_ml / V for e in recent[1:]) / elapsed_h
    bolus_overflow = sum(
        math.log(1 + e.delivered_ml / V) for e in recent[1:]
    ) / elapsed_h
    reported = rep.growth.mu_per_hour
    out(f"    reported mu (OD fit)      {reported:8.4f} /h   "
        f"({len(recent)} events in the window)")
    out(f"    diagnostic, v/V           {perfusion:8.4f} /h  "
        f"({100 * (perfusion - reported) / reported:+.1f} % vs reported)")
    out(f"    diagnostic, ln(1+v/V)     {bolus_overflow:8.4f} /h  "
        f"({100 * (bolus_overflow - reported) / reported:+.1f} % vs reported)")
    out(f"    module reports            {rep.dilution_check.mu_per_hour:8.4f} /h  "
        f"model={rep.dilution_check.mixing_model}  "
        f"disagreement={rep.dilution_check.disagreement_fraction:.3f}")

    out("\n1f. Gating: the diagnostic is dark until pump calibration exists")
    rep_off = g.estimate(samples=samples, now=now, dilution_events=events,
                         mode="turbidostat", volume_ml=V, pump_calibrated=False,
                         od_range=(0.1, 0.6), samples_seen=len(samples))
    d = rep_off.dilution_check
    out(f"    enabled={d.enabled}  mu={d.mu_per_hour}  "
        f"disagreement={d.disagreement_fraction}")
    out(f"    reason: {d.reason_unavailable[:96]}...")

    out("\n1g. Volume never reaches the reported mu (§4.5)")
    doubled = [g.DilutionEvent(e.t_start, e.t_efflux_end, e.delivered_ml * 2)
               for e in events]
    rep_2x = g.estimate(samples=samples, now=now, dilution_events=doubled,
                        mode="turbidostat", volume_ml=V, pump_calibrated=True,
                        od_range=(0.1, 0.6), samples_seen=len(samples))
    same = rep_2x.growth.mu_per_hour == rep.growth.mu_per_hour
    out(f"    delivered_ml x2 -> reported mu bit-identical: {same}")
    out(f"    diagnostic mu moved {rep.dilution_check.mu_per_hour:.4f} -> "
        f"{rep_2x.dilution_check.mu_per_hour:.4f} (it is the only consumer)")


# ===========================================================================
# Section 2 — generated 1x-time mock runs through the real engine
# ===========================================================================

def _iso(base: datetime, seconds: float) -> str:
    return (base + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _generate(name: str, mode: str, parameters: dict, hours: float,
              vials: list, seed: int) -> dict:
    """Drive the REAL engine, controllers and DataLogger against a culture
    model this function owns, at a real 10 s cadence.

    **Why the culture is simulated here and not by MockSerialManager.** The
    mock adds OD noise straight into its own state, once per `_advance` and
    unscaled by how much sim time that advance covered::

        self.od_abs = self.od_abs + self._rng.normal(0, OD_NOISE_SIGMA, N_VIALS)

    At its default `time_multiplier=100` that is a plausible wobble, because
    one advance covers 100 s of growth. At 1x it is a random walk with sd
    0.005 per 5 s tick — over a 1.5 h batch phase, sd 0.16 on a culture
    starting at OD 0.05. Measured on a first attempt at this fixture: the
    realized d(ln OD)/dt over the last 3 h was 0.64 /h against a modelled
    1.08 /h, and OD had touched the 1e-4 clip floor. Noise that accumulates
    into the state is process noise, not measurement noise, and it makes the
    mock unusable as a quantitative fixture at real time. Reported, not
    silently patched — changing it would alter behaviour for every other
    mock-backed test.

    So: OD state evolves deterministically here (the mock's own logistic
    model and constants), and measurement noise is added at read time where
    it belongs. Everything else is the real thing — the real
    `ExperimentEngine`, the real `TurbidostatController` /
    `ChemostatController` deciding dilutions, the real `DataLogger` writing
    the CSVs, and the real `MockSerialManager` as the actuator sink and the
    source of the thermal transient.

    Ground truth is then exact: `mu_max[v] * growth_rate_factor(T) * (1 - OD/K)`.
    """
    from data_logger import DataLogger
    from experiment_engine import ExperimentEngine
    from mock_serial_manager import (
        CARRYING_CAPACITY_OD, MU_MAX_RANGE, MockSerialManager,
        OD_NOISE_SIGMA, growth_rate_factor,
    )

    exp_dir = EXPERIMENTS_DIR / name
    if exp_dir.exists():
        shutil.rmtree(exp_dir)

    mgr = MockSerialManager(time_multiplier=1.0, tick_seconds=DT / 2.0, seed=seed)
    # The real calibration files, so the engine's °C -> raw setpoint inversion
    # (and hence the mock's heater target) is the one the machine would use.
    cal = PROJECT_ROOT / "calibration"
    mgr.load_calibration(str(cal / "temp_calibration.txt"), str(cal / "OD_cal.txt"))
    logger = DataLogger(EXPERIMENTS_DIR)
    clock = [0.0]
    engine = ExperimentEngine(
        mgr, logger, EXPERIMENTS_DIR, clock=lambda: clock[0],
        temp_cal=mgr.temp_cal,
    )
    engine.create_experiment(name=name, mode=mode, vials=vials,
                             parameters=parameters)
    engine.start_experiment(name)
    # The same calibration context app.py pushes at start, so the fixture
    # exercises the real flag path: no blank has ever been committed
    # (uncalibrated_floor on every vial) and vial 1's own OD envelope says to
    # exclude it from quantitative use (calibration_suspect).
    from calibration_service import CalibrationService
    engine.set_growth_context(
        CalibrationService(cal, EXPERIMENTS_DIR, mgr).growth_context(name)
    )

    rng = random.Random(seed)
    mu_max = {v: rng.uniform(*MU_MAX_RANGE) for v in vials}
    od_true = {v: 0.05 for v in vials}          # inoculation density
    flow = {v: float(mgr.flow_rate[v]) for v in vials}
    pending: list = []                          # (complete_at, vial, seconds)

    base = datetime(2026, 8, 23, 0, 0, 0, tzinfo=timezone.utc)
    truth = {str(v): [] for v in vials}
    for _ in range(int(hours * 3600 / DT)):
        now = clock[0]
        # 1. Apply dilutions whose pumps have finished, exactly as the mock
        #    does: the bolus lands when the influx pump stops, not when it
        #    starts. That is what makes a boundary an interval.
        for entry in [e for e in pending if e[0] <= now]:
            _, vial, secs = entry
            od_true[vial] *= math.exp(-flow[vial] * secs / V)
            pending.remove(entry)

        # 2. Temperature from the real mock (thermal lag, heater convention),
        #    read twice so its sim clock stays in step with ours.
        temps = mgr.read_temperature()
        mgr.read_od()

        # 3. Logistic growth, gated on stir exactly as the mock gates it.
        ods = [float("nan")] * 16
        for v in vials:
            f_t = float(growth_rate_factor(temps[v]))
            if mgr.stir_speed[v] <= 0:
                f_t = 0.0
            mu_now = mu_max[v] * f_t * (1.0 - od_true[v] / CARRYING_CAPACITY_OD)
            od_true[v] *= math.exp(mu_now * DT / 3600.0)
            truth[str(v)].append((now, od_true[v], mu_now))
            # Measurement noise -- added on READ, never fed back into state.
            ods[v] = od_true[v] + rng.gauss(0.0, OD_NOISE_SIGMA)

        ts = _iso(base, now)
        logger.log_sensor_cycle(
            timestamp_iso=ts,
            temperature_calibrated=temps,
            temperature_raw=[float("nan")] * 16,
            od_calibrated=ods,
            od_raw=[float("nan")] * 16,
        )
        for vial, action in engine.run_cycle(ts, temps, ods):
            mgr.pump_command(int(vial), "influx", action.pump_time)
            mgr.pump_command(int(vial), "efflux", action.efflux_seconds)
            pending.append((now + action.pump_time, int(vial), action.pump_time))
            for direction, secs in (("influx", action.pump_time),
                                    ("efflux", action.efflux_seconds)):
                logger.log_pump_event(
                    timestamp_iso=ts, vial=int(vial), direction=direction,
                    duration_seconds=float(secs), od_at_pump=action.average_od,
                )
        clock[0] += DT

    engine.stop_experiment(reason="verify_growth_rate --generate")

    gt = {
        "generator": "verify_growth_rate.py --generate",
        "seed": seed,
        "mode": mode,
        "hours": hours,
        "dt_seconds": DT,
        "carrying_capacity_od": CARRYING_CAPACITY_OD,
        "od_measurement_noise_sd": OD_NOISE_SIGMA,
        "mu_max_per_vial": {str(v): mu_max[v] for v in vials},
        "flow_rate_ml_s": {str(v): flow[v] for v in vials},
        "note": (
            "mu_true[t] = mu_max * growth_rate_factor(T[t]) * (1 - OD[t]/K), "
            "evaluated on the NOISELESS state. Samples are "
            "(t_seconds, od_true, mu_true_per_hour). Growth is logistic, so "
            "mu_true FALLS as OD rises -- the reported value is a growth rate "
            "at the operating density, not mu_max."
        ),
        "samples": truth,
    }
    (exp_dir / "ground_truth.json").write_text(
        json.dumps(gt, indent=2), encoding="utf-8",
    )
    return gt


def generate_datasets(out) -> None:
    out("Generating 1x-time validation datasets (real engine, seeded mock)...")
    gt = _generate(
        TURBIDOSTAT_DATASET, "turbidostat",
        {
            "temperature_c": 37, "stir_rate": 10, "volume_ml": V,
            "od_lower_thresh": 0.2, "od_upper_thresh": 0.6,
            # 15 min is the default and sits comfortably above the estimator's
            # 10 min minimum fit span; the runs already in experiments/ use 5.
            "pump_wait_minutes": 15,
            "efflux_extra_seconds": 5.0,
        },
        hours=10.0,
        # vial 1 is included on purpose: the committed OD envelope's own QC
        # says to exclude it from quantitative use, so it exercises the
        # calibration_suspect flag.
        vials=[0, 1, 4, 9],
        seed=SEED,
    )
    out(f"  {TURBIDOSTAT_DATASET}: mu_max = "
        + ", ".join(f"v{v}={m:.3f}" for v, m in gt["mu_max_per_vial"].items()))
    gt2 = _generate(
        CHEMOSTAT_DATASET, "chemostat",
        {
            "temperature_c": 37, "stir_rate": 10, "volume_ml": V,
            "dilution_rate_per_hour": 0.5, "bolus_interval_seconds": 60.0,
            "efflux_extra_seconds": 5.0,
        },
        hours=12.0, vials=[0, 4], seed=SEED + 1,
    )
    out(f"  {CHEMOSTAT_DATASET}:  mu_max = "
        + ", ".join(f"v{v}={m:.3f}" for v, m in gt2["mu_max_per_vial"].items()))
    out(f"  written under {EXPERIMENTS_DIR} (gitignored)")


def _read_growth_csv(exp_dir: Path, vial: int) -> list:
    path = exp_dir / f"vial{vial:02d}_growth.csv"
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _truth_mean(samples: list, t_end: float, window: float,
                od_floor: float) -> float:
    """Mean instantaneous μ_true over the estimator's own history window.

    This is the honest comparand: the estimator reports one number for a
    stretch over which the true rate is not constant (the mock is logistic, so
    μ falls as OD rises). Restricted to samples above the OD floor because
    those are the only ones the estimator was allowed to fit.
    """
    vals = [
        mu for t, od, mu in samples
        if t_end - window < t <= t_end and od >= od_floor
    ]
    return sum(vals) / len(vals) if vals else float("nan")


def section_2(out) -> bool:
    out("\n" + "=" * 74)
    out("2. GENERATED 1x-TIME MOCK RUNS  (real engine, real DataLogger)")
    out("=" * 74)
    missing = [
        n for n in (TURBIDOSTAT_DATASET, CHEMOSTAT_DATASET)
        if not (EXPERIMENTS_DIR / n / "ground_truth.json").is_file()
    ]
    if missing:
        out(f"  SKIPPED — run `--generate` first (missing: {', '.join(missing)})")
        return False

    for name in (TURBIDOSTAT_DATASET, CHEMOSTAT_DATASET):
        exp_dir = EXPERIMENTS_DIR / name
        gt = json.loads((exp_dir / "ground_truth.json").read_text(encoding="utf-8"))
        out(f"\n  {name}  ({gt['mode']}, {gt['hours']:g} h, seed {gt['seed']})")
        out("    Reported mu vs the mean of mu_true over the estimator's own "
            "3 h window.")
        out(f"    {'vial':>4} {'rows':>5} {'est':>5} {'regime':>10} "
            f"{'mu rep':>8} {'mu true':>8} {'err':>8} {'R2':>7} {'flags'}")
        for v_str in sorted(gt["samples"], key=int):
            vial = int(v_str)
            samples = [(float(a), float(b), float(c))
                       for a, b, c in gt["samples"][v_str]]
            rows = _read_growth_csv(exp_dir, vial)
            usable = [r for r in rows if r.get("growth_rate_per_hour")]
            if not usable:
                flags = sorted({
                    f for r in rows for f in (r.get("flags") or "").split("|") if f
                })
                out(f"    {vial:4d} {len(rows):5d} {0:5d} "
                    f"{'-':>10} {'-':>8} {'-':>8} {'-':>8} {'-':>7} "
                    f"{','.join(flags)}")
                continue
            errs = []
            last = usable[-1]
            for r in usable:
                t = float(r["elapsed_hours"]) * 3600.0
                mu_rep = float(r["growth_rate_per_hour"])
                mu_true = _truth_mean(
                    samples, t, g.HISTORY_WINDOW_SECONDS, g.DEFAULT_OD_FLOOR,
                )
                if mu_true == mu_true and mu_true > 0:
                    errs.append(100 * (mu_rep - mu_true) / mu_true)
            t_last = float(last["elapsed_hours"]) * 3600.0
            mu_true_last = _truth_mean(
                samples, t_last, g.HISTORY_WINDOW_SECONDS, g.DEFAULT_OD_FLOOR,
            )
            median_err = sorted(errs)[len(errs) // 2] if errs else float("nan")
            out(f"    {vial:4d} {len(rows):5d} {len(usable):5d} "
                f"{last['regime']:>10} {float(last['growth_rate_per_hour']):8.4f} "
                f"{mu_true_last:8.4f} {median_err:+7.1f}% "
                f"{float(last['r_squared']):7.4f} {last['flags']}")
        out("    (err is the MEDIAN over all estimable rows; the last row's "
            "mu is shown for context)")
    out("")
    out("    The small systematic negative bias on the turbidostat is expected:")
    out("    mu_true is highest immediately after a dilution, when OD is lowest")
    out("    and the logistic factor (1 - OD/K) is largest, and those are exactly")
    out("    the samples the 60 s post-dilution skip excises. The comparand")
    out("    averages them in; the estimator, correctly, does not.")
    return True


# ===========================================================================
# Section 3 — replay of the runs already on disk
# ===========================================================================

def section_3(out) -> None:
    out("\n" + "=" * 74)
    out("3. REPLAY OF LOGGED RUNS  (guard rails, not accuracy)")
    out("=" * 74)
    out("  These are MockSerialManager runs at the default time_multiplier=100")
    out("  with pump_wait_minutes=5, so every inter-dilution segment is 300 s —")
    out("  below MIN_FIT_SPAN_SECONDS (600). Returning nothing, correctly")
    out("  flagged, is the right answer here.")
    for name in ("TestForLabMeeting2", "TestForLabMeeting3"):
        exp_dir = EXPERIMENTS_DIR / name
        if not (exp_dir / "config.json").is_file():
            out(f"\n  {name}: not present, skipped")
            continue
        try:
            results = replay_growth.replay_experiment(name)
        except Exception as exc:                       # pragma: no cover
            out(f"\n  {name}: REPLAY RAISED {type(exc).__name__}: {exc}")
            continue
        out("")
        for line in replay_growth.summarise(name, results).splitlines():
            out("  " + line)


# ===========================================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--generate", action="store_true",
                    help="(re)write the 1x-time validation datasets and exit")
    ap.add_argument("--section", type=int, default=None, choices=(1, 2, 3),
                    help="run only one section")
    args = ap.parse_args(argv)

    lines: list = []

    def out(text=""):
        print(text)
        lines.append(text)

    if args.generate:
        generate_datasets(out)
        return 0

    if args.section in (None, 1):
        section_1(out)
    if args.section in (None, 2):
        section_2(out)
    if args.section in (None, 3):
        section_3(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
