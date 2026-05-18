"""server/watchdog.py — safety watchdog timer (SPEC §10).

A background thread expects a heartbeat from the experiment loop every
cycle. If no heartbeat is received for ``timeout_minutes`` (default 30),
the watchdog calls ``serial_manager.emergency_shutdown()`` and the
registered ``on_trigger`` callback.

The trigger is idempotent: once fired, the watchdog will not re-fire
until ``pet()`` is called again, which also clears the latched
``triggered`` flag so the timer re-arms.

Usage::

    wd = Watchdog(
        serial_manager,
        timeout_minutes=30,
        on_trigger=lambda reason: socketio.emit("alert", {...}),
    )
    wd.start()
    ...
    wd.pet()      # each sensor / experiment cycle
    ...
    wd.stop()
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional


DEFAULT_TIMEOUT_MINUTES = 30.0
DEFAULT_CHECK_INTERVAL_SECONDS = 5.0

log = logging.getLogger(__name__)


class Watchdog:
    def __init__(
        self,
        serial_manager,
        timeout_minutes: float = DEFAULT_TIMEOUT_MINUTES,
        check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
        on_trigger: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_minutes <= 0:
            raise ValueError(f"timeout_minutes must be > 0, got {timeout_minutes}")
        if check_interval_seconds <= 0:
            raise ValueError(
                f"check_interval_seconds must be > 0, got {check_interval_seconds}"
            )
        self._serial_manager = serial_manager
        self._timeout_seconds = float(timeout_minutes) * 60.0
        self._check_interval = float(check_interval_seconds)
        self._on_trigger = on_trigger
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_heartbeat = self._clock()
        self._triggered = False

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def triggered(self) -> bool:
        with self._lock:
            return self._triggered

    def seconds_since_heartbeat(self) -> float:
        with self._lock:
            return self._clock() - self._last_heartbeat

    # ------------------------------------------------------------------
    # Heartbeat / check
    # ------------------------------------------------------------------

    def pet(self) -> None:
        """Reset the timer. Call from the experiment loop each cycle.

        Also clears a prior trigger so the watchdog re-arms on the next
        heartbeat — keeps the timer self-recovering if the experiment
        loop comes back to life."""
        with self._lock:
            self._last_heartbeat = self._clock()
            self._triggered = False

    def check(self) -> bool:
        """Inspect the timer once.

        Returns True iff the watchdog fired during this call. If the
        timeout has elapsed and the watchdog is not already latched,
        invokes ``emergency_shutdown`` and ``on_trigger`` (each in its
        own ``try``/``except`` — a broken callback never prevents the
        actuator shutdown)."""
        with self._lock:
            if self._triggered:
                return False
            elapsed = self._clock() - self._last_heartbeat
            if elapsed <= self._timeout_seconds:
                return False
            self._triggered = True
            reason = (
                f"Watchdog triggered: no heartbeat for {elapsed / 60.0:.1f} min "
                f"(timeout {self._timeout_seconds / 60.0:.1f} min) — "
                "all actuators zeroed"
            )
        # Fire callbacks outside the lock; on_trigger may take a while
        # (e.g. socketio.emit) and we don't want to block pet().
        self._fire(reason)
        return True

    def _fire(self, reason: str) -> None:
        log.warning(reason)
        try:
            self._serial_manager.emergency_shutdown()
        except Exception:
            log.exception("watchdog: emergency_shutdown failed")
        if self._on_trigger is not None:
            try:
                self._on_trigger(reason)
            except Exception:
                log.exception("watchdog: on_trigger callback failed")

    # ------------------------------------------------------------------
    # Thread lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background monitor thread. Idempotent."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._last_heartbeat = self._clock()
            self._triggered = False
            self._thread = threading.Thread(
                target=self._monitor, name="watchdog", daemon=True
            )
            self._thread.start()
        log.info(
            "watchdog started (timeout=%.1f min, check_interval=%.1f s)",
            self._timeout_seconds / 60.0,
            self._check_interval,
        )

    def stop(self) -> None:
        """Signal the monitor thread to exit and wait briefly for it."""
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=self._check_interval * 2)

    def _monitor(self) -> None:
        while not self._stop.is_set():
            try:
                self.check()
            except Exception:
                log.exception("watchdog: check failed")
            self._stop.wait(timeout=self._check_interval)
        log.info("watchdog monitor stopped")
