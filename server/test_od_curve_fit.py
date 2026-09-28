"""Tests for od_curve_fit.py -- the pure four-parameter logistic fit behind
the Tier 3 OD dilution series (ROADMAP Session AA / CALIBRATION_PROTOCOL
§8.2).

The synthetic truths are the committed 2016 curves themselves, shifted to
dark-subtracted signal and anchored to a plausible blank, so the fit is
exercised on exactly the shapes this machine produces -- including the
weakly-identified upper asymptotes of vials 0, 1, 6, 14 and 15.

Run from the project root:
    python -m pytest server/test_od_curve_fit.py
"""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import od_curve_fit as F  # noqa: E402
from serial_manager import SerialManager  # noqa: E402

REPO_CAL = Path(__file__).resolve().parent.parent / "calibration"
DARK = 1200.0
BLANK = 58000.0
LEVELS = np.array([0.0, 0.05, 0.1, 0.2, 0.4, 0.8, 1.2, 1.6, 2.0])


def _truth(vial: int) -> tuple[float, float, float, float]:
    """The 2016 curve for ``vial`` on dark-subtracted signal, anchored so
    OD 0 sits at a 58 000-count blank."""
    a, b, _c, d = np.genfromtxt(REPO_CAL / "OD_cal.txt", delimiter=",")[:, vial]
    a, b = a - DARK, b - DARK
    c = math.log10((b - a) / (BLANK - DARK - a) - 1.0) / d
    return a, b, c, d


def test_recovers_every_2016_shape_exactly_when_noiseless():
    for v in range(16):
        a, b, c, d = _truth(v)
        s = F.signal_from_od(LEVELS, a, b, c, d)
        f = F.fit_logistic4(LEVELS, s)
        for name, want in (("a", a), ("b", b), ("c", c), ("d", d)):
            assert abs(f[name] - want) <= 1e-6 * max(1.0, abs(want)), (v, name, f[name], want)
        assert max(abs(r) for r in f["residuals"]) < 1e-9, v
        assert not f["b_at_bound"], v


def test_every_measured_signal_is_inside_the_fitted_domain():
    """The reader's validity guard is a < S < b: a fit that put a measured
    point outside it would NaN that point at run time."""
    rng = np.random.default_rng(3)
    for v in range(16):
        a, b, c, d = _truth(v)
        s = F.signal_from_od(LEVELS, a, b, c, d) + rng.normal(0, 80, LEVELS.size)
        f = F.fit_logistic4(LEVELS, s)
        assert f["a"] < s.min() and f["b"] > s.max() and f["d"] < 0, v
        assert f["a"] >= 0.0


def test_well_constrained_vials_keep_a_real_upper_asymptote_under_noise():
    """Vials 9 and 12 have a best-fit b the data actually pin down."""
    rng = np.random.default_rng(11)
    grid = np.linspace(0.0, 1.6, 81)
    for v in (9, 12):
        a, b, c, d = _truth(v)
        s = F.signal_from_od(LEVELS, a, b, c, d) + rng.normal(0, 80, LEVELS.size)
        od = LEVELS * (1 + rng.normal(0, 0.01, LEVELS.size))
        f = F.fit_logistic4(od, s, np.full(LEVELS.size, 80.0))
        assert not f["b_at_bound"], v
        assert f["r2"] > 0.999
        true_signal = F.signal_from_od(grid, a, b, c, d)
        err = np.abs(F.od_from_signal(true_signal, f["a"], f["b"], f["c"], f["d"]) - grid)
        assert err.max() < 0.03, (v, err.max())


def test_unidentifiable_b_hits_the_cap_but_in_range_od_stays_accurate():
    """b = infinity (a pure log-linear response): the error keeps falling as
    b grows, so the fit must stop at the cap -- and say so -- without that
    costing accuracy inside the measured range."""
    a, k = 500.0, 2.2
    C = k * math.log10(BLANK - DARK - a)
    s = a + 10 ** ((C - LEVELS) / k)
    f = F.fit_logistic4(LEVELS, s)
    assert f["b_at_bound"]
    assert f["b"] <= F.B_CAP_FACTOR * s.max() * (1 + 1e-9)
    grid = np.linspace(0.0, 2.0, 101)
    err = np.abs(F.od_from_signal(a + 10 ** ((C - grid) / k),
                                  f["a"], f["b"], f["c"], f["d"]) - grid)
    assert err.max() < 0.005, err.max()


def test_never_worse_than_a_brute_force_grid():
    rng = np.random.default_rng(7)
    for v in (0, 1, 6, 9, 14):
        a, b, c, d = _truth(v)
        s = F.signal_from_od(LEVELS, a, b, c, d) + rng.normal(0, 80, LEVELS.size)
        od = LEVELS * (1 + rng.normal(0, 0.01, LEVELS.size))
        f = F.fit_logistic4(od, s)
        sse_fit = float(np.sum((np.asarray(f["od_pred"]) - od) ** 2))
        smin, smax = s.min(), s.max()
        G, H = np.meshgrid(
            np.linspace(math.log(1e-5 * smin), math.log(smin), 200),
            np.linspace(math.log(1e-5 * smax), math.log(9 * smax), 200),
            indexing="ij",
        )
        sse, _c, _k = F._fit_cells(od, s, smin - np.exp(G.ravel()),
                                   smax + np.exp(H.ravel()))
        assert sse_fit <= sse.min() * (1 + 1e-6), (v, sse_fit, sse.min())


def test_the_inflection_point_sits_below_od_zero_on_ordinary_curves():
    """The evidence behind demoting §8.2's "c inside the measured range" to
    a warning: on all sixteen blank-anchored 2016 shapes, c < 0."""
    for v in range(16):
        a, b, c, d = _truth(v)
        f = F.fit_logistic4(LEVELS, F.signal_from_od(LEVELS, a, b, c, d))
        assert f["c"] < 0.0, v


def test_non_monotone_data_is_reported_not_hidden():
    a, b, c, d = _truth(9)
    s = F.signal_from_od(LEVELS, a, b, c, d)
    s[[5, 6]] = s[[6, 5]]                    # two vials loaded out of order
    f = F.fit_logistic4(LEVELS, s, np.full(LEVELS.size, 80.0))
    assert f["monotone_violations"], f
    assert f["monotone_violations"][0]["od_low"] == 0.8
    clean = F.fit_logistic4(LEVELS, F.signal_from_od(LEVELS, a, b, c, d),
                            np.full(LEVELS.size, 80.0))
    assert clean["monotone_violations"] == []


def test_rejects_data_that_cannot_be_fitted():
    a, b, c, d = _truth(9)
    s = F.signal_from_od(LEVELS, a, b, c, d)
    for od, sig, why in (
        (LEVELS[:4], s[:4], "OD levels"),
        (np.repeat(LEVELS[:4], 3), np.repeat(s[:4], 3), "OD levels"),  # replicates aren't levels
        (LEVELS, np.where(LEVELS == 2.0, -5.0, s), "positive"),
        (LEVELS, s[::-1].copy(), "monotone"),                           # signal RISES with OD
    ):
        try:
            F.fit_logistic4(od, sig)
        except ValueError as exc:
            assert why in str(exc), (why, str(exc))
        else:
            raise AssertionError(f"accepted unfittable data ({why})")


def test_deterministic():
    rng = np.random.default_rng(5)
    a, b, c, d = _truth(0)
    s = F.signal_from_od(LEVELS, a, b, c, d) + rng.normal(0, 80, LEVELS.size)
    assert F.fit_logistic4(LEVELS, s) == F.fit_logistic4(LEVELS, s)


def test_fitted_rows_round_trip_through_the_serial_reader():
    """The rows the wizard installs are read back by SerialManager's own
    conversion; the two must agree exactly."""
    rows = np.zeros((4, 16))
    signals = np.zeros((LEVELS.size, 16))
    for v in range(16):
        a, b, c, d = _truth(v)
        signals[:, v] = F.signal_from_od(LEVELS, a, b, c, d)
        f = F.fit_logistic4(LEVELS, signals[:, v])
        rows[:, v] = (f["a"], f["b"], f["c"], f["d"])

    class _NullSer:
        def flushInput(self):
            pass
    sm = SerialManager(ser=_NullSer())
    sm.od_cal = rows
    for i, level in enumerate(LEVELS):
        got = sm._od_raw_to_OD(signals[i])
        assert np.allclose(got, level, atol=1e-9), (level, got)


def test_counts_per_od_matches_a_numeric_derivative():
    a, b, c, d = _truth(3)
    od, h = 0.3, 1e-6
    numeric = abs(F.signal_from_od(od + h, a, b, c, d) - F.signal_from_od(od - h, a, b, c, d)) / (2 * h)
    assert math.isclose(float(F.counts_per_od(od, a, b, c, d)), float(numeric), rel_tol=1e-5)


def test_sixteen_fits_are_fast_enough_to_run_in_a_request():
    """Measured ~0.2 s locally; the Pi is several times slower. Re-measure
    there with this test if the budget is ever in doubt."""
    signals = [F.signal_from_od(LEVELS, *_truth(v)) for v in range(16)]
    t0 = time.perf_counter()
    for s in signals:
        F.fit_logistic4(LEVELS, s)
    assert time.perf_counter() - t0 < 3.0
