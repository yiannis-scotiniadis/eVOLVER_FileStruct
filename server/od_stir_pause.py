"""server/od_stir_pause.py — per-vial stir pause before OD reads (SPEC §9.1).

Pure and I/O-free, like ``fluidics.py`` and ``run_config.py``: plain data in,
plain data out, a monotonic clock injected. The supervisor owns one scheduler
for the whole machine and drives it from the sensor loop.

**Why per-vial acceptance, and why nothing ever blocks.** The machine keeps
reading all sixteen photodiodes on every tick, exactly as it would with no
pause configured. What a pausing vial changes is *which of those reads it
accepts*:

1. **Pause** -- the composed stir write drops the vial to ``pause_pwm``
   (:meth:`StirPauseScheduler.stir_overrides`).
2. **Settle** -- for at least ``settle_seconds`` its reads are labelled
   :data:`STIR_SETTLING` and discarded. Temperature, heater safety and every
   other vial carry on at the normal tick.
3. **Settled read** -- the first OD acquisition that *starts* at least
   ``settle_seconds`` after the pause is :data:`ACCEPTED`
   (:meth:`StirPauseScheduler.classify`).
4. **Restore** -- stir comes back in that same tick's composed write, and any
   dilution decided on that read fires after it, so pumps always run into a
   stirred vial.
5. **Stir** -- reads between pauses are :data:`STIR_UNSETTLED` and
   discarded, until the next pause is due (``paused_at + period_seconds``) and
   at least ``min_mix_seconds`` have passed since the vial's last dilution.

Blocking the sensor thread for a settle instead would stall the temperature
lane too, and over-temperature detection is three consecutive reads of that
lane. The ``zv`` frame this needs is the one the supervisor already sends
every tick, so the pause also costs no bus time.

Because acceptance is per vial, two groups -- or two parallel experiments --
that disagree about pausing share one tick without anything to merge.

State is deliberately not persisted. After a restart every vial starts
stirring, and the next composed write restores its normal stir.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Optional

from run_config import StirPause

#: The read was taken at the end of a settle: the vial accepts it.
ACCEPTED = "accepted"
#: The vial's stirrer is paused and has not settled for long enough yet.
STIR_SETTLING = "stir_settling"
#: The vial is stirring between pauses (or its pause is suspended).
STIR_UNSETTLED = "stir_unsettled"

#: OD flag values the engine treats as "this vial did not accept this read".
#: They are distinct from the reader's own ``dropped`` / ``out_of_range``:
#: the bus delivered data and the sleeve is fine, the vial just was not in
#: the optical condition its reads are taken in.
GATE_FLAGS = frozenset({STIR_SETTLING, STIR_UNSETTLED})

#: A settled read the reader dropped is retried on the next tick (stir stays
#: paused) this many times before the cycle gives up and restores stir. The
#: rig's RS485 bus drops frames routinely (CLAUDE.md fact 13); without a retry
#: one dropped frame would cost a whole period of data.
MAX_SETTLED_RETRIES = 1

PHASE_STIRRING = "stirring"
PHASE_SETTLING = "settling"


@dataclass
class _VialState:
    config: StirPause
    phase: str
    next_due: float
    mix_ok_after: float
    paused_at: Optional[float] = None
    retries_left: int = MAX_SETTLED_RETRIES
    suspended: bool = False
    last_accepted_at: Optional[float] = None


class StirPauseScheduler:
    """Machine-wide stir-pause schedule, one state per pausing vial."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._vials: dict[int, _VialState] = {}

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------

    def sync(
        self,
        configs: Mapping[int, StirPause],
        suspended: Iterable[int] = (),
        now: Optional[float] = None,
    ) -> None:
        """Make the schedule match ``configs`` -- the pause config of every
        vial an experiment is driving right now.

        Called every tick from the engines' reports, so start, stop, resume,
        faults and supervised overrides need no bookkeeping of their own: a
        vial that appears starts STIRRING (it pauses only after
        ``min_mix_seconds``); one that disappears is dropped, and the normal
        stir composition takes it back; a vial in ``suspended`` (an operator
        holds it, SPEC §21) keeps its place but never pauses, and a settle in
        progress is abandoned."""
        now = self._clock() if now is None else float(now)
        held = {int(v) for v in suspended}
        for vial in list(self._vials):
            if vial not in configs:
                del self._vials[vial]
        for vial, config in configs.items():
            vial = int(vial)
            st = self._vials.get(vial)
            if st is None:
                st = _VialState(
                    config=config,
                    phase=PHASE_STIRRING,
                    next_due=now + config.min_mix_seconds,
                    mix_ok_after=now,
                )
                self._vials[vial] = st
            elif st.config != config:
                st.config = config
            was_suspended = st.suspended
            st.suspended = vial in held
            if st.suspended and st.phase == PHASE_SETTLING:
                self._end_settle(st, now)
            if was_suspended and not st.suspended:
                # Back from an operator's hands, who may have pumped, spiked
                # or changed the stir: stir min_mix_seconds, then take a
                # settled read promptly rather than waiting out the period
                # the override interrupted.
                st.next_due = now + config.min_mix_seconds

    def vials(self) -> set[int]:
        return set(self._vials)

    # ------------------------------------------------------------------
    # The tick
    # ------------------------------------------------------------------

    def stir_overrides(self, now: Optional[float] = None) -> dict[int, int]:
        """``{vial: pause_pwm}`` for every vial whose stirrer must be paused in
        this tick's composed stir write. Starts any pause that has come due.

        Called immediately before the ``zv`` frame goes out, so ``paused_at``
        is the moment the stirrer actually slows."""
        now = self._clock() if now is None else float(now)
        out: dict[int, int] = {}
        for vial, st in self._vials.items():
            if st.suspended:
                continue
            if st.phase == PHASE_STIRRING and now >= st.next_due and now >= st.mix_ok_after:
                st.phase = PHASE_SETTLING
                st.paused_at = now
                st.retries_left = MAX_SETTLED_RETRIES
            if st.phase == PHASE_SETTLING:
                out[vial] = st.config.pause_pwm
        return out

    def classify(
        self,
        read_started_at: float,
        valid: Optional[Mapping[int, bool]] = None,
    ) -> dict[int, str]:
        """Label one OD acquisition for every pausing vial.

        ``read_started_at`` is when the acquisition began (same clock). A vial
        accepts the read when it has been paused for at least
        ``settle_seconds`` by then; accepting ends the settle, so the next
        composed write restores its stir. ``valid`` (vial -> did the reader
        get a value) lets a dropped settled read be retried once with the
        stirrer still paused, rather than costing a whole period.

        Vials not in the schedule are absent: they accept every read."""
        t = float(read_started_at)
        out: dict[int, str] = {}
        for vial, st in self._vials.items():
            if st.suspended or st.phase == PHASE_STIRRING:
                out[vial] = STIR_UNSETTLED
                continue
            paused_at = st.paused_at if st.paused_at is not None else t
            if t - paused_at + 1e-9 < st.config.settle_seconds:
                out[vial] = STIR_SETTLING
                continue
            ok = True if valid is None else bool(valid.get(vial, True))
            if not ok and st.retries_left > 0:
                st.retries_left -= 1
                out[vial] = STIR_SETTLING
                continue
            out[vial] = ACCEPTED
            st.last_accepted_at = t
            self._end_settle(st, t)
        return out

    def note_dilution(self, vial: int, ends_at: float) -> bool:
        """A dilution of ``vial`` finishes at ``ends_at``: no pause may start
        until it has then stirred for ``min_mix_seconds``.

        Dilutions are normally decided only on an accepted read, i.e. right
        as the stirrer comes back, so a vial is never mid-settle here. If one
        lands anyway (an operator's pump, a queued maintenance backlog), the
        settle is abandoned -- pumping into an unstirred vial is the very thing
        the restore ordering exists to avoid. Returns True when it was."""
        st = self._vials.get(int(vial))
        if st is None:
            return False
        st.mix_ok_after = max(st.mix_ok_after, float(ends_at) + st.config.min_mix_seconds)
        if st.phase == PHASE_SETTLING:
            self._end_settle(st, float(ends_at))
            return True
        return False

    @staticmethod
    def _end_settle(st: _VialState, now: float) -> None:
        paused_at = st.paused_at if st.paused_at is not None else now
        st.phase = PHASE_STIRRING
        st.paused_at = None
        # The next pause is due one period after this one began, but never
        # before the vial has stirred for min_mix_seconds again.
        st.next_due = max(paused_at + st.config.period_seconds,
                          now + st.config.min_mix_seconds)

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def snapshot(self, now: Optional[float] = None) -> dict[int, dict]:
        """Per-vial state for the dashboard and ``status()``."""
        now = self._clock() if now is None else float(now)
        out: dict[int, dict] = {}
        for vial, st in self._vials.items():
            if st.suspended:
                state = "suspended"
            else:
                state = st.phase
            out[vial] = {
                "state": state,
                "paused_for_seconds": (
                    round(now - st.paused_at, 1)
                    if st.phase == PHASE_SETTLING and st.paused_at is not None
                    else None
                ),
                "next_pause_in_seconds": (
                    round(max(st.next_due, st.mix_ok_after) - now, 1)
                    if st.phase == PHASE_STIRRING and not st.suspended
                    else None
                ),
                "last_accepted_seconds_ago": (
                    round(now - st.last_accepted_at, 1)
                    if st.last_accepted_at is not None else None
                ),
                **st.config.to_dict(),
            }
        return out
