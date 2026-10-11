"""server/vial_override.py — supervised per-vial override (SPEC §21, Session S).

A vial in a RUNNING experiment is locked: the manual actuator routes refuse
it, because the engine re-asserts its own targets every tick. An override is
an explicit, time-limited, audited unlock of ONE vial so an operator can
intervene -- dilute, spike, drain to the straw, change temperature or stir --
without corrupting the controller that owns it.

**The correctness requirement.** A manual dilution the controller does not
know about is followed by an automatic one: the turbidostat's ``pump_wait``
clock never restarted, so it re-dilutes on its next OD tick -- a double dose.
Every pump an override fires is therefore *booked* exactly where an automatic
dilution is booked (``run_cycle``, under the engine lock), through the same
controller state:

* ``record_manual_dilution(now, influx_ml)`` -- an optional, duck-typed
  controller hook (called via ``getattr``, like ``pop_events``). The
  turbidostat (and the morbidostat's inner turbidostat) restart
  ``last_pump_time``; the chemostat books the volume to ``manual_volume_ml``
  and leaves its schedule alone (manual dilution is *additive*).
* ``rebaseline(now)`` -- on release. Automatic control is PAUSED for the
  whole override (``decide()`` is not called; OD is still pushed so the
  rolling mean is fresh), and a chemostat must not size the paused interval
  into a catch-up bolus.

**Fluidics.** Override pumps never take a private path to the bus (CLAUDE.md
facts 8 and 10). They are queued here and handed back from ``run_cycle`` on
the next tick as :class:`OverridePumpAction`, which ``app._dispatch_dilutions``
folds into that tick's ONE concurrent schedule
(``fluidics.plan_pump_schedule``). A Dilute is a :class:`fluidics.Dilution`
-- influx and efflux start together, the overrun is the tail -- never two
per-direction frames.

**Restart.** An override grant is not restored after a restart: it is
revoked, transient temperature/stir revert to the vial's target and control
resumes. Targets the operator chose to *keep* are ``vial_targets`` in
``config.json`` and survive (``run_config.normalise_vial_targets``).
"""

from __future__ import annotations

import csv
import json
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import fluidics
import growth_rate as growth
import run_config
from control_modes.turbidostat import PumpAction

log = logging.getLogger(__name__)

OVERRIDE_DEFAULT_MINUTES = 10.0
OVERRIDE_MAX_MINUTES = 30.0
# The modal slider's range; safely under DEFAULT_HEATER_CRITICAL_C (50 °C).
OVERRIDE_TEMP_MIN_C = 22.0
OVERRIDE_TEMP_MAX_C = 45.0
# Vial geometry (CLAUDE.md, "Vial working volume"; SPEC §16.3.4). Used only
# to cap an influx-only spike, the one override action that raises the level
# above the straw. Run-wide parameters override both.
DEFAULT_VIAL_CAPACITY_ML = 40.0
DEFAULT_OVERFLOW_MARGIN_ML = 2.0
OVERRIDE_ACTIONS = ("dilute", "influx", "efflux")
MAX_OPERATOR_LEN = 64
MAX_REASON_LEN = 200


class OverrideError(Exception):
    """An override request the current state refuses. ``code`` is the stable
    machine-readable reason the API returns; ``status`` its HTTP status."""

    def __init__(self, message: str, code: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class OverridePumpAction(PumpAction):
    """An operator's pump request on an overridden vial, booked and handed
    to the dispatcher on the tick that fires it.

    ``influx_s`` / ``efflux_s`` are authoritative; ``pump_time`` /
    ``efflux_extra_seconds`` are filled in so anything that only knows
    :class:`PumpAction` still reads a sensible influx and efflux total.
    """

    kind: str = "dilute"
    influx_s: int = 0
    efflux_s: int = 0
    requested_ml: float = 0.0
    influx_ml: float = 0.0
    efflux_ml: float = 0.0
    operator: str = ""
    reason: str = ""

    def schedule_entry(self, vial: int):
        """This request as a schedule input: a paired Dilution (overrun tail
        kept) for ``dilute``, a single-sided ManualRun otherwise."""
        if self.kind == "dilute":
            return fluidics.Dilution(int(vial), self.influx_s, self.efflux_s)
        return fluidics.ManualRun(int(vial), self.influx_s, self.efflux_s)


@dataclass
class VialOverride:
    vial: int
    operator: str
    reason: str
    granted_at: float
    expires_at: float
    # Actuators currently changed TRANSIENTLY ("temperature_c" / "stir_rate"
    # -> the value set). They revert to the vial's target when the override
    # ends; a kept change lives in vial_targets instead and is not listed.
    transient: dict[str, Any] = field(default_factory=dict)
    pending: Optional[OverridePumpAction] = None
    pending_queued_at: Optional[str] = None
    actions: int = 0


def plan_override_pump(
    action: str,
    volume_ml: float,
    *,
    influx_rate_ml_s: float,
    efflux_rate_ml_s: float,
    efflux_extra_seconds: float,
    headroom_ml: float,
    cap_seconds: float,
    quantise,
) -> dict:
    """What an override pump request will actually do. Pure.

    ``quantise`` is ``experiment_engine.compute_pump_quantization`` (whole
    seconds, floor). Raises ``ValueError`` -- an HTTP 400 -- for anything
    that must not fire: an unknown action, a request below one second (the
    §16 rule: reject naming the minimum, never truncate to nothing), a
    duration over the cap (there is no software stop, so the cap IS the
    exposure window), or an influx-only spike past the vial's headroom.
    """
    if action not in OVERRIDE_ACTIONS:
        raise ValueError(f"action must be one of {OVERRIDE_ACTIONS}, got {action!r}")
    try:
        volume_ml = float(volume_ml)
    except (TypeError, ValueError):
        raise ValueError("volume_ml must be a number") from None
    if not math.isfinite(volume_ml) or volume_ml <= 0:
        raise ValueError("volume_ml must be > 0")

    rate = efflux_rate_ml_s if action == "efflux" else influx_rate_ml_s
    q = quantise(volume_ml, rate)
    seconds = int(q["seconds"])
    side = "efflux" if action == "efflux" else "influx"
    if seconds < 1:
        raise ValueError(
            f"{volume_ml:g} mL is under one second of the {side} pump; this "
            f"vial's minimum is {q['min_ml']:.2f} mL (the firmware takes whole "
            "seconds only)"
        )
    if seconds > cap_seconds:
        raise ValueError(
            f"{volume_ml:g} mL needs {seconds} s of the {side} pump, over the "
            f"{cap_seconds:g} s cap (≈ {cap_seconds * rate:.1f} mL) -- there is "
            "no software stop, so the duration cap is the exposure window"
        )
    deliverable = float(q["deliverable_ml"])
    if action == "influx" and deliverable > headroom_ml + 1e-9:
        raise ValueError(
            f"an influx-only spike of {deliverable:.1f} mL exceeds this vial's "
            f"{headroom_ml:.1f} mL headroom above the straw (capacity − working "
            "volume − overflow margin) -- use Dilute, which drains as it fills"
        )

    if action == "dilute":
        influx_s, efflux_s = fluidics.quantise_dilution(seconds, efflux_extra_seconds)
    elif action == "influx":
        influx_s, efflux_s = seconds, 0
    else:
        influx_s, efflux_s = 0, seconds
    return {
        "action": action,
        "requested_ml": volume_ml,
        "influx_s": influx_s,
        "efflux_s": efflux_s,
        "seconds": seconds,
        "deliverable_ml": deliverable,
        "influx_ml": influx_s * influx_rate_ml_s,
        "efflux_ml": efflux_s * efflux_rate_ml_s,
        "min_ml": float(q["min_ml"]),
        "max_ml": (
            min(cap_seconds * rate, headroom_ml) if action == "influx"
            else cap_seconds * rate
        ),
        "quantised": bool(q["quantised"]),
    }


_WINDOW_END = {"released", "expired", "revoked"}


def override_history(events_csv: Path, vial: Optional[int] = None) -> dict:
    """Override windows and actions from an experiment's ``events.csv`` --
    the marker layer for the vial plots (SPEC §21: "drawn as a marker on
    that vial's plots").

    Read from the event log rather than a parallel file so there is one
    record of what happened. Returns ``{"windows": [...], "actions": [...]}``;
    a window still open has ``end: None``.
    """
    windows: list[dict] = []
    actions: list[dict] = []
    open_by_vial: dict[int, dict] = {}
    if not Path(events_csv).is_file():
        return {"windows": windows, "actions": actions}
    with Path(events_csv).open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("category") != "override":
                continue
            try:
                v = int(row.get("vial") or "")
            except ValueError:
                continue
            if vial is not None and v != int(vial):
                continue
            try:
                data = json.loads(row.get("data_json") or "{}") or {}
            except ValueError:
                data = {}
            what = data.get("override")
            ts = row.get("timestamp")
            if what == "granted":
                prev = open_by_vial.pop(v, None)
                if prev is not None:
                    prev["end"] = ts            # never closed: close it here
                    prev["ended_by"] = "superseded"
                win = {"vial": v, "start": ts, "end": None, "ended_by": None,
                       "operator": data.get("operator"), "reason": data.get("reason")}
                windows.append(win)
                open_by_vial[v] = win
            elif what in _WINDOW_END:
                win = open_by_vial.pop(v, None)
                if win is not None:
                    win["end"] = ts
                    win["ended_by"] = what
            elif what is not None:
                actions.append({
                    "vial": v, "timestamp": ts, "kind": what,
                    "message": row.get("message"), **{
                        k: data.get(k) for k in (
                            "operator", "reason", "action", "influx_ml",
                            "influx_s", "efflux_s", "key", "old", "new",
                            "persistence", "cause",
                        ) if data.get(k) is not None
                    },
                })
    return {"windows": windows, "actions": actions}


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def _engine_module():
    # experiment_engine imports this module; resolve it lazily.
    import experiment_engine
    return experiment_engine


class VialOverrideMixin:
    """The override lifecycle on :class:`experiment_engine.ExperimentEngine`.

    Needs the engine's ``_lock``, ``_clock``, ``_status``, ``_vials``,
    ``_controllers``, ``_config``, ``_setpoint_raw``, ``_stir_by_vial``,
    ``_vial_faults`` and its booking helpers. Public methods take the lock;
    ``*_locked`` methods expect it held."""

    _overrides: dict[int, VialOverride]
    # Set by the supervisor each tick: pumps are held machine-wide.
    machine_held: bool

    # ------------------------------------------------------------------
    # Grant / extend / release
    # ------------------------------------------------------------------

    def grant_override(
        self,
        vial: int,
        operator: str,
        reason: str,
        duration_minutes: float = OVERRIDE_DEFAULT_MINUTES,
    ) -> dict:
        """Unlock ``vial`` for ``duration_minutes``. On a vial already
        overridden this EXTENDS it (from now) -- recorded as its own event.
        Dropping any automatic dilution queued for the vial by maintenance:
        control is paused from here, and that decision predates the
        operator's intervention."""
        ee = _engine_module()
        operator = str(operator or "").strip()[:MAX_OPERATOR_LEN]
        reason = str(reason or "").strip()[:MAX_REASON_LEN]
        if not operator:
            raise ValueError("operator is required")
        if not reason:
            raise ValueError("reason is required")
        try:
            minutes = float(duration_minutes)
        except (TypeError, ValueError):
            raise ValueError("duration_minutes must be a number") from None
        if not math.isfinite(minutes) or not (0 < minutes <= OVERRIDE_MAX_MINUTES):
            raise ValueError(
                f"duration_minutes must be in (0, {OVERRIDE_MAX_MINUTES:g}]"
            )
        with self._lock:
            vial = self._override_vial_locked(vial)
            if self._status != ee.ExperimentStatus.RUNNING:
                raise OverrideError(
                    f"experiment '{self._name}' is {self._status}; only a "
                    "running experiment's vial can be overridden",
                    "not_running",
                )
            if self._vial_faults.get(vial) is not None:
                raise OverrideError(
                    f"vial {vial} has a latched fault "
                    f"({self._vial_faults[vial]}); it cannot be overridden",
                    "vial_faulted",
                )
            now = float(self._clock())
            expires = now + minutes * 60.0
            ov = self._overrides.get(vial)
            if ov is not None:
                ov.expires_at = expires
                kind = "override_extended"
            else:
                ov = VialOverride(vial, operator, reason, now, expires)
                self._overrides[vial] = ov
                kind = "override_granted"
                dropped = self._pending_pump_actions.pop(vial, None)
                if dropped is not None:
                    self._broadcast_event({
                        "type": "override_pump_cancelled", "vial": vial,
                        "override": "pump_cancelled", "operator": operator,
                        "reason": reason,
                        "detail": "an automatic dilution queued by maintenance "
                                  "was dropped: control is paused by the override",
                    })
            self._state_dirty = True
            self._save_state_locked()
            self._broadcast_event({
                "type": kind, "vial": vial,
                "override": kind.split("_", 1)[1],
                "operator": operator if kind == "override_granted" else ov.operator,
                "reason": reason,
                "duration_minutes": minutes,
                "expires_at": _iso(expires),
            })
            return self._override_status_locked(vial, now)

    def release_override(self, vial: int, operator: str = "") -> dict:
        with self._lock:
            vial = self._override_vial_locked(vial)
            if vial not in self._overrides:
                raise OverrideError(
                    f"vial {vial} is not overridden", "override_required",
                )
            self._end_override_locked(
                vial, "override_released", by=str(operator or "").strip()[:MAX_OPERATOR_LEN],
            )
            self._save_state_locked()
            return {"vial": vial, "released": True}

    def check_override_expiry_locked(self, now: float) -> list[int]:
        """Re-lock every override past its expiry. Runs at the top of each
        RUNNING tick, both lanes."""
        expired = [v for v, ov in self._overrides.items() if now >= ov.expires_at]
        for vial in expired:
            self._end_override_locked(vial, "override_expired")
        return expired

    def _end_override_locked(
        self,
        vial: int,
        kind: str,
        *,
        by: str = "",
        cause: Optional[str] = None,
        revert: bool = True,
    ) -> None:
        """End ``vial``'s override: cancel a pump not yet fired, revert any
        transient actuator change (never on a faulted vial -- the safety path
        parked it), hand control back (``rebaseline``) and record it."""
        ov = self._overrides.pop(vial, None)
        if ov is None:
            return
        now = float(self._clock())
        reverted: dict[str, Any] = {}
        if revert and ov.transient and self._vial_faults.get(vial) is None:
            reverted = self._revert_transient_locked(vial, ov.transient)
        controller = self._controllers.get(vial)
        rebaseline = getattr(controller, "rebaseline", None)
        if rebaseline is not None:
            try:
                rebaseline(now)
            except Exception:
                log.exception("controller.rebaseline failed for vial %d", vial)
        self._state_dirty = True
        self._broadcast_event({
            "type": kind, "vial": vial,
            "override": kind.split("_", 1)[1],
            "operator": ov.operator, "reason": ov.reason,
            "released_by": by or None,
            "cause": cause,
            "granted_at": _iso(ov.granted_at),
            "actions": ov.actions,
            "pending_pump_cancelled": ov.pending is not None,
            "reverted": reverted or None,
        })

    def _revert_transient_locked(self, vial: int, transient: dict) -> dict:
        """Put a transiently-changed actuator back on the vial's target
        (group value, or a kept vial_targets value)."""
        out: dict[str, Any] = {}
        targets = self._vial_targets_locked()
        if "temperature_c" in transient and self._temp_cal is not None:
            target_c = run_config.temperature_c_by_vial(self._groups, targets).get(vial)
            if target_c is not None:
                self._setpoint_raw[vial] = self._C_to_raw(target_c, vial)
                try:
                    self._apply_temperature_locked()
                except Exception:
                    log.exception("override revert: heater re-send failed (vial %d)", vial)
                out["temperature_c"] = target_c
        if "stir_rate" in transient:
            target = run_config.stir_by_vial(self._groups, targets).get(vial)
            if target is not None:
                # Picked up by the composed `zv` write on the next tick.
                self._stir_by_vial[vial] = int(target)
                out["stir_rate"] = int(target)
        return out

    def _revoke_after_restart_locked(self, vials: list[int]) -> None:
        """Resume path: the grants for ``vials`` are gone (they were never
        restored), so return each to its target -- the persisted raw
        setpoint/stir may hold a transient override value -- and record the
        revocation. A faulted vial stays parked."""
        targets = self._vial_targets_locked()
        temps = run_config.temperature_c_by_vial(self._groups, targets)
        stirs = run_config.stir_by_vial(self._groups, targets)
        for vial in vials:
            reverted: dict[str, Any] = {}
            if self._vial_faults.get(vial) is None:
                if self._temp_cal is not None and vial in temps:
                    self._setpoint_raw[vial] = self._C_to_raw(temps[vial], vial)
                    reverted["temperature_c"] = temps[vial]
                if vial in stirs:
                    self._stir_by_vial[vial] = int(stirs[vial])
                    reverted["stir_rate"] = int(stirs[vial])
            self._state_dirty = True
            self._broadcast_event({
                "type": "override_revoked", "vial": vial,
                "override": "revoked", "cause": "restart",
                "reverted": reverted or None,
            })

    def revoke_overrides_locked(self, cause: str, *, revert: bool = True) -> None:
        for vial in sorted(self._overrides):
            self._end_override_locked(vial, "override_revoked", cause=cause, revert=revert)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def override_pump_preview(self, vial: int, action: str, volume_ml: float) -> dict:
        """What ``override_pump`` would fire, with no side effects and no
        override required (the dialog previews before unlocking)."""
        with self._lock:
            vial = self._override_vial_locked(vial)
            return self._plan_override_pump_locked(vial, action, volume_ml)

    def override_pump(self, vial: int, action: str, volume_ml: float) -> dict:
        """Queue one pump request on an overridden vial. It fires -- and is
        booked -- on the next tick, in that tick's one schedule."""
        with self._lock:
            vial = self._override_vial_locked(vial)
            ov = self._require_override_locked(vial)
            plan = self._plan_override_pump_locked(vial, action, volume_ml)
            # The interlock first: a full carboy also puts the run into
            # consumables maintenance, and the carboy is what to fix.
            blocked = self._override_blocked_locked(vial, action)
            if blocked is not None:
                raise OverrideError(
                    f"vial {vial}: pumping is blocked by the consumables "
                    f"interlock ({blocked})", "consumables_blocked",
                )
            if self._maintenance_active:
                raise OverrideError(
                    f"experiment '{self._name}' is in maintenance -- pumps are "
                    "suppressed; exit maintenance first", "maintenance_active",
                )
            if getattr(self, "machine_held", False):
                raise OverrideError(
                    "the machine hold suppresses every pump; release it first",
                    "machine_hold",
                )
            if ov.pending is not None:
                raise OverrideError(
                    f"vial {vial} already has an override pump queued for the "
                    "next tick", "override_pump_pending",
                )
            ov.pending = OverridePumpAction(
                pump_time=float(plan["influx_s"]),
                efflux_extra_seconds=float(max(0, plan["efflux_s"] - plan["influx_s"])),
                average_od=self._latest_od_locked(vial),
                kind=action,
                influx_s=int(plan["influx_s"]),
                efflux_s=int(plan["efflux_s"]),
                requested_ml=float(plan["requested_ml"]),
                influx_ml=float(plan["influx_ml"]),
                efflux_ml=float(plan["efflux_ml"]),
                operator=ov.operator,
                reason=ov.reason,
            )
            ov.pending_queued_at = _iso(self._clock())
            return {"status": "queued", **plan}

    def override_temperature(self, vial: int, celsius: float, keep: bool = False) -> dict:
        try:
            c = float(celsius)
        except (TypeError, ValueError):
            raise ValueError("celsius must be a number") from None
        if not math.isfinite(c) or not (OVERRIDE_TEMP_MIN_C <= c <= OVERRIDE_TEMP_MAX_C):
            raise ValueError(
                f"celsius must be in {OVERRIDE_TEMP_MIN_C:g}..{OVERRIDE_TEMP_MAX_C:g}"
            )
        with self._lock:
            vial = self._override_vial_locked(vial)
            ov = self._require_override_locked(vial)
            self._require_unfaulted_locked(vial)
            if self._temp_cal is None:
                raise OverrideError("no temperature calibration loaded", "no_calibration")
            ee = _engine_module()
            old_raw = self._setpoint_raw.get(vial, ee.HEATER_OFF_SETPOINT)
            old_c = (
                None if old_raw >= ee.HEATER_OFF_SETPOINT
                else round(self._raw_to_C(old_raw, vial), 2)
            )
            self._set_actuator_target_locked(ov, "temperature_c", c, keep)
            self._setpoint_raw[vial] = self._C_to_raw(c, vial)
            self._apply_temperature_locked()
            return self._record_actuator_change_locked(
                ov, "override_temperature", "temperature_c", old_c, c, keep,
            )

    def override_stir(self, vial: int, value: int, keep: bool = False) -> dict:
        ee = _engine_module()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not float(value).is_integer()
        ):
            raise ValueError("value must be an integer")
        pwm = int(value)
        # STIR_MAX in software is the only ceiling: the firmware does not
        # clamp (CLAUDE.md fact 13).
        if not (0 <= pwm <= ee.STIR_MAX):
            raise ValueError(f"value must be in 0..{ee.STIR_MAX}")
        with self._lock:
            vial = self._override_vial_locked(vial)
            ov = self._require_override_locked(vial)
            self._require_unfaulted_locked(vial)
            old = self._stir_by_vial.get(vial, 0)
            self._set_actuator_target_locked(ov, "stir_rate", pwm, keep)
            # Written by the supervisor's composed `zv` on the next tick.
            self._stir_by_vial[vial] = pwm
            return self._record_actuator_change_locked(
                ov, "override_stir", "stir_rate", old, pwm, keep,
            )

    def _set_actuator_target_locked(
        self, ov: VialOverride, key: str, value: Any, keep: bool,
    ) -> None:
        """A kept change becomes the vial's target for the rest of the run
        (config.json ``vial_targets``, so a restart or a recalibration cannot
        revert it); a transient one is remembered for the revert."""
        if keep:
            ov.transient.pop(key, None)
            entry = self._config.setdefault("vial_targets", {}).setdefault(str(ov.vial), {})
            entry[key] = value
            try:
                self._data_logger.update_experiment_config(
                    self._name, {"vial_targets": {str(ov.vial): {key: value}}},
                )
            except Exception:
                log.exception("persisting vial_targets to config.json failed")
                self._broadcast_alert(
                    level="critical",
                    message=(
                        f"Vial {ov.vial}: kept {key} = {value} is live but was NOT "
                        "written to config.json -- the run record is incomplete"
                    ),
                    vial=ov.vial, category="override",
                )
        else:
            ov.transient[key] = value

    def _record_actuator_change_locked(
        self, ov: VialOverride, kind: str, key: str, old: Any, new: Any, keep: bool,
    ) -> dict:
        ov.actions += 1
        self._state_dirty = True
        self._save_state_locked()
        self._broadcast_event({
            "type": kind, "vial": ov.vial,
            "override": kind.split("_", 1)[1],
            "operator": ov.operator, "reason": ov.reason,
            "key": key, "old": old, "new": new,
            "keep": bool(keep),
            "persistence": "kept for the run" if keep else "transient",
        })
        return {"vial": ov.vial, key: new, "previous": old, "keep": bool(keep)}

    # ------------------------------------------------------------------
    # The tick
    # ------------------------------------------------------------------

    def take_override_pumps_locked(
        self, now: float, timestamp_iso: str,
    ) -> list[tuple[int, OverridePumpAction]]:
        """Book every queued override pump that may fire this tick and
        return it for the dispatcher. Runs on every tick, after the
        maintenance step, so an override pump is never queued behind a pause.

        Booking mirrors an automatic dilution: the consumables interlock is
        re-checked, the controller is told (Dilute and Influx dilute the
        culture; Efflux removes culture without diluting it), the growth
        series gets a boundary spanning the real pump interval, and the media
        and waste books move."""
        out: list[tuple[int, OverridePumpAction]] = []
        for vial in sorted(self._overrides):
            ov = self._overrides[vial]
            action = ov.pending
            if action is None:
                continue
            ov.pending = None
            ov.pending_queued_at = None
            # The interlock first: a full carboy also auto-enters consumables
            # maintenance this tick, and its cause is the one to record.
            cancel = self._override_blocked_locked(vial, action.kind)
            if cancel is not None:
                self._handle_suppressed_pumps_locked([(vial, action, cancel)])
            elif self._maintenance_active:
                cancel = "maintenance"
            elif getattr(self, "machine_held", False):
                cancel = "machine_hold"
            if cancel is not None:
                self._broadcast_event({
                    "type": "override_pump_cancelled", "vial": vial,
                    "override": "pump_cancelled",
                    "operator": ov.operator, "reason": ov.reason,
                    "action": action.kind, "cause": cancel,
                })
                continue
            self._book_override_pump_locked(vial, action, now)
            ov.actions += 1
            self._state_dirty = True
            self._broadcast_event({
                "type": "override_pump", "vial": vial,
                "override": "pump",
                "operator": ov.operator, "reason": ov.reason,
                "action": action.kind,
                "requested_ml": action.requested_ml,
                "influx_s": action.influx_s, "efflux_s": action.efflux_s,
                "influx_ml": round(action.influx_ml, 3),
                "fired_at": timestamp_iso,
            })
            out.append((vial, action))
        return out

    def _book_override_pump_locked(
        self, vial: int, action: OverridePumpAction, now: float,
    ) -> None:
        if action.kind in ("dilute", "influx"):
            controller = self._controllers.get(vial)
            hook = getattr(controller, "record_manual_dilution", None)
            if hook is not None:
                hook(now, action.influx_ml)
        # Growth: the boundary spans the whole pump interval, not an instant
        # -- a 20 s pump straddles two OD samples. Efflux-only contributes
        # zero to the dilution diagnostic (it does not change concentration).
        events = self._dilution_events.setdefault(vial, deque())
        events.append(
            growth.DilutionEvent(
                t_start=float(now),
                t_efflux_end=float(now) + max(action.influx_s, action.efflux_s),
                delivered_ml=action.influx_ml if action.kind != "efflux" else 0.0,
            )
        )
        self._trim_dilution_events_locked(vial, now)

        bottle_id = self._vial_to_bottle.get(vial)
        if bottle_id is None or bottle_id not in self._media_bottles:
            return
        if action.influx_ml > 0:
            self._vessels.add_level(
                self._bottle_vessel[bottle_id], action.influx_ml, experiment=self._name,
            )
            self._check_bottle_threshold_locked(bottle_id)
        # Waste: a Dilute is booked exactly as _debit_media_locked books an
        # automatic dilution (whole efflux seconds at the influx rate), so a
        # run's books do not depend on who fired the pump. An efflux-only
        # drain books its nominal efflux volume -- an upper bound, since past
        # the straw it draws air, and waste-full is the safe direction to err.
        if action.kind == "dilute":
            controller = self._controllers.get(vial)
            rate = float(getattr(controller, "flow_rate_influx_ml_s", 0.0) or 0.0)
            waste_ml = action.efflux_s * rate
        elif action.kind == "efflux":
            waste_ml = action.efflux_ml
        else:
            waste_ml = 0.0
        if waste_ml > 0 and self._waste_vessel is not None:
            self._vessels.add_level(self._waste_vessel, waste_ml, experiment=self._name)
            self._check_waste_threshold_locked()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _override_vial_locked(self, vial: Any) -> int:
        if isinstance(vial, bool):
            raise ValueError("vial must be an integer")
        try:
            v = int(vial)
        except (TypeError, ValueError):
            raise ValueError("vial must be an integer") from None
        if v not in self._vials:
            raise OverrideError(
                f"vial {v} is not in experiment '{self._name}'", "not_owner", 404,
            )
        return v

    def _require_override_locked(self, vial: int) -> VialOverride:
        ov = self._overrides.get(vial)
        if ov is None:
            raise OverrideError(
                f"vial {vial} is locked by experiment '{self._name}' -- unlock it "
                "with an override first", "override_required",
            )
        if float(self._clock()) >= ov.expires_at:
            self._end_override_locked(vial, "override_expired")
            raise OverrideError(f"vial {vial}'s override has expired", "override_expired")
        return ov

    def _require_unfaulted_locked(self, vial: int) -> None:
        if self._vial_faults.get(vial) is not None:
            raise OverrideError(
                f"vial {vial} has a latched fault ({self._vial_faults[vial]})",
                "vial_faulted",
            )

    def _override_blocked_locked(self, vial: int, action: str) -> Optional[str]:
        """The consumables interlock for an override pump: an efflux-only
        drain draws no media, so only a full carboy stops it."""
        if action == "efflux":
            return "waste_full" if self._waste_blocked_locked() else None
        return self._vial_consumables_blocked_locked(vial)

    def _plan_override_pump_locked(self, vial: int, action: str, volume_ml: float) -> dict:
        ee = _engine_module()
        params = (self._config or {}).get("parameters", {}) or {}
        volume = float(params.get("volume_ml", ee.DEFAULT_VOLUME_ML))
        capacity = float(params.get("vial_capacity_ml") or DEFAULT_VIAL_CAPACITY_ML)
        margin = params.get("overflow_margin_ml")
        margin = DEFAULT_OVERFLOW_MARGIN_ML if margin is None else float(margin)
        return plan_override_pump(
            action, volume_ml,
            influx_rate_ml_s=self.flow_rate_ml_s(vial, "influx"),
            efflux_rate_ml_s=self.flow_rate_ml_s(vial, "efflux"),
            efflux_extra_seconds=float(
                params.get("efflux_extra_seconds", ee.DEFAULT_EFFLUX_EXTRA_SECONDS)
            ),
            headroom_ml=max(0.0, capacity - volume - margin),
            cap_seconds=ee.PUMP_DURATION_HARD_CAP_SECONDS,
            quantise=ee.compute_pump_quantization,
        )

    def _latest_od_locked(self, vial: int) -> Optional[float]:
        hist = self._od_history.get(vial)
        return float(hist[-1][1]) if hist else None

    def _vial_targets_locked(self) -> dict[int, dict]:
        return run_config.normalise_vial_targets((self._config or {}).get("vial_targets"))

    def _override_status_locked(self, vial: int, now: Optional[float] = None) -> dict:
        ov = self._overrides[vial]
        now = float(self._clock()) if now is None else now
        pending = ov.pending
        return {
            "vial": vial,
            "operator": ov.operator,
            "reason": ov.reason,
            "granted_at": _iso(ov.granted_at),
            "expires_at": _iso(ov.expires_at),
            "expires_in_seconds": max(0, int(round(ov.expires_at - now))),
            "transient": dict(ov.transient),
            "actions": ov.actions,
            "pending_pump": (
                None if pending is None else {
                    "action": pending.kind,
                    "influx_s": pending.influx_s,
                    "efflux_s": pending.efflux_s,
                    "requested_ml": pending.requested_ml,
                    "queued_at": ov.pending_queued_at,
                }
            ),
        }

    def overrides_status_locked(self) -> dict[str, dict]:
        now = float(self._clock())
        return {str(v): self._override_status_locked(v, now) for v in sorted(self._overrides)}

    def overrides_status(self) -> dict[str, dict]:
        with self._lock:
            return self.overrides_status_locked()
