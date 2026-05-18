"""Verification script for the Watchdog (SPEC §10).

Run from the project root:
    python server/test_watchdog.py

Uses an injectable clock so tests run instantly without sleeping
for 30-minute timeouts.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watchdog import Watchdog  # noqa: E402


class FakeSerial:
    """Counts emergency_shutdown invocations."""

    def __init__(self) -> None:
        self.shutdown_count = 0

    def emergency_shutdown(self) -> None:
        self.shutdown_count += 1


class FakeClock:
    """Manually-advanced clock; replaces ``time.monotonic`` in tests."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _build(timeout_minutes: float = 1.0, **kw):
    clock = FakeClock()
    fake = FakeSerial()
    triggers: list[str] = []
    wd = Watchdog(
        serial_manager=fake,
        timeout_minutes=timeout_minutes,
        on_trigger=triggers.append,
        clock=clock,
        **kw,
    )
    return wd, fake, clock, triggers


def test_no_trigger_before_timeout() -> None:
    wd, fake, clock, triggers = _build(timeout_minutes=1.0)
    # Half a minute in: still under 60 s timeout.
    clock.advance(30.0)
    fired = wd.check()
    assert not fired
    assert not wd.triggered
    assert fake.shutdown_count == 0
    assert triggers == []
    print("PASS  no trigger before timeout")


def test_trigger_after_timeout() -> None:
    wd, fake, clock, triggers = _build(timeout_minutes=1.0)
    clock.advance(60.1)
    fired = wd.check()
    assert fired
    assert wd.triggered
    assert fake.shutdown_count == 1
    assert len(triggers) == 1
    assert "Watchdog triggered" in triggers[0]
    print(f"PASS  trigger after timeout ({triggers[0][:60]}...)")


def test_trigger_is_idempotent() -> None:
    """Once latched, repeated checks must not re-fire emergency_shutdown."""
    wd, fake, clock, triggers = _build(timeout_minutes=1.0)
    clock.advance(120.0)
    assert wd.check() is True
    # Three more checks while still timed out — must not refire.
    assert wd.check() is False
    assert wd.check() is False
    assert wd.check() is False
    assert fake.shutdown_count == 1
    assert len(triggers) == 1
    print("PASS  trigger is idempotent (one shutdown per latch)")


def test_pet_resets_timer() -> None:
    wd, fake, clock, triggers = _build(timeout_minutes=1.0)
    clock.advance(50.0)
    wd.pet()
    clock.advance(50.0)  # 50 s since pet, still under 60 s
    assert wd.check() is False
    assert fake.shutdown_count == 0
    print("PASS  pet resets the timer")


def test_pet_rearms_after_trigger() -> None:
    """Recovery: if the experiment loop comes back to life, the next pet
    should clear the latch so the watchdog can fire again later."""
    wd, fake, clock, triggers = _build(timeout_minutes=1.0)
    clock.advance(120.0)
    assert wd.check() is True
    assert wd.triggered

    # Loop recovers and pets the watchdog.
    wd.pet()
    assert not wd.triggered
    assert wd.check() is False  # just-petted, under timeout

    # Loop dies again — watchdog must re-fire.
    clock.advance(120.0)
    assert wd.check() is True
    assert fake.shutdown_count == 2
    assert len(triggers) == 2
    print("PASS  pet re-arms watchdog after a trigger")


def test_on_trigger_failure_does_not_block_shutdown() -> None:
    """A broken alert callback must not stop the actuator shutdown — the
    safety action is more important than the notification."""
    clock = FakeClock()
    fake = FakeSerial()

    def boom(_reason: str) -> None:
        raise RuntimeError("simulated alert failure")

    wd = Watchdog(
        serial_manager=fake,
        timeout_minutes=1.0,
        on_trigger=boom,
        clock=clock,
    )
    clock.advance(120.0)
    assert wd.check() is True
    assert fake.shutdown_count == 1
    print("PASS  broken on_trigger does not block emergency_shutdown")


def test_emergency_shutdown_failure_still_latches() -> None:
    """If the actuator shutdown itself throws, the watchdog must still
    latch so we don't spin in a tight error loop firing every check."""
    clock = FakeClock()
    triggers: list[str] = []

    class FailingSerial:
        calls = 0

        def emergency_shutdown(self):
            FailingSerial.calls += 1
            raise RuntimeError("RS485 unplugged")

    wd = Watchdog(
        serial_manager=FailingSerial(),
        timeout_minutes=1.0,
        on_trigger=triggers.append,
        clock=clock,
    )
    clock.advance(120.0)
    assert wd.check() is True
    assert wd.triggered
    assert wd.check() is False  # latched even though shutdown threw
    assert FailingSerial.calls == 1
    assert len(triggers) == 1
    print("PASS  watchdog latches even when emergency_shutdown raises")


def test_monitor_thread_lifecycle() -> None:
    """Start the monitor thread with a real clock and a tiny interval;
    verify it actually fires the trigger and stops cleanly."""
    fake = FakeSerial()
    triggers: list[str] = []
    # 1-second timeout, 50 ms checks — fires within ~1.05 s.
    wd = Watchdog(
        serial_manager=fake,
        timeout_minutes=1.0 / 60.0,
        check_interval_seconds=0.05,
        on_trigger=triggers.append,
    )
    wd.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not wd.triggered:
            time.sleep(0.05)
        assert wd.triggered, "watchdog did not fire within 3 s"
        assert fake.shutdown_count >= 1
        assert len(triggers) >= 1
    finally:
        wd.stop()
    print(f"PASS  monitor thread fires and stops (triggers={len(triggers)})")


def test_start_is_idempotent() -> None:
    """Calling start() twice must not spawn a second monitor thread."""
    fake = FakeSerial()
    wd = Watchdog(serial_manager=fake, timeout_minutes=30.0)
    wd.start()
    first = wd._thread
    wd.start()
    second = wd._thread
    try:
        assert first is second, "start() spawned a duplicate monitor thread"
        assert first.is_alive()
    finally:
        wd.stop()
    print("PASS  start() is idempotent")


def test_seconds_since_heartbeat() -> None:
    wd, fake, clock, _ = _build(timeout_minutes=1.0)
    clock.advance(7.5)
    assert abs(wd.seconds_since_heartbeat() - 7.5) < 1e-9
    wd.pet()
    assert abs(wd.seconds_since_heartbeat() - 0.0) < 1e-9
    clock.advance(3.0)
    assert abs(wd.seconds_since_heartbeat() - 3.0) < 1e-9
    print("PASS  seconds_since_heartbeat tracks the injected clock")


def test_invalid_constructor_args() -> None:
    fake = FakeSerial()
    for bad in (0, -1, -0.5):
        try:
            Watchdog(serial_manager=fake, timeout_minutes=bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted bad timeout_minutes={bad}")
    for bad in (0, -1, -0.5):
        try:
            Watchdog(serial_manager=fake, check_interval_seconds=bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted bad check_interval_seconds={bad}")
    print("PASS  invalid constructor args rejected")


def test_concurrent_pet_and_check() -> None:
    """Hammer pet() from one thread while check() runs from another —
    no exceptions, state stays consistent."""
    wd, fake, clock, _ = _build(timeout_minutes=1.0)
    errors: list[BaseException] = []
    stop = threading.Event()

    def petter():
        try:
            while not stop.is_set():
                wd.pet()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def checker():
        try:
            while not stop.is_set():
                wd.check()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=petter)
    t2 = threading.Thread(target=checker)
    t1.start()
    t2.start()
    time.sleep(0.5)
    stop.set()
    t1.join()
    t2.join()
    assert not errors, f"concurrent errors: {errors}"
    # With pet() running constantly, the timer should never have fired.
    assert fake.shutdown_count == 0
    print("PASS  concurrent pet/check is safe")


def main() -> int:
    test_no_trigger_before_timeout()
    test_trigger_after_timeout()
    test_trigger_is_idempotent()
    test_pet_resets_timer()
    test_pet_rearms_after_trigger()
    test_on_trigger_failure_does_not_block_shutdown()
    test_emergency_shutdown_failure_still_latches()
    test_monitor_thread_lifecycle()
    test_start_is_idempotent()
    test_seconds_since_heartbeat()
    test_invalid_constructor_args()
    test_concurrent_pet_and_check()
    print("\nAll watchdog tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
