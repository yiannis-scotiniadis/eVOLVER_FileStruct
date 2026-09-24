"""server/fluidics.py — pump frame scheduling for the 2016 fluidics firmware.

Pure and I/O-free: plain data in, frozen dataclasses out. No imports from
``experiment_engine``, ``app``, ``serial_manager`` or any control mode, so the
schedule a cycle will put on the wire is testable without hardware (same
discipline as ``growth_rate.py``).

**The firmware model this is built on** (measured, ``FLUIDICS_FIRMWARE_AUDIT.md``
§2 — treat as ground truth):

* FW-2: one ``st<mask>,0,<s>, !`` frame drives **every pump in its mask
  concurrently**.
* FW-1 / FW-6: frames **queue** and run one at a time, in order, to completion;
  nothing preempts.
* FW-4 / FW-5: durations are **exact whole seconds** and **additive** across
  frames on the same pump.

**Why a schedule and not one frame per pump.** ``SerialManager.pump_command``
sets one bit, so a dilution sent as "influx frame, then efflux frame" runs the
two *serially* — nothing drains while influx runs and the level rises by the
whole bolus (audit §3.3, the pilot-run overflow). Sending vials one after
another also makes a cycle's bus time the *sum* of every vial's durations.

Here every pump in a cycle starts at t=0 and runs for its own whole-second
total; the schedule is cut into frames at each distinct end time::

    vial A: influx 3 s, efflux 5 s     frame {A_in, A_ef, B_in, B_ef}  3 s
    vial B: influx 5 s, efflux 7 s     frame {A_ef, B_in, B_ef}        2 s
                                       frame {B_ef}                    2 s

    bus time 7 s = the longest vial, not the sum (3+5+5+7 = 20 s)

Guarantees, all by construction (and all tested in ``test_fluidics.py``):

* each pump's summed frame seconds equal its request exactly;
* masks are nested — each frame's pumps are a subset of the previous frame's;
* frame 0 holds every influx and efflux bit, so a vial's efflux is running at
  every second its influx is;
* the efflux-only overrun (``efflux_extra_seconds``) is the tail of each vial's
  efflux run, exactly as in ``mac_original/custom_script.py:114-117``.

The inter-frame pause and start/stop transient are **deliberately not
compensated** (audit §6.1, operator decision 2026-09-23). Post-run mass
reconciliation (SPEC §19.4) is the detector if that bias ever matters.

Headroom-aware influx chunking (SPEC §16.3.4) is not implemented here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

N_VIALS = 16
N_PUMPS = 32

# FW-12: the firmware buffered 15 frames (~270 bytes) with zero loss while a
# pump was running. Nothing larger was tested, so a batch above this is
# unverified territory -- dispatchers should say so rather than split silently.
FIRMWARE_QUEUE_VERIFIED_FRAMES = 15

# A float efflux_extra_seconds of 2.0000000001 is 2 s, not 3.
_CEIL_TOLERANCE = 1e-9


class FluidicsDispatchError(RuntimeError):
    """A pump frame batch failed part-way through being written.

    ``frames_sent`` frames reached the wire before the failure. They are
    already in the firmware queue and cannot be recalled (FW-11), so callers
    must account for them as delivered.
    """

    def __init__(self, message: str, frames_sent: int) -> None:
        super().__init__(message)
        self.frames_sent = int(frames_sent)


def pump_index(vial: int, direction: str) -> int:
    """Canonical pump index (``CLAUDE.md``): 0..15 influx, 16..31 efflux.

    The index is also the bit exponent in the wire mask."""
    if not (0 <= vial < N_VIALS):
        raise ValueError(f"vial must be in 0..{N_VIALS - 1}, got {vial}")
    if direction == "influx":
        return int(vial)
    if direction == "efflux":
        return int(vial) + N_VIALS
    raise ValueError(f"direction must be 'influx' or 'efflux', got {direction!r}")


def check_mask(mask: int) -> int:
    """Validate a 32-pump bitmask; returns it as an int."""
    if isinstance(mask, bool) or not isinstance(mask, int):
        raise ValueError(f"pump mask must be an int, got {mask!r}")
    if not (0 < mask < (1 << N_PUMPS)):
        raise ValueError(f"pump mask must be in 1..2^{N_PUMPS}-1, got {mask}")
    return mask


def quantise_dilution(pump_time: float, efflux_extra_seconds: float) -> tuple[int, int]:
    """Whole-second ``(influx_s, efflux_s)`` for one dilution.

    The single home of this rule -- the engine's dilution boundaries and the
    dispatcher both call it, so the recorded interval is the fired interval.

    * ``influx_s = round(pump_time)``. Every control mode already emits whole
      seconds, so this is exact in practice.
    * ``efflux_s = influx_s + ceil(efflux_extra_seconds)``. For whole-second
      overruns this is identical to the pre-schedule ``round(pump_time +
      extra)``; a fractional overrun rounds toward *more* efflux, which the
      straw absorbs.
    * An influx that rounds to 0 is no dilution at all: ``(0, 0)``, and no
      overrun fires.
    """
    pump_time = float(pump_time)
    extra = float(efflux_extra_seconds)
    if pump_time < 0:
        raise ValueError(f"pump_time must be >= 0, got {pump_time}")
    if extra < 0:
        raise ValueError(f"efflux_extra_seconds must be >= 0, got {extra}")
    influx_s = int(round(pump_time))
    if influx_s == 0:
        return 0, 0
    overrun_s = max(0, math.ceil(extra - _CEIL_TOLERANCE))
    return influx_s, influx_s + overrun_s


@dataclass(frozen=True)
class Frame:
    """One ``st<mask>,0,<seconds>, !`` command: every pump in ``mask`` runs
    concurrently for ``seconds``."""

    mask: int
    seconds: int

    def __post_init__(self) -> None:
        check_mask(self.mask)
        if isinstance(self.seconds, bool) or not isinstance(self.seconds, int):
            raise ValueError(f"frame seconds must be an int, got {self.seconds!r}")
        if self.seconds < 1:
            raise ValueError(f"frame seconds must be >= 1, got {self.seconds}")

    def pumps(self) -> tuple[int, ...]:
        """Pump indices in this frame, ascending."""
        return tuple(p for p in range(N_PUMPS) if self.mask >> p & 1)

    @property
    def mask_bits(self) -> str:
        """The mask as it appears on the wire (bit 0 rightmost)."""
        return format(self.mask, "b")


@dataclass(frozen=True)
class Dilution:
    """One vial's dilution: influx and efflux start together; efflux runs
    ``efflux_s - influx_s`` seconds longer (the overrun)."""

    vial: int
    influx_s: int
    efflux_s: int


def decompose(runs: Mapping[int, int]) -> list[Frame]:
    """Frames for pumps that all start at t=0 and run ``runs[pump]`` seconds.

    Cuts at each distinct end time; the frame for ``[prev, end)`` holds every
    pump still running. Pumps with 0 s are dropped.
    """
    active: dict[int, int] = {}
    for pump, seconds in runs.items():
        if not (0 <= pump < N_PUMPS):
            raise ValueError(f"pump index must be in 0..{N_PUMPS - 1}, got {pump}")
        if isinstance(seconds, bool) or not isinstance(seconds, int):
            raise ValueError(f"pump {pump}: seconds must be an int, got {seconds!r}")
        if seconds < 0:
            raise ValueError(f"pump {pump}: seconds must be >= 0, got {seconds}")
        if seconds > 0:
            active[pump] = seconds

    frames: list[Frame] = []
    prev = 0
    for end in sorted(set(active.values())):
        mask = 0
        for pump, seconds in active.items():
            if seconds >= end:
                mask |= 1 << pump
        frames.append(Frame(mask=mask, seconds=end - prev))
        prev = end
    return frames


def plan_dilution_frames(dilutions: Iterable[Dilution]) -> list[Frame]:
    """One cycle's dilutions as a single concurrent schedule.

    Vials with ``influx_s == 0`` are skipped (no dilution, no overrun). A vial
    may appear once per schedule: two dilutions for one vial in one cycle
    would be summed by the firmware (FW-5) and is a caller bug.
    """
    runs: dict[int, int] = {}
    seen: set[int] = set()
    for d in dilutions:
        if d.vial in seen:
            raise ValueError(f"vial {d.vial} appears twice in one pump schedule")
        seen.add(d.vial)
        if d.influx_s < 0 or d.efflux_s < d.influx_s:
            raise ValueError(
                f"vial {d.vial}: need 0 <= influx_s <= efflux_s, "
                f"got influx_s={d.influx_s} efflux_s={d.efflux_s}"
            )
        if d.influx_s == 0:
            continue
        runs[pump_index(d.vial, "influx")] = d.influx_s
        runs[pump_index(d.vial, "efflux")] = d.efflux_s
    return decompose(runs)


def seconds_per_pump(frames: Sequence[Frame]) -> dict[int, int]:
    """Total seconds each pump runs across ``frames`` -- the inverse of
    :func:`decompose`. Pumps that never run are absent."""
    totals: dict[int, int] = {}
    for frame in frames:
        for pump in frame.pumps():
            totals[pump] = totals.get(pump, 0) + frame.seconds
    return totals


def span_seconds(frames: Sequence[Frame]) -> int:
    """Firmware-queue time the schedule occupies (inter-frame gaps excluded)."""
    return sum(f.seconds for f in frames)
