"""Tests for the pump frame scheduler (``server/fluidics.py``).

The load-bearing test is ``test_randomized_schedule_sweep``: it integrates
every pump's on/off state second by second through the generated frames and
asserts that **no vial ever has influx running while its efflux is off**. That
is the property whose absence overflowed the pilot run -- influx and efflux
fired as separate frames serialise on this firmware (FLUIDICS_FIRMWARE_AUDIT.md
FW-1, §3.3), so the level rose by the whole bolus before anything drained.

The rest pin down the other guarantees: the schedule changes no pump's dose,
the overrun survives exactly, and bus time is the longest vial's rather than
the sum.

Run from the project root:  python -m pytest server/test_fluidics.py
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fluidics as fl  # noqa: E402
from fluidics import Dilution, Frame  # noqa: E402

IN0, EF0 = 1 << 0, 1 << 16


def _pair(vial: int) -> int:
    return (1 << vial) | (1 << (vial + 16))


# ---------------------------------------------------------------------------
# Index, quantisation, validation
# ---------------------------------------------------------------------------

def test_pump_index_matches_wire_convention():
    assert fl.pump_index(0, "influx") == 0
    assert fl.pump_index(15, "influx") == 15
    assert fl.pump_index(0, "efflux") == 16
    assert fl.pump_index(15, "efflux") == 31
    with pytest.raises(ValueError):
        fl.pump_index(16, "influx")
    with pytest.raises(ValueError):
        fl.pump_index(0, "both")


def test_quantise_dilution():
    assert fl.quantise_dilution(3.0, 2.0) == (3, 5)
    assert fl.quantise_dilution(3.0, 0.0) == (3, 3)
    assert fl.quantise_dilution(20.0, 5.0) == (20, 25)
    # A fractional overrun rounds toward MORE efflux; the straw absorbs it.
    assert fl.quantise_dilution(3.0, 2.5) == (3, 6)
    assert fl.quantise_dilution(3.0, 0.2) == (3, 4)
    # Float noise on a whole-second overrun is not an extra second.
    assert fl.quantise_dilution(3.0, 2.0000000001) == (3, 5)
    # An influx that rounds to 0 is no dilution: no overrun fires either.
    assert fl.quantise_dilution(0.3, 2.0) == (0, 0)
    with pytest.raises(ValueError):
        fl.quantise_dilution(-1.0, 2.0)
    with pytest.raises(ValueError):
        fl.quantise_dilution(3.0, -1.0)


def test_quantise_matches_pre_schedule_rounding_for_whole_overruns():
    """For whole-second overruns the efflux total is byte-identical to what
    the old two-frame path sent (round(pump_time + extra))."""
    for pump_time in range(1, 21):
        for extra in (0.0, 1.0, 2.0, 5.0):
            _, efflux_s = fl.quantise_dilution(float(pump_time), extra)
            assert efflux_s == int(round(pump_time + extra))


@pytest.mark.parametrize("mask,seconds", [
    (0, 1), (1 << 32, 1), (-1, 1), (1, 0), (1, -3), (1, 1.5), (True, 1),
])
def test_frame_rejects_invalid(mask, seconds):
    with pytest.raises(ValueError):
        Frame(mask, seconds)


def test_frame_helpers():
    f = Frame(_pair(0), 10)
    assert f.pumps() == (0, 16)
    assert f.mask_bits == "10000000000000001"


# ---------------------------------------------------------------------------
# Worked examples
# ---------------------------------------------------------------------------

def test_single_vial_is_the_legacy_shape():
    """mac_original/custom_script.py:114-117: one OR'd frame for the bolus,
    then one efflux-only frame for the overrun."""
    assert fl.plan_dilution_frames([Dilution(0, 10, 12)]) == [
        Frame(IN0 | EF0, 10),
        Frame(EF0, 2),
    ]
    # No overrun: a single combined frame.
    assert fl.plan_dilution_frames([Dilution(0, 10, 10)]) == [Frame(IN0 | EF0, 10)]


def test_two_vial_example_from_the_module_docstring():
    a, b = 3, 7
    frames = fl.plan_dilution_frames([Dilution(a, 3, 5), Dilution(b, 5, 7)])
    assert frames == [
        Frame(_pair(a) | _pair(b), 3),
        Frame((1 << (a + 16)) | _pair(b), 2),
        Frame(1 << (b + 16), 2),
    ]
    assert fl.span_seconds(frames) == 7          # the old path: 3+5+5+7 = 20


def test_nested_masks_cost_max_not_sum():
    """FLUIDICS_FIRMWARE_AUDIT.md §4.2: A, B, C needing 3, 5, 8 s."""
    frames = fl.decompose({0: 3, 1: 5, 2: 8})
    assert frames == [Frame(0b111, 3), Frame(0b110, 2), Frame(0b100, 3)]
    assert fl.span_seconds(frames) == 8


def test_identical_dilutions_collapse_to_two_frames():
    """16 chemostat vials at the same bolus: 2 frames, 7 s of bus -- was
    32 frames and 16 x (5 + 7) = 192 s."""
    frames = fl.plan_dilution_frames([Dilution(v, 5, 7) for v in range(16)])
    assert frames == [Frame((1 << 32) - 1, 5), Frame(((1 << 16) - 1) << 16, 2)]


def test_empty_and_zero_influx():
    assert fl.plan_dilution_frames([]) == []
    assert fl.plan_dilution_frames([Dilution(4, 0, 0)]) == []
    frames = fl.plan_dilution_frames([Dilution(4, 0, 0), Dilution(5, 2, 2)])
    assert frames == [Frame(_pair(5), 2)]


def test_duplicate_vial_raises():
    with pytest.raises(ValueError, match="twice"):
        fl.plan_dilution_frames([Dilution(2, 3, 5), Dilution(2, 4, 6)])


def test_efflux_shorter_than_influx_raises():
    with pytest.raises(ValueError):
        fl.plan_dilution_frames([Dilution(2, 5, 3)])


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

def _pump_on_at(frames: list[Frame], t: int, pump: int) -> bool:
    """Whether ``pump`` runs during second ``t`` of the schedule, frames
    executed back to back (FW-1) -- inter-frame gaps ignored (§6.1)."""
    start = 0
    for f in frames:
        if start <= t < start + f.seconds:
            return bool(f.mask >> pump & 1)
        start += f.seconds
    return False


def test_randomized_schedule_sweep():
    rng = random.Random(20260923)
    for _ in range(1500):
        n = rng.randint(1, 16)
        vials = rng.sample(range(16), n)
        extra = rng.choice([0.0, 1.0, 2.0, 2.5, 5.0])
        dils = [
            Dilution(v, *fl.quantise_dilution(float(rng.randint(1, 20)), extra))
            for v in vials
        ]
        frames = fl.plan_dilution_frames(dils)

        # The schedule changes no pump's dose.
        want = {}
        for d in dils:
            want[fl.pump_index(d.vial, "influx")] = d.influx_s
            want[fl.pump_index(d.vial, "efflux")] = d.efflux_s
        assert fl.seconds_per_pump(frames) == want

        # Bus time is the longest vial, and never more frames than end times.
        assert fl.span_seconds(frames) == max(d.efflux_s for d in dils)
        assert len(frames) == len({s for s in want.values()})
        assert len(frames) <= 2 * n

        # Masks are nested and non-empty; frame 0 holds every bit.
        assert frames[0].mask == sum(1 << p for p in want)
        for prev, nxt in zip(frames, frames[1:]):
            assert nxt.mask and (nxt.mask & ~prev.mask) == 0

        # Second by second: each pump runs one contiguous block from t=0, and
        # no vial ever has influx on while its efflux is off.
        for t in range(fl.span_seconds(frames)):
            for d in dils:
                i, e = fl.pump_index(d.vial, "influx"), fl.pump_index(d.vial, "efflux")
                assert _pump_on_at(frames, t, i) == (t < d.influx_s)
                assert _pump_on_at(frames, t, e) == (t < d.efflux_s)
                if _pump_on_at(frames, t, i):
                    assert _pump_on_at(frames, t, e), (
                        f"vial {d.vial} influx without efflux at t={t}: {frames}"
                    )

        # The overrun survives exactly.
        for d in dils:
            assert d.efflux_s - d.influx_s == fl.quantise_dilution(1.0, extra)[1] - 1
