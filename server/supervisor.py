"""server/supervisor.py — parallel experiments on one eVOLVER.

Two operators, staggered start and stop dates, independent experiments
(PARALLEL_EXPERIMENTS.md). The machine is already a 16-channel instrument:
every actuator is addressed per vial and every sensor read returns all 16. So
parallel experiments need *logical* independence -- separate identities,
lifecycles, directories, operators, modes -- not execution concurrency.

**Multiplex, don't parallelize.** One sensor thread, one tick, one lock. The
supervisor holds one :class:`ExperimentEngine` per loaded experiment. Each
engine drives only its own vials exactly as a single experiment always has;
the supervisor owns what is machine-scoped:

* **Vial ownership.** A vial belongs to at most one loaded experiment, from
  create until stop. Create refuses a vial another experiment holds and names
  who holds it.
* **One tick for everyone.** ``run_cycle`` hands the same sensor arrays to
  every experiment and returns all their dilutions, so the caller fires them
  as ONE concurrent pump schedule (pumps fire together through OR'd masks,
  FLUIDICS_FIRMWARE_AUDIT.md). Stir is composed across experiments and
  written once per tick, not once per experiment.
* **Shared consumables.** One :class:`VesselRegistry` for every experiment, so
  a bottle or waste carboy two experiments use has one level and one
  interlock.
* **Machine hold.** "A person has the machine open": holds every
  experiment's pumps, with the same 30-minute auto-resume failsafe as an
  experiment's own maintenance. Distinct from per-experiment maintenance
  ("pause my run while I swap my bottle"), which stays on each engine.
* **Fan-out.** Emergency stop and shutdown stop every experiment.

Deliberately NOT here: threads per experiment (they buy nothing on a
millisecond workload and put locks in the heater path -- MULTIPLEX_OPTIONS.md
§4), and any cross-experiment pump arbitration (every dilution of a tick goes
out in one schedule already).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import run_config
import vessels as vessel_lib
from control_modes.turbidostat import PumpAction
from experiment_engine import (
    DEFAULT_BASE_TICK_SECONDS,
    DEFAULT_CYCLE_INTERVAL_SECONDS,
    DEFAULT_HEATER_CRITICAL_C,
    DEFAULT_HEATER_OVERRUN_C,
    DEFAULT_MAINTENANCE_TIMEOUT_MINUTES,
    DEFAULT_OD_ACQUISITION,
    DEFAULT_SENSOR_FAILURE_THRESHOLD,
    N_VIALS,
    ConflictError,
    ExperimentEngine,
    ExperimentStatus,
    InvalidExperimentStateError,
    _parse_od_acquisition,
)
from vessels import VesselRegistry

log = logging.getLogger(__name__)

# Sixteen vials bound it anyway; eight keeps a runaway client from filling
# the process with idle CREATED experiments.
MAX_CONCURRENT_RUNS = 8

# Loaded experiments in these states own their vials. (Stopped experiments
# are unloaded -- their record is on disk.)
_OWNING = (ExperimentStatus.CREATED, ExperimentStatus.RUNNING, ExperimentStatus.ERROR)


class AmbiguousExperimentError(InvalidExperimentStateError):
    """A call that names no experiment while several are loaded. The API maps
    it to 409 with ``code: "ambiguous_experiment"``."""

    def __init__(self, names: list[str]) -> None:
        super().__init__(
            f"several experiments are loaded ({', '.join(names)}); name one"
        )
        self.names = names


class ExperimentNotLoadedError(InvalidExperimentStateError):
    """The named experiment is not loaded (never created, or stopped)."""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _requested_vials(vials, media, groups) -> set[int]:
    """The vials a create request asks for, before the engine validates it.
    Malformed input yields an empty set; the engine then rejects it with its
    own message."""
    out: set[int] = set()
    try:
        if groups:
            for g in groups:
                out |= {int(v) for v in (g.get("vials") or [])}
        elif vials:
            out |= {int(v) for v in vials}
        elif media and media.get("vial_to_bottle"):
            out |= {int(k) for k in media["vial_to_bottle"]}
    except (TypeError, ValueError, AttributeError):
        return set()
    return out


def _vessel_modes(name: str, config: dict) -> dict[str, set[str]]:
    """``{media vessel id: {control modes of the vials it feeds}}`` for one
    experiment's config -- what the morbidostat sharing rule needs."""
    media = config.get("media")
    if not media:
        return {}
    try:
        ids = vessel_lib.resolved_vessel_ids(name, media)
        by_vial = run_config.group_by_vial(run_config.groups_from_config(config))
    except Exception:
        return {}
    out: dict[str, set[str]] = {}
    for key, bid in (media.get("vial_to_bottle") or {}).items():
        vid = ids.get(str(bid))
        if vid is None:
            continue
        g = by_vial.get(int(key))
        out.setdefault(vid, set()).add(g.mode if g else str(config.get("mode")))
    return out


class ExperimentSupervisor:
    """Holds every loaded experiment and drives them on one tick.

    Keeps the single-experiment call shapes the rest of the server grew up
    with -- a call that names no experiment means "the only one loaded", and
    raises :class:`AmbiguousExperimentError` when there are several.
    """

    def __init__(
        self,
        serial_manager,
        data_logger,
        experiments_root: Path,
        *,
        on_event: Optional[Callable[[dict], None]] = None,
        on_alert: Optional[Callable[[dict], None]] = None,
        temp_cal=None,
        clock: Callable[[], float] = time.time,
        cycle_interval_seconds: float = DEFAULT_CYCLE_INTERVAL_SECONDS,
        base_tick_seconds: float = DEFAULT_BASE_TICK_SECONDS,
        sensor_failure_threshold: int = DEFAULT_SENSOR_FAILURE_THRESHOLD,
        heater_overrun_C: float = DEFAULT_HEATER_OVERRUN_C,
        heater_critical_C: float = DEFAULT_HEATER_CRITICAL_C,
        maintenance_timeout_minutes: float = DEFAULT_MAINTENANCE_TIMEOUT_MINUTES,
        machine_dir: Optional[Path] = None,
        max_concurrent_runs: int = MAX_CONCURRENT_RUNS,
    ) -> None:
        self._manager = serial_manager
        self._data_logger = data_logger
        self._experiments_root = Path(experiments_root)
        self._on_event = on_event
        self._on_alert = on_alert
        self._clock = clock
        # ONE lock for every engine and for the supervisor: the engines
        # read-modify-write the same 16-vial heater and stir vectors on the
        # manager, and the sensor thread plus every Flask thread reach them.
        self._lock = threading.RLock()
        self._engine_kwargs = dict(
            on_event=on_event,
            on_alert=on_alert,
            temp_cal=temp_cal,
            cycle_interval_seconds=cycle_interval_seconds,
            base_tick_seconds=base_tick_seconds,
            sensor_failure_threshold=sensor_failure_threshold,
            heater_overrun_C=heater_overrun_C,
            heater_critical_C=heater_critical_C,
            maintenance_timeout_minutes=maintenance_timeout_minutes,
        )
        self._maintenance_timeout_seconds = float(maintenance_timeout_minutes) * 60.0
        self._max_runs = int(max_concurrent_runs)
        self._machine_dir = Path(machine_dir) if machine_dir is not None else None
        self._vessels = VesselRegistry(
            self._machine_dir / "vessels.json" if self._machine_dir else None
        )
        # name -> engine, in load order.
        self._runs: dict[str, ExperimentEngine] = {}
        # Stopped experiments, remembered until another experiment claims one
        # of their vials (or they are deleted/renamed). A manual pump on a
        # just-stopped experiment's vial still books against ITS bottle and
        # carboy -- post-run draining before the weigh-in is part of the run
        # that SPEC §19.4 reconciliation compares against the masses.
        self._stopped: dict[str, ExperimentEngine] = {}
        # Machine hold: None, or {"entered_at": datetime, "reason": str}.
        self._machine_hold: Optional[dict] = None
        # Dilutions decided while the machine hold was on, newest per vial:
        # vial -> (experiment, PumpAction, ts_iso). Fired on release.
        self._held: dict[int, tuple[str, PumpAction, str]] = {}
        self._experiments_root.mkdir(parents=True, exist_ok=True)
        self._load_machine_hold()

    # ------------------------------------------------------------------
    # Engines
    # ------------------------------------------------------------------

    def _new_engine(self) -> ExperimentEngine:
        engine = ExperimentEngine(
            serial_manager=self._manager,
            data_logger=self._data_logger,
            experiments_root=self._experiments_root,
            clock=self._clock,
            lock=self._lock,
            vessel_registry=self._vessels,
            **self._engine_kwargs,
        )
        # The supervisor writes one composed stir vector per tick instead.
        engine.resend_stir_in_cycle = False
        return engine

    def _offline(self) -> ExperimentEngine:
        """An engine with nothing loaded, for operations on experiments that
        are only on disk (list, data, rename, delete, metadata)."""
        return self._new_engine()

    def run(self, name: Optional[str] = None) -> ExperimentEngine:
        """The loaded engine for ``name`` (or the only one loaded)."""
        with self._lock:
            return self._runs[self._resolve_locked(name)]

    def get_run(self, name: str) -> Optional[ExperimentEngine]:
        with self._lock:
            return self._runs.get(name)

    def loaded_names(self) -> list[str]:
        with self._lock:
            return list(self._runs)

    def _resolve_locked(self, name: Optional[str]) -> str:
        if name is not None:
            if name not in self._runs:
                raise ExperimentNotLoadedError(f"experiment '{name}' is not loaded")
            return name
        if len(self._runs) == 1:
            return next(iter(self._runs))
        if not self._runs:
            raise ExperimentNotLoadedError("no experiment is loaded")
        raise AmbiguousExperimentError(list(self._runs))

    # ------------------------------------------------------------------
    # Ownership
    # ------------------------------------------------------------------

    def _owners_locked(self) -> dict[int, str]:
        owners: dict[int, str] = {}
        for name, engine in self._runs.items():
            if engine.status_string in _OWNING:
                for v in engine.loaded_vials:
                    owners[v] = name
        return owners

    def owner_of(self, vial: int) -> Optional[str]:
        """The loaded experiment holding ``vial`` (created, running or in
        error), or None when the vial is free."""
        with self._lock:
            return self._owners_locked().get(int(vial))

    def owner_engine(self, vial: int) -> Optional[ExperimentEngine]:
        with self._lock:
            name = self._owners_locked().get(int(vial))
            return None if name is None else self._runs[name]

    def driven_vials(self) -> dict[int, str]:
        """``{vial: experiment}`` for vials an experiment is driving right now
        (RUNNING, or CREATED and preconditioned) -- the ones manual control
        must not touch, because the engine re-asserts its setpoints."""
        with self._lock:
            out: dict[int, str] = {}
            for name, engine in self._runs.items():
                if engine.controls_actuators:
                    for v in engine.loaded_vials:
                        out[v] = name
            return out

    def operator_of(self, name: str) -> str:
        with self._lock:
            engine = self._runs.get(name)
            if engine is None:
                return ""
            return str((engine._config or {}).get("operator") or "")

    # ------------------------------------------------------------------
    # Single-experiment compatibility (one loaded experiment)
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """True while ANY experiment is RUNNING."""
        with self._lock:
            return any(e.is_running for e in self._runs.values())

    @property
    def controls_actuators(self) -> bool:
        with self._lock:
            return any(e.controls_actuators for e in self._runs.values())

    @property
    def loaded_experiment(self) -> Optional[str]:
        """The first loaded experiment (the only one, before parallel runs)."""
        with self._lock:
            return next(iter(self._runs), None)

    @property
    def loaded_vials(self) -> list[int]:
        with self._lock:
            return sorted(v for e in self._runs.values() for v in e.loaded_vials)

    @property
    def status_string(self) -> str:
        with self._lock:
            if not self._runs:
                return ExperimentStatus.IDLE
            return next(iter(self._runs.values())).status_string

    def running_names(self) -> list[str]:
        with self._lock:
            return [n for n, e in self._runs.items() if e.is_running]

    def first_with_status(self, *statuses: str) -> tuple[Optional[str], Optional[str]]:
        """``(name, status)`` of the first loaded experiment in ``statuses``,
        tried in order -- e.g. ("created", "running") prefers a CREATED one."""
        with self._lock:
            for status in statuses:
                for name, engine in self._runs.items():
                    if engine.status_string == status:
                        return name, status
            return None, None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def create_experiment(
        self,
        name: str,
        mode: Optional[str] = None,
        vials: Optional[list[int]] = None,
        parameters: Optional[dict] = None,
        calibration: Optional[dict] = None,
        notes: str = "",
        media: Optional[dict] = None,
        groups: Optional[list] = None,
        operator: str = "",
        expected_end: Optional[str] = None,
    ) -> dict:
        """Create an experiment alongside any already loaded.

        Beyond the engine's own validation: its vials must be free
        (``ConflictError`` naming the holder), its OD acquisition must agree
        with the running experiments' (one OD read serves every vial), and
        it may not share a morbidostat drug bottle with another experiment.
        """
        with self._lock:
            if name in self._runs:
                raise InvalidExperimentStateError(f"experiment '{name}' is already loaded")
            if len(self._runs) >= self._max_runs:
                raise InvalidExperimentStateError(
                    f"{len(self._runs)} experiments are already loaded "
                    f"(limit {self._max_runs}); stop one first"
                )
            owners = self._owners_locked()
            wanted = _requested_vials(vials, media, groups)
            taken = sorted(v for v in wanted if v in owners)
            if taken:
                holders = sorted({owners[v] for v in taken})
                who = "; ".join(
                    f"'{h}'" + (f" (operator {op})" if (op := self.operator_of(h)) else "")
                    for h in holders
                )
                raise ConflictError(
                    f"vials {taken} belong to experiment {who}; a vial can be "
                    "in one experiment at a time"
                )
            self._check_od_acquisition_locked(parameters or {})
            self._check_vessel_sharing_locked(name, mode, vials, parameters, media, groups)

            engine = self._new_engine()
            config = engine.create_experiment(
                name=name, mode=mode, vials=vials, parameters=parameters,
                calibration=calibration, notes=notes, media=media,
                groups=groups, operator=operator, expected_end=expected_end,
            )
            self._runs[name] = engine
            # The new experiment now owns these vials; post-run bookings for
            # a stopped experiment that had them end here.
            claimed = set(engine.loaded_vials)
            for old in [n for n, e in self._stopped.items()
                        if n == name or claimed & set(e.loaded_vials)]:
                self._stopped.pop(old, None)
            return config

    def _check_od_acquisition_locked(self, parameters: dict) -> None:
        """One OD read serves all sixteen vials, so acquisition settings are a
        machine property (PARALLEL_EXPERIMENTS.md T2). ``n_samples`` merges by
        taking the maximum -- more samples is strictly better, just slower --
        but aggregation and dark subtraction cannot differ."""
        try:
            new = _parse_od_acquisition(parameters)
        except ValueError:
            return  # the engine reports the malformed block
        for other, engine in self._runs.items():
            cur = engine.od_acquisition_params()
            for key in ("agg", "dark_subtract"):
                if cur.get(key) != new.get(key):
                    raise ConflictError(
                        f"od_acquisition.{key}={new.get(key)!r} conflicts with "
                        f"experiment '{other}' ({cur.get(key)!r}); one OD read "
                        "serves every vial, so these must match"
                    )

    def _check_vessel_sharing_locked(self, name, mode, vials, parameters, media, groups) -> None:
        """Two rules for shared vessels.

        * A vessel id this experiment would CREATE must not already be in use
          by another loaded experiment -- starting would reset its level.
        * A media bottle feeding a morbidostat vial may not be shared across
          experiments: confirming an escalation swaps that drug bottle.
        """
        if not media:
            return
        prospective = {
            "mode": mode or "turbidostat", "vials": sorted(
                _requested_vials(vials, media, groups)),
            "parameters": parameters or {}, "media": media,
        }
        if groups:
            prospective["groups"] = groups
            prospective["mode"] = None
        new_modes = _vessel_modes(name, prospective)
        try:
            owned = set(vessel_lib.iter_owned(name, media))
        except Exception:
            owned = set()
        for other, engine in self._runs.items():
            other_modes = _vessel_modes(other, engine._config or {})
            # Every vessel the other experiment uses -- bottles AND carboy,
            # declared or shared.
            try:
                other_ids = set(vessel_lib.resolved_vessel_ids(
                    other, (engine._config or {}).get("media")).values())
            except Exception:
                other_ids = set(other_modes)
            for kind_ids in engine.vessel_ids.values():
                other_ids |= set(kind_ids)
            clash = sorted(owned & other_ids)
            if clash:
                raise ConflictError(
                    f"vessel ids {clash} are already in use by experiment "
                    f"'{other}'; choose a different experiment name"
                )
            for vid, modes in new_modes.items():
                if vid in other_modes and (
                    "morbidostat" in modes or "morbidostat" in other_modes[vid]
                ):
                    raise ConflictError(
                        f"bottle vessel {vid!r} feeds a morbidostat in "
                        f"'{name if 'morbidostat' in modes else other}' and is "
                        f"also used by '{other if 'morbidostat' in modes else name}'; "
                        "a drug bottle cannot be shared between experiments"
                    )

    def start_experiment(self, name: str) -> dict:
        with self._lock:
            engine = self._runs[self._resolve_locked(name)]
            return engine.start_experiment(name)

    def precondition_experiment(self, name: str) -> dict:
        with self._lock:
            engine = self._runs[self._resolve_locked(name)]
            return engine.precondition_experiment(name)

    def stop_experiment(
        self, name: Optional[str] = None, reason: str = "manual",
    ) -> Optional[str]:
        """Stop one experiment and unload it; its vials become free. Returns
        None when there is nothing to stop. Other experiments are untouched:
        each engine parks only its own vials."""
        with self._lock:
            try:
                resolved = self._resolve_locked(name)
            except ExperimentNotLoadedError:
                return None
            engine = self._runs[resolved]
            stopped = engine.stop_experiment(reason=reason)
            self._vessels.flush()
            for v in engine.loaded_vials:
                self._held.pop(v, None)
            del self._runs[resolved]
            self._stopped[resolved] = engine
            return stopped

    def stop_all(self, reason: str) -> list[str]:
        """Stop every loaded experiment (shutdown)."""
        with self._lock:
            names = list(self._runs)
        stopped = []
        for name in names:
            try:
                if self.stop_experiment(name, reason=reason):
                    stopped.append(name)
            except Exception:
                log.exception("stop of '%s' failed (%s)", name, reason)
        return stopped

    def handle_emergency_stop(self) -> list[str]:
        """Emergency stop ends EVERY experiment: it asserts something is
        physically wrong with the instrument, not with one culture
        (PARALLEL_EXPERIMENTS.md T9). Each engine raises its own critical
        alert, so every run's events.csv records it."""
        with self._lock:
            names = list(self._runs)
            for name in names:
                try:
                    self._runs[name].handle_emergency_stop()
                except Exception:
                    log.exception("emergency stop of '%s' failed", name)
            for name in names:
                engine = self._runs.pop(name, None)
                if engine is not None:
                    self._stopped[name] = engine
            self._held.clear()
            self._set_machine_hold_locked(None)
        return names

    def delete_experiment(self, name: str) -> None:
        with self._lock:
            engine = self._runs.get(name)
            if engine is not None:
                raise InvalidExperimentStateError(
                    f"cannot delete '{name}' while it is loaded with "
                    f"status={engine.status_string}; stop it first"
                )
            self._stopped.pop(name, None)
        self._offline().delete_experiment(name)

    def rename_experiment(self, old: str, new: str) -> dict:
        with self._lock:
            if new in self._runs:
                raise FileExistsError(f"experiment '{new}' is loaded")
            engine = self._runs.get(old)
            if engine is None:
                self._stopped.pop(old, None)
                return self._offline().rename_experiment(old, new)
            result = engine.rename_experiment(old, new)
            self._runs = {
                (new if k == old else k): v for k, v in self._runs.items()
            }
            return result

    def update_metadata(self, name: str, *, notes=None, tags=None) -> dict:
        engine = self.get_run(name) or self._offline()
        return engine.update_metadata(name, notes=notes, tags=tags)

    def record_calibration_provenance(self, name: str, partial: dict) -> dict:
        engine = self.get_run(name) or self._offline()
        return engine.record_calibration_provenance(name, partial)

    def get_data(self, name: str, vial: int, parameter: str, **kwargs):
        return self._offline().get_data(name, vial, parameter, **kwargs)

    def list_experiments(self) -> list[dict]:
        results = self._offline().list_experiments()
        with self._lock:
            for info in results:
                engine = self._runs.get(info["name"])
                if engine is not None:
                    info["status"] = engine.status_string
                    info["vials"] = engine.loaded_vials
                    info["operator"] = self.operator_of(info["name"])
        return results

    # ------------------------------------------------------------------
    # Per-experiment operations (name=None means the only one loaded)
    # ------------------------------------------------------------------

    def status(self, name: Optional[str] = None) -> dict:
        with self._lock:
            if name is None and not self._runs:
                return {"status": "idle", "name": None}
            return self._runs[self._resolve_locked(name)].status()

    def enter_maintenance(self, name: Optional[str] = None, reason: str = "manual") -> dict:
        return self.run(name).enter_maintenance(reason)

    def exit_maintenance(
        self, name: Optional[str] = None, reason: str = "manual",
    ) -> list[tuple[int, PumpAction, str]]:
        with self._lock:
            engine = self._runs[self._resolve_locked(name)]
            queued = engine.exit_maintenance(reason=reason)
            return self._hold_if_machine_held_locked(engine, queued)

    def refill_media(
        self, name: Optional[str] = None, *, bottles=None, waste_filled_ml=None,
    ) -> dict:
        return self.run(name).refill_media(bottles=bottles, waste_filled_ml=waste_filled_ml)

    def confirm_escalation(self, name: str, vial: int, **kwargs) -> dict:
        return self.run(name).confirm_escalation(name=name, vial=vial, **kwargs)

    def set_growth_context(self, name: Optional[str], context: Optional[dict]) -> None:
        with self._lock:
            engine = self._runs.get(name) if name else None
            if engine is not None:
                engine.set_growth_context(context)

    # ------------------------------------------------------------------
    # Vial-addressed operations (routed to the owner)
    # ------------------------------------------------------------------

    def _booking_engine(self, vial: int) -> Optional[ExperimentEngine]:
        """The experiment a manual action on ``vial`` belongs to: its loaded
        owner, else the stopped experiment that last had it."""
        with self._lock:
            engine = self.owner_engine(vial)
            if engine is not None:
                return engine
            for stopped in reversed(list(self._stopped.values())):
                if int(vial) in stopped.loaded_vials:
                    return stopped
            return None

    def flow_rate_ml_s(self, vial: int, direction: str = "influx") -> float:
        """The vial's pump rate from the experiment it belongs to (its
        calibration is the right one for it), else the defaults."""
        engine = self._booking_engine(vial) or self._offline()
        return engine.flow_rate_ml_s(vial, direction)

    def record_manual_pump(self, vial: int, direction: str, delivered_ml: float) -> None:
        """Book a manual pump against the experiment the vial belongs to (see
        ``_booking_engine``). A vial no experiment has used is booked
        nowhere."""
        engine = self._booking_engine(vial)
        if engine is not None:
            engine.record_manual_pump(vial, direction, delivered_ml)
            self._vessels.flush()

    def vial_group(self, vial: int) -> Optional[dict]:
        engine = self.owner_engine(vial)
        return None if engine is None else engine.vial_group(vial)

    def growth_snapshot(self) -> dict:
        """Every experiment's per-vial growth reports, keyed by vial (vials
        are disjoint across experiments)."""
        with self._lock:
            out: dict = {}
            for engine in self._runs.values():
                out.update(engine.growth_snapshot())
            return out

    def escalation_pending_vials(self) -> list[int]:
        with self._lock:
            return sorted(
                v for e in self._runs.values() for v in e.escalation_pending_vials()
            )

    def od_acquisition_params(self) -> dict:
        """The acquisition for the ONE OD read that serves every running
        experiment: ``n_samples`` / ``n_dark`` at the maximum any asks for;
        ``agg`` and ``dark_subtract`` agree by construction (create refuses a
        mismatch)."""
        with self._lock:
            params = [e.od_acquisition_params() for e in self._runs.values() if e.is_running]
        if not params:
            return dict(DEFAULT_OD_ACQUISITION)
        merged = dict(params[0])
        merged["n_samples"] = max(int(p["n_samples"]) for p in params)
        merged["n_dark"] = max(int(p["n_dark"]) for p in params)
        return merged

    # ------------------------------------------------------------------
    # The tick
    # ------------------------------------------------------------------

    def run_cycle(
        self,
        timestamp_iso: str,
        temperature_calibrated: list[float],
        od_calibrated: Optional[list[float]] = None,
        od_flags: Optional[list[str]] = None,
    ) -> list[tuple[int, PumpAction]]:
        """One tick for every experiment. Returns every dilution decided this
        tick, for the caller to fire as ONE concurrent pump schedule.

        One experiment's failure does not cost the others their tick: it is
        caught, alerted and the loop continues -- a raise here would skip
        heater safety for every experiment after it."""
        actions: list[tuple[int, PumpAction]] = []
        with self._lock:
            for name, engine in list(self._runs.items()):
                try:
                    actions.extend(engine.run_cycle(
                        timestamp_iso, temperature_calibrated, od_calibrated,
                        od_flags=od_flags,
                    ))
                except Exception as exc:
                    log.exception("run_cycle failed for experiment '%s'", name)
                    self._alert(
                        "critical",
                        f"Control loop cycle failed for experiment '{name}': {exc}",
                        category="system",
                        dedup_key=("run_cycle_failed", name),
                        experiment=name,
                    )
            try:
                self._resend_stir_locked()
            except Exception:
                log.exception("composed stir re-send failed")
            # One registry write for every debit this tick (vessels.py).
            self._vessels.flush()
            if self._machine_hold is not None and actions:
                owners = self._owners_locked()
                for vial, action in actions:
                    self._held[vial] = (owners.get(vial, ""), action, timestamp_iso)
                return []
        return actions

    def _resend_stir_locked(self) -> None:
        """ONE stir write per tick for every experiment's driven vials,
        re-asserted every tick (drift protection, SPEC §9 step 6). Vials no
        experiment drives keep whatever manual control last set."""
        targets: dict[int, int] = {}
        for engine in self._runs.values():
            targets.update(engine.stir_targets())
        if not targets:
            return
        current = list(getattr(self._manager, "stir_speed", [0] * N_VIALS))
        stir = [int(v) for v in current]
        for vial, value in targets.items():
            stir[vial] = int(value)
        self._manager.set_stir(stir)

    def check_maintenance_timeout(self) -> list[tuple[int, PumpAction, str]]:
        """Every experiment's maintenance failsafe, plus the machine hold's.
        Returns the dilutions to fire now (possibly empty)."""
        queued: list[tuple[int, PumpAction, str]] = []
        with self._lock:
            engines = list(self._runs.values())
        for engine in engines:
            try:
                q = engine.check_maintenance_timeout()
            except Exception:
                log.exception("maintenance timeout check failed")
                continue
            if q:
                with self._lock:
                    queued.extend(self._hold_if_machine_held_locked(engine, q))
        with self._lock:
            hold = self._machine_hold
            due = (
                hold is not None
                and (_now_utc() - hold["entered_at"]).total_seconds()
                >= self._maintenance_timeout_seconds
            )
        if due:
            self._alert(
                "critical",
                "Machine hold auto-resumed after "
                f"{self._maintenance_timeout_seconds / 60:.0f} min -- every "
                "experiment was at risk of stalling",
                category="maintenance",
                dedup_key="machine_hold_timeout",
            )
            queued.extend(self.exit_machine_hold(reason="auto_timeout"))
        return queued

    # ------------------------------------------------------------------
    # Machine hold
    # ------------------------------------------------------------------

    @property
    def machine_hold(self) -> Optional[dict]:
        with self._lock:
            hold = self._machine_hold
            if hold is None:
                return None
            auto_at = hold["entered_at"].timestamp() + self._maintenance_timeout_seconds
            return {
                "active": True,
                "reason": hold["reason"],
                "entered_at": hold["entered_at"].isoformat(timespec="seconds"),
                "auto_resume_at": datetime.fromtimestamp(
                    auto_at, tz=timezone.utc).isoformat(timespec="seconds"),
                "auto_resume_in_seconds": max(
                    0.0, round(auto_at - _now_utc().timestamp(), 1)),
                "queued_pump_count": len(self._held),
            }

    def enter_machine_hold(self, reason: str = "physical_intervention") -> dict:
        """Hold every experiment's pumps -- someone has the machine open.
        Sensor reads, logging, heater control and control decisions go on;
        decided dilutions are held, newest per vial, and fire on release.
        Auto-resumes after the maintenance timeout (30 min) like an
        experiment's own maintenance. Idempotent."""
        with self._lock:
            if self._machine_hold is None:
                self._set_machine_hold_locked({"entered_at": _now_utc(), "reason": str(reason)})
                entered = True
            else:
                entered = False
            status = self.machine_hold
        if entered:
            self._event({"type": "machine_hold_entered", "reason": reason})
            self._alert(
                "warning",
                "Machine hold -- pumps suppressed for EVERY experiment. "
                f"Auto-resume in {self._maintenance_timeout_seconds / 60:.0f} min.",
                category="maintenance",
                dedup_key="machine_hold",
            )
        return status

    def exit_machine_hold(self, reason: str = "manual") -> list[tuple[int, PumpAction, str]]:
        """Release the machine hold. Returns the held dilutions to fire --
        except those of an experiment now in its own maintenance, which join
        that experiment's queue instead."""
        with self._lock:
            if self._machine_hold is None:
                return []
            held = self._held
            self._held = {}
            self._set_machine_hold_locked(None)
            by_run: dict[str, list[tuple[int, PumpAction, str]]] = {}
            for vial, (name, action, ts) in held.items():
                by_run.setdefault(name, []).append((vial, action, ts))
            to_fire: list[tuple[int, PumpAction, str]] = []
            for name, entries in by_run.items():
                engine = self._runs.get(name)
                if engine is None or not engine.is_running:
                    continue  # stopped meanwhile: stale, never fired
                if not engine.defer_actions(entries):
                    to_fire.extend(entries)
        self._event({
            "type": "machine_hold_exited", "reason": reason,
            "queued_actions": len(to_fire),
        })
        return sorted(to_fire, key=lambda e: e[0])

    def _hold_if_machine_held_locked(
        self, engine: ExperimentEngine, queued: list,
    ) -> list:
        """Dilutions an experiment releases while the machine hold is on are
        held too."""
        if self._machine_hold is None or not queued:
            return list(queued or [])
        name = engine.loaded_experiment or ""
        for vial, action, ts in queued:
            self._held[vial] = (name, action, ts)
        return []

    def _set_machine_hold_locked(self, hold: Optional[dict]) -> None:
        self._machine_hold = hold
        if self._machine_dir is None:
            return
        path = self._machine_dir / "hold.json"
        try:
            if hold is None:
                if path.exists():
                    path.unlink()
                return
            self._machine_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({
                "entered_at": hold["entered_at"].isoformat(timespec="seconds"),
                "reason": hold["reason"],
            }), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            log.exception("persisting the machine hold failed")

    def _load_machine_hold(self) -> None:
        """A hold survives a restart, with its timer: the person with the
        machine open does not stop being there because the server did."""
        if self._machine_dir is None:
            return
        path = self._machine_dir / "hold.json"
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entered = _parse_iso(data.get("entered_at"))
            if entered is not None:
                self._machine_hold = {"entered_at": entered,
                                      "reason": str(data.get("reason", "manual"))}
        except Exception:
            log.exception("reading the machine hold failed")

    # ------------------------------------------------------------------
    # Resume
    # ------------------------------------------------------------------

    def resume_on_startup(self) -> list[str]:
        """Resume EVERY experiment whose state.json says RUNNING.

        Before parallel experiments all but the most recent were demoted to
        ERROR. Now each resumes on its own engine -- unless two claim the same
        vial, which means something already went wrong: BOTH go to ERROR and
        a critical alert says so, rather than guessing a winner and writing
        heater setpoints on a guess (PARALLEL_EXPERIMENTS.md T5)."""
        candidates: list[tuple[str, dict]] = []
        for entry in sorted(self._experiments_root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            state_path = entry / "state.json"
            if not state_path.is_file():
                continue
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                log.exception("failed to read %s on resume scan", state_path)
                continue
            if state.get("status") == ExperimentStatus.RUNNING:
                candidates.append((entry.name, state))

        claims: dict[int, list[str]] = {}
        for name, state in candidates:
            for v in state.get("vials", []):
                claims.setdefault(int(v), []).append(name)
        conflicted = {n for names in claims.values() if len(names) > 1 for n in names}
        for name, state in candidates:
            if name not in conflicted:
                continue
            others = sorted({
                o for v in state.get("vials", []) for o in claims[int(v)] if o != name
            })
            state["status"] = ExperimentStatus.ERROR
            state["stop_reason"] = "resume_ownership_conflict"
            state["stopped"] = _now_utc().isoformat(timespec="seconds")
            try:
                (self._experiments_root / name / "state.json").write_text(
                    json.dumps(state, indent=4), encoding="utf-8")
            except Exception:
                log.exception("failed to mark '%s' as ERROR", name)
            self._alert(
                "critical",
                f"Experiment '{name}' was NOT resumed: it claims vials also "
                f"claimed by {others}. Both were set to ERROR -- check the "
                "machine before starting either again.",
                category="lifecycle",
                dedup_key=("resume_conflict", name),
            )

        resumed: list[str] = []
        candidates.sort(key=lambda item: (item[1].get("started") or "") + item[0])
        for name, state in candidates:
            if name in conflicted:
                continue
            engine = self._new_engine()
            try:
                with self._lock:
                    engine.resume_from_state(name, state)
                    self._runs[name] = engine
                resumed.append(name)
            except Exception as exc:
                log.exception("resume of '%s' failed", name)
                self._alert(
                    "critical", f"Experiment '{name}' failed to resume: {exc}",
                    category="lifecycle", dedup_key=("resume_failed", name),
                )
        return resumed

    # ------------------------------------------------------------------
    # Vessels
    # ------------------------------------------------------------------

    def vessels(self) -> list[dict]:
        """Every vessel with the loaded experiments that use it."""
        with self._lock:
            users: dict[str, set[str]] = {}
            for name, engine in self._runs.items():
                ids = set(vessel_lib.resolved_vessel_ids(
                    name, (engine._config or {}).get("media")).values())
                for kind_ids in engine.vessel_ids.values():
                    ids |= set(kind_ids)
                for vid in ids:
                    users.setdefault(vid, set()).add(name)
            out = []
            for rec in self._vessels.list():
                rec["users"] = sorted(users.get(rec["id"], set()))
                out.append(rec)
            return out

    def retire_vessel(self, vessel_id: str) -> None:
        with self._lock:
            users = next(
                (v["users"] for v in self.vessels() if v["id"] == vessel_id), None,
            )
            if users is None:
                raise FileNotFoundError(f"no vessel {vessel_id!r}")
            if users:
                raise ConflictError(
                    f"vessel {vessel_id!r} is in use by {users}; stop them first"
                )
            self._vessels.retire(vessel_id)

    # ------------------------------------------------------------------
    # Dashboard summary
    # ------------------------------------------------------------------

    def experiments_summary(self) -> list[dict]:
        """One compact entry per loaded experiment, for every sensor_update."""
        with self._lock:
            engines = list(self._runs.items())
        out = []
        for name, engine in engines:
            try:
                s = engine.status()
            except Exception:
                log.exception("status of '%s' failed", name)
                continue
            out.append({
                "name": s.get("name"),
                "status": s.get("status"),
                "mode": s.get("mode"),
                "operator": s.get("operator") or "",
                "expected_end": s.get("expected_end"),
                "vials": s.get("vials", []),
                "elapsed_hours": s.get("elapsed_hours"),
                "groups": [
                    {"name": g["name"], "mode": g["mode"], "vials": g["vials"]}
                    for g in s.get("groups") or []
                ],
                "grouped": bool(s.get("grouped")),
                "preconditioned": bool(s.get("preconditioned")),
                "maintenance": s.get("maintenance"),
            })
        return out

    # ------------------------------------------------------------------
    # Funnels
    # ------------------------------------------------------------------

    def _alert(
        self, level: str, message: str, *, category: str = "system",
        dedup_key: Any = None, experiment: Optional[str] = None,
    ) -> None:
        if self._on_alert is None:
            return
        payload: dict = {
            "level": level, "message": message, "category": category,
            "timestamp": _now_utc().isoformat(timespec="seconds"),
        }
        if dedup_key is not None:
            payload["dedup_key"] = dedup_key
        if experiment is not None:
            payload["experiment"] = experiment
            payload["data"] = {"experiment": experiment}
        try:
            self._on_alert(payload)
        except Exception:
            log.exception("on_alert callback failed")

    def _event(self, payload: dict) -> None:
        if self._on_event is None:
            return
        try:
            payload = dict(payload)
            payload.setdefault("timestamp", _now_utc().isoformat(timespec="seconds"))
            self._on_event(payload)
        except Exception:
            log.exception("on_event callback failed")
