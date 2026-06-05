"""Turbidostat control mode (SPEC §9).

Pure-Python control state with no I/O — entirely deterministic given
(od, time) inputs. The engine wraps this controller per active vial and
calls :meth:`push_od` each cycle and :meth:`decide` to discover whether
to fire pumps.

Algorithm (SPEC §9, with a sub-second deficit accumulator on top)::

    average_od = mean(od_history[-history_window:])

    # Hysteresis target switching
    if average_od > od_upper and target != od_lower:
        target = od_lower                       # start diluting
    if average_od < midpoint(od_lower, od_upper) and target != od_upper:
        target = od_upper                       # stop diluting

    # Pump decision
    if average_od > target:
        pump_time = -(ln(od_lower / average_od) * volume) / flow_rate
        pump_time = min(pump_time, 20)          # SPEC §9 hard cap

        # Deficit accumulator: the legacy custom_script.py formatted
        # pump_time with %d, so sub-second formulas silently truncated to
        # zero. We instead roll them into a per-vial deficit and only fire
        # when the integer part >= 1 s, carrying the fractional remainder
        # to the next cycle.
        deficit = min(deficit + pump_time, 20)

        if (now - last_pump_time) >= pump_wait and int(deficit) >= 1:
            whole = int(deficit)
            deficit -= whole
            fire influx + efflux for whole seconds
            fire efflux alone for efflux_extra seconds

Variable names mirror SPEC §9 directly.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PumpAction:
    """One dilution event returned from :meth:`TurbidostatController.decide`.

    The engine translates this into two ``SerialManager.pump_command`` calls:
    influx for ``pump_time`` seconds, efflux for
    ``pump_time + efflux_extra_seconds`` seconds.
    """

    pump_time: float
    efflux_extra_seconds: float
    average_od: float

    @property
    def efflux_seconds(self) -> float:
        """Total efflux pump duration (= pump_time + efflux_extra_seconds)."""
        return self.pump_time + self.efflux_extra_seconds


# Legacy warmup gate: custom_script.py:83 required ``len(data) > 7`` — at
# least 8 OD samples accumulated — before any dilution decision. Without
# this guard the controller can fire on noisy startup data (the first 1-2
# reads after a fresh vial can be wildly off from the steady-state value).
DEFAULT_MIN_SAMPLES_BEFORE_ACTION = 8


class TurbidostatController:
    def __init__(
        self,
        vial: int,
        *,
        od_lower: float,
        od_upper: float,
        pump_wait_seconds: float,
        flow_rate_ml_s: float,
        volume_ml: float,
        efflux_extra_seconds: float = 0.0,
        history_window: int = 5,
        pump_duration_cap_seconds: float = 20.0,
        min_samples_before_action: int = DEFAULT_MIN_SAMPLES_BEFORE_ACTION,
    ) -> None:
        if not (0 <= vial < 16):
            raise ValueError(f"vial must be in 0..15, got {vial}")
        if not (od_lower > 0):
            raise ValueError(f"od_lower must be > 0, got {od_lower}")
        if not (od_upper > od_lower):
            raise ValueError(
                f"od_upper ({od_upper}) must be > od_lower ({od_lower})"
            )
        if pump_wait_seconds < 0:
            raise ValueError(
                f"pump_wait_seconds must be >= 0, got {pump_wait_seconds}"
            )
        if flow_rate_ml_s <= 0:
            raise ValueError(f"flow_rate_ml_s must be > 0, got {flow_rate_ml_s}")
        if volume_ml <= 0:
            raise ValueError(f"volume_ml must be > 0, got {volume_ml}")
        if history_window < 1:
            raise ValueError(f"history_window must be >= 1, got {history_window}")
        if pump_duration_cap_seconds <= 0:
            raise ValueError(
                f"pump_duration_cap_seconds must be > 0, got {pump_duration_cap_seconds}"
            )
        if min_samples_before_action < 1:
            raise ValueError(
                f"min_samples_before_action must be >= 1, got {min_samples_before_action}"
            )

        self.vial = vial
        self.od_lower = float(od_lower)
        self.od_upper = float(od_upper)
        self.pump_wait_seconds = float(pump_wait_seconds)
        self.flow_rate_ml_s = float(flow_rate_ml_s)
        self.volume_ml = float(volume_ml)
        self.efflux_extra_seconds = float(efflux_extra_seconds)
        self.history_window = int(history_window)
        self.pump_duration_cap_seconds = float(pump_duration_cap_seconds)
        self.min_samples_before_action = int(min_samples_before_action)

        # State
        self.target: float = self.od_upper
        self.last_pump_time: Optional[float] = None
        self.od_history: deque[float] = deque(maxlen=self.history_window)
        # Cumulative count of non-NaN samples ever pushed; the warmup gate
        # in decide() requires this to reach min_samples_before_action.
        # Persisted across restart so resumed experiments don't reset the
        # warmup period.
        self.total_samples_seen: int = 0
        # Carries the sub-second tail of each cycle's pump_time across cycles.
        # The legacy firmware truncates sub-second commands to zero (the Mac
        # client formatted pump_time with %d), so each cycle that needs e.g.
        # 0.3 s of dilution would silently no-op. We accumulate the formula
        # value instead and only fire when the deficit rolls past 1 s; the
        # integer part is fired and the residual carries to the next cycle.
        # Capped at pump_duration_cap_seconds so a long stall (e.g. a stuck
        # pump_wait window) cannot snowball into a giant catch-up dose.
        self.pump_time_deficit_seconds: float = 0.0

    # ------------------------------------------------------------------
    # Observations
    # ------------------------------------------------------------------

    def push_od(self, od: float) -> None:
        """Append one OD reading to the rolling history. NaN samples are
        silently dropped so a single bus glitch doesn't poison the average
        and does not advance the warmup counter."""
        try:
            if od != od:  # NaN check
                return
        except TypeError:
            return
        self.od_history.append(float(od))
        self.total_samples_seen += 1

    def average_od(self) -> Optional[float]:
        """Mean of all samples currently in ``od_history`` (capped at
        ``history_window``), or ``None`` if no samples yet."""
        if not self.od_history:
            return None
        return sum(self.od_history) / len(self.od_history)

    def time_since_last_pump(self, now: float) -> float:
        """Seconds since the most recent pump fire, or ``+inf`` if never."""
        if self.last_pump_time is None:
            return float("inf")
        return now - self.last_pump_time

    # ------------------------------------------------------------------
    # Control decision (SPEC §9 turbidostat block, verbatim)
    # ------------------------------------------------------------------

    def decide(self, now: float) -> Optional[PumpAction]:
        """Return a :class:`PumpAction` if a dilution should fire now,
        else ``None``. Mutates ``target`` and ``last_pump_time`` when a
        fire is returned.

        Warmup gate (legacy custom_script.py:83): the first
        ``min_samples_before_action`` cycles never fire, regardless of OD.
        This protects against acting on noisy startup data."""
        if self.total_samples_seen < self.min_samples_before_action:
            return None
        average_od = self.average_od()
        if average_od is None:
            return None  # cold start, no samples yet

        # Hysteresis target switching
        if average_od > self.od_upper and self.target != self.od_lower:
            self.target = self.od_lower
        midpoint = (self.od_lower + self.od_upper) / 2.0
        if average_od < midpoint and self.target != self.od_upper:
            self.target = self.od_upper

        # Pump-fire decision
        if average_od <= self.target:
            return None
        if average_od <= self.od_lower:
            # Guard against log(<=0): would happen if user sets od_lower above
            # the actual OD floor. Caller has already gated on average_od > target,
            # so this only fires when target == od_lower and avg is still below.
            return None

        pump_time = -(math.log(self.od_lower / average_od) * self.volume_ml) / self.flow_rate_ml_s
        # SPEC §9: cap at 20 s. Engine then re-clips against its own hard cap.
        pump_time = min(pump_time, self.pump_duration_cap_seconds)

        # Roll this cycle's needed dilution into the per-vial deficit.
        # Capped so a long pump_wait window or repeated rate-limit can't
        # snowball into a bolus larger than the legacy hard cap allows.
        self.pump_time_deficit_seconds = min(
            self.pump_time_deficit_seconds + pump_time,
            self.pump_duration_cap_seconds,
        )

        if self.time_since_last_pump(now) < self.pump_wait_seconds:
            return None

        # The firmware accepts whole seconds; truncate (don't round) so we
        # never over-fire vs. the accumulated need. < 1 s stays as residual.
        whole_seconds = int(self.pump_time_deficit_seconds)
        if whole_seconds < 1:
            return None
        self.pump_time_deficit_seconds -= whole_seconds

        self.last_pump_time = now
        return PumpAction(
            pump_time=float(whole_seconds),
            efflux_extra_seconds=self.efflux_extra_seconds,
            average_od=average_od,
        )

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def to_state(self) -> dict:
        """Serialise mutable state for ``state.json`` persistence."""
        return {
            "target": self.target,
            "last_pump_time": self.last_pump_time,
            "od_history": list(self.od_history),
            "total_samples_seen": self.total_samples_seen,
            "pump_time_deficit_seconds": self.pump_time_deficit_seconds,
        }

    def restore_state(self, state: dict) -> None:
        """Restore mutable state from a previously-saved dict. Tolerant of
        missing keys (treats them as initial values)."""
        if "target" in state and state["target"] is not None:
            self.target = float(state["target"])
        if "last_pump_time" in state:
            v = state["last_pump_time"]
            self.last_pump_time = None if v is None else float(v)
        if "od_history" in state and state["od_history"] is not None:
            self.od_history = deque(
                (float(x) for x in state["od_history"]),
                maxlen=self.history_window,
            )
        if "total_samples_seen" in state and state["total_samples_seen"] is not None:
            self.total_samples_seen = int(state["total_samples_seen"])
        elif "od_history" in state and state["od_history"]:
            # Back-compat: state.json from before the warmup gate was added
            # doesn't have total_samples_seen. The conservative recovery is
            # "at least as many as we have in the history" so we don't undo
            # already-elapsed warmup time.
            self.total_samples_seen = max(
                self.total_samples_seen, len(state["od_history"])
            )
        if (
            "pump_time_deficit_seconds" in state
            and state["pump_time_deficit_seconds"] is not None
        ):
            # Clamp on restore: a corrupt file mustn't grant a >cap bolus, and
            # negative values would silently suppress the next pump.
            self.pump_time_deficit_seconds = max(
                0.0,
                min(
                    float(state["pump_time_deficit_seconds"]),
                    self.pump_duration_cap_seconds,
                ),
            )
