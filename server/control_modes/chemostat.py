"""Chemostat control mode (SPEC §9).

Open-loop, time-based bolus dilution. OD is read and logged for the
``pump_log`` ``od_at_pump`` column but does not influence pump
decisions — chemostat is by definition fixed-rate.

Algorithm (SPEC §9, with a sub-second deficit accumulator on top)::

    pump_time_per_bolus = (dilution_rate * volume * bolus_interval / 3600) / flow_rate
    every bolus_interval seconds:
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
so dilution begins immediately rather than waiting one bolus interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .turbidostat import PumpAction


class ChemostatController:
    def __init__(
        self,
        vial: int,
        *,
        dilution_rate_per_hour: float,
        bolus_interval_seconds: float,
        volume_ml: float,
        flow_rate_ml_s: float,
        efflux_extra_seconds: float = 0.0,
        pump_duration_cap_seconds: float = 20.0,
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
        if volume_ml <= 0:
            raise ValueError(f"volume_ml must be > 0, got {volume_ml}")
        if flow_rate_ml_s <= 0:
            raise ValueError(f"flow_rate_ml_s must be > 0, got {flow_rate_ml_s}")
        if pump_duration_cap_seconds <= 0:
            raise ValueError(
                f"pump_duration_cap_seconds must be > 0, got {pump_duration_cap_seconds}"
            )

        self.vial = vial
        self.dilution_rate_per_hour = float(dilution_rate_per_hour)
        self.bolus_interval_seconds = float(bolus_interval_seconds)
        self.volume_ml = float(volume_ml)
        self.flow_rate_ml_s = float(flow_rate_ml_s)
        self.efflux_extra_seconds = float(efflux_extra_seconds)
        self.pump_duration_cap_seconds = float(pump_duration_cap_seconds)

        # State
        self.last_bolus_time: Optional[float] = None
        # Count of bolus *cycles* completed (i.e. decide() calls that passed
        # the bolus_interval gate), independent of how many produced an
        # actual PumpAction. Total dilution accounting uses the per-cycle
        # influx_volume (constant), so this counter drives total_volume_ml.
        self.boli_fired: int = 0
        self.total_volume_ml: float = 0.0
        self.last_od: Optional[float] = None
        # Carries the sub-second tail of each bolus's pump_time across
        # cycles. The legacy firmware truncates sub-second commands to
        # zero, so without the accumulator a slow chemostat (e.g. D=0.5/h
        # with default V/T/F) would silently never dilute. Capped at the
        # bolus_interval-derived safety margin so a long stall can't
        # snowball into an overlap with the next bolus.
        self.pump_time_deficit_seconds: float = 0.0

    # ------------------------------------------------------------------
    # Observations
    # ------------------------------------------------------------------

    def push_od(self, od: float) -> None:
        """Store the latest OD reading. Chemostat does not act on OD —
        this is only for the ``od_at_pump`` column of the pump log."""
        try:
            if od != od:  # NaN check
                return
        except TypeError:
            return
        self.last_od = float(od)

    # ------------------------------------------------------------------
    # Control decision
    # ------------------------------------------------------------------

    def decide(self, now: float) -> Optional[PumpAction]:
        """Run one bolus cycle: accumulate the per-bolus pump_time into the
        deficit, then return a :class:`PumpAction` if the deficit has
        rolled past 1 s of whole-second pump time. Returns ``None`` when
        the bolus_interval gate hasn't elapsed yet, or when the deficit
        is still sub-second."""
        if self.last_bolus_time is not None and (
            now - self.last_bolus_time < self.bolus_interval_seconds
        ):
            return None

        influx_volume_ml = (
            self.dilution_rate_per_hour
            * self.volume_ml
            * self.bolus_interval_seconds
            / 3600.0
        )
        pump_time = influx_volume_ml / self.flow_rate_ml_s
        pump_time = min(pump_time, self.pump_duration_cap_seconds)
        # Safety margin: the next bolus interval must arrive after the
        # current pump finishes, otherwise the two physically overlap.
        safety_cap = min(
            self.pump_duration_cap_seconds,
            max(self.bolus_interval_seconds - 1.0, 0.1),
        )
        pump_time = min(pump_time, safety_cap)

        # Bolus cycle completed: advance the cadence timer + dilution book-
        # keeping. The accounting tracks *intended* dilution (the constant
        # per-bolus influx_volume), which is what dilution-rate verification
        # really wants — separate from whether an individual cycle's pump
        # actually fired.
        self.last_bolus_time = now
        self.boli_fired += 1
        self.total_volume_ml += influx_volume_ml

        # Roll into the deficit and fire only when the integer part >= 1 s.
        self.pump_time_deficit_seconds = min(
            self.pump_time_deficit_seconds + pump_time,
            safety_cap,
        )
        whole_seconds = int(self.pump_time_deficit_seconds)
        if whole_seconds < 1:
            return None
        self.pump_time_deficit_seconds -= whole_seconds

        return PumpAction(
            pump_time=float(whole_seconds),
            efflux_extra_seconds=self.efflux_extra_seconds,
            average_od=self.last_od if self.last_od is not None else 0.0,
        )

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def to_state(self) -> dict:
        return {
            "last_bolus_time": self.last_bolus_time,
            "boli_fired": self.boli_fired,
            "total_volume_ml": self.total_volume_ml,
            "last_od": self.last_od,
            "pump_time_deficit_seconds": self.pump_time_deficit_seconds,
        }

    def restore_state(self, state: dict) -> None:
        if "last_bolus_time" in state:
            v = state["last_bolus_time"]
            self.last_bolus_time = None if v is None else float(v)
        if "boli_fired" in state and state["boli_fired"] is not None:
            self.boli_fired = int(state["boli_fired"])
        if "total_volume_ml" in state and state["total_volume_ml"] is not None:
            self.total_volume_ml = float(state["total_volume_ml"])
        if "last_od" in state:
            v = state["last_od"]
            self.last_od = None if v is None else float(v)
        if (
            "pump_time_deficit_seconds" in state
            and state["pump_time_deficit_seconds"] is not None
        ):
            # Clamp on restore: a corrupt file mustn't grant a >cap bolus, and
            # negative values would silently suppress the next pump.
            safety_cap = min(
                self.pump_duration_cap_seconds,
                max(self.bolus_interval_seconds - 1.0, 0.1),
            )
            self.pump_time_deficit_seconds = max(
                0.0,
                min(float(state["pump_time_deficit_seconds"]), safety_cap),
            )
