"""server/od_curve_fit.py — four-parameter logistic fit for the OD dilution
series (ROADMAP Session AA / CALIBRATION_PROTOCOL.md §8.2 / SPEC §19.5).

Pure and I/O-free, like growth_rate.py: numbers in, numbers out. numpy only
-- scipy is not on the Pi (requirements.txt), and this does not need it.

The model is the one ``SerialManager._od_raw_to_OD`` applies::

    OD(S) = c - log10((b - a) / (S - a) - 1) / d        a < S < b,  d < 0

with ``S`` the (dark-subtracted) photodiode signal. Signal FALLS as OD rises;
``a`` is the lower asymptote (OD -> +inf), ``b`` the upper (OD -> -inf).

How it is fitted
----------------
For fixed ``(a, b)`` put ``y = log10((b - a) / (S - a) - 1)``. Then
``OD = c + k*y`` with ``k = -1/d`` -- LINEAR in ``(c, k)``. Regressing OD on
``y`` is therefore the exact OD-space least-squares solution for that
``(a, b)``, and the whole fit reduces to a bounded two-dimensional search
over the asymptotes: a log-spaced grid to find the basin, then a simplex
polish inside it (it recovers noiseless 2016-shaped curves to machine
precision). Deterministic, no starting guess, no 4-parameter optimiser to
diverge. OD space is the right space to minimise in: the acceptance
criteria (R², per-point residual) are stated in OD.

Two things the bench data will do, and what the fit does about them
------------------------------------------------------------------
- **The upper asymptote is often unidentifiable.** Transmitted light falls
  off convexly from OD 0, so the data rarely show the curve flattening
  toward ``b``. The error keeps shrinking as ``b -> inf`` (the model tends
  to ``OD = C - k*log10(S - a)``) while ``c`` slides far below 0 -- the 2016
  fits' ``b`` of 1.4-8.7x the observed signal and ``c`` down to -3.6 are
  exactly that. ``b`` is therefore capped at ``B_CAP_FACTOR`` x the highest
  signal and ``b_at_bound`` records when the cap binds. OD inside the
  measured range is unaffected (the test suite checks this).
- **The inflection ``c`` sits below OD 0 on physically ordinary data**
  (c < 0 on all 16 blank-anchored 2016 curves). It is reported, and the
  wizard WARNS when it is outside the measured range, rather than refusing
  (CALIBRATION_PROTOCOL §8.2, amended in Session AA).
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

# Upper asymptote cap, as a multiple of the highest measured signal.
B_CAP_FACTOR = 10.0
# Fewer distinct OD levels than this cannot constrain four parameters.
MIN_OD_LEVELS = 5
# "Practical OD ceiling": the OD at which the signal is this fraction of
# (b - a) above the lower asymptote (CALIBRATION_PROTOCOL Appendix A.1's
# definition, restated so it also works when a ~ 0 on dark-subtracted signal).
PRACTICAL_CEILING_FRACTION = 0.02

# Search grid. Offsets from the data are log-spaced from a tiny fraction of
# the signal scale up to the bound.
_GRID_N = 41
_MIN_OFFSET_FRACTION = 1e-5
# Fixed, deliberately: no size-based early stop. In the narrow diagonal
# valley a nearly collapsed simplex recovers by repeated expansion, and an
# early stop cut that off (7e-3 OD of error on 2016-shaped vials 1 and 6).
_SIMPLEX_ITERATIONS = 300


def od_from_signal(signal, a: float, b: float, c: float, d: float):
    """OD at signal ``S`` -- the ``_od_raw_to_OD`` convention. NaN outside
    the domain ``(a, b)``."""
    s = np.asarray(signal, dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        return c - np.log10((b - a) / (s - a) - 1.0) / d


def signal_from_od(od, a: float, b: float, c: float, d: float):
    """The inverse: signal at OD. Defined for every OD."""
    od = np.asarray(od, dtype=float)
    return a + (b - a) / (1.0 + np.power(10.0, d * (c - od)))


def counts_per_od(od, a: float, b: float, c: float, d: float):
    """|dS/dOD| at ``od`` -- how many counts one OD unit moves the signal.
    Multiply by 0.01 for the "counts per 0.01 OD" the protocol tabulates."""
    od = np.asarray(od, dtype=float)
    z = np.power(10.0, d * (c - od))
    return np.abs((b - a) * z * math.log(10.0) * d / (1.0 + z) ** 2)


def _fit_cells(od: np.ndarray, s: np.ndarray, a: np.ndarray, b: np.ndarray):
    """Vectorised exact OLS of OD on y for every (a, b) cell.

    Returns ``(sse, c, k)``, one entry per cell; cells whose slope ``k`` is
    not positive (signal would RISE with OD) get ``sse = inf``."""
    y = np.log10((b - a)[:, None] / (s[None, :] - a[:, None]) - 1.0)
    y_mean = y.mean(axis=1)
    od_mean = od.mean()
    dy = y - y_mean[:, None]
    var_y = (dy * dy).sum(axis=1)
    cov = (dy * (od - od_mean)[None, :]).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        k = cov / var_y
    c = od_mean - k * y_mean
    resid = od[None, :] - (c[:, None] + k[:, None] * y)
    sse = (resid * resid).sum(axis=1)
    bad = ~np.isfinite(sse) | ~(k > 0) | ~(var_y > 0)
    sse = np.where(bad, np.inf, sse)
    return sse, c, k


def _nelder_mead(fun, x0, step, iterations: int = _SIMPLEX_ITERATIONS):
    """Minimal deterministic Nelder-Mead (reflect / expand / contract /
    shrink) for the two-parameter polish. Returns the best vertex."""
    x0 = np.asarray(x0, dtype=float)
    pts = [x0.copy()]
    for j in range(x0.size):
        p = x0.copy()
        p[j] += step[j]
        pts.append(p)
    vals = [fun(p) for p in pts]
    for _ in range(iterations):
        order = np.argsort(vals)
        pts = [pts[j] for j in order]
        vals = [vals[j] for j in order]
        centroid = np.mean(pts[:-1], axis=0)
        reflected = centroid + (centroid - pts[-1])
        f_r = fun(reflected)
        if f_r < vals[0]:
            expanded = centroid + 2.0 * (centroid - pts[-1])
            f_e = fun(expanded)
            pts[-1], vals[-1] = (expanded, f_e) if f_e < f_r else (reflected, f_r)
        elif f_r < vals[-2]:
            pts[-1], vals[-1] = reflected, f_r
        else:
            contracted = centroid + 0.5 * (pts[-1] - centroid)
            f_c = fun(contracted)
            if f_c < vals[-1]:
                pts[-1], vals[-1] = contracted, f_c
            else:
                for j in range(1, len(pts)):
                    pts[j] = pts[0] + 0.5 * (pts[j] - pts[0])
                    vals[j] = fun(pts[j])
    return pts[int(np.argmin(vals))]


def _monotone_violations(od: np.ndarray, s: np.ndarray,
                         sd: Optional[np.ndarray]) -> list[dict]:
    """Adjacent OD levels whose MEDIAN signal does not fall by more than the
    noise allows. Replicates at one OD level are pooled first. A mis-loaded
    vial, a bubble or a spectrophotometer transcription error shows up here
    before it shows up as a bad fit."""
    levels = np.unique(od)
    med = np.array([np.median(s[od == L]) for L in levels])
    if sd is not None:
        noise = np.array([np.max(sd[od == L]) for L in levels])
    else:
        noise = np.zeros(levels.size)
    out = []
    for i in range(levels.size - 1):
        allowance = 2.0 * max(noise[i], noise[i + 1])
        if med[i + 1] > med[i] + allowance or (
            allowance == 0.0 and med[i + 1] >= med[i]
        ):
            out.append({
                "od_low": float(levels[i]), "od_high": float(levels[i + 1]),
                "signal_low_od": float(med[i]), "signal_high_od": float(med[i + 1]),
            })
    return out


def fit_logistic4(
    od_true,
    signal,
    signal_sd=None,
    *,
    b_cap_factor: float = B_CAP_FACTOR,
) -> dict:
    """Fit the four-parameter logistic to (true OD600, signal) pairs.

    ``signal`` should already be dark-subtracted (the Session AA wizard fits
    on ``lit - dark``); it must be positive. Replicates at an OD level are
    fine and are fitted individually. ``signal_sd`` (per point, counts) is
    used only for the monotonicity allowance.

    Raises ``ValueError`` for data that cannot be fitted at all (too few OD
    levels, non-positive signal). Everything else -- a poor fit, an
    unidentifiable ``b``, non-monotone data -- is REPORTED in the result for
    the caller's QC to judge; the fit itself never refuses.

    Returns a dict with ``a, b, c, d``, ``r2`` (OD space), ``rmse``,
    ``residuals`` (per point, predicted - true), ``od_pred``,
    ``b_at_bound``, ``a_at_zero``, ``practical_od_ceiling``,
    ``counts_per_0p01_od`` (per point), ``monotone_violations``, ``n``,
    ``n_levels``."""
    od = np.asarray(od_true, dtype=float)
    s = np.asarray(signal, dtype=float)
    if od.shape != s.shape or od.ndim != 1:
        raise ValueError("od_true and signal must be 1-D and the same length")
    if not (np.all(np.isfinite(od)) and np.all(np.isfinite(s))):
        raise ValueError("od_true and signal must be finite")
    if np.any(s <= 0):
        raise ValueError(
            "signal must be positive — a dark-subtracted signal at or below "
            "0 means the LED read no more than the dark; re-take that point"
        )
    n_levels = int(np.unique(od).size)
    if n_levels < MIN_OD_LEVELS:
        raise ValueError(
            f"{n_levels} distinct OD levels cannot constrain a four-parameter "
            f"fit; need at least {MIN_OD_LEVELS}"
        )
    sd = None if signal_sd is None else np.asarray(signal_sd, dtype=float)
    if sd is not None and sd.shape != s.shape:
        raise ValueError("signal_sd must match signal")

    s_min = float(s.min())
    s_max = float(s.max())
    # a = s_min - g, g in (tiny, s_min]  (so 0 <= a < s_min)
    # b = s_max + h, h in (tiny, (cap - 1) * s_max]
    g_lo = math.log(_MIN_OFFSET_FRACTION * s_min)
    g_hi = math.log(s_min)
    h_lo = math.log(_MIN_OFFSET_FRACTION * s_max)
    h_hi = math.log(max(b_cap_factor - 1.0, 1e-6) * s_max)

    def clip(p) -> tuple[float, float]:
        return (min(max(float(p[0]), g_lo), g_hi),
                min(max(float(p[1]), h_lo), h_hi))

    def cells(g: np.ndarray, h: np.ndarray):
        return _fit_cells(od, s, s_min - np.exp(g), s_max + np.exp(h))

    def objective(p) -> float:
        # Out of bounds scores infinite rather than being clipped: clipping
        # makes the objective flat past the b cap, and a simplex that wanders
        # onto that plateau stalls there even when the optimum is interior.
        if not (g_lo <= p[0] <= g_hi and h_lo <= p[1] <= h_hi):
            return math.inf
        sse, _c, _k = cells(np.array([p[0]]), np.array([p[1]]))
        return float(sse[0])

    # 1. Coarse log-spaced grid: finds the basin, whatever the curve shape.
    G, H = np.meshgrid(np.linspace(g_lo, g_hi, _GRID_N),
                       np.linspace(h_lo, h_hi, _GRID_N), indexing="ij")
    sse, _c, _k = cells(G.ravel(), H.ravel())
    i = int(np.argmin(sse))
    if not math.isfinite(sse[i]):
        raise ValueError(
            "no monotone decreasing curve fits these points — check that "
            "signal falls as OD rises (LED on? vials loaded in order?)"
        )
    # 2. Polish inside it. The valley runs diagonally in (g, h) when b is
    # weakly identified, which a shrinking axis-aligned grid follows badly
    # (it left up to 3e-3 OD of error on noiseless 2016-shaped curves); a
    # simplex follows it to machine precision.
    step = ((g_hi - g_lo) / (_GRID_N - 1), (h_hi - h_lo) / (_GRID_N - 1))
    g, h = clip(_nelder_mead(objective, (G.ravel()[i], H.ravel()[i]), step))
    _sse, c_arr, k_arr = cells(np.array([g]), np.array([h]))
    a = float(s_min - math.exp(g))
    b = float(s_max + math.exp(h))
    c = float(c_arr[0])
    d = float(-1.0 / k_arr[0])
    od_pred = od_from_signal(s, a, b, c, d)
    resid = od_pred - od
    ss_res = float(np.sum(resid ** 2))
    ss_tot = float(np.sum((od - od.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    ceiling_signal = a + PRACTICAL_CEILING_FRACTION * (b - a)
    return {
        "a": a,
        "b": b,
        "c": c,
        "d": d,
        "r2": float(r2),
        "rmse": float(math.sqrt(ss_res / od.size)),
        "residuals": resid.tolist(),
        "od_pred": od_pred.tolist(),
        "b_at_bound": bool(h >= h_hi - 1e-6),
        "a_at_zero": bool(g >= g_hi - 1e-6),
        "practical_od_ceiling": float(od_from_signal(ceiling_signal, a, b, c, d)),
        "counts_per_0p01_od": (0.01 * counts_per_od(od, a, b, c, d)).tolist(),
        "monotone_violations": _monotone_violations(od, s, sd),
        "n": int(od.size),
        "n_levels": n_levels,
    }
