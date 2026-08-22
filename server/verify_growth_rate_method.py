"""Numerical validation behind GROWTH_RATE_METHOD.md.

Standalone, no repo imports — same role `verify_control_modes.py` plays for
CONTROL_MODE_AUDIT.md. Every figure quoted in GROWTH_RATE_METHOD.md §2, §3.3,
§4.1 and §4.4 is produced here.

    python3 server/verify_growth_rate_method.py

Requires numpy only.
"""
from __future__ import annotations
import numpy as np

SEED = 7


# --------------------------------------------------------------------------
# Faithful re-implementation of the notebook's Doubling_time inner loop
# (Growth analysis New HTX 2025_03_05.ipynb, SECTION 4).
# --------------------------------------------------------------------------
def notebook_fit(Time, OD_cal, window_size=8, lo=0.1, hi=0.5,
                 early_exit=0.998, use_early_exit=True, guard=True):
    """Returns (DT_minutes, r2, (start, end), n_windows_searched).

    `guard=False` reproduces the notebook exactly, including its
    unguarded reversed-band path (which raises TypeError).
    """
    CI = np.zeros(2, dtype=int)
    CI[0] = (np.abs(OD_cal - lo)).argmin()
    CI[1] = (np.abs(OD_cal - hi)).argmin()
    fitTime_tol = Time[CI[0]:CI[1] + 1]
    log_OD = np.log2(OD_cal)
    best_r2, best, searched = -np.inf, (0, window_size), 0

    if len(fitTime_tol) - window_size > 0:
        for i in range(len(fitTime_tol) - window_size):
            s = CI[0] + i
            e = s + window_size
            searched += 1
            p = np.polyfit(Time[s:e], log_OD[s:e], 1)
            r2 = _r2(Time[s:e], log_OD[s:e], p)
            if r2 > best_r2:
                best_r2, best = r2, (s, e)
            if use_early_exit and best_r2 > early_exit:
                break
    else:
        s, e = CI[0], CI[1] + 1
        searched = 1
        if guard and e - s < 2:
            return None, None, (s, e), searched
        p = np.polyfit(Time[s:e], log_OD[s:e], 1)
        best_r2, best = _r2(Time[s:e], log_OD[s:e], p), (s, e)

    s, e = best
    p = np.polyfit(Time[s:e], log_OD[s:e], 1)
    return 60.0 / p[0], _r2(Time[s:e], log_OD[s:e], p), best, searched


def _r2(x, y, p):
    res = y - np.polyval(p, x)
    sst = np.sum((y - np.mean(y)) ** 2)
    return 1 - np.sum(res ** 2) / sst if sst > 0 else np.nan


def _ols(x, y):
    p = np.polyfit(x, y, 1)
    return p[0], _r2(x, y, p)


def logistic(t, mu_h, od0=0.001, K=1.5):
    return K / (1 + (K / od0 - 1) * np.exp(-mu_h * t))


# --------------------------------------------------------------------------
def check_1_unbiased_on_exponential(rng):
    """§2 #1 — the estimator itself is unbiased on truly exponential data."""
    print("=== 1. pure exponential, plate-reader cadence: is the estimator biased? ===")
    t = np.arange(1, 97) * 0.25
    for mu in (0.4, 0.7, 1.0, 1.4):
        errs = []
        for _ in range(300):
            od = np.clip(0.001 * np.exp(mu * t) + rng.normal(0, 0.005, t.size), 0.001, None)
            DT, _, _, _ = notebook_fit(t, od)
            if DT and DT > 0:
                errs.append(100 * (np.log(2) / (DT / 60) - mu) / mu)
        print(f"  mu={mu:.1f}/h  bias={np.mean(errs):+6.2f}%  sd={np.std(errs):5.2f}%")


def check_2_band_averaging(rng):
    """§2 #2 — the band folds deceleration into the answer, ~20% lever."""
    print("\n=== 2. band-averaging: logistic K=1.5, mu_max=0.9/h ===")
    t = np.arange(1, 97) * 0.25
    for lo, hi in ((0.1, 0.5), (0.2, 0.7), (0.05, 0.25), (0.025, 0.15)):
        errs = []
        for _ in range(200):
            od = np.clip(logistic(t, 0.9) + rng.normal(0, 0.005, t.size), 0.001, None)
            DT, _, _, _ = notebook_fit(t, od, lo=lo, hi=hi)
            if DT and DT > 0:
                errs.append(100 * (np.log(2) / (DT / 60) - 0.9) / 0.9)
        pred = 100 * ((1 - ((lo + hi) / 2) / 1.5) - 1)
        print(f"  band [{lo:5.3f},{hi:4.2f}]: measured {np.mean(errs):+6.2f}%   "
              f"predicted by (1-OD/K): {pred:+6.2f}%")


def check_3_early_exit(rng):
    """§2 #3 — the R2>0.998 break is nearly free at plate-reader cadence."""
    print("\n=== 3. early exit (R2>0.998) vs exhaustive search ===")
    t = np.arange(1, 97) * 0.25
    for mu in (0.4, 0.7, 1.0):
        a_err, b_err, fired = [], [], 0
        for _ in range(300):
            od = np.clip(logistic(t, mu) + rng.normal(0, 0.005, t.size), 0.001, None)
            a = notebook_fit(t, od, use_early_exit=True)
            b = notebook_fit(t, od, use_early_exit=False)
            if not (a[0] and b[0]):
                continue
            a_err.append(100 * (np.log(2) / (a[0] / 60) - mu) / mu)
            b_err.append(100 * (np.log(2) / (b[0] / 60) - mu) / mu)
            fired += a[3] < b[3]
        print(f"  mu={mu:.1f}: fired on {100*fired/300:5.1f}% of wells | "
              f"bias early {np.mean(a_err):+6.2f}% vs exhaustive {np.mean(b_err):+6.2f}%")


def check_4_r2_inflation(rng):
    """§2 #4 — max-R2 selection inflates the reported R2."""
    print("\n=== 4. max-R2 selection bias on EXACTLY log-linear data ===")
    for n_win in (5, 20, 60, 200):
        sel, mean = [], []
        for _ in range(400):
            tt = np.arange(n_win + 8) * 0.25
            y = np.log2(0.2) + 0.9 * tt + rng.normal(0, 0.03, tt.size)
            r2s = [_ols(tt[i:i + 8], y[i:i + 8])[1] for i in range(n_win)]
            sel.append(max(r2s))
            mean.append(np.mean(r2s))
        print(f"  windows={n_win:4d}: selected R2={np.mean(sel):.5f}  "
              f"mean R2={np.mean(mean):.5f}  inflation={np.mean(sel)-np.mean(mean):+.5f}")


def check_5_cadence(rng):
    """§2 #5/#6 — the window must be specified in TIME, not samples."""
    print("\n=== 5. eVOLVER 10 s cadence: mu error vs fit span and OD noise ===")
    for noise in (0.002, 0.005, 0.010):
        row = []
        for span_min in (1.33, 10, 30):
            n = int(span_min * 60 / 10)
            tt = np.arange(n) * 10 / 3600.0
            errs = []
            for _ in range(400):
                od = 0.3 * np.exp(0.7 * tt) + rng.normal(0, noise, n)
                if np.any(od <= 0):
                    continue
                m, _ = _ols(tt, np.log(od))
                errs.append(100 * (m - 0.7) / 0.7)
            row.append(f"{span_min:5.2f}min(n={n:3d}) sd={np.std(errs):6.1f}%")
        print(f"  OD noise sd={noise:.3f}: " + "  ".join(row))


def check_6_degenerate_band(rng):
    """§2 #7/#8 — the two silent/loud failures on non-batch-shaped data."""
    print("\n=== 6. degenerate band cases ===")
    t = np.arange(96) * 0.25
    flat = np.clip(np.full(96, 0.30) + rng.normal(0, 0.02, 96), 0.001, None)
    c0, c1 = (np.abs(flat - 0.1)).argmin(), (np.abs(flat - 0.5)).argmin()
    print(f"  flat OD~0.30 (turbidostat-shaped): CI[0]={c0} CI[1]={c1} reversed={c1 < c0}")
    try:
        notebook_fit(t, flat, guard=False)
        print("    unguarded notebook: no error (unexpected)")
    except Exception as e:
        print(f"    unguarded notebook: {type(e).__name__}: {e}")
    stalled = np.clip(logistic(t, 0.8, K=0.25) + rng.normal(0, 0.004, t.size), 0.001, None)
    DT, r2, win, _ = notebook_fit(t, stalled)
    print(f"  culture stalls at OD {stalled.max():.2f} (never reaches band top 0.5):")
    print(f"    returns DT={DT:.1f} min at R2={r2:.3f} -> no warning fires (0.95 threshold)")


def _sim_turbidostat(rng, mu, hours=6, dt_s=10, lo=0.2, hi=0.4, V=25.0, noise=0.004):
    n = int(hours * 3600 / dt_s)
    t = np.arange(n) * dt_s / 3600.0
    od = np.empty(n)
    dil = []
    x = 0.25
    for i in range(n):
        x *= np.exp(mu * dt_s / 3600.0)
        if x > hi:
            v = min(V * np.log(x / lo), 8.0)
            x *= np.exp(-v / V)
            dil.append((t[i], v))
        od[i] = x
    return t, od + rng.normal(0, noise, n), dil


def check_7_naive_vs_segmented(rng):
    """§3.3 — the number that justifies SPEC 17's segment split."""
    print("\n=== 7. turbidostat: naive rolling fit vs segment split ===")
    for mu in (0.35, 0.70, 1.20):
        t, od, dil = _sim_turbidostat(rng, mu)
        W = int(30 * 60 / 10)
        naive = [_ols(t[i-W:i], np.log(np.clip(od[i-W:i], 1e-6, None)))[0]
                 for i in range(W, len(t), 60)]
        bounds = [0] + [int(round(dt * 360)) + 1 for dt, _ in dil] + [len(t)]
        seg = []
        for a, b in zip(bounds[:-1], bounds[1:]):
            if b - a < 30:
                continue
            x, y = t[a+6:b-2], np.log(np.clip(od[a+6:b-2], 1e-6, None))
            if len(x) >= 20:
                seg.append(_ols(x, y)[0])
        f = lambda arr: 100 * (np.mean(arr) - mu) / mu if len(arr) else float("nan")
        print(f"  mu={mu:.2f}/h ({len(dil)} dilutions):  naive rolling {f(naive):+7.1f}%   "
              f"segment split {f(seg):+6.1f}%")


def check_8_window_earns_its_place(rng):
    """§4.1 — the window search buys TRANSIENT REJECTION, not accuracy."""
    print("\n=== 8. does max-R2 windowing beat whole-segment OLS? ===")
    print("    (depends entirely on the post-dilution mixing transient)")
    for transient_s in (0, 30, 60, 120, 240):
        ep, ew = [], []
        for _ in range(300):
            n = int(25 * 60 / 10)
            t = np.arange(n) * 10 / 3600.0
            od = 0.2 * np.exp(0.7 * t)
            k = int(transient_s / 10)
            if k:
                od[:k] *= 1 - 0.12 * np.exp(-np.arange(k) / max(k / 3, 1))
            od = od + rng.normal(0, 0.004, n)
            y = np.log(np.clip(od, 1e-6, None))
            ep.append(100 * (_ols(t, y)[0] - 0.7) / 0.7)
            wl = max(30, int(len(t) * 0.6))
            best = max((_ols(t[i:i+wl], y[i:i+wl])[::-1] for i in range(0, len(t)-wl+1, 3)))
            ew.append(100 * (best[1] - 0.7) / 0.7)
        verdict = "WINDOW WINS" if abs(np.mean(ew)) < abs(np.mean(ep)) else "window loses"
        print(f"  transient={transient_s:4d}s: whole-segment {np.mean(ep):+6.2f}% "
              f"(sd {np.std(ep):4.2f}) | max-R2 {np.mean(ew):+6.2f}% "
              f"(sd {np.std(ew):4.2f})  -> {verdict}")


def check_9_mixing_model():
    """§4.4 — SPEC 17's ln(1+v/V) is the wrong mixing model for this machine."""
    print("\n=== 9. dilution-rate estimator: which mixing model? (V = 25 mL) ===")
    V = 25.0
    print(f"  {'bolus':>6} {'v/V':>6} | {'ln(1+v/V)':>10} {'v/V':>10} {'-ln(1-v/V)':>11} | spread")
    for v in (1.0, 2.0, 4.0, 6.0, 8.0, 10.0):
        a, b, c = np.log(1 + v / V), v / V, -np.log(1 - v / V)
        print(f"  {v:5.1f}mL {v/V:6.3f} | {a:10.4f} {b:10.4f} {c:11.4f} | "
              f"{100*(max(a,b,c)/min(a,b,c)-1):5.1f}%")
    print("  turbidostat.py fires influx and efflux SIMULTANEOUSLY -> continuous")
    print("  perfusion -> v/V. SPEC 17 specifies ln(1+v/V) (bolus-add-then-overflow),")
    print("  which reads 9-15% low at a typical 5-8 mL bolus.")


if __name__ == "__main__":
    rng = np.random.default_rng(SEED)
    check_1_unbiased_on_exponential(rng)
    check_2_band_averaging(rng)
    check_3_early_exit(rng)
    check_4_r2_inflation(rng)
    check_5_cadence(rng)
    check_6_degenerate_band(rng)
    check_7_naive_vs_segmented(rng)
    check_8_window_earns_its_place(rng)
    check_9_mixing_model()
