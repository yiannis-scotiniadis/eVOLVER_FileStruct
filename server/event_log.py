"""server/event_log.py — structured logging, the unified event log, and error
classification (SPEC §20.1–§20.3, ROADMAP Session M).

Three concerns live here. None of them import Flask, the serial manager, or the
experiment engine, so the whole module is unit-testable on its own:

  1. **Rotating, disk-aware file logs** (§20.1) — :func:`setup_file_logging`.
  2. **The event ring buffer** backing ``GET /api/events/recent``, and (through
     the DataLogger) ``experiments/{name}/events.csv`` (§20.2) — :class:`EventLog`.
  3. **Error classification and rate limiting** (§20.3) — :class:`RateLimiter`,
     :class:`BusHealth`, :class:`VialHealth`.

The classification vocabulary is SPEC §20.3's, and the distinctions matter:

    TRANSIENT   A single malformed or dropped RS485 frame. Counted, never
                alerted — the bus is lossy by design and commit ``b9b135a``
                already tolerates it.
    DEGRADED    One vial's sensor failing repeatedly. Surfaced as a per-vial
                health badge, not as a stream of alerts.
    PERSISTENT  The whole bus silent — port gone, Arduino unresponsive. This is
                the class that ends experiments, so it alerts immediately.

The ring buffer is deliberately **independent of experiment state**: calibration
faults, manual-control failures, and serial errors all happen while idle, so a
per-experiment ``events.csv`` is not sufficient on its own (§20.4).
"""

from __future__ import annotations

import logging
import math
import shutil
import threading
import time
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

N_VIALS = 16

# --- Levels -----------------------------------------------------------------
# Three levels, matching the `alert` WebSocket contract the browser already
# speaks. "warning" must never render in the success colour (SPEC §20.4).
LEVEL_INFO = "info"
LEVEL_WARNING = "warning"
LEVEL_CRITICAL = "critical"
LEVELS = (LEVEL_INFO, LEVEL_WARNING, LEVEL_CRITICAL)
_LEVEL_RANK = {LEVEL_INFO: 0, LEVEL_WARNING: 1, LEVEL_CRITICAL: 2}


class ErrorClass:
    """SPEC §20.3 classification vocabulary."""

    TRANSIENT = "transient"
    DEGRADED = "degraded"
    PERSISTENT = "persistent"
    RECOVERED = "recovered"


log = logging.getLogger(__name__)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Rotating file logs (SPEC §20.1)
# ---------------------------------------------------------------------------

DEFAULT_LOG_BYTES = 10 * 1024 ** 2       # 10 MB per file ...
DEFAULT_LOG_BACKUPS = 4                  # ... x 5 files total (ROADMAP "5 x 10 MB")
DEFAULT_ERROR_LOG_BYTES = 5 * 1024 ** 2
DEFAULT_ERROR_LOG_BACKUPS = 4
# Free-space floor below which file logging suspends itself. Deliberately BELOW
# app.py's DISK_CRITICAL_FREE_BYTES (256 MB) so the operator gets the critical
# disk alert *before* the logs that would explain it stop being written.
DEFAULT_DISK_FLOOR_BYTES = 128 * 1024 ** 2
DEFAULT_DISK_CHECK_INTERVAL_SECONDS = 30.0

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class DiskGuardedRotatingFileHandler(RotatingFileHandler):
    """A ``RotatingFileHandler`` that stops writing when the filesystem is
    nearly full, instead of helping to fill it.

    The RPi's SD card holds the experiment data as well as the logs, so a stuck
    loop emitting an error every 10 ms must not be able to take the run down
    with it. ``shutil.disk_usage`` is cached for ``check_interval`` seconds, so
    a hot loop costs one stat per interval rather than one per line.

    Failing to *read* free space fails open (keep logging) — a stat error is not
    by itself a reason to go silent.
    """

    def __init__(
        self,
        filename,
        *,
        floor_bytes: int = DEFAULT_DISK_FLOOR_BYTES,
        check_interval: float = DEFAULT_DISK_CHECK_INTERVAL_SECONDS,
        clock=time.monotonic,
        **kwargs,
    ) -> None:
        super().__init__(filename, **kwargs)
        self.floor_bytes = int(floor_bytes)
        self.check_interval = float(check_interval)
        self._clock = clock
        self._last_check: Optional[float] = None
        self._suspended = False
        self.skipped_records = 0

    @property
    def suspended(self) -> bool:
        return self._suspended

    def free_bytes(self) -> Optional[int]:
        try:
            return shutil.disk_usage(Path(self.baseFilename).parent).free
        except OSError:
            return None

    def refresh_suspended(self, force: bool = False) -> bool:
        """Re-evaluate the free-space floor (at most once per check_interval
        unless ``force``). Returns the current suspension state."""
        now = self._clock()
        if (
            not force
            and self._last_check is not None
            and (now - self._last_check) < self.check_interval
        ):
            return self._suspended
        self._last_check = now
        free = self.free_bytes()
        if free is None:
            return self._suspended  # can't tell -> fail open
        self._suspended = free < self.floor_bytes
        return self._suspended

    def emit(self, record: logging.LogRecord) -> None:
        self.refresh_suspended()
        if self._suspended:
            self.skipped_records += 1
            return
        super().emit(record)


# Handlers installed by setup_file_logging, so callers can report suspension
# state without threading the objects through every site.
_INSTALLED_HANDLERS: list[DiskGuardedRotatingFileHandler] = []


def setup_file_logging(
    logs_dir,
    *,
    level: str = "INFO",
    floor_bytes: int = DEFAULT_DISK_FLOOR_BYTES,
    max_bytes: int = DEFAULT_LOG_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUPS,
    error_max_bytes: int = DEFAULT_ERROR_LOG_BYTES,
    error_backup_count: int = DEFAULT_ERROR_LOG_BACKUPS,
    check_interval: float = DEFAULT_DISK_CHECK_INTERVAL_SECONDS,
) -> dict:
    """Attach rotating ``evolver.log`` + ``errors.log`` handlers to the root
    logger (SPEC §20.1).

    Any existing StreamHandler is left in place — systemd still captures stdout
    into the journal, which is the one sink that keeps working when the disk is
    full. Idempotent: a second call replaces the handlers this function
    installed rather than stacking duplicates.

    Returns ``{"app": handler, "errors": handler}``.
    """
    logs_path = Path(logs_dir)
    logs_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    for h in list(_INSTALLED_HANDLERS):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:  # pragma: no cover - close is best-effort
            pass
    _INSTALLED_HANDLERS.clear()

    formatter = logging.Formatter(LOG_FORMAT)
    numeric_level = getattr(logging, str(level).upper(), logging.INFO)

    app_handler = DiskGuardedRotatingFileHandler(
        str(logs_path / "evolver.log"),
        floor_bytes=floor_bytes,
        check_interval=check_interval,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    app_handler.setLevel(numeric_level)
    app_handler.setFormatter(formatter)

    error_handler = DiskGuardedRotatingFileHandler(
        str(logs_path / "errors.log"),
        floor_bytes=floor_bytes,
        check_interval=check_interval,
        maxBytes=error_max_bytes,
        backupCount=error_backup_count,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(formatter)

    root.addHandler(app_handler)
    root.addHandler(error_handler)
    # The root logger's own level gates before any handler sees a record.
    if root.level == logging.NOTSET or root.level > numeric_level:
        root.setLevel(numeric_level)
    _INSTALLED_HANDLERS.extend([app_handler, error_handler])
    return {"app": app_handler, "errors": error_handler}


def file_log_status() -> dict:
    """Suspension state of the installed file handlers, for ``/api/health``."""
    if not _INSTALLED_HANDLERS:
        return {"enabled": False, "suspended": False, "skipped_records": 0}
    return {
        "enabled": True,
        "suspended": any(h.suspended for h in _INSTALLED_HANDLERS),
        "skipped_records": sum(h.skipped_records for h in _INSTALLED_HANDLERS),
        "files": [Path(h.baseFilename).name for h in _INSTALLED_HANDLERS],
    }


# ---------------------------------------------------------------------------
# Rate limiting (SPEC §20.3)
# ---------------------------------------------------------------------------

DEFAULT_EVERY_NTH = 10
DEFAULT_RESET_AFTER_SECONDS = 300.0


class RateLimiter:
    """Log the first occurrence of a repeating fault, then every Nth with a
    running count (SPEC §20.3).

    A key that has been quiet for ``reset_after_seconds`` starts over, so a
    fault that recurs tomorrow alerts again instead of being swallowed forever
    by a counter set during yesterday's incident.
    """

    def __init__(
        self,
        *,
        every_nth: int = DEFAULT_EVERY_NTH,
        reset_after_seconds: float = DEFAULT_RESET_AFTER_SECONDS,
        clock=time.monotonic,
    ) -> None:
        self.every_nth = max(1, int(every_nth))
        self.reset_after_seconds = float(reset_after_seconds)
        self._clock = clock
        # key -> [count, last_seen]
        self._entries: dict = {}

    def check(self, key) -> tuple[bool, int, bool]:
        """Register one occurrence of ``key``.

        Returns ``(should_emit, count, is_new)`` where ``count`` is the running
        occurrence count since the window opened and ``is_new`` marks the first
        occurrence of a fresh window.
        """
        now = self._clock()
        entry = self._entries.get(key)
        if entry is None or (now - entry[1]) > self.reset_after_seconds:
            self._entries[key] = [1, now]
            return True, 1, True
        entry[0] += 1
        entry[1] = now
        count = entry[0]
        return (count % self.every_nth == 0), count, False

    def count(self, key) -> int:
        entry = self._entries.get(key)
        return entry[0] if entry else 0

    def active_keys(self) -> set:
        return set(self._entries)

    def prune(self) -> None:
        """Drop windows that have gone quiet, so a long-running server does not
        accumulate one dict entry per distinct message forever."""
        now = self._clock()
        stale = [
            k for k, v in self._entries.items()
            if (now - v[1]) > self.reset_after_seconds
        ]
        for k in stale:
            del self._entries[k]


# ---------------------------------------------------------------------------
# The event log (SPEC §20.2)
# ---------------------------------------------------------------------------

DEFAULT_RING_SIZE = 500

# Categories are a closed-ish vocabulary so the UI can filter on them. Not
# enforced — an unknown category is recorded as-is rather than dropped.
CATEGORY_LIFECYCLE = "lifecycle"
CATEGORY_PUMP = "pump"
CATEGORY_SENSOR = "sensor"
CATEGORY_SERIAL = "serial"
CATEGORY_HEATER = "heater"
CATEGORY_MEDIA = "media"
CATEGORY_WASTE = "waste"
CATEGORY_MAINTENANCE = "maintenance"
CATEGORY_ESCALATION = "escalation"
CATEGORY_ACTUATOR = "actuator"
CATEGORY_STORAGE = "storage"
CATEGORY_SYSTEM = "system"
CATEGORY_CALIBRATION = "calibration"


class EventLog:
    """In-memory ring buffer + per-experiment ``events.csv`` fan-out.

    The ring is populated whether or not an experiment is running; the CSV
    write is a no-op while idle (the DataLogger enforces that). One
    :meth:`record` call therefore serves both the live drawer and the
    lab-notebook artefact.
    """

    def __init__(
        self,
        data_logger=None,
        *,
        ring_size: int = DEFAULT_RING_SIZE,
        every_nth: int = DEFAULT_EVERY_NTH,
        reset_after_seconds: float = DEFAULT_RESET_AFTER_SECONDS,
        clock=time.monotonic,
    ) -> None:
        self._data_logger = data_logger
        self._ring: deque = deque(maxlen=int(ring_size))
        self._lock = threading.RLock()
        self._next_id = 1
        self._limiter = RateLimiter(
            every_nth=every_nth,
            reset_after_seconds=reset_after_seconds,
            clock=clock,
        )
        # dedup key -> the ring entry it belongs to, so a repeat updates the
        # existing row in place instead of appending a near-duplicate.
        self._by_key: dict = {}
        self._records_since_prune = 0

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        level: str = LEVEL_INFO,
        category: str = CATEGORY_SYSTEM,
        message: str,
        vial: Optional[int] = None,
        data: Optional[dict] = None,
        dedup_key=None,
        timestamp: Optional[str] = None,
    ) -> Optional[dict]:
        """Record one event.

        Returns the event dict when the caller should surface it (emit an
        alert, write a log line), or ``None`` when the rate limiter suppressed
        this occurrence. A suppressed occurrence still bumps ``count`` on the
        existing ring entry, which is what lets the drawer show *one* row with
        a rising count rather than hundreds of rows (SPEC §20.4).
        """
        level = level if level in LEVELS else LEVEL_INFO
        key = dedup_key if dedup_key is not None else (category, level, message)
        ts = timestamp or _iso_now()

        with self._lock:
            should_emit, count, is_new = self._limiter.check(key)
            entry = None if is_new else self._by_key.get(key)

            if entry is None:
                entry = {
                    "id": self._next_id,
                    "timestamp": ts,
                    "last_timestamp": ts,
                    "level": level,
                    "category": category,
                    "vial": (None if vial is None else int(vial)),
                    "message": message,
                    "data": dict(data) if data else None,
                    "count": count,
                    "acknowledged": False,
                    "acknowledged_by": None,
                    "acknowledged_at": None,
                }
                self._next_id += 1
                self._ring.append(entry)
                self._by_key[key] = entry
            else:
                entry["count"] = count
                entry["last_timestamp"] = ts
                if data:
                    entry["data"] = dict(data)

            self._prune_locked()

            if not should_emit:
                return None
            # Snapshot outside the CSV write so callers can't mutate the ring.
            payload = dict(entry)

        self._write_csv(payload)
        return payload

    def record_alert(self, payload: dict) -> Optional[dict]:
        """Record an ``alert``-shaped payload (``level``/``message``/``vial``).

        Returns the enriched event to emit, or None when suppressed as a repeat.
        """
        payload = dict(payload or {})
        return self.record(
            level=payload.get("level", LEVEL_WARNING),
            category=payload.get("category", CATEGORY_SYSTEM),
            message=payload.get("message", "Alert"),
            vial=payload.get("vial"),
            data=payload.get("data"),
            dedup_key=payload.get("dedup_key"),
            timestamp=payload.get("timestamp"),
        )

    def _write_csv(self, entry: dict) -> None:
        """Append to the active experiment's events.csv. No-op while idle."""
        if self._data_logger is None:
            return
        try:
            self._data_logger.log_event(
                timestamp_iso=entry["last_timestamp"],
                level=entry["level"],
                category=entry["category"],
                message=entry["message"],
                vial=entry["vial"],
                data=entry.get("data"),
            )
        except Exception:
            # Never let the observability path take down the caller. This is
            # the one log line that must not route back through EventLog.
            log.exception("events.csv write failed")

    def _prune_locked(self) -> None:
        """Keep ``_by_key`` from growing without bound.

        Entries evicted from the ring can leave stale references behind; drop
        anything older than the oldest surviving ring entry.
        """
        self._records_since_prune += 1
        if self._records_since_prune < 200:
            return
        self._records_since_prune = 0
        self._limiter.prune()
        if not self._ring:
            self._by_key.clear()
            return
        oldest_id = self._ring[0]["id"]
        live = self._limiter.active_keys()
        self._by_key = {
            k: v
            for k, v in self._by_key.items()
            if v["id"] >= oldest_id and k in live
        }

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def recent(
        self,
        *,
        level: Optional[str] = None,
        category: Optional[str] = None,
        vial: Optional[int] = None,
        limit: int = 100,
        unacked_only: bool = False,
    ) -> list[dict]:
        """Most-recent-first slice of the ring.

        ``level`` is a *minimum* severity ("warning" returns warnings and
        criticals), which is how an operator filtering for problems expects it
        to behave.
        """
        min_rank = _LEVEL_RANK.get(level) if level else None
        out: list[dict] = []
        with self._lock:
            for entry in reversed(self._ring):
                if min_rank is not None and _LEVEL_RANK.get(entry["level"], 0) < min_rank:
                    continue
                if category is not None and entry["category"] != category:
                    continue
                if vial is not None and entry["vial"] != int(vial):
                    continue
                if unacked_only and entry["acknowledged"]:
                    continue
                out.append(dict(entry))
                if len(out) >= max(1, int(limit)):
                    break
        return out

    def counts(self) -> dict:
        with self._lock:
            unacked_critical = 0
            unacked_warning = 0
            for entry in self._ring:
                if entry["acknowledged"]:
                    continue
                if entry["level"] == LEVEL_CRITICAL:
                    unacked_critical += 1
                elif entry["level"] == LEVEL_WARNING:
                    unacked_warning += 1
            return {
                "total": len(self._ring),
                "unacked_critical": unacked_critical,
                "unacked_warning": unacked_warning,
            }

    def acknowledge(self, event_id: int, by: str = "operator") -> Optional[dict]:
        """Acknowledge one event. The acknowledgement is itself recorded as an
        event (SPEC §20.4), so the lab-notebook artefact shows who cleared what
        and when."""
        with self._lock:
            target = None
            for entry in self._ring:
                if entry["id"] == int(event_id):
                    target = entry
                    break
            if target is None:
                return None
            if not target["acknowledged"]:
                target["acknowledged"] = True
                target["acknowledged_by"] = str(by)
                target["acknowledged_at"] = _iso_now()
            payload = dict(target)

        self.record(
            level=LEVEL_INFO,
            category=CATEGORY_SYSTEM,
            message=f"Acknowledged: {payload['message']}",
            vial=payload.get("vial"),
            data={"acknowledged_event_id": payload["id"], "by": str(by)},
            dedup_key=("ack", payload["id"]),
        )
        return payload


# ---------------------------------------------------------------------------
# Read-success helpers (shared by the sensor loop and the tests)
# ---------------------------------------------------------------------------

def _is_finite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def temperature_read_ok(calibrated) -> bool:
    """True when the temperature Arduino answered at all.

    An all-NaN array is what SerialManager returns when the frame timed out or
    was malformed; a single valid value proves the bus is alive.
    """
    return any(_is_finite(v) for v in (calibrated or []))


def od_read_ok(flags=None, n_valid=None, calibrated=None) -> bool:
    """True when the OD Arduino answered at all.

    Note the distinction the engine already draws: an ``out_of_range`` reading
    is NaN *with a working bus* (the culture is denser than the calibration
    covers), so it must not be counted as a bus failure. Only ``dropped``
    frames and zero surviving samples mean the bus went quiet.
    """
    if n_valid:
        try:
            if any(int(n) > 0 for n in n_valid):
                return True
        except (TypeError, ValueError):
            pass
    if flags:
        if any(f != "dropped" for f in flags):
            return True
        return False
    return any(_is_finite(v) for v in (calibrated or []))


# ---------------------------------------------------------------------------
# Bus health (SPEC §20.3 TRANSIENT / PERSISTENT)
# ---------------------------------------------------------------------------

DEFAULT_BUS_FAILURE_THRESHOLD = 3  # matches DEFAULT_SENSOR_FAILURE_THRESHOLD


class BusHealth:
    """Per-subsystem RS485 read health.

    States, keyed on consecutive failed reads:

        0                       -> "ok"
        1 .. threshold-1        -> "degraded"   (classified TRANSIENT)
        >= threshold            -> "down"       (classified PERSISTENT)

    :meth:`record` returns a classification string only on a state *change*, so
    a bus that has been down for an hour produces one alert, not 360.
    """

    def __init__(
        self,
        subsystems=("temperature", "od"),
        *,
        failure_threshold: int = DEFAULT_BUS_FAILURE_THRESHOLD,
        clock=time.monotonic,
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self._clock = clock
        self._lock = threading.RLock()
        self._state = {
            s: {
                "consecutive_failures": 0,
                "total_failures": 0,
                "last_success": None,
                "last_failure": None,
                "state": "ok",
            }
            for s in subsystems
        }

    def record(self, subsystem: str, ok: bool) -> Optional[str]:
        """Register one read attempt. Returns an :class:`ErrorClass` value when
        the state changed in a way worth reporting, else ``None``."""
        with self._lock:
            st = self._state.get(subsystem)
            if st is None:
                st = self._state.setdefault(subsystem, {
                    "consecutive_failures": 0,
                    "total_failures": 0,
                    "last_success": None,
                    "last_failure": None,
                    "state": "ok",
                })
            now = self._clock()
            previous = st["state"]
            if ok:
                st["consecutive_failures"] = 0
                st["last_success"] = now
                st["state"] = "ok"
                return ErrorClass.RECOVERED if previous == "down" else None

            st["consecutive_failures"] += 1
            st["total_failures"] += 1
            st["last_failure"] = now
            if st["consecutive_failures"] >= self.failure_threshold:
                st["state"] = "down"
                # Only on the crossing edge.
                return ErrorClass.PERSISTENT if previous != "down" else None
            st["state"] = "degraded"
            # A lossy frame is counted, never alerted (SPEC §20.3).
            return ErrorClass.TRANSIENT if previous == "ok" else None

    def state(self, subsystem: str) -> str:
        with self._lock:
            st = self._state.get(subsystem)
            return st["state"] if st else "ok"

    def snapshot(self) -> dict:
        with self._lock:
            now = self._clock()
            out = {}
            for name, st in self._state.items():
                last_ok = st["last_success"]
                out[name] = {
                    "state": st["state"],
                    "consecutive_failures": st["consecutive_failures"],
                    "total_failures": st["total_failures"],
                    "seconds_since_success": (
                        None if last_ok is None else round(now - last_ok, 1)
                    ),
                }
            out["threshold"] = self.failure_threshold
            return out


def classify_cycle(
    bus_health,
    vial_health,
    *,
    temperature=None,
    od_calibrated=None,
    od_flags=None,
    od_n_valid=None,
) -> list:
    """Feed one sensor cycle into both health trackers (SPEC §20.3).

    Returns the ``(subsystem, ErrorClass)`` transitions the caller should act
    on. TRANSIENT is returned so a caller can count it; per §20.3 it must not
    raise an alert -- the bus drops frames by design.

    Kept here rather than in the sensor loop so the ok/fail decision has one
    home and can be tested without a Flask app.
    """
    transitions = []
    checks = (
        ("temperature", temperature_read_ok(temperature)),
        ("od", od_read_ok(
            flags=od_flags, n_valid=od_n_valid, calibrated=od_calibrated,
        )),
    )
    for subsystem, ok in checks:
        outcome = bus_health.record(subsystem, ok)
        if outcome is not None:
            transitions.append((subsystem, outcome))
    vial_health.record_cycle(
        temperature=temperature, od_flags=od_flags, od_n_valid=od_n_valid,
    )
    return transitions


# ---------------------------------------------------------------------------
# Per-vial health (SPEC §20.3 DEGRADED)
# ---------------------------------------------------------------------------

DEFAULT_VIAL_DEGRADED_THRESHOLD = 3


class VialHealth:
    """Per-vial consecutive dropped-read streaks.

    Derived from the sensor arrays the loop already has, so the dashboard badge
    works whether or not an experiment is running — the engine's own
    ``_nan_streak`` only advances while RUNNING.

    ``out_of_range`` is tracked separately from ``dropped``: it means the
    culture outgrew the calibration, not that the sleeve is faulty.
    """

    def __init__(
        self,
        n_vials: int = N_VIALS,
        *,
        degraded_threshold: int = DEFAULT_VIAL_DEGRADED_THRESHOLD,
    ) -> None:
        self.n_vials = int(n_vials)
        self.degraded_threshold = max(1, int(degraded_threshold))
        self._lock = threading.RLock()
        self._dropped = [0] * self.n_vials
        self._out_of_range = [0] * self.n_vials

    def record_cycle(
        self,
        *,
        temperature=None,
        od_flags=None,
        od_n_valid=None,
    ) -> None:
        with self._lock:
            for v in range(self.n_vials):
                temp_bad = (
                    temperature is not None
                    and v < len(temperature)
                    and not _is_finite(temperature[v])
                )
                flag = (
                    od_flags[v]
                    if od_flags is not None and v < len(od_flags)
                    else None
                )
                n_valid = (
                    od_n_valid[v]
                    if od_n_valid is not None and v < len(od_n_valid)
                    else None
                )
                od_dropped = flag == "dropped" or (
                    n_valid is not None and int(n_valid) == 0
                )
                if flag == "out_of_range":
                    self._out_of_range[v] += 1
                else:
                    self._out_of_range[v] = 0

                if temp_bad or od_dropped:
                    self._dropped[v] += 1
                else:
                    self._dropped[v] = 0

    def snapshot(self) -> list[dict]:
        with self._lock:
            out = []
            for v in range(self.n_vials):
                streak = self._dropped[v]
                oor = self._out_of_range[v]
                if streak >= self.degraded_threshold:
                    state = "degraded"
                elif oor >= self.degraded_threshold:
                    state = "out_of_range"
                elif streak > 0:
                    state = "lossy"
                else:
                    state = "ok"
                out.append({
                    "vial": v,
                    "state": state,
                    "dropped_streak": streak,
                    "out_of_range_streak": oor,
                })
            return out
