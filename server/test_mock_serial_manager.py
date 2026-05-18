"""Verification script for MockSerialManager.

Run from the project root:
    python server/test_mock_serial_manager.py
"""

from __future__ import annotations

import math
import sys
import threading
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_serial_manager import (  # noqa: E402
    CARRYING_CAPACITY_OD,
    DEFAULT_FLOW_RATES_ML_PER_SEC,
    DEFAULT_VOLUME_ML,
    HEATER_OFF_SETPOINT,
    MockSerialManager,
    N_VIALS,
    T_AMBIENT_C,
    T_GROWTH_OPT_C,
    raw_setpoint_to_target_C,
)


CAL_DIR = Path(__file__).resolve().parent.parent / "calibration"
TEMP_CAL = str(CAL_DIR / "temp_calibration.txt")
OD_CAL = str(CAL_DIR / "OD_cal.txt")


def fresh(seed: int = 42, time_multiplier: float = 1.0, calibrated: bool = True) -> MockSerialManager:
    m = MockSerialManager(time_multiplier=time_multiplier, seed=seed)
    if calibrated:
        m.load_calibration(TEMP_CAL, OD_CAL)
    return m


def test_calibration_roundtrip() -> None:
    m = fresh()
    for target_C in (25.0, 30.0, 37.0, 42.0):
        temps = np.full(N_VIALS, target_C)
        back = m._temp_raw_to_C(m._temp_C_to_raw(temps))
        assert np.allclose(back, temps, atol=1e-6), f"temp roundtrip {target_C} -> {back}"
    for target_OD in (0.05, 0.2, 0.5, 1.0, 1.5):
        ods = np.full(N_VIALS, target_OD)
        back = m._od_raw_to_OD(m._od_abs_to_raw(ods))
        assert np.allclose(back, ods, atol=1e-6), f"OD roundtrip {target_OD} -> {back}"
    print("PASS  calibration round-trip (temp & OD, all 16 vials)")


def test_temperature_settles() -> None:
    """Mock thermal lag drives toward the calibration-derived target.

    With raw setpoint = 550 and the lab calibration (negative slope), the
    steady-state target temperature is ~30 °C on vial 0 — the mock should
    converge there. This indirectly verifies the inverted convention is
    preserved end-to-end."""
    m = fresh(time_multiplier=10.0)
    m.set_temperature_raw([550] * N_VIALS)
    last = None
    for _ in range(30):
        last = m.read_temperature()
    target = float(raw_setpoint_to_target_C(550, m.temp_cal)[0])
    assert last is not None
    assert abs(last[0] - target) < 0.5, f"vial 0 final {last[0]:.3f}, target {target:.3f}"
    print(f"PASS  temperature settles (vial 0 -> {last[0]:.2f} C, target {target:.2f})")


def test_setpoint_zero_is_max_heat() -> None:
    """Regression test for the inverted convention: raw setpoint = 0 must
    map to MAX heat (~ intercept ≈ 82 °C), NOT to ambient. The previous
    mock special-cased PWM=0 -> ambient, which would have hidden the
    real-hardware behaviour where xr=0 pins the heater on."""
    m = fresh(time_multiplier=20.0)
    # Use set_temperature_raw to bypass the celsius cap and floor — the
    # cap should also bump 0 up to the safety floor, so we verify both:
    # 1) raw_setpoint_to_target_C(0, cal) ≈ 82 °C (the dangerous answer)
    # 2) Calling set_temperature_raw([0]*16) clips up to the per-vial floor
    target_at_zero = float(raw_setpoint_to_target_C(0, m.temp_cal)[0])
    assert target_at_zero > 70.0, (
        f"raw_setpoint_to_target_C(0) should reflect inverted convention "
        f"(close to intercept ~82 C); got {target_at_zero:.2f}. "
        "If this fails, the 'pwm=0 -> ambient' mock special-case is back."
    )
    # And confirm the floor in set_temperature_raw actually protects us.
    m.set_temperature_raw([0] * N_VIALS)
    # Floor for vial 0 at MAX_SAFE_TEMP_C=45 with cal (slope=-0.10267,
    # intercept=86.493) is round((45 - 86.493) / -0.10267) = 404.
    assert m.temp_setpoint_raw[0] > 0, (
        f"raw=0 should be clipped up to the safety floor; got {m.temp_setpoint_raw[0]}"
    )
    print(
        f"PASS  raw setpoint = 0 maps to ~{target_at_zero:.0f} C (max heat); "
        f"floor clips to {m.temp_setpoint_raw[0]}"
    )


def test_heater_off_setpoint_decays_to_ambient() -> None:
    """Conversely, HEATER_OFF_SETPOINT must NOT heat anything — the
    target is well below ambient so the (active-heat-only) mock just
    relaxes to room temperature."""
    m = fresh(time_multiplier=20.0)
    m.set_temperature_raw([HEATER_OFF_SETPOINT] * N_VIALS)
    last = None
    for _ in range(50):
        last = m.read_temperature()
    assert last is not None
    assert abs(last[0] - T_AMBIENT_C) < 1.5, (
        f"HEATER_OFF_SETPOINT should decay to ambient ({T_AMBIENT_C}), "
        f"got {last[0]:.2f}"
    )
    print(
        f"PASS  HEATER_OFF_SETPOINT decays to ambient "
        f"(vial 0 -> {last[0]:.2f} C, target ambient {T_AMBIENT_C})"
    )


def test_od_grows_logistically() -> None:
    m = fresh(time_multiplier=60.0)
    m.set_stir([10] * N_VIALS)
    m.set_temperature_celsius([T_GROWTH_OPT_C] * N_VIALS)
    initial = float(m.od_abs[0])
    for _ in range(160):
        m.read_od()
    final = float(m.od_abs[0])
    assert final > initial + 0.5, f"OD did not grow: {initial} -> {final}"
    assert final < CARRYING_CAPACITY_OD * 1.1, f"OD blew past K: {final}"
    print(f"PASS  OD logistic growth (vial 0: {initial:.3f} -> {final:.3f}, K={CARRYING_CAPACITY_OD})")


def test_pump_dilutes_od() -> None:
    m = fresh(time_multiplier=60.0)
    m.set_stir([10] * N_VIALS)
    m.set_temperature_celsius([T_GROWTH_OPT_C] * N_VIALS)
    for _ in range(180):
        m.read_od()
    pre = float(m.od_abs[0])
    assert pre > 1.0, f"expected near-saturated OD before dilution, got {pre}"

    F = float(DEFAULT_FLOW_RATES_ML_PER_SEC[0])
    seconds = 10.0
    expected_factor = math.exp(-F * seconds / DEFAULT_VOLUME_ML)
    expected_post = pre * expected_factor

    m.pump_command(0, "influx", seconds)
    m.pump_command(0, "efflux", seconds + 5)
    m.read_od()
    post = float(m.od_abs[0])

    rel_err = abs(post - expected_post) / pre
    assert rel_err < 0.15, (
        f"post-dilution OD {post:.3f}, expected ~{expected_post:.3f} "
        f"(factor={expected_factor:.3f}, rel_err={rel_err:.2%})"
    )
    print(f"PASS  pump dilutes OD ({pre:.3f} -> {post:.3f}, expected {expected_post:.3f})")


def test_turbidostat_loop() -> None:
    """Hand-run the custom_script.py turbidostat algorithm and verify oscillation."""
    m = fresh(time_multiplier=60.0)
    m.set_stir([10] * N_VIALS)
    m.set_temperature_celsius([T_GROWTH_OPT_C] * N_VIALS)
    lower_thresh = 0.2
    upper_thresh = 0.4
    pump_wait_seconds = 15 * 60
    target = upper_thresh
    last_pump_sim_time = -1e9
    history: list[float] = []
    pumps_fired = 0

    for _ in range(150):
        od = m.read_od()[0]
        history.append(od)
        if od > upper_thresh and target != lower_thresh:
            target = lower_thresh
        if od < (lower_thresh + upper_thresh) / 2 and target != upper_thresh:
            target = upper_thresh
        if od > target:
            t_in = -math.log(lower_thresh / max(od, 1e-3)) * DEFAULT_VOLUME_ML / float(
                DEFAULT_FLOW_RATES_ML_PER_SEC[0]
            )
            t_in = min(t_in, 20.0)
            if m.sim_time - last_pump_sim_time >= pump_wait_seconds:
                m.pump_command(0, "influx", t_in)
                m.pump_command(0, "efflux", t_in + 5)
                last_pump_sim_time = m.sim_time
                pumps_fired += 1

    settled = history[20:]
    assert pumps_fired > 0, "no pumps fired"
    assert max(settled) > upper_thresh * 0.9, f"OD never reached upper thresh: max={max(settled):.3f}"
    assert min(settled) < (lower_thresh + upper_thresh) / 2 + 0.1, (
        f"OD never dipped low: min={min(settled):.3f}"
    )
    influx_events = [e for e in m.pump_log if e["direction"] == "influx"]
    assert len(influx_events) == pumps_fired
    print(
        f"PASS  turbidostat loop ({pumps_fired} pumps, OD range "
        f"{min(settled):.3f}..{max(settled):.3f})"
    )


def test_uncalibrated_mode() -> None:
    """The mock returns physical units (°C / OD600) regardless of whether
    load_calibration has been called — the simulation works in direct
    units. Calibration loading is only kept for SerialManager parity."""
    m = fresh(calibrated=False)
    temps = m.read_temperature()
    ods = m.read_od()
    assert len(temps) == N_VIALS and len(ods) == N_VIALS
    assert all(isinstance(t, float) for t in temps)
    assert all(isinstance(o, float) for o in ods)
    # Initial state: 22 °C, OD 0.05. Heater starts parked at
    # HEATER_OFF_SETPOINT (target below ambient, clamped to ambient) and
    # stir=0, so one tick later readings stay near initial conditions.
    assert 15.0 < temps[0] < 30.0, f"temp not near ambient: {temps[0]}"
    assert 0.0 < ods[0] < 0.2, f"OD not near initial: {ods[0]}"
    print(f"PASS  uncalibrated mode (temp={temps[0]:.2f} °C, OD={ods[0]:.4f})")


def test_thread_safety() -> None:
    m = fresh()
    errors: list[BaseException] = []
    n_pumps = 50

    def hammer_reads() -> None:
        try:
            for _ in range(100):
                m.read_temperature()
                m.read_od()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def hammer_pumps() -> None:
        try:
            for i in range(n_pumps):
                m.pump_command(i % N_VIALS, "influx", 1.0)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [
        threading.Thread(target=hammer_reads),
        threading.Thread(target=hammer_reads),
        threading.Thread(target=hammer_pumps),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"thread errors: {errors}"
    influx_in_log = sum(1 for e in m.pump_log if e["direction"] == "influx")
    assert influx_in_log == n_pumps, f"lost pump events: got {influx_in_log}, expected {n_pumps}"
    print(f"PASS  thread safety ({n_pumps} pump events logged, no errors)")


def main() -> int:
    test_calibration_roundtrip()
    test_temperature_settles()
    test_setpoint_zero_is_max_heat()
    test_heater_off_setpoint_decays_to_ambient()
    test_od_grows_logistically()
    test_pump_dilutes_od()
    test_turbidostat_loop()
    test_uncalibrated_mode()
    test_thread_safety()
    print("\nAll tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
