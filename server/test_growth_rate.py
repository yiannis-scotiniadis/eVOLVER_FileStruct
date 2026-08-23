"""Tests for the growth-rate service (SPEC §17, GROWTH_RATE_METHOD.md §10).

Two halves:

* **Estimator** — `growth_rate.py` in isolation, against synthetic series with
  a known μ. Every box in GROWTH_RATE_METHOD.md §10's checklist that does not
  need the notebook's companion spreadsheets is here.
* **Integration** — the engine, the CSV, `status()`, and the export bundle,
  driven through the real `ExperimentEngine` and `MockSerialManager`.

Several tests assert the *failure* numbers as well as the success ones (the
naive cross-dilution fit's −80 %, the `ln(1+v/V)` mixing model's 9–15 % low
read). That is deliberate: a test that only proves the current code works
tells a future reader nothing about what it is protecting against.

Run from the project root:  python -m pytest server/test_growth_rate.py
"""

from __future__ import annotations

import math
import random
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import growth_rate as g                                        # noqa: E402
from data_logger import DataLogger, GROWTH_HEADER              # noqa: E402
from experiment_engine import ExperimentEngine                 # noqa: E402
from mock_serial_manager import MockSerialManager              # noqa: E402

CAL_DIR = Path(__file__).resolve().parent.parent / "calibration"
TEMP_CAL = str(CAL_DIR / "temp_calibration.txt")
OD_CAL = str(CAL_DIR / "OD_cal.txt")

DT = 10.0
V = 25.0
F = 1.0
NOISE = 0.004
SEED = 5


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def exponential(mu, hours=1.0, od0=0.2, noise=NOISE, seed=SEED, dt=DT):
    rng = random.Random(seed)
    n = int(hours * 3600 / dt)
    return [
        (i * dt, od0 * math.exp(mu * i * dt / 3600.0) + rng.gauss(0, noise))
        for i in range(n)
    ]


def turbidostat_series(mu, hours=6.0, lo=0.2, hi=0.4, od0=0.25,
                       pump_wait_s=0.0, seed=SEED):
    """Exponential culture with washout dilutions, `od *= exp(-v/V)`.

    The inverse of the formula the turbidostat uses, which is the right model
    because influx and efflux fire concurrently on separate pump bits.
    """
    rng = random.Random(seed)
    od, t = od0, 0.0
    samples, events = [], []
    last = -1e9
    for _ in range(int(hours * 3600 / DT)):
        od *= math.exp(mu * DT / 3600.0)
        samples.append((t, od + rng.gauss(0, NOISE)))
        if od > hi and (t - last) >= pump_wait_s:
            secs = float(int(min(math.log(od / lo) * V / F, 20.0)))
            if secs >= 1.0:
                od *= math.exp(-F * secs / V)
                events.append(g.DilutionEvent(t, t + secs + 5.0, secs * F))
                last = t
        t += DT
    return samples, events, t


# ===========================================================================
# Accuracy
# ===========================================================================

@pytest.mark.parametrize("mu", [0.35, 0.70, 1.20])
def test_pure_exponential_recovered_within_5_percent(mu):
    """§10: synthetic pure exponential at known μ recovered within 5 %."""
    samples = exponential(mu, hours=1.0)
    est = g.best_window_fit([t for t, _ in samples], [o for _, o in samples])
    assert est.mu_per_hour == pytest.approx(mu, rel=0.05)
    assert est.r_squared > 0.9


@pytest.mark.parametrize("mu", [0.35, 0.70, 1.20])
def test_dilution_events_do_not_depress_the_estimate(mu):
    """§10 + §3.3. Segmenting recovers μ; the naive cross-dilution fit that
    segmenting replaces reads catastrophically low, and this test pins that
    number so the reason for the machinery stays visible."""
    samples, events, now = turbidostat_series(mu)
    times = [t for t, _ in samples]
    ods = [o for _, o in samples]

    segmented = g.estimate_segment(g.split_segments(times, ods, events))
    assert segmented.mu_per_hour == pytest.approx(mu, rel=0.05)

    naive = g.fit_log_linear(times, [max(o, 1e-6) for o in ods])
    naive_err = (naive.mu_per_hour - mu) / mu
    assert naive_err < -0.5, (
        f"the naive fit should be catastrophically low; got {naive_err:+.1%}"
    )


def test_mixing_transient_rejected_by_the_window_search():
    """§10: a segment with a 120 s injected mixing transient is estimated
    within 5 %, where whole-segment OLS reads about +7 %."""
    mu, transient = 0.70, 120.0
    rng = random.Random(SEED)
    n = int(25 * 60 / DT)
    times = [i * DT for i in range(n)]
    ods = []
    for t in times:
        od = 0.2 * math.exp(mu * t / 3600.0)
        if t < transient:
            od *= 1.0 - 0.12 * math.exp(-3.0 * t / transient)
        ods.append(max(od + rng.gauss(0, NOISE), 1e-6))

    windowed = g.best_window_fit(times, ods)
    whole = g.fit_log_linear(times, ods)
    assert windowed.mu_per_hour == pytest.approx(mu, rel=0.05)
    assert abs(windowed.mu_per_hour - mu) < abs(whole.mu_per_hour - mu)


@pytest.mark.parametrize("d,mu_true", [(0.30, 0.30), (0.50, 0.50), (0.50, 0.62)])
def test_chemostat_mass_balance(d, mu_true):
    """§4.6: μ = D + d(ln OD)/dt, within 10 %, flagged `assumes_commanded_D`,
    and regime B is not attempted."""
    rng = random.Random(SEED)
    od = 0.4
    samples = []
    for i in range(int(3 * 3600 / DT)):
        od *= math.exp((mu_true - d) * DT / 3600.0)
        samples.append((i * DT, od + rng.gauss(0, NOISE)))
    rep = g.estimate(samples=samples, now=samples[-1][0], mode="chemostat",
                     dilution_rate_per_hour=d, samples_seen=len(samples))
    assert rep.regime == g.REGIME_CHEMOSTAT
    assert rep.growth.method == g.METHOD_CHEMOSTAT
    assert rep.growth.mu_per_hour == pytest.approx(mu_true, rel=0.10)
    assert g.FLAG_ASSUMES_COMMANDED_D in rep.growth.flags
    assert g.FLAG_R2_IS_DRIFT_FIT in rep.growth.flags


def test_chemostat_diagnostic_is_marked_tautological():
    """§8: in chemostat mode the dilution diagnostic is near-tautological, so
    its divergence signal must be suppressed rather than reported."""
    samples = exponential(0.5, hours=2.0)
    rep = g.estimate(samples=samples, now=samples[-1][0], mode="chemostat",
                     dilution_rate_per_hour=0.5, pump_calibrated=True,
                     samples_seen=len(samples))
    assert rep.dilution_check.tautological is True
    assert rep.dilution_check.disagreement_fraction is None


# ===========================================================================
# Refusals — the cases that must return None rather than a plausible number
# ===========================================================================

def test_low_od_returns_none_and_is_never_clamped():
    """§8: below the per-vial floor, return None. The notebook clamps to 0.001,
    which creates a plateau of identical values that a log-linear fit reads as
    zero growth — a wrong number where None was available."""
    samples = [(i * DT, 0.01) for i in range(200)]
    rep = g.estimate(samples=samples, now=samples[-1][0], od_floor=0.05,
                     samples_seen=len(samples))
    assert rep.growth.mu_per_hour is None
    assert g.FLAG_LOW_OD in rep.growth.flags


def test_short_history_returns_none():
    samples = exponential(0.7, hours=0.05)          # 3 min
    rep = g.estimate(samples=samples, now=samples[-1][0],
                     samples_seen=len(samples))
    assert rep.growth.mu_per_hour is None
    assert g.FLAG_SHORT_SPAN in rep.growth.flags


def test_warmup_is_dormant():
    """§8: the turbidostat is dormant for its first 8 cycles anyway."""
    samples = exponential(0.7, hours=1.0)
    rep = g.estimate(samples=samples, now=samples[-1][0], samples_seen=3,
                     warmup_samples=8)
    assert rep.growth.mu_per_hour is None
    assert g.FLAG_WARMUP in rep.growth.flags


def test_one_segment_is_insufficient_and_does_not_fall_back():
    """§8: fewer than two usable segments returns None. It must NOT fall back
    to a cross-dilution fit — that is the −94 % failure mode."""
    samples, events, now = turbidostat_series(0.7, hours=1.2)
    est = g.estimate_segment(
        g.split_segments([t for t, _ in samples], [o for _, o in samples],
                         events)[:1]
    )
    assert est.mu_per_hour is None
    assert g.FLAG_INSUFFICIENT_SEGMENTS in est.flags


def test_non_exponential_series_produces_a_low_r2():
    """§10: R² is reported, and a deliberately non-exponential series produces
    a low one."""
    rng = random.Random(SEED)
    times = [i * DT for i in range(400)]
    ods = [0.3 + 0.1 * math.sin(i / 15.0) + rng.gauss(0, NOISE)
           for i in range(400)]
    est = g.best_window_fit(times, ods)
    assert est.r_squared is not None
    assert est.r_squared < g.MIN_R2_TRUST
    assert g.FLAG_LOW_R2 in est.flags
    # Below 0.90 it is additionally `low_confidence`, so a UI can grey the
    # number out rather than merely annotate it.
    assert (est.r_squared < g.MIN_R2_REPORT) == (
        g.FLAG_LOW_CONFIDENCE in est.flags
    )


def test_low_r2_still_returns_the_number():
    """§8: a number *with* a low R² is still useful to a human; a number
    without its R² is worse than nothing."""
    rng = random.Random(SEED)
    times = [i * DT for i in range(400)]
    ods = [0.3 * math.exp(0.2 * t / 3600.0) + rng.gauss(0, 0.05)
           for t in times]
    est = g.best_window_fit(times, ods)
    assert est.mu_per_hour is not None
    assert est.r_squared is not None


# ===========================================================================
# The notebook's two silent/loud failures  (§2 #7 and #8)
# ===========================================================================

def test_flat_turbidostat_shaped_series_does_not_raise():
    """§2 #7: pointed at a flat OD ≈ 0.30 series the notebook's band search
    produces CI[1] < CI[0], slices empty, and raises
    `TypeError: expected non-empty vector for x`. Here it returns an estimate."""
    rng = random.Random(SEED)
    times = [i * DT for i in range(400)]
    ods = [0.30 + rng.gauss(0, 0.02) for _ in times]
    est = g.estimate_batch(times, ods, lo=0.1, hi=0.5)          # must not raise
    assert est.mu_per_hour is None or abs(est.mu_per_hour) < 0.2
    assert g.FLAG_BAND_NOT_SPANNED in est.flags or \
        g.FLAG_INSUFFICIENT_RANGE in est.flags


def test_reversed_range_is_flagged_not_raised():
    """The reversed-index failure reached directly: a series that spans the
    gate but crosses it downwards, so argmin|OD-hi| lands BEFORE argmin|OD-lo|
    and the notebook's `Time[CI[0]:CI[1]+1]` slices empty."""
    times = [i * DT for i in range(200)]
    ods = [0.6 - 0.00275 * i for i in range(200)]     # 0.600 down to 0.053
    assert min(ods) < 0.1 and max(ods) > 0.5          # the gate IS spanned
    est = g.estimate_batch(times, ods, lo=0.1, hi=0.5)
    assert est.mu_per_hour is None
    assert g.FLAG_INSUFFICIENT_RANGE in est.flags


def test_descending_series_that_misses_the_gate_still_reports_mu():
    """A washing-out culture that never drops to the gate floor is not a
    reversed range — it is a real, negative growth rate, and §8 says report
    it (with no doubling time) rather than swallowing it."""
    times = [i * DT for i in range(200)]
    ods = [0.5 - 0.002 * i for i in range(200)]       # 0.500 down to 0.102
    est = g.estimate_batch(times, ods, lo=0.1, hi=0.5)
    assert est.mu_per_hour < 0
    assert est.doubling_time_min is None
    assert g.FLAG_NEGATIVE_SLOPE in est.flags
    assert g.FLAG_BAND_NOT_SPANNED in est.flags


def test_stalled_culture_is_flagged_band_not_spanned():
    """§2 #8: a culture stalling below the range top returns DT = 131 min at
    R² = 0.991 from the notebook — a confident-looking number off a
    decelerating curve, with no warning because 0.991 clears 0.95. The high R²
    is the problem: the fit is excellent and the quantity fitted is not what
    the caller thinks."""
    rng = random.Random(SEED)
    times = [i * DT for i in range(1200)]
    ods = []
    for t in times:
        logistic = 0.25 / (1 + (0.25 / 0.001 - 1) * math.exp(-0.8 * t / 3600.0))
        ods.append(max(logistic + rng.gauss(0, 0.004), 1e-4))
    assert max(ods) < 0.5                            # never reaches the top
    est = g.estimate_batch(times, ods, lo=0.1, hi=0.5)
    assert g.FLAG_BAND_NOT_SPANNED in est.flags


# ===========================================================================
# Reporting contract
# ===========================================================================

def test_negative_slope_reports_mu_but_no_doubling_time():
    """§8: report μ (a shrinking culture is real information) but never
    `60/slope`. The notebook returns it and papers over the infinities
    downstream with `replace([inf, -inf], nan)` in a plotting cell."""
    times = [i * DT for i in range(400)]
    ods = [0.5 * math.exp(-0.3 * t / 3600.0) for t in times]
    est = g.best_window_fit(times, ods)
    assert est.mu_per_hour < 0
    assert est.doubling_time_min is None
    assert g.FLAG_NEGATIVE_SLOPE in est.flags


def test_doubling_time_matches_the_notebook_conversion():
    """§4.3: DT_min = 60 / slope_log2 = ln2 / mu * 60. The factor of 0.693 is
    exactly what gets silently absorbed as a 'calibration difference'."""
    mu = 0.9
    slope_log2 = mu / math.log(2.0)
    assert g.doubling_time_minutes(mu) == pytest.approx(60.0 / slope_log2)


def test_windows_searched_present_in_every_estimate():
    """§10: `windows_searched` is present in every returned estimate, because
    max-R² selection inflates R² and the reader needs to know over how many
    candidates the maximum was taken."""
    cases = [
        g.best_window_fit(*zip(*exponential(0.7, hours=1.0))),
        g.estimate_batch(*zip(*exponential(0.7, hours=1.0)), lo=0.1, hi=0.9),
        g.estimate(samples=exponential(0.7, hours=1.0), now=3600,
                   samples_seen=360).growth,
        g.estimate(samples=exponential(0.7, hours=0.02), now=100,
                   samples_seen=7).growth,
    ]
    for est in cases:
        assert isinstance(est.windows_searched, int)
        assert "windows_searched" in est.to_dict()


def test_lag_time_is_measured_from_the_supplied_origin():
    """The engine hands over a rolling 3 h window, so a lag measured from the
    first supplied sample is a number about the window, not the culture."""
    mu, od0 = 0.7, 0.02
    samples = exponential(mu, hours=4.0, od0=od0, noise=0.0)
    # Pretend only the last hour survives the rolling window.
    tail = [s for s in samples if s[0] >= samples[-1][0] - 3600]
    lag = g.lag_time_hours([t for t, _ in tail], [o for _, o in tail],
                           origin_s=0.0)
    assert lag == pytest.approx(-math.log(od0 / 0.001) / mu, rel=0.02)


# ===========================================================================
# Segmentation mechanics  (§4.5)
# ===========================================================================

def test_dilution_spanning_three_cycles_makes_exactly_one_boundary():
    """§10: one boundary, and no mid-dilution sample in either adjoining
    segment. `pump_time` is capped at 20 s against a 10 s loop, so a single
    dilution routinely spans two or three sensor cycles."""
    times = [i * DT for i in range(120)]           # 0 .. 1190 s
    ods = [0.3] * 120
    # Influx 20 s + 5 s overrun starting at t=500: covers 500..525, plus the
    # 60 s mixing skip, so 500 < t <= 585 is excised.
    event = g.DilutionEvent(t_start=500.0, t_efflux_end=525.0, delivered_ml=20.0)
    segs = g.split_segments(times, ods, [event])
    assert len(segs) == 2
    assert segs[0].times[-1] == 500.0
    assert segs[1].times[0] == 590.0
    excised = [t for t in times if 500.0 < t <= 585.0]
    assert excised, "the fixture must actually straddle sensor cycles"
    for seg in segs:
        assert not set(seg.times) & set(excised)


def test_overlapping_events_collapse_into_one_gap():
    """A manual pump during an automatic dilution's overrun must not produce
    an empty segment between them."""
    times = [i * DT for i in range(200)]
    ods = [0.3] * 200
    events = [
        g.DilutionEvent(500.0, 520.0, 20.0),
        g.DilutionEvent(540.0, 560.0, 5.0),
    ]
    segs = g.split_segments(times, ods, events)
    assert len(segs) == 2
    assert all(len(s.times) > 0 for s in segs)


def test_estimate_is_bit_identical_when_delivered_ml_is_doubled():
    """§10: proves volume never reaches the reported path. A segment boundary
    is knowable exactly; a bolus volume is `duration × an unmeasured flow
    rate`, and that dependency is what §0 kept out of the reported μ."""
    samples, events, now = turbidostat_series(0.7)
    kw = dict(samples=samples, now=now, mode="turbidostat", volume_ml=V,
              pump_calibrated=True, samples_seen=len(samples))
    a = g.estimate(dilution_events=events, **kw)
    doubled = [g.DilutionEvent(e.t_start, e.t_efflux_end, e.delivered_ml * 2)
               for e in events]
    b = g.estimate(dilution_events=doubled, **kw)
    assert a.growth.mu_per_hour == b.growth.mu_per_hour
    assert a.growth.r_squared == b.growth.r_squared
    # ...and the diagnostic, the only consumer of volume, moved exactly 2x.
    assert b.dilution_check.mu_per_hour == pytest.approx(
        2 * a.dilution_check.mu_per_hour
    )


# ===========================================================================
# The dilution diagnostic  (§4.4)
# ===========================================================================

def test_diagnostic_is_dark_without_pump_calibration():
    """§10: with `current.json` `"pump": null`, `enabled` is False, no
    divergence is raised, and `reason_unavailable` is populated so the UI can
    say *why* rather than showing a blank."""
    samples, events, now = turbidostat_series(0.7)
    rep = g.estimate(samples=samples, now=now, dilution_events=events,
                     mode="turbidostat", volume_ml=V, pump_calibrated=False,
                     samples_seen=len(samples))
    d = rep.dilution_check
    assert d.enabled is False
    assert d.mu_per_hour is None
    assert d.disagreement_fraction is None
    assert d.reason_unavailable and "calibrat" in d.reason_unavailable


def test_diagnostic_uses_v_over_V_and_agrees_within_10_percent():
    """§10's regression test for §4.4: with pump rates present the diagnostic
    agrees with the reported μ within 10 % **using v/V** — and demonstrably
    fails to when `ln(1+v/V)` is substituted."""
    mu = 0.70
    samples, events, now = turbidostat_series(mu, hours=8.0)
    rep = g.estimate(samples=samples, now=now, dilution_events=events,
                     mode="turbidostat", volume_ml=V, pump_calibrated=True,
                     samples_seen=len(samples))
    reported = rep.growth.mu_per_hour
    assert rep.dilution_check.mixing_model == g.MIXING_MODEL_PERFUSION
    assert rep.dilution_check.mu_per_hour == pytest.approx(reported, rel=0.10)

    # The SPEC §17 formula, over the same events and the same span.
    recent = sorted(
        (e for e in events if e.t_efflux_end >= now - g.HISTORY_WINDOW_SECONDS),
        key=lambda e: e.t_start,
    )
    hours = (recent[-1].t_start - recent[0].t_start) / 3600.0
    wrong = sum(math.log(1 + e.delivered_ml / V) for e in recent[1:]) / hours
    assert wrong == pytest.approx(reported, rel=0.30)      # same ballpark...
    assert abs(wrong - reported) / reported > 0.10, (
        "ln(1+v/V) must be visibly worse than v/V here, or this test is not "
        "protecting §4.4"
    )


def test_diagnostic_needs_two_events_to_state_a_rate():
    """One event gives no completed interval, so there is no rate to report."""
    d = g.dilution_check([g.DilutionEvent(0.0, 20.0, 5.0)], V, 3600.0,
                         enabled=True)
    assert d.enabled is True
    assert d.mu_per_hour is None
    assert "two dilution events" in d.reason_unavailable


# ===========================================================================
# Notebook round-trip  (§10's acceptance test)
# ===========================================================================

NOTEBOOK_DATA = (
    Path(__file__).resolve().parent.parent / "reference" / "2026_08_16 DEO.074-4.xlsx"
)


def test_notebook_helper_matches_a_hand_computed_log2_fit():
    """The notebook helper on a noiseless exponential, using the notebook's own
    constants (band 0.1–0.5, 8-sample window)."""
    mu = 0.9
    times_h = [i * 0.25 for i in range(96)]
    ods = [0.02 * math.exp(mu * t) for t in times_h]
    result = g.notebook_batch_fit(times_h, ods)
    assert result is not None
    dt_min, r2, _, searched = result
    assert dt_min == pytest.approx(math.log(2) / mu * 60.0, rel=1e-6)
    assert r2 == pytest.approx(1.0, abs=1e-9)
    assert searched >= 1


@pytest.mark.skipif(
    not NOTEBOOK_DATA.is_file(),
    reason=(
        "the plate-reader round-trip needs the notebook's companion files "
        f"({NOTEBOOK_DATA.name}, strain_layout.csv, strain_info.csv, "
        "growth_media.csv), which are not in the repository — "
        "GROWTH_RATE_METHOD.md §10.1"
    ),
)
def test_plate_reader_round_trip():        # pragma: no cover - needs lab data
    """§10's acceptance test: reproduce one well's notebook doubling time to
    within 1 %. This is what makes eVOLVER numbers and plate-reader numbers
    the same quantity."""
    raise AssertionError(
        "wire this to the notebook's well series once the xlsx is added"
    )


# ===========================================================================
# Integration — engine, CSV, status(), export
# ===========================================================================

class TmpRoot:
    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="evolver-growth-test-"))
        return self.path

    def __exit__(self, *args) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _engine(root: Path, clock_state):
    manager = MockSerialManager(seed=42)
    manager.load_calibration(TEMP_CAL, OD_CAL)
    logger = DataLogger(root)
    engine = ExperimentEngine(
        serial_manager=manager,
        data_logger=logger,
        experiments_root=root,
        temp_cal=np.genfromtxt(TEMP_CAL, delimiter=","),
        clock=lambda: clock_state["t"],
    )
    return engine, manager, logger


def _drive(engine, clock_state, *, vials, mu=0.9, cycles=400, od0=0.25,
           mode_is_chemostat=False):
    """Tick the engine with a synthetic exponential culture."""
    rng = random.Random(SEED)
    od = {v: od0 for v in vials}
    for i in range(cycles):
        temps = [37.0] * 16
        ods = [float("nan")] * 16
        for v in vials:
            od[v] *= math.exp(mu * DT / 3600.0)
            ods[v] = od[v] + rng.gauss(0, NOISE)
        ts = f"2026-08-23T{i // 360:02d}:{(i // 6) % 60:02d}:{(i % 6) * 10:02d}+00:00"
        actions = engine.run_cycle(ts, temps, ods)
        for vial, action in actions:
            od[vial] *= math.exp(-F * action.pump_time / V)
        clock_state["t"] += DT
    return od


def test_engine_surfaces_growth_in_status_and_csv():
    """§10: μ appears in `status()`, the WebSocket payload (via
    `growth_snapshot`), and the per-vial CSV."""
    with TmpRoot() as root:
        clock_state = {"t": 1_000_000.0}
        engine, _mgr, _logger = _engine(root, clock_state)
        vials = [0, 3]
        engine.create_experiment(
            name="GrowthStatus", mode="turbidostat", vials=vials,
            parameters={
                "temperature_c": 37, "stir_rate": 10, "volume_ml": V,
                "od_lower_thresh": 0.2, "od_upper_thresh": 0.6,
                "pump_wait_minutes": 15,
            },
        )
        engine.start_experiment("GrowthStatus")
        _drive(engine, clock_state, vials=vials, cycles=500)

        status = engine.status()
        for v in vials:
            block = status["per_vial"][str(v)]
            for key in ("mu_per_hour", "doubling_time_min", "r_squared",
                        "regime", "growth_flags", "dilution_check"):
                assert key in block, key
            assert block["mu_per_hour"] == pytest.approx(0.9, rel=0.10)
            assert block["doubling_time_min"] == pytest.approx(
                math.log(2) / block["mu_per_hour"] * 60.0
            )
            # No blank has ever been committed, so every estimate says so.
            assert g.FLAG_UNCALIBRATED_FLOOR in block["growth_flags"]
            assert block["dilution_check"]["enabled"] is False

        snapshot = engine.growth_snapshot()
        assert set(snapshot) == {str(v) for v in vials}
        assert "windows_searched" in snapshot["0"]

        for v in vials:
            path = root / "GrowthStatus" / f"vial{v:02d}_growth.csv"
            assert path.is_file()
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            assert lines[0].split(",") == list(GROWTH_HEADER)
            assert len(lines) > 2
            # Written on the 60 s throttle, not the 10 s sensor cadence.
            assert len(lines) - 1 <= 500 // 6 + 2


def test_growth_csv_flags_are_pipe_separated():
    """A comma inside `flags` would split one estimate across two columns for
    every line-based reader (data_export.filter_rows_by_hours splits on ',')."""
    with TmpRoot() as root:
        clock_state = {"t": 1_000_000.0}
        engine, _mgr, _logger = _engine(root, clock_state)
        engine.create_experiment(
            name="GrowthFlags", mode="turbidostat", vials=[0],
            parameters={"temperature_c": 37, "stir_rate": 10, "volume_ml": V,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.6},
        )
        engine.start_experiment("GrowthFlags")
        _drive(engine, clock_state, vials=[0], cycles=200)
        rows = (root / "GrowthFlags" / "vial00_growth.csv").read_text(
            encoding="utf-8").strip().splitlines()
        for line in rows[1:]:
            assert len(line.split(",")) == len(GROWTH_HEADER)


def test_od_csv_header_is_unchanged():
    """§7.7: `data_export.py` carries no schema marker, so any positional
    parser — including the lab's own analysis scripts — breaks silently on a
    column inserted into vialNN_OD.csv. Growth went to a parallel file for
    exactly this reason."""
    from data_logger import OD_HEADER
    assert OD_HEADER == (
        "timestamp", "elapsed_hours", "raw_adc", "calibrated_od",
        "n_valid", "flag", "dark",
    )


def test_suppressed_pump_creates_no_boundary():
    """§10 + §4.5: the consumables gate runs before the boundary is recorded,
    so a pump that never fired must not cut the OD series."""
    with TmpRoot() as root:
        clock_state = {"t": 1_000_000.0}
        engine, _mgr, _logger = _engine(root, clock_state)
        engine.create_experiment(
            name="GrowthSuppressed", mode="turbidostat", vials=[0],
            parameters={"temperature_c": 37, "stir_rate": 10, "volume_ml": V,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.4,
                        "pump_wait_minutes": 0},
            media={
                "bottles": [{
                    "id": "b", "name": "empty", "initial_volume_ml": 1.0,
                    "reserve_ml": 5.0,
                }],
                "vial_to_bottle": {"0": "b"},
                "waste": {"name": "w", "capacity_ml": 4000.0},
            },
        )
        engine.start_experiment("GrowthSuppressed")
        _drive(engine, clock_state, vials=[0], cycles=400, od0=0.35)
        # The bottle is below its reserve, so every dilution is suppressed.
        assert len(engine._dilution_events[0]) == 0
        assert engine.status()["per_vial"]["0"]["regime"] == g.REGIME_BATCH


def test_manual_efflux_only_pump_is_a_boundary_worth_zero_ml():
    """§4.5: removing culture does not change its concentration, so it
    contributes 0 mL to the diagnostic — but it perturbs the optical path and
    changes working volume, so it must still cut the series."""
    with TmpRoot() as root:
        clock_state = {"t": 1_000_000.0}
        engine, _mgr, _logger = _engine(root, clock_state)
        engine.create_experiment(
            name="GrowthManual", mode="turbidostat", vials=[0],
            parameters={"temperature_c": 37, "stir_rate": 10, "volume_ml": V,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.6},
        )
        engine.start_experiment("GrowthManual")
        engine.record_manual_pump(0, "efflux", 3.0)
        events = list(engine._dilution_events[0])
        assert len(events) == 1
        assert events[0].delivered_ml == 0.0

        engine.record_manual_pump(0, "influx", 3.0)
        events = list(engine._dilution_events[0])
        assert len(events) == 2
        assert events[1].delivered_ml == 3.0


def test_manual_pump_on_a_vial_with_no_bottle_still_cuts_the_series():
    """`record_manual_pump` returns early when the vial has no bottle mapping.
    The boundary must be recorded before that return — a pump disturbs the
    culture whether or not media accounting applies."""
    with TmpRoot() as root:
        clock_state = {"t": 1_000_000.0}
        engine, _mgr, _logger = _engine(root, clock_state)
        engine.create_experiment(
            name="GrowthNoBottle", mode="turbidostat", vials=[0],
            parameters={"temperature_c": 37, "stir_rate": 10, "volume_ml": V,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.6},
        )
        engine.start_experiment("GrowthNoBottle")
        assert engine._vial_to_bottle == {}
        engine.record_manual_pump(0, "influx", 2.0)
        assert len(engine._dilution_events[0]) == 1


def test_growth_context_flags_suspect_vials_and_the_missing_blank():
    """§8: vial 1's committed OD envelope says 'exclude from quantitative
    use', and no blank has ever been committed."""
    from calibration_service import CalibrationService
    with TmpRoot() as root:
        svc = CalibrationService(CAL_DIR, root, None)
        ctx = svc.growth_context(None)
        assert ctx["blank_present"] is False
        assert ctx["pump_calibrated"] is False
        assert 1 in ctx["suspect_vials"]
        assert ctx["pump_reason_unavailable"]


def test_engine_applies_context_flags():
    with TmpRoot() as root:
        clock_state = {"t": 1_000_000.0}
        engine, _mgr, _logger = _engine(root, clock_state)
        engine.set_growth_context({
            "od_floor": {0: 0.08},
            "blank_present": True,
            "suspect_vials": [0],
            "pump_calibrated": False,
        })
        engine.create_experiment(
            name="GrowthCtx", mode="turbidostat", vials=[0],
            parameters={"temperature_c": 37, "stir_rate": 10, "volume_ml": V,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.6,
                        "pump_wait_minutes": 15},
        )
        engine.start_experiment("GrowthCtx")
        _drive(engine, clock_state, vials=[0], cycles=300)
        flags = engine.status()["per_vial"]["0"]["growth_flags"]
        assert g.FLAG_CALIBRATION_SUSPECT in flags
        assert g.FLAG_UNCALIBRATED_FLOOR not in flags


def test_short_pump_wait_warns_that_segmentation_starves():
    """§4.5: `pump_wait` below the minimum fit span makes every segment too
    short to fit. Both TestForLabMeeting runs are configured this way."""
    from experiment_engine import validate_control_parameters
    warnings = validate_control_parameters(
        "turbidostat",
        {"od_lower_thresh": 0.2, "od_upper_thresh": 0.6,
         "volume_ml": V, "pump_wait_minutes": 5},
        [1.0] * 32, [0],
    )
    assert any("minimum fit span" in w for w in warnings)

    ok = validate_control_parameters(
        "turbidostat",
        {"od_lower_thresh": 0.2, "od_upper_thresh": 0.6,
         "volume_ml": V, "pump_wait_minutes": 15},
        [1.0] * 32, [0],
    )
    assert not any("minimum fit span" in w for w in ok)


def test_export_bundle_carries_the_growth_file():
    import data_export as dx
    with TmpRoot() as root:
        clock_state = {"t": 1_000_000.0}
        engine, _mgr, _logger = _engine(root, clock_state)
        engine.create_experiment(
            name="GrowthExport", mode="turbidostat", vials=[0],
            parameters={"temperature_c": 37, "stir_rate": 10, "volume_ml": V,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.6},
        )
        engine.start_experiment("GrowthExport")
        _drive(engine, clock_state, vials=[0], cycles=200)

        text = dx.growth_csv(root / "GrowthExport", [0])
        assert text is not None
        assert text.splitlines()[0].startswith("timestamp,elapsed_hours,vial,")

        fname, payload = dx.build_bundle(
            root / "GrowthExport", name="GrowthExport", vials=[0],
            parameters=["od", "temp"],
        )
        assert fname.endswith(".zip")
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            assert "GrowthExport_growth.csv" in zf.namelist()


def test_growth_csv_absent_from_a_pre_growth_experiment():
    """An experiment recorded before the growth service existed has no growth
    files; the export must skip the section rather than emit an empty one."""
    import data_export as dx
    with TmpRoot() as root:
        exp = root / "Old"
        exp.mkdir()
        (exp / "config.json").write_text("{}", encoding="utf-8")
        assert dx.growth_csv(exp, [0]) is None


# ===========================================================================
# Route level — GET /api/growth_rate and the sensor_update payload
# ===========================================================================

def _closure(fn, name):
    return fn.__closure__[fn.__code__.co_freevars.index(name)].cell_contents


def _mock_app(tmp: Path):
    """A --mock server rooted in a temp directory, so `resume_on_startup`
    cannot latch onto a real experiment in the repo's experiments/."""
    import app as A
    saved = (A.EXPERIMENTS_DIR, A.EXPORTS_DIR, A.LOGS_DIR)
    A.EXPERIMENTS_DIR = tmp / "experiments"
    A.EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    A.EXPORTS_DIR = tmp / "exports"
    A.LOGS_DIR = tmp / "logs"
    try:
        flask_app, _socketio = A.create_app(use_mock=True)
    finally:
        A.EXPERIMENTS_DIR, A.EXPORTS_DIR, A.LOGS_DIR = saved
    return flask_app


def test_api_growth_rate_idle_and_running():
    with TmpRoot() as root:
        flask_app = _mock_app(root)
        state = _closure(flask_app.view_functions["api_health"], "state")
        client = flask_app.test_client()

        idle = client.get("/api/growth_rate").get_json()
        assert idle["running"] is False
        assert idle["per_vial"] == {}

        body = {
            "name": "ApiGrowth", "mode": "turbidostat", "vials": [0],
            "parameters": {"temperature_c": 37, "stir_rate": 10,
                           "volume_ml": V, "od_lower_thresh": 0.2,
                           "od_upper_thresh": 0.6, "pump_wait_minutes": 15},
        }
        assert client.post("/api/experiments/create", json=body).status_code == 200
        started = client.post("/api/experiments/ApiGrowth/start",
                              json={"allow_missing_od_blank": True})
        assert started.status_code == 200

        # Drive the engine directly; the sensor thread's own cadence would
        # take 10 real minutes to clear MIN_FIT_SPAN_SECONDS. The app builds
        # its engine on `time.time`, so the clock is swapped for one that
        # advances a tick per cycle -- otherwise 500 iterations land inside
        # the same second and every span is zero.
        clock = [state.engine._clock()]
        state.engine._clock = lambda: clock[0]
        state.engine._growth_run_start = clock[0]
        rng = random.Random(SEED)
        od = 0.25
        for i in range(500):
            od *= math.exp(0.9 * DT / 3600.0)
            ods = [float("nan")] * 16
            ods[0] = od + rng.gauss(0, NOISE)
            ts = (f"2026-08-23T{i // 360:02d}:{(i // 6) % 60:02d}:"
                  f"{(i % 6) * 10:02d}+00:00")
            for _vial, action in state.engine.run_cycle(ts, [37.0] * 16, ods):
                od *= math.exp(-F * action.pump_time / V)
            clock[0] += DT

        payload = client.get("/api/growth_rate").get_json()
        assert payload["running"] is True
        assert payload["experiment"] == "ApiGrowth"
        assert payload["recompute_interval_seconds"] == g.RECOMPUTE_INTERVAL_SECONDS
        block = payload["per_vial"]["0"]
        assert block["mu_per_hour"] == pytest.approx(0.9, rel=0.10)
        # r_squared never travels without windows_searched beside it.
        assert block["r_squared"] is not None
        assert isinstance(block["windows_searched"], int)
        assert block["dilution_check"]["enabled"] is False
        assert block["dilution_check"]["reason_unavailable"]
        assert block["dilution_check"]["mixing_model"] == g.MIXING_MODEL_PERFUSION
        assert g.FLAG_UNCALIBRATED_FLOOR in block["flags"]

        state.engine.stop_experiment(reason="test")
        after = client.get("/api/growth_rate").get_json()
        assert after["running"] is False


# ===========================================================================
# The prefix-sum fast path must not change any answer
# ===========================================================================

def _reference_best_window(times, ods, *, window_seconds=None):
    """Brute-force reference: fit every candidate window with the direct
    implementation and keep the best R². This is what `best_window_fit` did
    before the prefix-sum scanner replaced the inner loop."""
    pairs = sorted((float(t), float(o)) for t, o in zip(times, ods)
                   if o == o and o > 0)
    times = [t for t, _ in pairs]
    ods = [o for _, o in pairs]
    n = len(times)
    span = times[-1] - times[0]
    if n < g.MIN_SAMPLES or span < g.MIN_FIT_SPAN_SECONDS:
        return None
    cadence = g._median_cadence(times)
    window = window_seconds or min(
        max(g.WINDOW_FRACTION * span, g.MIN_FIT_SPAN_SECONDS),
        g.PREFERRED_FIT_SPAN_SECONDS,
    )
    window = min(window, span)
    slack = span - window
    step = (max(window, 1e-9) if slack <= 0
            else max(cadence, slack / g.MAX_WINDOW_CANDIDATES, 1e-9))
    expected = (window / cadence + 1.0) if cadence > 0 else float(g.MIN_SAMPLES)
    min_n = max(g.MIN_SAMPLES,
                int(math.ceil((1 - g.MAX_MISSING_FRACTION) * expected)))
    best, best_bounds, searched = None, None, 0
    start = times[0]
    while start <= times[-1] - window + 1e-6:
        import bisect as _b
        lo = _b.bisect_left(times, start)
        hi = _b.bisect_right(times, start + window)
        if hi - lo >= min_n:
            fit = g.fit_log_linear(times[lo:hi], ods[lo:hi])
            if fit is not None and fit.r_squared is not None:
                searched += 1
                if best is None or fit.r_squared > best.r_squared:
                    best, best_bounds = fit, (lo, hi)
        start += step
    return best, best_bounds, searched


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_prefix_sum_scanner_matches_the_direct_fit(seed):
    """The scanner picks the window; `fit_log_linear` produces the numbers.
    Both halves must agree with the brute-force reference, or the speedup
    changed an answer."""
    rng = random.Random(seed)
    mu = rng.uniform(0.3, 1.4)
    n = int(rng.uniform(1.0, 3.0) * 3600 / DT)
    times = [i * DT for i in range(n)]
    ods = [max(0.2 * math.exp(mu * t / 3600.0) + rng.gauss(0, 0.006), 1e-6)
           for t in times]

    ref = _reference_best_window(times, ods)
    est = g.best_window_fit(times, ods)
    assert ref is not None
    ref_fit, _bounds, ref_searched = ref
    assert est.windows_searched == ref_searched
    assert est.mu_per_hour == pytest.approx(ref_fit.mu_per_hour, rel=1e-9)
    assert est.r_squared == pytest.approx(ref_fit.r_squared, rel=1e-9)


def test_scanner_is_robust_to_large_absolute_timestamps():
    """Prefix-sum differencing is a cancelling subtraction. Epoch-second
    timestamps square past 2^53, so time is recentred before the sums are
    built; without that the variance terms are noise. Same series, shifted by
    a real epoch, must give the same answer."""
    rng = random.Random(9)
    n = 400
    ods = [max(0.2 * math.exp(0.8 * i * DT / 3600.0) + rng.gauss(0, 0.004), 1e-6)
           for i in range(n)]
    near_zero = g.best_window_fit([i * DT for i in range(n)], ods)
    epoch = g.best_window_fit([1_787_000_000.0 + i * DT for i in range(n)], ods)
    assert near_zero.mu_per_hour == pytest.approx(epoch.mu_per_hour, rel=1e-9)
    assert near_zero.r_squared == pytest.approx(epoch.r_squared, rel=1e-9)
    assert near_zero.windows_searched == epoch.windows_searched


# ===========================================================================
# Staggering — the burst must not land on one tick
# ===========================================================================

def test_growth_recompute_is_spread_across_ticks():
    """The recompute runs on the sensor thread inside the engine lock. All 16
    vials in one tick is the shape that stalls a tick on the deployment Pi;
    ceil(16/6)=3 per tick is not. Every vial must still refresh once per
    interval."""
    with TmpRoot() as root:
        clock_state = {"t": 1_000_000.0}
        engine, _mgr, _logger = _engine(root, clock_state)
        vials = list(range(16))
        engine.create_experiment(
            name="GrowthStagger", mode="turbidostat", vials=vials,
            parameters={"temperature_c": 37, "stir_rate": 10, "volume_ml": V,
                        "od_lower_thresh": 0.2, "od_upper_thresh": 0.6,
                        "pump_wait_minutes": 15},
        )
        engine.start_experiment("GrowthStagger")

        cap = math.ceil(len(vials) / g.VIALS_PER_RECOMPUTE_GROUP_DIVISOR)
        per_tick = []
        for _ in range(60):                      # 10 min of ticks
            now = clock_state["t"]
            per_tick.append(len(engine._growth_due_vials_locked(now)))
            engine._maybe_update_growth_locked(now, "2026-08-23T00:00:00+00:00")
            clock_state["t"] += DT

        assert max(per_tick) <= cap, (
            f"a tick recomputed {max(per_tick)} vials; the cap is {cap}"
        )
        # ...and nothing is starved: over 10 min (10 intervals) every vial
        # must have come due repeatedly.
        counts = {v: 0 for v in vials}
        clock_state["t"] = 1_000_000.0
        engine._reset_growth_state_locked()
        for _ in range(36):                      # 6 min = 6 intervals
            now = clock_state["t"]
            for v in engine._growth_due_vials_locked(now):
                counts[v] += 1
            engine._maybe_update_growth_locked(now, "2026-08-23T00:00:00+00:00")
            clock_state["t"] += DT
        assert min(counts.values()) >= 5, counts
        assert max(counts.values()) - min(counts.values()) <= 1, counts


def test_zero_od_floor_cannot_reach_math_log():
    """The hot-path filter is the only guard between a non-positive OD and
    math.log(); a caller passing floor=0 must not crash it."""
    samples = [(i * DT, 0.0 if i % 3 == 0 else 0.3) for i in range(400)]
    rep = g.estimate(samples=samples, now=samples[-1][0], od_floor=0.0,
                     samples_seen=len(samples))
    assert rep.growth is not None          # no exception
