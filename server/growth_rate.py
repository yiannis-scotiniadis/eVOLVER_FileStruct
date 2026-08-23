"""server/growth_rate.py — per-vial growth rate estimation (SPEC §17, Session N).

Pure and I/O-free: plain data in, frozen dataclasses out. No imports from
``experiment_engine``, ``app``, or any control mode — that constraint is what
keeps Session N out of the control modes and what makes every claim in
``GROWTH_RATE_METHOD.md`` §10 testable without hardware.

The estimator is the Isaacs Lab algorithm (``Growth analysis New HTX
2025_03_05.ipynb`` SECTION 4, after Lajoie 2013 / Kuznetsov 2017): windowed
log-linear regression, the window chosen by maximum R². What changed in the
port, and why, is ``GROWTH_RATE_METHOD.md``; the numbers behind those choices
are reproducible with ``server/verify_growth_rate_method.py``.

Two regimes, one module (§4.2):

* **Regime A — batch / startup.** Inoculation to the first dilution is a
  genuine plate-reader-shaped curve, so the notebook algorithm applies almost
  directly: a range gate, a windowed max-R² fit, and lag time by extrapolation.
  The gate is derived from the run's own ``od_lower_thresh`` /
  ``od_upper_thresh`` — the notebook's 0.1–0.5 band is a plate-reader artefact
  and is deliberately not ported (§1.1).
* **Regime B — continuous culture.** The OD series is split at dilution events
  and each segment fitted separately. Fitting *across* a dilution reads
  −80 % to −99 % low (§3.3); segmenting reads −2.3 % to +0.2 %.

Chemostat mode uses neither: boli every ``bolus_interval_seconds`` leave
segments a few samples long, so it takes the mass balance ``μ = D + d(ln OD)/dt``
instead (§4.6).

**Unit conventions (§4.3).** Fitting is in *natural* log internally; μ is
reported in h⁻¹ and doubling time as ``ln2 / μ`` in *minutes*, matching the
lab's plate-reader output so the two are comparable without arithmetic. The
notebook fits log₂ and reports ``DT = 60 / slope_log2``; the conversion is::

    mu_per_hour   = slope_log2 * ln2          # 0.693...
    DT_minutes    = 60 / slope_log2 = ln2 / mu_per_hour * 60

Someone will eventually compare a GUI number against a notebook number, and a
factor of 0.693 is exactly the kind of thing that gets silently absorbed as a
"calibration difference".

**What the reported number is.** The range gate folds deceleration into the
answer: on a logistic with K = 1.5 the notebook's own band reports 14.6 % below
μ_max (§2 #2). This is a **growth rate at the operating density**, not μ_max.
That is the right quantity for comparing strains under a fixed protocol and the
right quantity for eVOLVER control — but the fitted OD span is recorded with
every value so it can be read as such.

**The reported R² is an optimistic bound, not an unbiased fit statistic.**
Selecting the max over *k* candidate windows inflates R² by +0.0008 (k = 5) to
+0.0022 (k = 200) on data that is exactly log-linear (§2 #4). Every estimate
therefore carries ``windows_searched`` beside its ``r_squared``, and downstream
rules (SPEC §22.1) must not treat the latter as unbiased.

**Volume never touches the reported growth rate.** Dilution events feed the
reported μ as *timestamps only*; ``delivered_ml`` is read solely by the gated
diagnostic (§4.5). A segment boundary is knowable exactly — the pump either
fired or it did not — whereas a bolus volume is ``duration × an unmeasured flow
rate``. Keeping volume out of the reported path is what makes μ independent of
the uncalibrated pump array.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import asdict, dataclass, field
from typing import Iterable, NamedTuple, Optional, Sequence


# ---------------------------------------------------------------------------
# Constants — GROWTH_RATE_METHOD.md §5. Every value has a measured basis;
# see the table there before changing one.
# ---------------------------------------------------------------------------

#: Shortest fit span that yields a usable μ. Measurement §2 #6: at eVOLVER's
#: 10 s cadence a 10 min span gives μ sd 2.4–11.6 % across OD noise
#: 0.002–0.010; the notebook's 8 *samples* (80 s here) gives 64–339 %.
MIN_FIT_SPAN_SECONDS = 600.0

#: Preferred fit span when the segment is long enough (sd 0.4–2.2 %).
PREFERRED_FIT_SPAN_SECONDS = 1800.0

#: Floor under MIN_FIT_SPAN for lossy sleeves: 5 min at 10 s cadence.
MIN_SAMPLES = 30

#: Window width as a fraction of segment span, floored at MIN_FIT_SPAN.
#: Replaces the notebook's fixed 8 samples, which is a cadence artefact (§1.1).
WINDOW_FRACTION = 0.6

#: Nominal post-dilution skip. The max-R² window search then trims whatever
#: mixing transient survives it — §4.1 measured the pair beating either alone,
#: which is why this can be a rough constant rather than a tuned one.
POST_DILUTION_SKIP_SECONDS = 60.0

#: Below this, μ is returned flagged ``low_confidence`` -- a stronger signal
#: than ``low_r2``, for a UI that wants to grey the number out rather than
#: merely annotate it. Never a reason to withhold the number: a value WITH a
#: bad R² is still useful to a human, a value without its R² is not.
MIN_R2_REPORT = 0.90

#: The notebook's own warning threshold; kept for continuity with lab practice.
MIN_R2_TRUST = 0.95

#: Fallback per-vial OD floor when the run has no committed blank (§8). The
#: measured floor is ``max(0.05, 10 × blank_sd_od[vial])`` and is supplied by
#: the caller; this is what stands in until a blank exists.
DEFAULT_OD_FLOOR = 0.05

#: Reject a window missing more than this fraction of its expected samples.
MAX_MISSING_FRACTION = 0.30

#: Fewer usable segments than this returns None + ``insufficient_segments``.
#: Never fall back to a cross-dilution fit — that is §3.3's −94 % failure mode.
MIN_SEGMENTS = 2

#: How much timestamped OD history the engine retains per vial.
HISTORY_WINDOW_SECONDS = 3 * 3600.0

#: How often the engine recomputes. The estimate does not change meaningfully
#: in one 10 s sensor cycle, and this keeps vialNN_growth.csv ~1/6 the size of
#: vialNN_OD.csv.
#:
#: Cost matters here because the recompute runs on the sensor-loop thread and
#: inside the engine lock, on a pre-2016 Raspberry Pi that has one core (Pi 1
#: Model B) or four slow ones (Pi 2 Model B) to share with Flask, socketio and
#: the serial loop. Worst realistic case -- a full 3 h history at 10 s
#: cadence, six segments -- measures 3.9 ms per vial on a development x86 box;
#: the engine staggers the sixteen vials across ticks, so a tick carries three
#: of them.
#:
#: Do NOT extrapolate that to the Pi by guessing a factor. Run
#: ``server/bench_growth_rate.py`` on the actual machine.
RECOMPUTE_INTERVAL_SECONDS = 60.0

#: Minimum span for the chemostat drift term (§4.6). Dilution is continuous
#: there, so no segmentation is needed and a long window is affordable.
CHEMOSTAT_DRIFT_WINDOW_SECONDS = 3600.0

#: Cap on candidate windows per fit. Bounds both the cost of the search and
#: the R² inflation it causes (§2 #4) — with hundreds of candidates at 10 s
#: cadence, a high R² becomes reachable by luck alone.
MAX_WINDOW_CANDIDATES = 40

#: The engine recomputes ``ceil(n_vials / this)`` vials per sensor tick,
#: round-robin, rather than all sixteen in one. Six is the number of 10 s
#: ticks in the 60 s recompute interval, so every vial is still refreshed
#: once per interval — the work is spread, not reduced.
#:
#: This exists for the deployment target, not for the developer laptop. On a
#: pre-2016 Pi 1 Model B (single-core ARM1176 at 700 MHz) scalar CPython runs
#: roughly 30–60x slower than a modern x86 core, so a burst that measures in
#: tens of milliseconds here measures in seconds there — and it is stolen
#: from the sensor thread, on a machine with one core to share with Flask,
#: socketio and the serial loop. Run ``server/bench_growth_rate.py`` on the
#: actual Pi rather than trusting a scaling guess.
VIALS_PER_RECOMPUTE_GROUP_DIVISOR = 6

#: Legacy warmup gate, mirroring ``DEFAULT_MIN_SAMPLES_BEFORE_ACTION`` in
#: control_modes/turbidostat.py. Duplicated rather than imported to keep this
#: module free of control-mode imports.
DEFAULT_WARMUP_SAMPLES = 8

#: Only used when the caller does not supply a vial volume.
DEFAULT_VOLUME_ML = 25.0

#: The one mixing model this machine implements. turbidostat.py fires influx
#: and efflux SIMULTANEOUSLY with volume pinned by the efflux straw, which is
#: continuous perfusion → v/V. SPEC §17's ln(1+v/V) is bolus-add-then-overflow
#: and reads 9–15 % low at the turbidostat's typical 5–8 mL bolus (§4.4).
MIXING_MODEL_PERFUSION = "perfusion_v_over_V"

_LN2 = math.log(2.0)


# ---------------------------------------------------------------------------
# Flags — the full vocabulary. Every one is documented in §8.
# ---------------------------------------------------------------------------

FLAG_LOW_OD = "low_od"
FLAG_SHORT_SPAN = "short_span"
FLAG_LOW_R2 = "low_r2"
#: R² below MIN_R2_REPORT (0.90) -- worse than `low_r2` (below 0.95).
FLAG_LOW_CONFIDENCE = "low_confidence"
FLAG_NEGATIVE_SLOPE = "negative_slope"
FLAG_INSUFFICIENT_SEGMENTS = "insufficient_segments"
FLAG_WARMUP = "warmup"
FLAG_BAND_NOT_SPANNED = "band_not_spanned"
FLAG_INSUFFICIENT_RANGE = "insufficient_range"
FLAG_UNCALIBRATED_FLOOR = "uncalibrated_floor"
FLAG_CALIBRATION_SUSPECT = "calibration_suspect"
FLAG_ASSUMES_COMMANDED_D = "assumes_commanded_D"
#: Chemostat only: r_squared describes the *drift* fit, not confidence in μ.
#: At steady state the drift is ~0 and its R² is near zero by construction.
FLAG_R2_IS_DRIFT_FIT = "r2_is_drift_fit"

METHOD_SEGMENT = "segment"
METHOD_BATCH = "batch"
METHOD_CHEMOSTAT = "chemostat_balance"

REGIME_BATCH = "batch"
REGIME_CONTINUOUS = "continuous"
REGIME_CHEMOSTAT = "chemostat"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class DilutionEvent(NamedTuple):
    """One dilution boundary.

    ``t_start`` is when influx began; ``t_efflux_end`` is when the *longer* of
    the two pumps stopped (``pump_time + efflux_extra_seconds``). A boundary is
    an interval, not an instant: with ``pump_time`` capped at 20 s against a
    10 s loop, one dilution can span two or three sensor cycles (§4.5).

    ``delivered_ml`` is read **only** by :func:`dilution_check`. It never
    reaches the reported μ — see the module docstring.
    """

    t_start: float
    t_efflux_end: float
    delivered_ml: float = 0.0


class Segment(NamedTuple):
    """One inter-dilution stretch of the OD series."""

    times: list
    ods: list

    @property
    def span_seconds(self) -> float:
        return (self.times[-1] - self.times[0]) if len(self.times) >= 2 else 0.0


class LinearFit(NamedTuple):
    """Closed-form OLS of ln(OD) on time. A tuple, so ``mu, r2, intercept =
    fit_log_linear(...)`` reads naturally while the named fields stay available.
    """

    mu_per_hour: float
    r_squared: Optional[float]
    intercept: float
    slope_per_second: float
    n: int


@dataclass(frozen=True)
class GrowthEstimate:
    """One growth-rate estimate. ``mu_per_hour`` is ``None`` when the quantity
    is not estimable — never a guess, and never a clamped stand-in."""

    mu_per_hour: Optional[float] = None
    doubling_time_min: Optional[float] = None
    r_squared: Optional[float] = None
    method: str = METHOD_SEGMENT
    n_points: int = 0
    span_seconds: float = 0.0
    window_start_od: Optional[float] = None
    window_end_od: Optional[float] = None
    windows_searched: int = 0
    flags: tuple = ()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["flags"] = list(self.flags)
        return d

    def with_flags(self, *extra: str) -> "GrowthEstimate":
        """Return a copy carrying ``extra`` appended, de-duplicated, order
        preserved. Used to attach caller-supplied context flags
        (``uncalibrated_floor``, ``calibration_suspect``) without the estimator
        needing to know about calibration."""
        return _replace_flags(self, _merge_flags(self.flags, extra))


@dataclass(frozen=True)
class DilutionDiagnostic:
    """The dilution-rate calculation — a **diagnostic, never a reported μ**
    (§0). Retained because it is the only independent check on the OD path: it
    depends on pump flow and elapsed time, not on optics, so it is blind to the
    failure modes the OD fit is vulnerable to. Concretely it detects a pump that
    is not actually pumping, and biofilm or wall growth making the culture
    denser than the planktonic OD suggests.

    Gated on pump calibration (§4.4). While ``enabled`` is False no divergence
    warning may be raised and ``reason_unavailable`` must be shown in the UI
    instead of a blank.
    """

    enabled: bool = False
    mu_per_hour: Optional[float] = None
    mixing_model: str = MIXING_MODEL_PERFUSION
    n_events: int = 0
    disagreement_fraction: Optional[float] = None
    reason_unavailable: Optional[str] = None
    tautological: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class GrowthReport:
    """What the engine caches per vial and what ``GET /api/growth_rate``
    returns."""

    growth: GrowthEstimate = field(default_factory=GrowthEstimate)
    regime: str = REGIME_BATCH
    dilution_check: DilutionDiagnostic = field(default_factory=DilutionDiagnostic)
    lag_time_hours: Optional[float] = None

    def to_dict(self) -> dict:
        d = self.growth.to_dict()
        d["regime"] = self.regime
        d["dilution_check"] = self.dilution_check.to_dict()
        d["lag_time_hours"] = self.lag_time_hours
        return d


def _merge_flags(base: Iterable[str], extra: Iterable[str]) -> tuple:
    out: list = []
    for f in list(base) + list(extra):
        if f and f not in out:
            out.append(f)
    return tuple(out)


def _replace_flags(est: GrowthEstimate, flags: tuple) -> GrowthEstimate:
    return GrowthEstimate(
        mu_per_hour=est.mu_per_hour,
        doubling_time_min=est.doubling_time_min,
        r_squared=est.r_squared,
        method=est.method,
        n_points=est.n_points,
        span_seconds=est.span_seconds,
        window_start_od=est.window_start_od,
        window_end_od=est.window_end_od,
        windows_searched=est.windows_searched,
        flags=flags,
    )


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------

_INF = math.inf

#: Smallest OD the estimator will take the log of. Only ever reached when a
#: caller passes a zero or negative floor.
_MIN_POSITIVE_OD = 1e-12


def _is_finite(x) -> bool:
    """Readable predicate for the cold paths.

    The per-sample filters do NOT call this: at 16 vials x a 3 h history it
    was measured at ~16 700 calls per sensor tick, and a Python-level call
    costs far more than the comparison it wraps. They inline the equivalent
    chained comparison instead, which is exact because NaN compares False
    against everything::

        -_INF < t < _INF          rejects NaN and both infinities
        0.0 < od < _INF           rejects NaN, zero, negatives and +inf

    Keep the two in step if either changes.
    """
    try:
        return -_INF < float(x) < _INF
    except (TypeError, ValueError):
        return False


def _median(values: Sequence[float]) -> float:
    n = len(values)
    if n == 0:
        return 0.0
    s = sorted(values)
    mid = n // 2
    if n % 2:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


def _median_cadence(times: Sequence[float]) -> float:
    """Median inter-sample interval. Real timestamps, never nominal cadence:
    the sensor loop's period is ``max(10 s, work)`` and genuinely varies
    (§4.3)."""
    if len(times) < 2:
        return 0.0
    diffs = [b - a for a, b in zip(times, times[1:]) if b > a]
    return _median(diffs) if diffs else 0.0


def doubling_time_minutes(mu_per_hour: Optional[float]) -> Optional[float]:
    """``ln2 / μ`` in minutes, or ``None`` for a non-positive μ.

    A shrinking culture has a real, informative μ but no doubling time. The
    notebook returns ``60/slope`` regardless and papers over the resulting
    infinities downstream with ``replace([inf, -inf], nan)`` in a plotting
    cell; that is the bug this function exists to not have (§8).
    """
    if mu_per_hour is None or not _is_finite(mu_per_hour) or mu_per_hour <= 0:
        return None
    return (_LN2 / mu_per_hour) * 60.0


# ---------------------------------------------------------------------------
# Core fit
# ---------------------------------------------------------------------------

def fit_log_linear(times_s: Sequence[float], ods: Sequence[float]) -> Optional[LinearFit]:
    """Closed-form OLS of ``ln(OD)`` against time in seconds.

    Returns ``None`` for a degenerate fit (fewer than two points, all samples
    at one timestamp, or a non-positive OD). ``mu_per_hour`` is the slope
    rescaled to inverse hours; ``r_squared`` is ``None`` when ln(OD) has zero
    variance, which is a flat series rather than a perfect fit.
    """
    n = len(times_s)
    if n < 2 or n != len(ods):
        return None
    xs: list = []
    ys: list = []
    _log = math.log
    for t, od in zip(times_s, ods):
        # Inlined finiteness test -- see _is_finite's docstring.
        if not (0.0 < od < _INF) or not (-_INF < t < _INF):
            return None
        xs.append(t)
        ys.append(_log(od))
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        return None
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    sst = sum((y - mean_y) ** 2 for y in ys)
    if sst > 0:
        ssr = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
        r2 = 1.0 - ssr / sst
    else:
        r2 = None
    return LinearFit(
        mu_per_hour=slope * 3600.0,
        r_squared=r2,
        intercept=intercept,
        slope_per_second=slope,
        n=n,
    )


class _WindowScanner:
    """O(1)-per-candidate OLS over a sorted series, via prefix sums.

    The window search evaluates up to :data:`MAX_WINDOW_CANDIDATES` heavily
    overlapping windows. Refitting each from scratch is O(candidates x window)
    and was the single dominant cost of the whole service: measured at 55 ms
    per vial on a developer laptop for a full 3 h history, which extrapolates
    to *seconds* per vial on a pre-2016 Pi — stolen from the 10 s sensor tick,
    on a box with one core to share with Flask and the serial loop.

    Prefix sums make each candidate O(1), so the search costs one pass over
    the segment plus a constant per window.

    **Precision.** Differencing prefix sums is a cancelling subtraction, so
    time is recentred on the first sample before the sums are built. Without
    that, epoch-second timestamps (~1.7e9) square to ~2.9e18, past the 2^53
    boundary where float64 stops representing integers exactly, and the
    variance terms would be noise. Recentred, x runs 0..a few thousand and the
    sums stay far inside the safe range.

    **The reported numbers do not come from here.** This picks the *window*;
    the winner is then re-fitted with :func:`fit_log_linear`, the direct and
    obviously-correct implementation. Any residual precision difference can
    therefore at worst select a marginally different window, never perturb a
    value the operator sees.
    """

    __slots__ = ("n", "sx", "sy", "sxx", "sxy", "syy")

    def __init__(self, times_s, ys) -> None:
        n = len(times_s)
        origin = times_s[0] if n else 0.0
        sx = [0.0] * (n + 1)
        sy = [0.0] * (n + 1)
        sxx = [0.0] * (n + 1)
        sxy = [0.0] * (n + 1)
        syy = [0.0] * (n + 1)
        ax = ay = axx = axy = ayy = 0.0
        for i in range(n):
            x = times_s[i] - origin
            y = ys[i]
            ax += x
            ay += y
            axx += x * x
            axy += x * y
            ayy += y * y
            sx[i + 1] = ax
            sy[i + 1] = ay
            sxx[i + 1] = axx
            sxy[i + 1] = axy
            syy[i + 1] = ayy
        self.n = n
        self.sx, self.sy, self.sxx, self.sxy, self.syy = sx, sy, sxx, sxy, syy

    def r_squared(self, a: int, b: int) -> Optional[float]:
        """R² of the OLS fit over ``[a, b)``, or None where it is undefined.

        Uses r² = Sxy²/(Sxx·Syy) on centred sums, which is algebraically
        identical to 1 − SSres/SStot for a simple linear regression and needs
        no second pass.
        """
        n = b - a
        if n < 2:
            return None
        sx = self.sx[b] - self.sx[a]
        sy = self.sy[b] - self.sy[a]
        sxx = self.sxx[b] - self.sxx[a]
        sxy = self.sxy[b] - self.sxy[a]
        syy = self.syy[b] - self.syy[a]
        cxx = sxx - sx * sx / n
        cyy = syy - sy * sy / n
        if cxx <= 0 or cyy <= 0:
            # Zero variance in time (all one timestamp) or in ln(OD) (a flat
            # series). Neither is a fit; the caller treats it as no candidate.
            return None
        cxy = sxy - sx * sy / n
        return (cxy * cxy) / (cxx * cyy)


def _estimate_from_fit(
    fit: LinearFit,
    times: Sequence[float],
    ods: Sequence[float],
    *,
    method: str,
    windows_searched: int,
    extra_flags: Iterable[str] = (),
) -> GrowthEstimate:
    flags: list = []
    mu = fit.mu_per_hour
    if mu <= 0:
        flags.append(FLAG_NEGATIVE_SLOPE)
    if fit.r_squared is not None and fit.r_squared < MIN_R2_TRUST:
        flags.append(FLAG_LOW_R2)
        if fit.r_squared < MIN_R2_REPORT:
            flags.append(FLAG_LOW_CONFIDENCE)
    return GrowthEstimate(
        mu_per_hour=mu,
        doubling_time_min=doubling_time_minutes(mu),
        r_squared=fit.r_squared,
        method=method,
        n_points=fit.n,
        span_seconds=(times[-1] - times[0]) if len(times) >= 2 else 0.0,
        window_start_od=float(ods[0]) if ods else None,
        window_end_od=float(ods[-1]) if ods else None,
        windows_searched=windows_searched,
        flags=_merge_flags(flags, extra_flags),
    )


def best_window_fit(
    times_s: Sequence[float],
    ods: Sequence[float],
    *,
    window_seconds: Optional[float] = None,
    step_seconds: Optional[float] = None,
    method: str = METHOD_SEGMENT,
    extra_flags: Iterable[str] = (),
) -> GrowthEstimate:
    """Slide a fixed-*duration* window over the series and keep the fit with the
    highest R².

    The window is specified in seconds, not samples. Eight samples — the
    notebook's constant — is 80 s at eVOLVER's cadence and gives μ error sd of
    64–339 % (§2 #5); the same measurement puts 10 min at 2.4–11.6 % and 30 min
    at 0.4–2.2 %.

    **What the search actually buys is transient rejection, not accuracy.**
    Inside a clean segment it is mildly *harmful* — it selects the luckiest
    noise realisation and pays ~2.5 % bias and double the variance for nothing.
    Its value is finding the exponential stretch when you do not know how long
    mixing or sensor recovery contaminated the ends: at a 240 s transient it
    reads +3.8 % where whole-segment OLS reads +12.6 % (§4.1). Same job the
    notebook gives it, different contaminant.

    The notebook's ``R² > 0.998`` early exit is deliberately **not**
    implemented: it costs < 0.25 pp at plate-reader cadence, and at eVOLVER
    cadence R² > 0.998 is reachable by luck alone (§5).
    """
    # Drop unusable samples; never interpolate (§8). A single NaN or a
    # noise-driven non-positive OD must not take the whole window with it,
    # which is what strict `fit_log_linear` would do if handed the raw series.
    pairs = [
        (t, od)
        for t, od in zip(times_s, ods)
        if 0.0 < od < _INF and -_INF < t < _INF
    ]
    pairs.sort(key=lambda p: p[0])
    times_s = [t for t, _ in pairs]
    ods = [o for _, o in pairs]

    n = len(times_s)
    span = (times_s[-1] - times_s[0]) if n >= 2 else 0.0
    if n < MIN_SAMPLES or span < MIN_FIT_SPAN_SECONDS:
        return GrowthEstimate(
            method=method,
            n_points=n,
            span_seconds=span,
            windows_searched=0,
            flags=_merge_flags((FLAG_SHORT_SPAN,), extra_flags),
        )

    cadence = _median_cadence(times_s)
    if window_seconds is None:
        window = min(
            max(WINDOW_FRACTION * span, MIN_FIT_SPAN_SECONDS),
            PREFERRED_FIT_SPAN_SECONDS,
        )
    else:
        window = float(window_seconds)
    window = min(window, span)

    slack = span - window
    if step_seconds is not None:
        step = max(float(step_seconds), 1e-9)
    elif slack <= 0:
        step = max(window, 1e-9)
    else:
        step = max(cadence, slack / MAX_WINDOW_CANDIDATES, 1e-9)

    expected = (window / cadence + 1.0) if cadence > 0 else float(MIN_SAMPLES)
    min_window_samples = max(MIN_SAMPLES, int(math.ceil((1.0 - MAX_MISSING_FRACTION) * expected)))

    best_r2: float = -1.0
    best_bounds: Optional[tuple] = None
    searched = 0
    start = times_s[0]
    end_limit = times_s[-1] - window
    # `times_s` is sorted, so each candidate window is a contiguous index
    # range found by binary search, and the fit over it is O(1) against the
    # prefix sums. Scanning and refitting per candidate instead is
    # O(candidates x window) and was measured at 55 ms per vial for a 3 h
    # history -- seconds per vial on the deployment Pi.
    scanner = _WindowScanner(times_s, [math.log(o) for o in ods])
    # +1e-6 so a window that exactly fills the span is still visited.
    while start <= end_limit + 1e-6:
        lo_i = bisect.bisect_left(times_s, start)
        hi_i = bisect.bisect_right(times_s, start + window)
        if (hi_i - lo_i) >= min_window_samples:
            r2 = scanner.r_squared(lo_i, hi_i)
            if r2 is not None:
                searched += 1
                if r2 > best_r2:
                    best_r2 = r2
                    best_bounds = (lo_i, hi_i)
        start += step

    # The winning window is re-fitted with the direct implementation, so every
    # reported number comes from `fit_log_linear` and the fast path only ever
    # chose *which* samples to fit.
    best = None
    if best_bounds is not None:
        lo_i, hi_i = best_bounds
        best = fit_log_linear(times_s[lo_i:hi_i], ods[lo_i:hi_i])

    if best is None:
        # Every candidate was rejected (heavy dropout, or a perfectly flat
        # series with no ln(OD) variance). Fall back to the whole segment —
        # one fit, honestly reported as one window searched.
        fit = fit_log_linear(list(times_s), list(ods))
        if fit is None:
            return GrowthEstimate(
                method=method,
                n_points=n,
                span_seconds=span,
                windows_searched=0,
                flags=_merge_flags((FLAG_SHORT_SPAN,), extra_flags),
            )
        return _estimate_from_fit(
            fit, times_s, ods, method=method, windows_searched=1,
            extra_flags=extra_flags,
        )

    lo_i, hi_i = best_bounds
    return _estimate_from_fit(
        best, times_s[lo_i:hi_i], ods[lo_i:hi_i], method=method,
        windows_searched=searched, extra_flags=extra_flags,
    )


# ---------------------------------------------------------------------------
# Segmentation (regime B)
# ---------------------------------------------------------------------------

def split_segments(
    times_s: Sequence[float],
    ods: Sequence[float],
    dilution_times_s: Sequence[DilutionEvent],
    *,
    skip_seconds: float = POST_DILUTION_SKIP_SECONDS,
) -> list:
    """Split the OD series at dilution events, excising each event's gap.

    A boundary is an interval, not an instant (§4.5)::

        segment_end   = last OD sample with t <= t_start
        segment_start = t_efflux_end + skip_seconds

    ``run_cycle`` reads OD *before* it calls ``decide``, so the sample taken on
    the firing cycle is genuinely pre-dilution and belongs to the ending
    segment. A dilution spanning three sensor cycles therefore produces exactly
    one boundary, and no mid-dilution sample lands in either adjoining segment.

    Overlapping events (a manual pump during an automatic dilution's overrun)
    collapse into a single gap rather than producing an empty segment.
    """
    if not times_s:
        return []
    events = sorted(
        (e for e in dilution_times_s if _is_finite(e.t_start)),
        key=lambda e: e.t_start,
    )
    if not events:
        return [Segment(list(times_s), list(ods))]

    # Merge overlapping / touching gaps.
    gaps: list = []
    for e in events:
        gap_start = float(e.t_start)
        gap_end = max(float(e.t_efflux_end), gap_start) + float(skip_seconds)
        if gaps and gap_start <= gaps[-1][1]:
            gaps[-1] = (gaps[-1][0], max(gaps[-1][1], gap_end))
        else:
            gaps.append((gap_start, gap_end))

    # Each gap excises the contiguous index range (gap_start, gap_end]; the
    # stretches between consecutive gaps are the segments. Binary search
    # rather than a walk, for the same reason as best_window_fit.
    segments: list = []
    cursor = 0
    n = len(times_s)
    for gap_start, gap_end in gaps:
        cut = bisect.bisect_right(times_s, gap_start)
        if cut > cursor:
            segments.append(
                Segment(list(times_s[cursor:cut]), list(ods[cursor:cut]))
            )
        cursor = max(cursor, bisect.bisect_right(times_s, gap_end))
        if cursor >= n:
            break
    if cursor < n:
        segments.append(Segment(list(times_s[cursor:]), list(ods[cursor:])))
    return segments


def estimate_segment(
    segments: Sequence[Segment],
    *,
    extra_flags: Iterable[str] = (),
) -> GrowthEstimate:
    """Regime B. Fit each segment with :func:`best_window_fit`, then take a
    weighted mean of the usable segments' μ, weighted by span × R².

    Fewer than :data:`MIN_SEGMENTS` usable segments returns ``None`` flagged
    ``insufficient_segments``. It deliberately does **not** fall back to a fit
    across dilution boundaries: that reads −80 % / −94 % / −99 % low at
    μ = 0.35 / 0.70 / 1.20 h⁻¹ (§3.3), which is the entire reason segmentation
    exists.

    The reported ``r_squared`` is the same weighted mean over the contributing
    segments, and ``window_start_od`` / ``window_end_od`` come from the most
    recent one.
    """
    fits: list = []
    total_points = 0
    total_span = 0.0
    total_searched = 0
    for seg in segments:
        est = best_window_fit(seg.times, seg.ods, method=METHOD_SEGMENT)
        total_searched += est.windows_searched
        if est.mu_per_hour is None or est.r_squared is None:
            continue
        fits.append(est)
        total_points += est.n_points
        total_span += est.span_seconds

    if len(fits) < MIN_SEGMENTS:
        return GrowthEstimate(
            method=METHOD_SEGMENT,
            n_points=total_points,
            span_seconds=total_span,
            windows_searched=total_searched,
            flags=_merge_flags((FLAG_INSUFFICIENT_SEGMENTS,), extra_flags),
        )

    weights = [max(f.r_squared, 0.0) * max(f.span_seconds, 0.0) for f in fits]
    total_w = sum(weights)
    if total_w <= 0:
        weights = [1.0] * len(fits)
        total_w = float(len(fits))
    mu = sum(w * f.mu_per_hour for w, f in zip(weights, fits)) / total_w
    r2 = sum(w * f.r_squared for w, f in zip(weights, fits)) / total_w

    flags: list = []
    if mu <= 0:
        flags.append(FLAG_NEGATIVE_SLOPE)
    if r2 < MIN_R2_TRUST:
        flags.append(FLAG_LOW_R2)
        if r2 < MIN_R2_REPORT:
            flags.append(FLAG_LOW_CONFIDENCE)
    latest = fits[-1]
    return GrowthEstimate(
        mu_per_hour=mu,
        doubling_time_min=doubling_time_minutes(mu),
        r_squared=r2,
        method=METHOD_SEGMENT,
        n_points=total_points,
        span_seconds=total_span,
        window_start_od=latest.window_start_od,
        window_end_od=latest.window_end_od,
        windows_searched=total_searched,
        flags=_merge_flags(flags, extra_flags),
    )


# ---------------------------------------------------------------------------
# Batch / startup (regime A)
# ---------------------------------------------------------------------------

def _argmin_abs(values, target: float) -> int:
    best_i, best_d = 0, abs(values[0] - target)
    for i, v in enumerate(values):
        d = abs(v - target)
        if d < best_d:
            best_i, best_d = i, d
    return best_i


def _batch_window(ods, lo: Optional[float], hi: Optional[float]) -> tuple:
    """Resolve the regime-A range gate to an index slice plus its flags.

    Returns ``(i0, i1, flags)``; ``i1`` is inclusive. ``(None, None, flags)``
    means the whole series should be fitted instead.
    """
    if lo is None or hi is None or hi <= lo or len(ods) < 2:
        return None, None, ()
    if min(ods) > lo or max(ods) < hi:
        # §8: fit what exists, flagged. This is measurement §2 #8 — the
        # notebook silently returns DT = 131 min at R² = 0.991 off a
        # decelerating curve, and the high R² is exactly the problem: the fit
        # is excellent, and the quantity fitted is not what the caller thinks.
        return None, None, (FLAG_BAND_NOT_SPANNED,)
    i0 = _argmin_abs(ods, lo)
    i1 = _argmin_abs(ods, hi)
    if i1 <= i0:
        # §2 #7: the notebook slices empty here and raises
        # "TypeError: expected non-empty vector for x".
        return None, None, (FLAG_INSUFFICIENT_RANGE,)
    return i0, i1, ()


def estimate_batch(
    times_s: Sequence[float],
    ods: Sequence[float],
    *,
    lo: Optional[float] = None,
    hi: Optional[float] = None,
    extra_flags: Iterable[str] = (),
) -> GrowthEstimate:
    """Regime A — inoculation to the first dilution.

    The range gate answers "which stretch do we call exponential", excluding
    lag below and stationary above. It is **derived from the run's own config**
    rather than ported: the caller passes ``lo``/``hi`` computed from
    ``od_lower_thresh`` / ``od_upper_thresh``, which are literally the density
    range the operator chose to run at — exactly what the notebook's 0.1–0.5
    band was approximating (§4.2).

    Two failures the notebook has here and this does not:

    * A **reversed** range (``CI[1] < CI[0]``, which a flat turbidostat-shaped
      series produces) slices empty and raises ``TypeError`` (§2 #7). Here it
      returns ``None`` + ``insufficient_range``.
    * A culture that **stalls below the range top** returns DT = 131 min at
      R² = 0.991 with no warning, because 0.991 clears the 0.95 threshold
      (§2 #8). Here the whole series is fitted and flagged
      ``band_not_spanned``.
    """
    n = len(times_s)
    if n < 2:
        return GrowthEstimate(
            method=METHOD_BATCH, n_points=n,
            flags=_merge_flags((FLAG_SHORT_SPAN,), extra_flags),
        )

    i0, i1, gate_flags = _batch_window(ods, lo, hi)
    if FLAG_INSUFFICIENT_RANGE in gate_flags:
        return GrowthEstimate(
            method=METHOD_BATCH,
            n_points=n,
            span_seconds=times_s[-1] - times_s[0],
            flags=_merge_flags(gate_flags, extra_flags),
        )
    if i0 is None:
        t_win, o_win = list(times_s), list(ods)
    else:
        t_win = list(times_s[i0:i1 + 1])
        o_win = list(ods[i0:i1 + 1])

    return best_window_fit(
        t_win, o_win, method=METHOD_BATCH,
        extra_flags=_merge_flags(gate_flags, extra_flags),
    )


def lag_time_hours(
    times_s: Sequence[float],
    ods: Sequence[float],
    *,
    reference_od: float = 0.001,
    lo: Optional[float] = None,
    hi: Optional[float] = None,
    origin_s: Optional[float] = None,
) -> Optional[float]:
    """The notebook's lag extrapolation: hours from ``origin_s`` to the point
    where the fitted exponential crosses ``reference_od``.

    Negative means the culture was already above the detection floor at the
    origin. Kept because it is genuinely useful at inoculation — it answers
    "when did this culture actually start growing", the first thing anyone
    asks about an overnight run that under-performed (§6).

    ``origin_s`` must be the run's start on the caller's clock. Defaulting it
    to the first supplied sample would be wrong for the engine, which hands
    over only a rolling 3 h window: a lag measured from the start of that
    window is a number about the window, not about the culture, and on a
    batch phase longer than 3 h it is off by however much history was
    dropped. Measured at -16.7 h against a true -8.6 h before this argument
    existed.
    """
    if len(times_s) < 2 or reference_od <= 0:
        return None
    i0, i1, gate_flags = _batch_window(ods, lo, hi)
    if FLAG_INSUFFICIENT_RANGE in gate_flags:
        return None
    if i0 is None:
        t_win, o_win = list(times_s), list(ods)
    else:
        t_win = list(times_s[i0:i1 + 1])
        o_win = list(ods[i0:i1 + 1])
    fit = fit_log_linear(t_win, o_win)
    if fit is None or fit.slope_per_second <= 0:
        return None
    t_cross = (math.log(reference_od) - fit.intercept) / fit.slope_per_second
    start = times_s[0] if origin_s is None else float(origin_s)
    return (t_cross - start) / 3600.0


# ---------------------------------------------------------------------------
# Chemostat mass balance (§4.6)
# ---------------------------------------------------------------------------

def estimate_chemostat(
    times_s: Sequence[float],
    ods: Sequence[float],
    *,
    dilution_rate_per_hour: float,
    window_seconds: float = CHEMOSTAT_DRIFT_WINDOW_SECONDS,
    extra_flags: Iterable[str] = (),
) -> GrowthEstimate:
    """``μ = D + d(ln OD)/dt``.

    Regime B does not apply to a chemostat: ``ChemostatController`` fires a
    bolus every ``bolus_interval_seconds`` — tens of seconds, not tens of
    minutes — so segmenting leaves segments a few samples long and would return
    ``None`` indefinitely. Dilution here is continuous rather than stepwise, so
    the drift term needs no segmentation and can use a long window.

    At steady state the drift is ~0 and μ = D. A drifting OD is the informative
    case: the culture is out-running or washing out of the imposed rate.

    Always flagged ``assumes_commanded_D`` — a *commanded* dilution rate is only
    a *delivered* one once the Tier 2 gravimetric bench work exists. Also
    flagged ``r2_is_drift_fit``, because that R² describes the drift regression
    and is near zero at steady state by construction, not by failure.
    """
    n = len(times_s)
    span = (times_s[-1] - times_s[0]) if n >= 2 else 0.0
    base_flags = _merge_flags(
        (FLAG_ASSUMES_COMMANDED_D, FLAG_R2_IS_DRIFT_FIT), extra_flags
    )
    if n < MIN_SAMPLES or span < MIN_FIT_SPAN_SECONDS:
        return GrowthEstimate(
            method=METHOD_CHEMOSTAT, n_points=n, span_seconds=span,
            flags=_merge_flags((FLAG_SHORT_SPAN,), base_flags),
        )
    cutoff = times_s[-1] - float(window_seconds)
    t_win: list = []
    o_win: list = []
    for t, od in zip(times_s, ods):
        if t >= cutoff:
            t_win.append(t)
            o_win.append(od)
    if len(t_win) < MIN_SAMPLES:
        t_win, o_win = list(times_s), list(ods)
    fit = fit_log_linear(t_win, o_win)
    if fit is None:
        return GrowthEstimate(
            method=METHOD_CHEMOSTAT, n_points=len(t_win), span_seconds=span,
            flags=_merge_flags((FLAG_SHORT_SPAN,), base_flags),
        )
    mu = float(dilution_rate_per_hour) + fit.mu_per_hour
    flags = list(base_flags)
    if mu <= 0:
        flags.insert(0, FLAG_NEGATIVE_SLOPE)
    return GrowthEstimate(
        mu_per_hour=mu,
        doubling_time_min=doubling_time_minutes(mu),
        r_squared=fit.r_squared,
        method=METHOD_CHEMOSTAT,
        n_points=len(t_win),
        span_seconds=(t_win[-1] - t_win[0]),
        window_start_od=float(o_win[0]),
        window_end_od=float(o_win[-1]),
        windows_searched=1,
        flags=tuple(flags),
    )


# ---------------------------------------------------------------------------
# Dilution-rate diagnostic (§4.4) — gated, never a reported μ
# ---------------------------------------------------------------------------

def dilution_check(
    events: Sequence[DilutionEvent],
    volume_ml: float,
    window_seconds: float,
    *,
    enabled: bool,
    reported_mu: Optional[float] = None,
    reason_unavailable: Optional[str] = None,
    tautological: bool = False,
) -> DilutionDiagnostic:
    """``μ ≈ Σ (vᵢ / V) / Δt`` over the window.

    **The factor is ``v/V``, not ``ln(1 + v/V)``.** SPEC §17 and Session N both
    specify the latter, which is the factor for bolus-add-then-overflow mixing.
    This machine fires influx and efflux *simultaneously* with volume pinned by
    the efflux straw — continuous perfusion, whose factor is ``v/V``. At the
    turbidostat's typical 5–8 mL bolus into 25 mL the §17 formula reads 9–15 %
    low (§4.4). Nothing reported depends on this any more, but a diagnostic
    built on the wrong factor is a diagnostic that cries wolf.

    ``enabled`` must be ``calibration_store.current_pump_rates() is not None``.
    Until the Tier 2 bench work is done every flow rate is a hardcoded default
    and this would false-alarm, so it ships off with ``reason_unavailable``
    populated — the UI shows that string rather than a blank.
    """
    n_events = len(events)
    if not enabled:
        return DilutionDiagnostic(
            enabled=False,
            mu_per_hour=None,
            n_events=n_events,
            disagreement_fraction=None,
            reason_unavailable=(
                reason_unavailable
                or "pump flow rates have not been calibrated "
                "(calibration/current.json has \"pump\": null) — the dilution "
                "cross-check would compare against a hardcoded default"
            ),
            tautological=tautological,
        )
    if volume_ml <= 0 or window_seconds <= 0:
        return DilutionDiagnostic(
            enabled=True, mu_per_hour=None, n_events=n_events,
            reason_unavailable="invalid vial volume or window",
            tautological=tautological,
        )
    # Rate over the span BETWEEN the first and last event, not over the
    # nominal window. A turbidostat dilutes in discrete boluses tens of
    # minutes apart, so a 3 h window holds only two or three of them and
    # dividing by the window length inherits the ~1/k edge effect of where
    # the window boundaries happen to fall -- measured at -24 % on a
    # steady-state simulation. Summing the k-1 boluses that *completed* an
    # interval and dividing by the elapsed interval time is the unbiased
    # estimator, and it is exact at steady state: the culture regrows
    # ln(od_hi/od_lo) = v/V between fires, so (v/V)/dt is mu by construction.
    ordered = sorted(events, key=lambda e: e.t_start)
    if len(ordered) < 2:
        return DilutionDiagnostic(
            enabled=True,
            mu_per_hour=None,
            n_events=n_events,
            reason_unavailable=(
                "fewer than two dilution events in the window -- a rate needs "
                "at least one completed interval between fires"
            ),
            tautological=tautological,
        )
    elapsed = ordered[-1].t_start - ordered[0].t_start
    if elapsed <= 0:
        return DilutionDiagnostic(
            enabled=True, mu_per_hour=None, n_events=n_events,
            reason_unavailable="all dilution events share one timestamp",
            tautological=tautological,
        )
    total = sum(
        max(float(e.delivered_ml), 0.0) / float(volume_ml) for e in ordered[1:]
    )
    hours = elapsed / 3600.0
    mu = total / hours
    disagreement = None
    if (
        mu is not None
        and reported_mu is not None
        and _is_finite(reported_mu)
        and abs(reported_mu) > 1e-9
        and not tautological
    ):
        disagreement = abs(mu - reported_mu) / abs(reported_mu)
    return DilutionDiagnostic(
        enabled=True,
        mu_per_hour=mu,
        n_events=n_events,
        disagreement_fraction=disagreement,
        reason_unavailable=None,
        tautological=tautological,
    )


# ---------------------------------------------------------------------------
# The entry point the engine calls
# ---------------------------------------------------------------------------

def estimate(
    *,
    samples: Sequence,
    now: float,
    dilution_events: Sequence[DilutionEvent] = (),
    mode: str = "turbidostat",
    od_floor: float = DEFAULT_OD_FLOOR,
    od_range: Optional[tuple] = None,
    volume_ml: float = DEFAULT_VOLUME_ML,
    dilution_rate_per_hour: Optional[float] = None,
    pump_calibrated: bool = False,
    pump_reason_unavailable: Optional[str] = None,
    samples_seen: Optional[int] = None,
    warmup_samples: int = DEFAULT_WARMUP_SAMPLES,
    extra_flags: Sequence[str] = (),
    history_window_seconds: float = HISTORY_WINDOW_SECONDS,
    origin_s: Optional[float] = None,
) -> GrowthReport:
    """Estimate one vial's growth rate.

    ``samples`` is a sequence of ``(t_seconds, od)`` using the *same* clock as
    ``dilution_events`` and ``now`` — the engine's ``self._clock()``. NaN and
    non-positive OD samples are dropped, never interpolated; samples below
    ``od_floor`` are dropped because the sigmoid calibration is at its noisy
    tail there, and are never clamped to a floor value (the notebook's clamp to
    0.001 creates a plateau of identical values that a log-linear fit reads as
    zero growth, §3.2).

    ``od_range`` is the regime-A gate ``(lo, hi)``; ``extra_flags`` carries
    caller context the estimator has no business knowing about
    (``uncalibrated_floor``, ``calibration_suspect``); ``origin_s`` is the
    run start on the same clock, used only to make ``lag_time_hours``
    measure from inoculation rather than from the start of whatever rolling
    window happened to be handed over.
    """
    ctx_flags = tuple(extra_flags or ())
    is_chemostat = str(mode) == "chemostat"
    regime = REGIME_CHEMOSTAT if is_chemostat else REGIME_BATCH

    cutoff = float(now) - float(history_window_seconds)
    recent_events = [
        e for e in dilution_events
        if _is_finite(e.t_start) and e.t_efflux_end >= cutoff
    ]
    if not is_chemostat and recent_events:
        regime = REGIME_CONTINUOUS

    diag = dilution_check(
        recent_events,
        volume_ml,
        min(float(history_window_seconds), max(float(now) - cutoff, 0.0)),
        enabled=bool(pump_calibrated),
        reported_mu=None,
        reason_unavailable=pump_reason_unavailable,
        tautological=is_chemostat,
    )

    # Warmup gate: the turbidostat is dormant for its first 8 cycles anyway,
    # and an estimate off the first one or two reads of a fresh vial is noise.
    if samples_seen is not None and samples_seen < int(warmup_samples):
        return GrowthReport(
            growth=GrowthEstimate(
                method=METHOD_CHEMOSTAT if is_chemostat else METHOD_SEGMENT,
                n_points=int(samples_seen),
                flags=_merge_flags((FLAG_WARMUP,), ctx_flags),
            ),
            regime=regime,
            dilution_check=diag,
        )

    # One pass, no per-sample function calls, floor applied inline: this runs
    # over the whole retained history for every vial on every recompute and is
    # the hottest loop in the module.
    # Clamped strictly positive: the filter below is now the ONLY thing
    # standing between a non-positive OD and math.log(), where the old
    # two-stage `od > 0` then `od >= floor` gave that guarantee for free.
    floor = max(float(od_floor), _MIN_POSITIVE_OD)
    above = [
        (t, od) for t, od in samples
        if floor <= od < _INF and cutoff <= t < _INF
    ]
    above.sort(key=lambda p: p[0])
    # Only needed to tell `low_od` from `short_span`, so it is computed only
    # when the estimate is about to be refused.
    n_valid = len(above) if len(above) >= MIN_SAMPLES else sum(
        1 for t, od in samples
        if 0.0 < od < _INF and cutoff <= t < _INF
    )

    if len(above) < MIN_SAMPLES:
        # Distinguish "the culture is too dilute to measure" from "we simply
        # have not logged enough yet" — they need different operator responses.
        flag = FLAG_LOW_OD if n_valid >= MIN_SAMPLES else FLAG_SHORT_SPAN
        return GrowthReport(
            growth=GrowthEstimate(
                method=METHOD_CHEMOSTAT if is_chemostat else METHOD_SEGMENT,
                n_points=len(above),
                span_seconds=(above[-1][0] - above[0][0]) if len(above) >= 2 else 0.0,
                flags=_merge_flags((flag,), ctx_flags),
            ),
            regime=regime,
            dilution_check=diag,
        )

    times = [t for t, _ in above]
    ods = [od for _, od in above]

    lag: Optional[float] = None
    if is_chemostat:
        growth = estimate_chemostat(
            times, ods,
            dilution_rate_per_hour=float(dilution_rate_per_hour or 0.0),
            extra_flags=ctx_flags,
        )
    elif regime == REGIME_CONTINUOUS:
        segments = split_segments(times, ods, recent_events)
        growth = estimate_segment(segments, extra_flags=ctx_flags)
    else:
        lo = hi = None
        if od_range is not None:
            lo, hi = float(od_range[0]), float(od_range[1])
        growth = estimate_batch(times, ods, lo=lo, hi=hi, extra_flags=ctx_flags)
        lag = lag_time_hours(times, ods, lo=lo, hi=hi, origin_s=origin_s)

    # Re-run the diagnostic now that a reported μ exists, so its disagreement
    # fraction has something to compare against.
    diag = dilution_check(
        recent_events,
        volume_ml,
        min(float(history_window_seconds), max(float(now) - cutoff, 0.0)),
        enabled=bool(pump_calibrated),
        reported_mu=growth.mu_per_hour,
        reason_unavailable=pump_reason_unavailable,
        tautological=is_chemostat,
    )

    return GrowthReport(
        growth=growth, regime=regime, dilution_check=diag, lag_time_hours=lag,
    )


# ---------------------------------------------------------------------------
# Notebook-compatibility helper — acceptance test only
# ---------------------------------------------------------------------------

def notebook_batch_fit(
    times_h: Sequence[float],
    ods: Sequence[float],
    *,
    lo: float = 0.1,
    hi: float = 0.5,
    window_samples: int = 8,
) -> Optional[tuple]:
    """The notebook's ``Doubling_time`` inner loop, with its own constants.

    Present **only** so ``GROWTH_RATE_METHOD.md`` §10's round-trip acceptance
    test can reproduce a plate-reader doubling time to within 1 % — the test
    that makes eVOLVER numbers and plate-reader numbers the same quantity. It
    is not part of the online estimator and must not be used by it: the band
    and the 8-sample window are a plate-reader cadence artefact and a
    strain/media-specific phase selector respectively (§1.1).

    ``times_h`` is in *hours* (as the notebook's ``Time_course``). Returns
    ``(doubling_time_minutes, r_squared, (start_i, end_i), windows_searched)``,
    or ``None`` where the notebook would raise on a reversed band.
    """
    n = len(times_h)
    if n < 2 or n != len(ods):
        return None
    i0 = _argmin_abs(ods, lo)
    i1 = _argmin_abs(ods, hi)
    band_len = i1 - i0 + 1
    best_r2 = -math.inf
    best: tuple = (i0, min(i0 + window_samples, n))
    searched = 0

    def _log2_fit(s: int, e: int):
        xs = list(times_h[s:e])
        ys = [math.log(o, 2.0) for o in ods[s:e] if o > 0]
        if len(ys) != len(xs) or len(xs) < 2:
            return None
        m = len(xs)
        mx, my = sum(xs) / m, sum(ys) / m
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx <= 0:
            return None
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        inter = my - slope * mx
        sst = sum((y - my) ** 2 for y in ys)
        ssr = sum((y - (inter + slope * x)) ** 2 for x, y in zip(xs, ys))
        return slope, (1.0 - ssr / sst if sst > 0 else None)

    if band_len - window_samples > 0:
        for i in range(band_len - window_samples):
            s = i0 + i
            e = s + window_samples
            r = _log2_fit(s, e)
            if r is None:
                continue
            searched += 1
            if r[1] is not None and r[1] > best_r2:
                best_r2, best = r[1], (s, e)
    else:
        s, e = i0, i1 + 1
        if e - s < 2:
            return None          # the notebook raises TypeError here (§2 #7)
        r = _log2_fit(s, e)
        if r is None:
            return None
        searched = 1
        best_r2, best = (r[1] if r[1] is not None else -math.inf), (s, e)

    r = _log2_fit(best[0], best[1])
    if r is None or r[0] == 0:
        return None
    return 60.0 / r[0], r[1], best, searched
