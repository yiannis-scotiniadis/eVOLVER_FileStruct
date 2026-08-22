"""Chemostat control mode (SPEC §9).

Open-loop, time-based bolus dilution. OD is read and logged for the
``pump_log`` ``od_at_pump`` column but does not influence pump
decisions — chemostat is by definition fixed-rate.

Algorithm (SPEC §9, with a sub-second deficit accumulator on top)::

    # Optional start gate: hold at inoculation density until the culture
    # is dense enough (or enough time has passed) to dilute at rate D.
    if not dilution_started:
        if start_od reached or start_after_seconds elapsed:
            dilution_started = True
        else:
            return

    every bolus_interval seconds:
        # Sized from the time that ACTUALLY elapsed, not the nominal
        # interval -- the sensor loop sleeps `interval - work`, so its real
        # period is max(interval, work) and can only ever run slower.
        elapsed = min(now - last_bolus_time, 4 * bolus_interval)
        pump_time_per_bolus = (dilution_rate * volume * elapsed / 3600) / flow_rate

        # Roll this bolus's needed pump_time into the per-vial deficit.
        # The legacy firmware truncates sub-second commands to zero, so
        # without the accumulator a slow chemostat (e.g. D=0.5/h, V=25,
        # T=60: pump_time_per_bolus = 0.208 s) would silently never fire.
        deficit = min(deficit + pump_time_per_bolus, safety_cap)
        if int(deficit) >= 1:
            whole = int(deficit)
            deficit -= whole
            fire influx for whole seconds
            fire efflux for whole + efflux_extra seconds

Cold start: the first bolus cycle runs on the first :meth:`decide` call
so dilution begins immediately rather than waiting one bolus interval
(subject to the start gate, which is off by default).

**The accumulator is correct here and wrong in the turbidostat** — see
``turbidostat.py``. The quantity accumulated here really is a per-interval
increment of prescribed dilution, so carrying its sub-second remainder
forward makes total delivered equal total prescribed. In the turbidostat
the same quantity is an absolute setpoint error, and accumulating it makes
an integrator with no anti-windup.
"""

from __future__ import annotations

from typing import Optional

from .turbidostat import PumpAction, _rebaseline_future

# A resume after an outage must not fire one enormous catch-up bolus, so
# the elapsed time a single bolus may be sized from is clamped to this many
# nominal intervals (CONTROL_MODE_AUDIT.md C-1).
MAX_CATCHUP_INTERVALS = 4.0

# Below this, `safety_cap = max(interval - 1, 0.1)` drops under 1 s, so
# `int(deficit) >= 1` is never true: zero media is delivered, forever, with
# no warning (CONTROL_MODE_AUDIT.md C-3).
MIN_BOLUS_INTERVAL_SECONDS = 2.0


class ChemostatController:
    def __init__(
        self,
        vial: int,
        *,
        dilution_rate_per_hour: float,
        bolus_interval_seconds: float,
        volume_ml: float,
        flow_rate_ml_s: Optional[float] = None,
        flow_rate_influx_ml_s: Optional[float] = None,
        flow_rate_efflux_ml_s: Optional[float] = None,
        efflux_extra_seconds: float = 0.0,
        pump_duration_cap_seconds: float = 20.0,
        start_od: Optional[float] = None,
        start_after_seconds: Optional[float] = None,
    ) -> None:
        if not (0 <= vial < 16):
            raise ValueError(f"vial must be in 0..15, got {vial}")
        if dilution_rate_per_hour <= 0:
            raise ValueError(
                f"dilution_rate_per_hour must be > 0, got {dilution_rate_per_hour}"
            )
        if bolus_interval_seconds <= 0:
            raise ValueError(
                f"bolus_interval_seconds must be > 0, got {bolus_interval_seconds}"
            )
        if bolus_interval_seconds < MIN_BOLUS_INTERVAL_SECONDS:
            raise ValueError(
                f"bolus_interval_seconds must be >= {MIN_BOLUS_INTERVAL_SECONDS} s, "
                f"got {bolus_interval_seconds}: the per-bolus duration is capped at "
                "bolus_interval - 1 s so consecutive boli cannot physically overlap, "
                "and below 2 s that cap falls under the firmware's 1 s resolution -- "
                "the controller would silently deliver no media at all"
            )
        if volume_ml <= 0:
            raise ValueError(f"volume_ml must be > 0, got {volume_ml}")
        # Influx and efflux are physically separate pumps (SPEC §16.1).
        # `flow_rate_ml_s` is the deprecated single-rate form: it seeds
        # whichever direction wasn't given explicitly.
        influx = flow_rate_influx_ml_s if flow_rate_influx_ml_s is not None else flow_rate_ml_s
        if influx is None:
            raise ValueError(
                "one of flow_rate_influx_ml_s or flow_rate_ml_s is required"
            )
        efflux = flow_rate_efflux_ml_s if flow_rate_efflux_ml_s is not None else influx
        if influx <= 0:
            raise ValueError(f"flow_rate_ml_s must be > 0, got {influx}")
        if efflux <= 0:
            raise ValueError(f"flow_rate_efflux_ml_s must be > 0, got {efflux}")
        if pump_duration_cap_seconds <= 0:
            raise ValueError(
                f"pump_duration_cap_seconds must be > 0, got {pump_duration_cap_seconds}"
            )
        if start_od is not None and start_od <= 0:
            raise ValueError(f"start_od must be > 0 when given, got {start_od}")
        if start_after_seconds is not None and start_after_seconds < 0:
            raise ValueError(
                f"start_after_seconds must be >= 0 when given, got {start_after_seconds}"
            )

        self.vial = vial
        self.dilution_rate_per_hour = float(dilution_rate_per_hour)
        self.bolus_interval_seconds = float(bolus_interval_seconds)
        self.volume_ml = float(volume_ml)
        self.flow_rate_influx_ml_s = float(influx)
        self.flow_rate_efflux_ml_s = float(efflux)
        self.efflux_extra_seconds = float(efflux_extra_seconds)
        self.pump_duration_cap_seconds = float(pump_duration_cap_seconds)
        self.start_od = None if start_od is None else float(start_od)
        self.start_after_seconds = (
            None if start_after_seconds is None else float(start_after_seconds)
        )

        # State
        self.last_bolus_time: Optional[float] = None
        # Count of bolus cycles that actually produced a PumpAction. Cycles
        # that only advanced the deficit are counted by `bolus_cycles`.
        self.boli_fired: int = 0
        # Count of bolus *cycles* completed (i.e. decide() calls that passed
        # the bolus_interval gate), independent of how many produced an
        # actual PumpAction.
        self.bolus_cycles: int = 0
        # Media actually DELIVERED: fired whole seconds x influx flow rate.
        # This is the number a "did I really get D=0.5?" check reaches for,
        # and what SPEC §17's dilution-rate estimator will consume, so it
        # must not book intent (CONTROL_MODE_AUDIT.md C-4).
        self.total_volume_ml: float = 0.0
        # Media PRESCRIBED over the same period, uncapped. Kept alongside so
        # the shortfall when the duration cap binds stays visible instead of
        # being erased.
        self.total_volume_intended_ml: float = 0.0
        self.last_od: Optional[float] = None
        # Carries the sub-second tail of each bolus's pump_time across
        # cycles. The legacy firmware truncates sub-second commands to
        # zero, so without the accumulator a slow chemostat (e.g. D=0.5/h
        # with default V/T/F) would silently never dilute. Capped at the
        # bolus_interval-derived safety margin so a long stall can't
        # snowball into an overlap with the next bolus.
        self.pump_time_deficit_seconds: float = 0.0
        # Start gate (CONTROL_MODE_AUDIT.md C-5). Off by default, in which
        # case dilution begins on the first decide() as before.
        self.dilution_started: bool = (
            self.start_od is None and self.start_after_seconds is None
        )
        self.first_decide_time: Optional[float] = None
        # One-shot records drained by the engine each cycle; see pop_events.
        self._pending_events: list[dict] = []

    @property
    def flow_rate_ml_s(self) -> float:
        """Deprecated single-rate alias — the influx rate."""
        return self.flow_rate_influx_ml_s

    @property
    def requires_od(self) -> bool:
        """Whether a dropped OD read should suspend control for this vial
        (CONTROL_MODE_AUDIT.md C-2).

        Normally ``False``: a chemostat is open-loop, so gating it on OD
        validity means the culture stops being diluted at exactly the moment
        the OD reads out of range — i.e. when it is *denser* than the
        calibration covers. Measured cost was −29 % D at 30 % dropped
        samples and −100 % on a saturated or dead sensor.

        It is ``True`` only while an unmet ``start_od`` gate is armed, since
        that gate is a genuine OD precondition. ``start_after_seconds``
        exists as the escape hatch for that window.
        """
        return self.start_od is not None and not self.dilution_started

    @property
    def safety_cap_seconds(self) -> float:
        """Longest bolus that cannot overlap the next bolus interval."""
        return min(
            self.pump_duration_cap_seconds,
            max(self.bolus_interval_seconds - 1.0, 0.1),
        )

    # ------------------------------------------------------------------
    # Observations
    # ------------------------------------------------------------------

    def push_od(self, od: float) -> None:
        """Store the latest OD reading. Chemostat does not act on OD
        except at the optional ``start_od`` gate — otherwise this is only
        for the ``od_at_pump`` column of the pump log."""
        try:
            if od != od:  # NaN check
                return
        except TypeError:
            return
        self.last_od = float(od)

    # ------------------------------------------------------------------
    # Engine-facing events
    # ------------------------------------------------------------------

    def pop_events(self) -> list[dict]:
        """Return and clear the one-shot events the engine should surface
        (consume-on-read, mirroring
        :meth:`MorbidostatController.pending_escalation`). Keeps this
        controller pure — it never logs or emits anything itself."""
        events = self._pending_events
        self._pending_events = []
        return events

    # ------------------------------------------------------------------
    # Control decision
    # ------------------------------------------------------------------

    def _check_start_gate(self, now: float) -> bool:
        """Return True when dilution may proceed. Releases the gate on the
        first satisfied condition and records a one-shot event.

        The conditions are OR'd, not AND'd, deliberately: with both set,
        ``start_after_seconds`` acts as the escape hatch for a sleeve whose
        OD never reads, so a dead sensor cannot hold the run at inoculation
        density indefinitely.
        """
        if self.dilution_started:
            return True
        if self.first_decide_time is None:
            self.first_decide_time = float(now)

        reason: Optional[str] = None
        if self.start_od is not None and self.last_od is not None:
            if self.last_od >= self.start_od:
                reason = f"OD {self.last_od:.3f} reached start_od {self.start_od:.3f}"
        if reason is None and self.start_after_seconds is not None:
            waited = now - self.first_decide_time
            if waited >= self.start_after_seconds:
                reason = (
                    f"waited {waited / 60.0:.1f} min "
                    f"(start_after_seconds={self.start_after_seconds:.0f})"
                )
        if reason is None:
            return False

        self.dilution_started = True
        self._pending_events.append({
            "type": "start_gate_released",
            "vial": self.vial,
            "reason": reason,
            "od": self.last_od,
        })
        return True

    def decide(self, now: float) -> Optional[PumpAction]:
        """Run one bolus cycle: accumulate the elapsed-time-sized pump_time
        into the deficit, then return a :class:`PumpAction` if the deficit
        has rolled past 1 s of whole-second pump time. Returns ``None`` when
        the start gate is still held, when the bolus_interval gate hasn't
        elapsed yet, or when the deficit is still sub-second."""
        if not self._check_start_gate(now):
            # Held at inoculation density: deliberately does NOT touch
            # last_bolus_time, so the first bolus after release is a clean
            # cold start rather than a catch-up for the whole hold.
            return None

        if self.last_bolus_time is not None and (
            now - self.last_bolus_time < self.bolus_interval_seconds
        ):
            return None

        # C-1: size from the time that actually elapsed. app.py's sensor
        # loop sleeps `interval - work`, so its true period is
        # max(interval, work) -- it can only ever run slower than nominal,
        # never faster, which makes a nominal-interval bolus systematically
        # under-dose (measured -17% to -34% D under realistic overrun).
        # Clamped so a resume after an outage cannot dump one huge bolus.
        if self.last_bolus_time is None:
            elapsed = self.bolus_interval_seconds
        else:
            elapsed = min(
                now - self.last_bolus_time,
                MAX_CATCHUP_INTERVALS * self.bolus_interval_seconds,
            )

        influx_volume_ml = (
            self.dilution_rate_per_hour * self.volume_ml * elapsed / 3600.0
        )
        pump_time = influx_volume_ml / self.flow_rate_influx_ml_s
        # Safety margin: the next bolus interval must arrive after the
        # current pump finishes, otherwise the two physically overlap.
        safety_cap = self.safety_cap_seconds
        if pump_time > safety_cap:
            # C-3: the cap binding means the requested D is unreachable with
            # this interval and flow rate. Silently clipping it is how a run
            # reports D=5.00 while delivering 4.80.
            self._pending_events.append({
                "type": "bolus_cap_clipped",
                "vial": self.vial,
                "requested_seconds": pump_time,
                "capped_seconds": safety_cap,
            })
            pump_time = safety_cap

        # Bolus cycle completed: advance the cadence timer and the
        # prescribed-volume book.
        self.last_bolus_time = now
        self.bolus_cycles += 1
        self.total_volume_intended_ml += influx_volume_ml

        # Roll into the deficit and fire only when the integer part >= 1 s.
        self.pump_time_deficit_seconds = min(
            self.pump_time_deficit_seconds + pump_time,
            safety_cap,
        )
        whole_seconds = int(self.pump_time_deficit_seconds)
        if whole_seconds < 1:
            return None
        self.pump_time_deficit_seconds -= whole_seconds

        # C-4: book what was DELIVERED, not what was asked for.
        self.boli_fired += 1
        self.total_volume_ml += whole_seconds * self.flow_rate_influx_ml_s

        return PumpAction(
            pump_time=float(whole_seconds),
            efflux_extra_seconds=self.efflux_extra_seconds,
            average_od=self.last_od if self.last_od is not None else 0.0,
            sizing_od=self.last_od,
        )

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def to_state(self) -> dict:
        return {
            "last_bolus_time": self.last_bolus_time,
            "boli_fired": self.boli_fired,
            "bolus_cycles": self.bolus_cycles,
            "total_volume_ml": self.total_volume_ml,
            "total_volume_intended_ml": self.total_volume_intended_ml,
            "last_od": self.last_od,
            "pump_time_deficit_seconds": self.pump_time_deficit_seconds,
            "dilution_started": self.dilution_started,
            "first_decide_time": self.first_decide_time,
        }

    def restore_state(self, state: dict, now: Optional[float] = None) -> None:
        """Restore mutable state. ``now``, when supplied, re-baselines any
        timestamp restored from the future — see
        :func:`turbidostat._rebaseline_future` (X-2)."""
        if "last_bolus_time" in state:
            v = state["last_bolus_time"]
            self.last_bolus_time = None if v is None else float(v)
            self.last_bolus_time = _rebaseline_future(
                self.last_bolus_time, now, f"vial {self.vial} last_bolus_time"
            )
        if "boli_fired" in state and state["boli_fired"] is not None:
            self.boli_fired = int(state["boli_fired"])
        if "bolus_cycles" in state and state["bolus_cycles"] is not None:
            self.bolus_cycles = int(state["bolus_cycles"])
        elif "boli_fired" in state and state["boli_fired"] is not None:
            # Back-compat: before C-4 split the two counters, `boli_fired`
            # held what `bolus_cycles` now holds.
            self.bolus_cycles = int(state["boli_fired"])
        if "total_volume_ml" in state and state["total_volume_ml"] is not None:
            self.total_volume_ml = float(state["total_volume_ml"])
        if (
            "total_volume_intended_ml" in state
            and state["total_volume_intended_ml"] is not None
        ):
            self.total_volume_intended_ml = float(state["total_volume_intended_ml"])
        elif "total_volume_ml" in state and state["total_volume_ml"] is not None:
            # Pre-C-4 state.json booked intent in total_volume_ml.
            self.total_volume_intended_ml = float(state["total_volume_ml"])
        if "last_od" in state:
            v = state["last_od"]
            self.last_od = None if v is None else float(v)
        if (
            "pump_time_deficit_seconds" in state
            and state["pump_time_deficit_seconds"] is not None
        ):
            # Clamp on restore: a corrupt file mustn't grant a >cap bolus, and
            # negative values would silently suppress the next pump.
            self.pump_time_deficit_seconds = max(
                0.0,
                min(float(state["pump_time_deficit_seconds"]), self.safety_cap_seconds),
            )
        if "dilution_started" in state and state["dilution_started"] is not None:
            self.dilution_started = bool(state["dilution_started"])
        if "first_decide_time" in state:
            v = state["first_decide_time"]
            self.first_decide_time = None if v is None else float(v)
            self.first_decide_time = _rebaseline_future(
                self.first_decide_time, now, f"vial {self.vial} first_decide_time"
            )
