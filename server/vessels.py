"""server/vessels.py — media bottles and waste carboys as machine-scoped objects.

A bottle or a carboy is a physical vessel on the bench. Until parallel
experiments it was folded into the one loaded experiment, because only one
experiment could draw from it. With two experiments running, two runs can
drain into one waste carboy, or feed from one media bottle, and the level
each run sees must be the vessel's real level -- including what the OTHER run
put in or took out. Otherwise both interlocks pass while the shared carboy
overflows at a fraction of the fill either run believes (PARALLEL_EXPERIMENTS.md
T8). So the level lives here, once per vessel, and runs reference it.

**Identity.** A vessel an experiment declares itself gets the id
``"<experiment>.<bottle_id>"`` (``"<experiment>.waste"`` for the carboy) and
is reset when that experiment starts. An experiment that names an existing
vessel (``media.bottles[i].vessel`` / ``media.waste.vessel``) shares it: the
level carries over and both runs debit it.

**Records** are plain JSON-serialisable dicts:

    {"id", "kind": "media" | "waste", "name", "contents",
     "initial_volume_ml" (media) | "capacity_ml" (waste),
     "low_volume_alert_ml" (media) | "high_fill_alert_ml" (waste),
     "level_ml"         -- media: consumed; waste: filled,
     "alerted_level"    -- the low-media / high-waste warning latch,
     "alerted_blocked"  -- the §15 interlock's critical-alert latch,
     "attribution": {experiment: mL},   -- who moved how much
     "created", "updated"}

**Persistence.** With a ``path`` the registry is written atomically (tmp +
fsync + replace). Registration, refills and alert latches write at once;
level changes (``add_level``, one per pump per dilution -- 32 when sixteen
vials dilute together) only mark it dirty, and the supervisor calls
:meth:`VesselRegistry.flush` once per tick, so a busy tick costs one SD-card
fsync rather than dozens. A crash loses at most one tick of debits, which
every run's own state.json snapshot also holds. Without a path it is in-memory -- what a standalone ``ExperimentEngine`` uses,
where each run's ``state.json`` snapshot remains the record, exactly as
before vessels existed.

The level is inferred (``duration x flow_rate``), never measured (SPEC §15).
Nothing here changes that; it only stops two runs inferring it separately.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

KIND_MEDIA = "media"
KIND_WASTE = "waste"
KINDS = (KIND_MEDIA, KIND_WASTE)

# Static fields a vessel takes from the declaring experiment's media block.
_STATIC_FIELDS = {
    KIND_MEDIA: ("name", "contents", "initial_volume_ml", "low_volume_alert_ml"),
    KIND_WASTE: ("name", "capacity_ml", "high_fill_alert_ml"),
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def owned_vessel_id(experiment: str, local_id: str) -> str:
    """Id of a vessel an experiment declares itself (not a shared reference)."""
    return f"{experiment}.{local_id}"


class VesselRegistry:
    """Machine-scoped levels for media bottles and waste carboys.

    Thread-safe; every public method takes the registry's own lock, so it can
    be shared by every experiment the supervisor holds.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._vessels: dict[str, dict] = {}
        self._dirty = False
        if self._path is not None and self._path.is_file():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                for rec in data.get("vessels", []):
                    if isinstance(rec, dict) and rec.get("id"):
                        self._vessels[str(rec["id"])] = rec
            except Exception:
                # A corrupt registry must not stop the server booting. Runs
                # resuming then re-seed their vessels from their own
                # state.json snapshots (ExperimentEngine._restore_media_*).
                log.exception("failed to read vessel registry %s", self._path)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def exists(self, vessel_id: Optional[str]) -> bool:
        with self._lock:
            return vessel_id is not None and vessel_id in self._vessels

    def get(self, vessel_id: str) -> Optional[dict]:
        with self._lock:
            rec = self._vessels.get(vessel_id)
            return None if rec is None else copy.deepcopy(rec)

    def field(self, vessel_id: str, key: str, default: Any = None) -> Any:
        with self._lock:
            rec = self._vessels.get(vessel_id)
            return default if rec is None else rec.get(key, default)

    def list(self, *, kind: Optional[str] = None) -> list[dict]:
        with self._lock:
            return [
                copy.deepcopy(r) for r in self._vessels.values()
                if kind is None or r.get("kind") == kind
            ]

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def register(
        self, vessel_id: str, kind: str, spec: dict, *, reset: bool,
    ) -> dict:
        """Create ``vessel_id`` from ``spec`` (a media-bottle or waste block),
        or -- when it exists and ``reset`` is False -- leave its level alone.

        ``reset=True`` is an experiment starting on a vessel it declared
        itself: static fields are (re)taken from ``spec`` and the level,
        latches and attribution start from zero. A shared reference never
        resets; the level is whatever the vessel holds.
        """
        if kind not in KINDS:
            raise ValueError(f"vessel kind must be one of {KINDS}, got {kind!r}")
        with self._lock:
            rec = self._vessels.get(vessel_id)
            if rec is not None and rec.get("kind") != kind:
                raise ValueError(
                    f"vessel {vessel_id!r} is a {rec.get('kind')} vessel, not {kind}"
                )
            if rec is None or reset:
                rec = {
                    "id": vessel_id,
                    "kind": kind,
                    "level_ml": 0.0,
                    "alerted_level": False,
                    "alerted_blocked": False,
                    "attribution": {},
                    "created": _iso_now(),
                    "updated": _iso_now(),
                }
                for key in _STATIC_FIELDS[kind]:
                    if key in spec:
                        rec[key] = copy.deepcopy(spec[key])
                rec.setdefault("name", "")
                if kind == KIND_MEDIA:
                    rec.setdefault("contents", "")
                self._vessels[vessel_id] = rec
                self._persist_locked()
            return copy.deepcopy(rec)

    def set_field(self, vessel_id: str, key: str, value: Any) -> None:
        with self._lock:
            rec = self._vessels.get(vessel_id)
            if rec is None:
                raise KeyError(vessel_id)
            if rec.get(key) == value:
                return
            rec[key] = value
            rec["updated"] = _iso_now()
            self._persist_locked()

    def add_level(
        self, vessel_id: str, ml: float, *, experiment: Optional[str] = None,
    ) -> float:
        """Add ``ml`` to the level (media consumed / waste filled) and to the
        experiment's attribution. Returns the new level."""
        with self._lock:
            rec = self._vessels.get(vessel_id)
            if rec is None:
                raise KeyError(vessel_id)
            rec["level_ml"] = float(rec.get("level_ml", 0.0)) + float(ml)
            if experiment:
                attr = rec.setdefault("attribution", {})
                attr[experiment] = float(attr.get(experiment, 0.0)) + float(ml)
            rec["updated"] = _iso_now()
            self._dirty = True   # written by flush(), once per tick
            return rec["level_ml"]

    def flush(self) -> None:
        """Write pending level changes, if any."""
        with self._lock:
            if self._dirty:
                self._persist_locked()

    def retire(self, vessel_id: str) -> None:
        """Forget a vessel. The caller checks nothing references it."""
        with self._lock:
            if self._vessels.pop(vessel_id, None) is not None:
                self._persist_locked()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist_locked(self) -> None:
        self._dirty = False
        if self._path is None:
            return
        payload = {"vessels": list(self._vessels.values()), "saved": _iso_now()}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, self._path)
        except Exception:
            # Levels are also snapshotted into every run's state.json; a
            # failed registry write degrades resume, it must not stop control.
            log.exception("vessel registry write failed (%s)", self._path)


def media_vessel_refs(media: Optional[dict]) -> dict[str, list[str]]:
    """``{"media": [...], "waste": [...]}`` vessel ids a media block REFERENCES
    (shares), as opposed to declaring. Pure -- used for validation."""
    refs: dict[str, list[str]] = {KIND_MEDIA: [], KIND_WASTE: []}
    if not media:
        return refs
    for b in media.get("bottles") or []:
        if isinstance(b, dict) and b.get("vessel"):
            refs[KIND_MEDIA].append(str(b["vessel"]))
    waste = media.get("waste") or {}
    if isinstance(waste, dict) and waste.get("vessel"):
        refs[KIND_WASTE].append(str(waste["vessel"]))
    return refs


def resolved_vessel_ids(experiment: str, media: Optional[dict]) -> dict[str, str]:
    """``{local_id: vessel_id}`` for every bottle, plus ``"waste"``.

    A bottle's own ``vessel`` key wins (a shared reference); otherwise the
    experiment owns the vessel under :func:`owned_vessel_id`. Deterministic
    from the config, so resume re-derives the same ids."""
    out: dict[str, str] = {}
    if not media:
        return out
    for b in media.get("bottles") or []:
        bid = str(b["id"])
        out[bid] = str(b.get("vessel") or owned_vessel_id(experiment, bid))
    waste = media.get("waste") or {}
    out["waste"] = str(waste.get("vessel") or owned_vessel_id(experiment, "waste"))
    return out


def iter_owned(experiment: str, media: Optional[dict]) -> Iterable[str]:
    """Vessel ids the experiment declares itself (reset at its start)."""
    if not media:
        return []
    ids = [
        owned_vessel_id(experiment, str(b["id"]))
        for b in media.get("bottles") or [] if not b.get("vessel")
    ]
    if not (media.get("waste") or {}).get("vessel"):
        ids.append(owned_vessel_id(experiment, "waste"))
    return ids
