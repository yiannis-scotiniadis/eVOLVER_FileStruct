"""Protocol-level verification for SerialManager. Uses an injected FakeSerial
to assert exact wire bytes without touching real hardware.

Run from the project root:
    python server/test_serial_manager.py
"""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serial_manager import (  # noqa: E402
    COMMAND_TERMINATOR,
    DEFAULT_RAW_FLOOR,
    HEATER_OFF_SETPOINT,
    MAX_SAFE_TEMP_C,
    MIN_INTER_COMMAND_SECONDS,
    N_VIALS,
    OD_LED_MAX,
    STOP_ALL_PUMPS_BODY,
    SerialManager,
)


CAL_DIR = Path(__file__).resolve().parent.parent / "calibration"
TEMP_CAL = str(CAL_DIR / "temp_calibration.txt")
OD_CAL = str(CAL_DIR / "OD_cal.txt")


class FakeSerial:
    """Minimal pyserial.Serial stand-in for protocol testing."""

    def __init__(self) -> None:
        self.write_log: list[bytes] = []
        self.read_queue: list[bytes] = []
        self.flushed = False
        self.closed = False

    def flushInput(self) -> None:
        self.flushed = True

    def write(self, data: bytes) -> int:
        self.write_log.append(bytes(data))
        return len(data)

    def readline(self) -> bytes:
        if self.read_queue:
            return self.read_queue.pop(0)
        return b""  # simulates the 5 s timeout returning empty

    def close(self) -> None:
        self.closed = True


def fresh(calibrated: bool = True) -> tuple[SerialManager, FakeSerial]:
    fake = FakeSerial()
    sm = SerialManager(ser=fake)
    if calibrated:
        sm.load_calibration(TEMP_CAL, OD_CAL)
    return sm, fake


def _temp_response(values: list[int]) -> bytes:
    return ("temp" + ",".join(str(v) for v in values) + ",end\n").encode("ascii")


def _od_response(values: list[int]) -> bytes:
    return ("turb" + ",".join(str(v) for v in values) + ",end\n").encode("ascii")


def test_temperature_command_format() -> None:
    """set_temperature_raw sends the integers verbatim as the `xr` payload.

    Uses a value well above DEFAULT_RAW_FLOOR (so it isn't clipped up) and
    below HEATER_OFF_SETPOINT (so it isn't clipped down)."""
    sm, fake = fresh(calibrated=False)
    fake.read_queue.append(_temp_response([500] * N_VIALS))
    sm.set_temperature_raw([550] * N_VIALS)
    expected = (
        "xr" + ",".join(["550"] * N_VIALS) + "," + COMMAND_TERMINATOR
    ).encode("ascii")
    assert fake.write_log[-1] == expected, f"temp wire bytes wrong: {fake.write_log[-1]!r}"
    print("PASS  temperature command wire format (set_temperature_raw)")


def test_set_temperature_celsius_roundtrip() -> None:
    """set_temperature_celsius converts °C to raw via per-vial calibration
    and dispatches. Verifies the worked example from CLAUDE.md / the plan:
    37 °C on vial 0 -> raw ~= 482."""
    sm, fake = fresh(calibrated=True)
    fake.read_queue.append(_temp_response([482] * N_VIALS))
    sm.set_temperature_celsius([37.0] * N_VIALS)
    # Expected raw for vial 0: (37 - 86.493) / -0.10267 = 482.07 -> rounded 482.
    msg = fake.write_log[-1]
    assert msg.startswith(b"xr"), f"first command not xr: {msg!r}"
    # The first comma-separated value is vial 0's setpoint.
    payload = msg[2:].split(b" !")[0].split(b",")[0]
    raw_v0 = int(payload)
    assert raw_v0 == 482, f"vial 0: 37 C -> {raw_v0} (expected 482)"
    print(f"PASS  set_temperature_celsius converts 37 C -> raw {raw_v0} on vial 0")


def test_od_command_format() -> None:
    sm, fake = fresh(calibrated=False)
    fake.read_queue.append(_od_response([50000] * N_VIALS))
    sm.read_od(led_power=2125)
    expected = (
        "we" + ",".join(["2125"] * N_VIALS) + "," + COMMAND_TERMINATOR
    ).encode("ascii")
    assert fake.write_log[-1] == expected, f"OD wire bytes wrong: {fake.write_log[-1]!r}"
    print("PASS  OD command wire format")


def test_stir_command_format() -> None:
    sm, fake = fresh(calibrated=False)
    sm.set_stir([10] * N_VIALS)
    expected = (
        "zv" + ",".join(["10"] * N_VIALS) + "," + COMMAND_TERMINATOR
    ).encode("ascii")
    assert fake.write_log[-1] == expected, f"stir wire bytes wrong: {fake.write_log[-1]!r}"
    # Stir is write-only — nothing else should have been written.
    assert len(fake.write_log) == 1, fake.write_log
    print("PASS  stir command wire format (write-only)")


def test_pump_address_encoding() -> None:
    sm, fake = fresh(calibrated=False)
    # Vial 0 influx -> bit 0 = 1, body "1,0,5,"
    sm.pump_command(0, "influx", 5)
    assert fake.write_log[-1] == b"st1,0,5, !", f"vial0 influx: {fake.write_log[-1]!r}"
    # Vial 5 influx -> bit 5 = 32 -> binary 100000, body "100000,0,7,"
    sm.pump_command(5, "influx", 7)
    assert fake.write_log[-1] == b"st100000,0,7, !", f"vial5 influx: {fake.write_log[-1]!r}"
    # Vial 0 efflux -> bit 16 = 65536, body "10000000000000000,0,3,"
    sm.pump_command(0, "efflux", 3)
    expected_efflux_v0 = b"st" + format(1 << 16, "b").encode("ascii") + b",0,3, !"
    assert fake.write_log[-1] == expected_efflux_v0, f"vial0 efflux: {fake.write_log[-1]!r}"
    # Vial 15 efflux -> bit 31 -> 31 zeros after the leading 1
    sm.pump_command(15, "efflux", 2)
    expected_efflux_v15 = b"st" + format(1 << 31, "b").encode("ascii") + b",0,2, !"
    assert fake.write_log[-1] == expected_efflux_v15, f"vial15 efflux: {fake.write_log[-1]!r}"
    print("PASS  pump address encoding (influx 2^N, efflux 2^(N+16))")


def test_stop_all_pumps_body() -> None:
    sm, fake = fresh(calibrated=False)
    sm.stop_all_pumps()
    expected = ("st" + STOP_ALL_PUMPS_BODY + COMMAND_TERMINATOR).encode("ascii")
    assert fake.write_log[-1] == expected, f"stop_all wire bytes: {fake.write_log[-1]!r}"
    # Must contain the 32-bit all-ones mask CLAUDE.md prescribes.
    assert b"11111111111111111111111111111111" in fake.write_log[-1]
    print("PASS  stop_all_pumps body (32-bit all-ones mask)")


def test_temperature_response_parse_and_calibration() -> None:
    sm, fake = fresh(calibrated=True)
    # Pick an ADC reading that maps to a known °C via CLAUDE.md formula.
    # Vial 0: slope=-0.10267, intercept=86.493 -> raw=550 should give ~30.025 °C.
    fake.read_queue.append(_temp_response([550] * N_VIALS))
    temps = sm.read_temperature()
    assert len(temps) == N_VIALS
    expected_v0 = 550 * -0.10267 + 86.493
    assert math.isclose(temps[0], expected_v0, abs_tol=1e-6), \
        f"vial 0: got {temps[0]:.4f}, expected {expected_v0:.4f}"
    print(f"PASS  temperature response parse + calibration (550 ADC -> {temps[0]:.3f} °C)")


def test_od_response_parse_and_calibration() -> None:
    sm, fake = fresh(calibrated=True)
    # Vial 0 example from CLAUDE.md: turb response ~57711 -> OD ~ 0.27.
    fake.read_queue.append(_od_response([57711] * N_VIALS))
    ods = sm.read_od()
    assert len(ods) == N_VIALS
    # Apply the CLAUDE.md formula directly to verify
    od_cal = np.genfromtxt(OD_CAL, delimiter=",")
    raw = 57711.0
    expected_v0 = od_cal[2, 0] - math.log10(
        (od_cal[1, 0] - od_cal[0, 0]) / (raw - od_cal[0, 0]) - 1.0
    ) / od_cal[3, 0]
    assert math.isclose(ods[0], expected_v0, abs_tol=1e-6), \
        f"vial 0: got {ods[0]:.4f}, expected {expected_v0:.4f}"
    print(f"PASS  OD response parse + calibration (57711 ADC -> {ods[0]:.3f} OD)")


def test_malformed_response_returns_nan() -> None:
    sm, fake = fresh(calibrated=True)
    # Empty (timeout)
    fake.read_queue.append(b"")
    out = sm.read_temperature()
    assert all(math.isnan(v) for v in out), f"timeout: {out}"
    # Wrong prefix
    fake.read_queue.append(b"junk1,2,3,end\n")
    out = sm.read_temperature()
    assert all(math.isnan(v) for v in out), f"wrong prefix: {out}"
    # Wrong terminator
    fake.read_queue.append(b"temp1,2,3,nope\n")
    out = sm.read_temperature()
    assert all(math.isnan(v) for v in out), f"wrong terminator: {out}"
    # Wrong number of values
    fake.read_queue.append(b"temp1,2,3,end\n")
    out = sm.read_temperature()
    assert all(math.isnan(v) for v in out), f"wrong count: {out}"
    print("PASS  malformed responses return NaN list (timeout, prefix, terminator, count)")


def test_inter_command_delay() -> None:
    sm, fake = fresh(calibrated=False)
    # Two writes back-to-back. The second must trigger a sleep ≈ MIN_INTER_COMMAND_SECONDS.
    sleep_args: list[float] = []
    real_sleep = time.sleep

    def spy_sleep(s: float) -> None:
        sleep_args.append(s)
        if s > 0:
            real_sleep(min(s, 0.001))  # don't actually wait the full 50 ms

    with patch("serial_manager.time.sleep", side_effect=spy_sleep):
        sm.set_stir([5] * N_VIALS)  # first write — should not sleep beforehand
        sm.set_stir([6] * N_VIALS)  # second write — should sleep ~50 ms beforehand
    assert any(s >= MIN_INTER_COMMAND_SECONDS * 0.95 for s in sleep_args), (
        f"no inter-command delay observed; sleep calls = {sleep_args}"
    )
    print(f"PASS  inter-command delay >= {MIN_INTER_COMMAND_SECONDS*1000:.0f} ms")


def test_emergency_shutdown_parks_heater_off() -> None:
    """emergency_shutdown sends pump-stop + stir-zero + heater PARK-OFF.

    The heater command MUST be HEATER_OFF_SETPOINT per vial, NOT zero —
    under the inverted convention zero pins the heater at maximum. This
    is the safety-critical regression test for the bug where the
    watchdog would cook cultures."""
    sm, fake = fresh(calibrated=False)
    # The heater-off step does an xr write and then drains its response.
    fake.read_queue.append(_temp_response([HEATER_OFF_SETPOINT] * N_VIALS))
    sm.emergency_shutdown()
    prefixes = [w[:2] for w in fake.write_log]
    assert b"st" in prefixes, f"no pump-stop in {prefixes}"
    assert b"zv" in prefixes, f"no stir-zero in {prefixes}"
    assert b"xr" in prefixes, f"no heater command in {prefixes}"
    # Stir really is raw PWM, so 0 = off.
    stir_msg = next(w for w in fake.write_log if w.startswith(b"zv"))
    assert stir_msg == (b"zv" + b",".join([b"0"] * N_VIALS) + b"," + b" !"), stir_msg
    # Heater must be HEATER_OFF_SETPOINT (4095), NOT zero.
    temp_msg = next(w for w in fake.write_log if w.startswith(b"xr"))
    off_str = str(HEATER_OFF_SETPOINT).encode("ascii")
    expected_temp = b"xr" + b",".join([off_str] * N_VIALS) + b"," + b" !"
    assert temp_msg == expected_temp, (
        f"emergency_shutdown sent {temp_msg!r}; expected heater parked off "
        f"({HEATER_OFF_SETPOINT}), NOT zero"
    )
    assert b"xr0,0,0" not in temp_msg, (
        "emergency_shutdown sent xr=0 — under inverted convention this "
        "pins the heater at ~82 C, which is the bug this test guards against"
    )
    # Internal state matches.
    assert (sm.stir_speed == 0).all()
    assert (sm.temp_setpoint_raw == HEATER_OFF_SETPOINT).all()
    print("PASS  emergency_shutdown parks heater off (HEATER_OFF_SETPOINT, not 0)")


def test_celsius_cap() -> None:
    """set_temperature_celsius clamps requests above MAX_SAFE_TEMP_C to the
    cap before converting. Requests above the cap convert to a setpoint
    no smaller than the per-vial floor."""
    sm, fake = fresh(calibrated=True)
    fake.read_queue.append(_temp_response([0] * N_VIALS))
    # Request a clearly unsafe 80 °C — should be clamped to MAX_SAFE_TEMP_C.
    sm.set_temperature_celsius([80.0] * N_VIALS)
    # For vial 0 calibration (slope=-0.10267, intercept=86.493) the floor at
    # MAX_SAFE_TEMP_C=45 is (45 - 86.493) / -0.10267 ≈ 404.
    expected_floor_v0 = round((MAX_SAFE_TEMP_C - 86.493) / -0.10267)
    assert sm.temp_setpoint_raw[0] == expected_floor_v0, (
        f"vial 0: expected floor {expected_floor_v0}, got {sm.temp_setpoint_raw[0]}"
    )
    print(
        f"PASS  Celsius cap (request 80 C -> raw {sm.temp_setpoint_raw[0]} "
        f"~ MAX_SAFE_TEMP_C={MAX_SAFE_TEMP_C} C)"
    )


def test_raw_floor() -> None:
    """set_temperature_raw enforces a per-vial floor. Sending a too-low (=
    too hot) setpoint must be clipped up to the floor. Sending a cold
    setpoint (e.g. 4095) passes through."""
    sm, fake = fresh(calibrated=True)
    fake.read_queue.append(_temp_response([0] * N_VIALS))
    # Request raw=0 (the dangerous "off" misconception value). The floor
    # should bump it up to the value that maps to MAX_SAFE_TEMP_C.
    sm.set_temperature_raw([0] * N_VIALS)
    floor_v0 = round((MAX_SAFE_TEMP_C - 86.493) / -0.10267)
    assert sm.temp_setpoint_raw[0] == floor_v0, (
        f"raw=0 should be clipped up to floor {floor_v0}; got {sm.temp_setpoint_raw[0]}"
    )
    # 4095 (off) passes through unchanged.
    fake.read_queue.append(_temp_response([4095] * N_VIALS))
    sm.set_temperature_raw([HEATER_OFF_SETPOINT] * N_VIALS)
    assert (sm.temp_setpoint_raw == HEATER_OFF_SETPOINT).all(), sm.temp_setpoint_raw
    print(f"PASS  raw floor (0 -> {floor_v0}; {HEATER_OFF_SETPOINT} passes through)")


def test_raw_floor_uncalibrated_fallback() -> None:
    """Without calibration loaded, set_temperature_raw uses DEFAULT_RAW_FLOOR."""
    sm, fake = fresh(calibrated=False)
    fake.read_queue.append(_temp_response([0] * N_VIALS))
    sm.set_temperature_raw([0] * N_VIALS)
    assert (sm.temp_setpoint_raw == DEFAULT_RAW_FLOOR).all(), sm.temp_setpoint_raw
    print(
        f"PASS  raw floor uncalibrated fallback "
        f"(0 -> DEFAULT_RAW_FLOOR={DEFAULT_RAW_FLOOR})"
    )


def test_set_temperature_celsius_requires_calibration() -> None:
    """set_temperature_celsius needs calibration to know how °C maps to raw
    setpoints. Without it, the call should raise rather than silently
    using a wrong default."""
    sm, _ = fresh(calibrated=False)
    try:
        sm.set_temperature_celsius([37.0] * N_VIALS)
    except RuntimeError as exc:
        assert "calibration" in str(exc).lower()
        print("PASS  set_temperature_celsius requires calibration (RuntimeError raised)")
        return
    raise AssertionError("set_temperature_celsius should have raised without calibration")


def test_stir_and_od_caps() -> None:
    """Stir genuinely is raw PWM 0-15 with 0=off (NOT inverted), and OD
    LED power is capped at 2200 per legacy."""
    sm, fake = fresh(calibrated=False)
    sm.set_stir([99] * N_VIALS)
    assert (sm.stir_speed == 15).all(), sm.stir_speed
    fake.read_queue.append(_od_response([50000] * N_VIALS))
    sm.read_od(led_power=9999)
    od_msg = next(w for w in fake.write_log if w.startswith(b"we"))
    assert b"2200" in od_msg, f"OD LED not capped to {OD_LED_MAX}: {od_msg!r}"
    print(f"PASS  stir capped at 15, OD LED capped at {OD_LED_MAX}")


def test_pump_subsecond_rounds_or_warns() -> None:
    """Pump durations are integer-seconds on the wire (legacy firmware
    uses %d). round() preserves 0.6+ -> 1 (legacy %d would have lost
    these as 0). Requests that round to 0 must skip the write entirely
    rather than silently sending a no-op pump command.

    Note: Python's round() is banker's (round half to even), so 0.5 -> 0,
    not 1. This is fine — the legacy int() truncation would also have
    dropped 0.5, and a half-second pump is well below the granularity
    we care about."""
    sm, fake = fresh(calibrated=False)
    # 0.6 s: rounds to 1 s, should send "1,". Without round() (legacy int()
    # truncation) this would have silently rounded to 0 and lost the pump.
    sm.pump_command(0, "influx", 0.6)
    assert fake.write_log[-1].endswith(b",0,1, !"), (
        f"0.6s expected to round to 1: {fake.write_log[-1]!r}"
    )
    # 0.4 s: rounds to 0 s, should NOT send anything (skipped + warned).
    n_after_first = len(fake.write_log)
    sm.pump_command(0, "influx", 0.4)
    assert len(fake.write_log) == n_after_first, (
        f"0.4s should be skipped, but a command was sent: {fake.write_log[-1]!r}"
    )
    # 7.3 s: rounds to 7 s.
    sm.pump_command(0, "influx", 7.3)
    assert fake.write_log[-1].endswith(b",0,7, !"), (
        f"7.3s expected to round to 7: {fake.write_log[-1]!r}"
    )
    print(f"PASS  pump duration rounding (0.4s skipped, 0.6s -> 1s, 7.3s -> 7s)")


def test_uncalibrated_returns_raw() -> None:
    sm, fake = fresh(calibrated=False)
    fake.read_queue.append(_temp_response([429] * N_VIALS))
    temps = sm.read_temperature()
    assert temps[0] == 429.0, f"uncalibrated temp should be raw ADC, got {temps[0]}"
    fake.read_queue.append(_od_response([57711] * N_VIALS))
    ods = sm.read_od()
    assert ods[0] == 57711.0, f"uncalibrated OD should be raw ADC, got {ods[0]}"
    print("PASS  uncalibrated mode returns raw ADC values (per SPEC §5)")


def main() -> int:
    test_temperature_command_format()
    test_set_temperature_celsius_roundtrip()
    test_od_command_format()
    test_stir_command_format()
    test_pump_address_encoding()
    test_stop_all_pumps_body()
    test_temperature_response_parse_and_calibration()
    test_od_response_parse_and_calibration()
    test_malformed_response_returns_nan()
    test_inter_command_delay()
    test_emergency_shutdown_parks_heater_off()
    test_celsius_cap()
    test_raw_floor()
    test_raw_floor_uncalibrated_fallback()
    test_set_temperature_celsius_requires_calibration()
    test_stir_and_od_caps()
    test_pump_subsecond_rounds_or_warns()
    test_uncalibrated_returns_raw()
    print("\nAll tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
