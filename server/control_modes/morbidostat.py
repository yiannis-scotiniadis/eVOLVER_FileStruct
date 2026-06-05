"""Morbidostat control mode (SPEC §9, modified for manual-swap escalation).

Composes a :class:`TurbidostatController` for the dilution decision. On
top of dilution, runs a sliding-window growth-rate monitor and, when
growth exceeds the adaptation threshold, **proposes** an escalation to a
higher drug concentration. The escalation is purely a software prompt —
the user physically swaps the bottle for higher-strength media and then
confirms via :meth:`confirm_escalation`. The pump algorithm itself is
unchanged across escalation; what changes is the contents of the bottle
that the influx pump draws from.

This design avoids requiring extra hardware (spare pump per vial) and
keeps the wire protocol untouched.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .turbidostat import PumpAction, TurbidostatController


@dataclass(frozen=True)
class EscalationEvent:
    """One escalation proposal emitted via :meth:`MorbidostatController.pending_escalation`.

    The engine broadcasts this as an ``experiment_event`` plus a
    user-facing ``alert``; the user confirms (or ignores) by POSTing to
    ``/api/experiments/<name>/morbidostat/confirm_escalation``.
    """

    vial: int
    old_drug_conc: float
    new_drug_conc: float
    growth_rate: float
    timestamp: float


def estimate_growth_rate(
    timestamped_history: List[Tuple[float, float]],
    now: float,
    window_seconds: float,
    min_samples: int = 6,
) -> Optional[float]:
    """Closed-form OLS slope of ``ln(OD)`` vs time, in inverse hours.

    Returns ``None`` if there are fewer than ``min_samples`` positive-OD
    samples within the trailing ``window_seconds`` or if all samples are
    at the same timestamp (degenerate fit).
    """
    cutoff = now - window_seconds
    recent = [(t, od) for t, od in timestamped_history if t >= cutoff and od > 0]
    if len(recent) < min_samples:
        return None
    xs = [t for t, _ in recent]
    ys = [math.log(od) for _, od in recent]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0:
        return None
    slope_per_sec = num / den
    return slope_per_sec * 3600.0


class MorbidostatController:
    def __init__(
        self,
        vial: int,
        *,
        # Turbidostat-equivalent (forwarded to inner controller)
        target_od: float,
        od_lower: float,
        pump_wait_seconds: float,
        flow_rate_ml_s: float,
        volume_ml: float,
        efflux_extra_seconds: float = 0.0,
        history_window: int = 5,
        pump_duration_cap_seconds: float = 20.0,
        min_samples_before_action: int = 8,
        # Morbidostat-specific
        initial_drug_conc: float = 1.0,
        drug_step: float = 2.0,
        adaptation_threshold_per_hour: float = 0.4,
        growth_window_seconds: float = 1800.0,
        growth_min_samples: int = 6,
        escalation_cooldown_seconds: float = 3600.0,
        escalation_reminder_interval_seconds: float = 1800.0,
    ) -> None:
        if initial_drug_conc <= 0:
            raise ValueError(
                f"initial_drug_conc must be > 0, got {initial_drug_conc}"
            )
        if drug_step <= 1.0:
            raise ValueError(
                f"drug_step must be > 1.0 (escalation must increase), got {drug_step}"
            )
        if adaptation_threshold_per_hour <= 0:
            raise ValueError(
                "adaptation_threshold_per_hour must be > 0, got "
                f"{adaptation_threshold_per_hour}"
            )
        if growth_window_seconds <= 0:
            raise ValueError(
                f"growth_window_seconds must be > 0, got {growth_window_seconds}"
            )
        if growth_min_samples < 2:
            raise ValueError(
                f"growth_min_samples must be >= 2, got {growth_min_samples}"
            )
        if escalation_cooldown_seconds < 0:
            raise ValueError(
                "escalation_cooldown_seconds must be >= 0, got "
                f"{escalation_cooldown_seconds}"
            )
        if escalation_reminder_interval_seconds <= 0:
            raise ValueError(
                "escalation_reminder_interval_seconds must be > 0, got "
                f"{escalation_reminder_interval_seconds}"
            )

        # Inner turbidostat — handles all dilution math. od_upper = target_od.
        self._inner = TurbidostatController(
            vial=vial,
            od_lower=od_lower,
            od_upper=target_od,
            pump_wait_seconds=pump_wait_seconds,
            flow_rate_ml_s=flow_rate_ml_s,
            volume_ml=volume_ml,
            efflux_extra_seconds=efflux_extra_seconds,
            history_window=history_window,
            pump_duration_cap_seconds=pump_duration_cap_seconds,
            min_samples_before_action=min_samples_before_action,
        )

        self.vial = vial
        self.target_od = float(target_od)
        self.od_lower = float(od_lower)
        self.drug_step = float(drug_step)
        self.adaptation_threshold_per_hour = float(adaptation_threshold_per_hour)
        self.growth_window_seconds = float(growth_window_seconds)
        self.growth_min_samples = int(growth_min_samples)
        self.escalation_cooldown_seconds = float(escalation_cooldown_seconds)
        self.escalation_reminder_interval_seconds = float(
            escalation_reminder_interval_seconds
        )

        # Morbidostat state
        self.drug_conc: float = float(initial_drug_conc)
        self.last_escalation_time: Optional[float] = None
        self.awaiting_escalation_confirm: bool = False
        self.last_reminder_time: Optional[float] = None
        self.escalation_log: list = []
        self.timestamped_od_history: list = []     # [(t, od), ...]
        self._pending_escalation: Optional[EscalationEvent] = None
        self._latest_growth_rate: Optional[float] = None

    # ------------------------------------------------------------------
    # Convenience properties (used by engine + status())
    # ------------------------------------------------------------------

    @property
    def flow_rate_ml_s(self) -> float:
        """Forwarded from the inner turbidostat — used by the engine's
        media-debit pass which expects ``controller.flow_rate_ml_s``."""
        return self._inner.flow_rate_ml_s

    @property
    def proposed_new_drug_conc(self) -> Optional[float]:
        """The pending escalation target, or None if not awaiting confirm."""
        if not self.awaiting_escalation_confirm:
            return None
        return self.drug_conc * self.drug_step

    @property
    def latest_growth_rate(self) -> Optional[float]:
        """Most recent growth rate estimate (None if not yet computed)."""
        return self._latest_growth_rate

    @property
    def escalation_count(self) -> int:
        return sum(1 for e in self.escalation_log if e.get("confirmed_time") is not None)

    # ------------------------------------------------------------------
    # Observations
    # ------------------------------------------------------------------

    def push_od(self, now: float, od: float) -> None:
        """Record one timestamped OD sample. Forwards to the inner
        turbidostat (which keeps only the short ring buffer) and also
        appends to the longer timestamped history used for growth fit.
        """
        try:
            if od != od:  # NaN check
                return
        except TypeError:
            return
        od = float(od)
        self._inner.push_od(od)
        self.timestamped_od_history.append((float(now), od))
        # Truncate to 2 * window to bound memory.
        cutoff = now - 2.0 * self.growth_window_seconds
        if self.timestamped_od_history and self.timestamped_od_history[0][0] < cutoff:
            self.timestamped_od_history = [
                (t, v) for t, v in self.timestamped_od_history if t >= cutoff
            ]

    # ------------------------------------------------------------------
    # Control decision
    # ------------------------------------------------------------------

    def decide(self, now: float) -> Optional[PumpAction]:
        """Evaluate escalation (flagging via :meth:`pending_escalation`)
        and return the dilution :class:`PumpAction` from the inner
        turbidostat (unchanged by escalation state)."""
        # Compute growth rate every cycle for status reporting.
        mu = estimate_growth_rate(
            self.timestamped_od_history,
            now=now,
            window_seconds=self.growth_window_seconds,
            min_samples=self.growth_min_samples,
        )
        self._latest_growth_rate = mu

        # Escalation evaluation (only if not already awaiting and cooled).
        if not self.awaiting_escalation_confirm:
            cooled = (
                self.last_escalation_time is None
                or (now - self.last_escalation_time) >= self.escalation_cooldown_seconds
            )
            if cooled and mu is not None and mu > self.adaptation_threshold_per_hour:
                proposed = self.drug_conc * self.drug_step
                self._pending_escalation = EscalationEvent(
                    vial=self.vial,
                    old_drug_conc=self.drug_conc,
                    new_drug_conc=proposed,
                    growth_rate=mu,
                    timestamp=now,
                )
                self.awaiting_escalation_confirm = True
                # Record the proposal in the log; confirm_* will back-patch.
                self.escalation_log.append({
                    "time": now,
                    "old_conc": self.drug_conc,
                    "new_conc": proposed,
                    "growth_rate": mu,
                    "confirmed_time": None,
                    "confirmed_conc": None,
                })

        # Dilution decision — unchanged by escalation state.
        return self._inner.decide(now)

    def pending_escalation(self) -> Optional[EscalationEvent]:
        """Engine polls this after :meth:`decide`. Returns the latest
        unread escalation event, consumed on read."""
        event = self._pending_escalation
        self._pending_escalation = None
        return event

    def due_for_reminder(self, now: float) -> bool:
        """True when the controller is awaiting confirmation and the
        reminder interval has elapsed since the last reminder (or since
        the original proposal if no reminder has fired yet)."""
        if not self.awaiting_escalation_confirm:
            return False
        # The most recent log entry is the unconfirmed proposal.
        last_propose = self.escalation_log[-1]["time"] if self.escalation_log else None
        baseline = self.last_reminder_time or last_propose
        if baseline is None:
            return False
        return (now - baseline) >= self.escalation_reminder_interval_seconds

    def mark_reminder_sent(self, now: float) -> None:
        self.last_reminder_time = float(now)

    def confirm_escalation(self, new_conc: float, timestamp: float) -> None:
        """Apply a user-confirmed escalation. Sets ``drug_conc`` to
        ``new_conc``, stamps ``last_escalation_time``, clears
        ``awaiting_escalation_confirm``, and back-patches the open log
        entry with the confirmation."""
        if not self.awaiting_escalation_confirm:
            raise ValueError(
                f"vial {self.vial} is not awaiting escalation confirmation"
            )
        if new_conc <= self.drug_conc:
            raise ValueError(
                f"new_conc ({new_conc}) must be > current drug_conc ({self.drug_conc})"
            )
        if self.escalation_log and self.escalation_log[-1].get("confirmed_time") is None:
            self.escalation_log[-1]["confirmed_time"] = float(timestamp)
            self.escalation_log[-1]["confirmed_conc"] = float(new_conc)
        self.drug_conc = float(new_conc)
        self.last_escalation_time = float(timestamp)
        self.awaiting_escalation_confirm = False
        self.last_reminder_time = None
        self._pending_escalation = None

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def to_state(self) -> dict:
        return {
            "drug_conc": self.drug_conc,
            "last_escalation_time": self.last_escalation_time,
            "awaiting_escalation_confirm": self.awaiting_escalation_confirm,
            "last_reminder_time": self.last_reminder_time,
            "escalation_log": list(self.escalation_log),
            "timestamped_od_history": [
                [t, od] for t, od in self.timestamped_od_history
            ],
            "inner": self._inner.to_state(),
        }

    def restore_state(self, state: dict) -> None:
        if "drug_conc" in state and state["drug_conc"] is not None:
            self.drug_conc = float(state["drug_conc"])
        if "last_escalation_time" in state:
            v = state["last_escalation_time"]
            self.last_escalation_time = None if v is None else float(v)
        if "awaiting_escalation_confirm" in state:
            self.awaiting_escalation_confirm = bool(
                state["awaiting_escalation_confirm"]
            )
        if "last_reminder_time" in state:
            v = state["last_reminder_time"]
            self.last_reminder_time = None if v is None else float(v)
        if "escalation_log" in state and state["escalation_log"] is not None:
            self.escalation_log = [dict(e) for e in state["escalation_log"]]
        if "timestamped_od_history" in state and state["timestamped_od_history"] is not None:
            self.timestamped_od_history = [
                (float(t), float(od)) for t, od in state["timestamped_od_history"]
            ]
        if "inner" in state and state["inner"] is not None:
            self._inner.restore_state(state["inner"])
        # _pending_escalation is NOT persisted; re-evaluation happens on next decide().
        self._pending_escalation = None
