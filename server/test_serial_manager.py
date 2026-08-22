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
    ODReading,
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

    def read_until(self, expected: bytes = b"\n", size=None) -> bytes:
        # SerialManager now reads to the 'end' terminator instead of a newline;
        # tests queue whole framed responses, so return one per call.
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


def fresh_dark_marked(tmp: Path) -> tuple[SerialManager, FakeSerial]:
    """Like fresh(calibrated=True) but the OD calibration carries an
    OD_cal.meta.json sidecar with dark_subtracted=true, so dark subtraction
    is permitted (SPEC §19.2 coherence guard)."""
    import json as _json
    import shutil as _shutil
    od_copy = tmp / "OD_cal.txt"
    _shutil.copy(OD_CAL, od_copy)
    (tmp / "OD_cal.meta.json").write_text(
        _json.dumps({"dark_subtracted": True}), encoding="utf-8"
    )
    fake = FakeSerial()
    sm = SerialManager(ser=fake)
    sm.load_calibration(TEMP_CAL, str(od_copy))
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


# ---------------------------------------------------------------------------
# Enhanced OD acquisition: median-of-N + dark subtraction + range guard
# ---------------------------------------------------------------------------

def _od_logistic(raw: float, vial: int) -> float:
    od_cal = np.genfromtxt(OD_CAL, delimiter=",")
    return od_cal[2, vial] - math.log10(
        (od_cal[1, vial] - od_cal[0, vial]) / (raw - od_cal[0, vial]) - 1.0
    ) / od_cal[3, vial]


def test_od_enhanced_median_rejects_outlier() -> None:
    """5 light reads, per-vial median. A single wild sample must not move the
    result (mean would; median doesn't)."""
    sm, fake = fresh(calibrated=True)
    # Four good samples at 57711 plus one large outlier. Median = 57711;
    # mean would be ~126k and yield a very different OD.
    for val in (57711, 57711, 57711, 57711, 400000):
        fake.read_queue.append(_od_response([val] * N_VIALS))
    r = sm.read_od_enhanced(2125, n_samples=5, dark_subtract=False)
    assert isinstance(r, ODReading)
    assert r.flags[0] == "ok" and r.n_valid[0] == 5, (r.flags[0], r.n_valid[0])
    assert math.isclose(r.raw[0], 57711.0, abs_tol=1e-6), r.raw[0]
    assert math.isclose(r.calibrated[0], _od_logistic(57711.0, 0), abs_tol=1e-6), \
        r.calibrated[0]
    print("PASS  enhanced OD median-of-5 rejects an outlier sample")


def test_od_enhanced_dropped_samples() -> None:
    """Garbled/timed-out samples drop out of n_valid; all-dropped -> flag
    'dropped' + NaN."""
    sm, fake = fresh(calibrated=True)
    # 3 valid, 2 timeouts (empty) interleaved -> n_valid == 3, still ok.
    fake.read_queue.extend([
        _od_response([57711] * N_VIALS),
        b"",
        _od_response([57711] * N_VIALS),
        b"",
        _od_response([57711] * N_VIALS),
    ])
    r = sm.read_od_enhanced(2125, n_samples=5, dark_subtract=False)
    assert r.n_valid[0] == 3, r.n_valid[0]
    assert r.flags[0] == "ok", r.flags[0]
    assert math.isclose(r.raw[0], 57711.0, abs_tol=1e-6), r.raw[0]

    # All five reads time out -> dropped, NaN calibrated + raw.
    r2 = sm.read_od_enhanced(2125, n_samples=5, dark_subtract=False)
    assert r2.n_valid[0] == 0, r2.n_valid[0]
    assert r2.flags[0] == "dropped", r2.flags[0]
    assert math.isnan(r2.calibrated[0]) and math.isnan(r2.raw[0])
    print("PASS  enhanced OD: partial drop lowers n_valid; all-drop -> 'dropped'+NaN")


def test_od_enhanced_dark_subtraction_requires_sidecar() -> None:
    """Provenance guard is a HARD ERROR (SPEC §19.2): the bundled OD_cal.txt
    has no dark-subtracted marker (no OD_cal.meta.json), so requesting dark
    subtraction against it must raise, not warn."""
    sm, fake = fresh(calibrated=True)
    try:
        sm.read_od_enhanced(2125, n_samples=5, dark_subtract=True, n_dark=3)
    except ValueError as exc:
        assert "dark_subtracted" in str(exc), exc
    else:
        raise AssertionError("dark_subtract against a non-sidecar cal did not raise")
    # No calibration loaded -> raw passthrough, dark subtraction is harmless
    # arithmetic on raw counts and stays allowed.
    sm2, fake2 = fresh(calibrated=False)
    for _ in range(2):
        fake2.read_queue.append(_od_response([2000] * N_VIALS))
    for _ in range(3):
        fake2.read_queue.append(_od_response([60000] * N_VIALS))
    r = sm2.read_od_enhanced(2125, n_samples=3, dark_subtract=True, n_dark=2)
    assert math.isclose(r.raw[0], 58000.0, abs_tol=1e-6), r.raw[0]
    print("PASS  dark subtraction without sidecar raises; uncalibrated stays allowed")


def test_od_enhanced_dark_subtraction() -> None:
    """corrected = median(light) - median(dark) is what feeds the calibration
    (with a sidecar-marked calibration, per the coherence guard)."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        sm, fake = fresh_dark_marked(Path(tmp))
        assert sm.od_cal_dark_subtracted is True
        # Dark phase first (n_dark reads), then light phase (n_samples reads).
        for _ in range(3):
            fake.read_queue.append(_od_response([2000] * N_VIALS))
        for _ in range(5):
            fake.read_queue.append(_od_response([60000] * N_VIALS))
        r = sm.read_od_enhanced(2125, n_samples=5, dark_subtract=True, n_dark=3)
        assert math.isclose(r.dark[0], 2000.0, abs_tol=1e-6), r.dark[0]
        assert math.isclose(r.raw[0], 58000.0, abs_tol=1e-6), r.raw[0]
        assert math.isclose(r.calibrated[0], _od_logistic(58000.0, 0), abs_tol=1e-6), \
            r.calibrated[0]
        assert r.flags[0] == "ok"
    print("PASS  enhanced OD dark subtraction (60000 - 2000 -> OD via calibration)")


def test_collect_od_raw_stats() -> None:
    """collect_od_raw returns NaN-aware per-vial median/sd/n_valid of raw
    counts — the calibration wizard's dark/blank read primitive."""
    sm, fake = fresh(calibrated=True)
    for val in (57000, 58000, 59000):
        fake.read_queue.append(_od_response([val] * N_VIALS))
    fake.read_queue.append(b"")  # one timeout -> drops out of the stats
    stats = sm.collect_od_raw(2125, n_samples=4)
    assert stats["n_valid"][0] == 3, stats["n_valid"][0]
    assert math.isclose(stats["median"][0], 58000.0, abs_tol=1e-6), stats["median"][0]
    expected_sd = float(np.std([57000.0, 58000.0, 59000.0]))
    assert math.isclose(stats["sd"][0], expected_sd, abs_tol=1e-6), stats["sd"][0]
    # LED power is clamped and sent as the `we` payload (0 = dark read).
    fake.read_queue.append(_od_response([1200] * N_VIALS))
    sm.collect_od_raw(0, n_samples=1)
    assert fake.write_log[-1].startswith(b"we0,"), fake.write_log[-1]
    print("PASS  collect_od_raw per-vial median/sd/n_valid + LED-0 dark read")


def test_apply_od_blank_row2_only() -> None:
    """apply_od_blank changes ONLY row 2 for the named vials; clear_od_blank
    restores the pristine curve (rows 0/1/3 bitwise unchanged throughout)."""
    sm, _fake = fresh(calibrated=True)
    before = sm.od_cal.copy()
    sm.apply_od_blank({0: -1.234, 5: 0.5})
    after = sm.od_cal
    assert after[2, 0] == -1.234 and after[2, 5] == 0.5
    for row in (0, 1, 3):
        assert np.array_equal(after[row], before[row]), f"row {row} changed"
    untouched = [v for v in range(N_VIALS) if v not in (0, 5)]
    assert np.array_equal(after[2, untouched], before[2, untouched])
    sm.clear_od_blank()
    assert np.array_equal(sm.od_cal, before), "clear_od_blank did not restore"
    print("PASS  apply_od_blank re-anchors row 2 only; clear restores pristine")


def test_od_enhanced_out_of_range() -> None:
    """A corrected signal outside the calibration domain (mn, mx) flags
    out_of_range, returns NaN OD, but keeps the raw value for diagnostics."""
    sm, fake = fresh(calibrated=True)
    # 5000 is below every vial's dark-floor asymptote (min mn = 13109).
    for _ in range(5):
        fake.read_queue.append(_od_response([5000] * N_VIALS))
    r = sm.read_od_enhanced(2125, n_samples=5, dark_subtract=False)
    assert r.flags[0] == "out_of_range", r.flags[0]
    assert math.isnan(r.calibrated[0]), r.calibrated[0]
    assert math.isclose(r.raw[0], 5000.0, abs_tol=1e-6), r.raw[0]
    assert r.n_valid[0] == 5
    print("PASS  enhanced OD range guard (below dark floor -> out_of_range + NaN)")


def test_od_enhanced_inter_command_delay() -> None:
    """The 50 ms inter-command floor is honored across every extra read in a
    cycle (dark + light), not just the first."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        sm, fake = fresh_dark_marked(Path(tmp))
    for _ in range(2):  # n_dark
        fake.read_queue.append(_od_response([2000] * N_VIALS))
    for _ in range(3):  # n_samples
        fake.read_queue.append(_od_response([60000] * N_VIALS))
    sleep_args: list[float] = []
    real_sleep = time.sleep

    def spy_sleep(s: float) -> None:
        sleep_args.append(s)
        if s > 0:
            real_sleep(min(s, 0.001))

    with patch("serial_manager.time.sleep", side_effect=spy_sleep):
        sm.read_od_enhanced(2125, n_samples=3, dark_subtract=True, n_dark=2)
    # 5 writes total -> the inter-command wait must engage before each of the 4
    # subsequent writes. Each sleep is only the REMAINING time (work between
    # reads consumes part of the 50 ms window), so assert on count, not duration.
    positive = [s for s in sleep_args if s > 0]
    assert len(positive) >= 4, f"expected >=4 inter-command waits, got {sleep_args}"
    assert all(s <= MIN_INTER_COMMAND_SECONDS + 1e-9 for s in positive), sleep_args
    print(f"PASS  enhanced OD honors {MIN_INTER_COMMAND_SECONDS*1000:.0f} ms delay across reads")


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
    test_od_enhanced_median_rejects_outlier()
    test_od_enhanced_dropped_samples()
    test_od_enhanced_dark_subtraction_requires_sidecar()
    test_od_enhanced_dark_subtraction()
    test_collect_od_raw_stats()
    test_apply_od_blank_row2_only()
    test_od_enhanced_out_of_range()
    test_od_enhanced_inter_command_delay()
    print("\nAll tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
