"""Mock SerialManager for development and testing without eVOLVER hardware.

Drop-in replacement for the real SerialManager (SPEC.md §5 and §12). The
mock works in real physical units (°C and OD600) end-to-end rather than
going through RS485 raw-ADC counts — ADC calibration is loaded only for
parity with the real SerialManager's signature; the simulation does not
consume it.

Heater convention reminder (CLAUDE.md §Testing, SPEC.md §10): the `xr`
setpoint is INVERTED. Lower raw setpoint = hotter target. The mock
applies the temp calibration uniformly so this convention is preserved —
``set_temperature_raw([0]*16)`` simulates a heater pinned at ~82 °C, and
``set_temperature_raw([HEATER_OFF_SETPOINT]*16)`` parks all vials off.

Dynamics
--------
- Temperature: first-order lag (τ = THERMAL_TAU_SECONDS) toward the
  per-vial setpoint converted via the (negative-slope) temp calibration.
  Targets below T_AMBIENT_C are floored to ambient — there is no active
  cooling, so the vial just relaxes toward room temp.
- Growth: logistic toward CARRYING_CAPACITY_OD, gated by stir > 0
  (mixing/oxygenation) and modulated by a cardinal-temperature factor
  for E. coli (zero outside [T_GROWTH_MIN_C, T_GROWTH_MAX_C], peak at
  T_GROWTH_OPT_C = 37 °C). Per-vial μ_max is drawn from MU_MAX_RANGE —
  the standard range for E. coli in rich media at 37 °C (doubling time
  ~25 min, μ ≈ 1.4–1.8 /hr).
- Pumps: influx events dilute OD by exp(-F·t/V) once the pump finishes;
  efflux is tracked but does not change OD.
- Stir: stored as plain integer state; the only dynamical effect is
  gating growth.
"""

from __future__ import annotations

import logging
import math
import threading
from typing import Optional

import numpy as np

from serial_manager import (
    DEFAULT_RAW_FLOOR,
    HEATER_OFF_SETPOINT,
    MAX_SAFE_TEMP_C,
)


N_VIALS = 16

# ---- Initial conditions ----
INITIAL_TEMP_C = 22.0   # ambient room temperature
INITIAL_OD = 0.05

# ---- Heater model (closed-loop setpoint + passive cooling) ----
# The Arduino's firmware drives heater PWM until the thermistor ADC
# matches the setpoint. The mock simulates that closed loop directly in
# Celsius: setpoint -> target_C via the (negative-slope) calibration, and
# the vial relaxes toward target_C with first-order dynamics. There is no
# active cooling, so target_C is floored at T_AMBIENT_C.
T_AMBIENT_C = 22.0
THERMAL_TAU_SECONDS = 60.0
# Fallback calibration used only when no temp_cal is loaded. Picked to be
# representative of the lab's actual temp_calibration.txt — negative slope
# is the load-bearing property; precise numbers don't matter for the mock.
_FALLBACK_SLOPE = -0.11
_FALLBACK_INTERCEPT = 82.0

# ---- Growth model — E. coli cardinal-temperature triangle ----
T_GROWTH_MIN_C = 10.0
T_GROWTH_OPT_C = 37.0
T_GROWTH_MAX_C = 45.0
# μ_max for E. coli in rich media at 37 °C: doubling time 23–30 min.
MU_MAX_RANGE = (1.4, 1.8)
CARRYING_CAPACITY_OD = 1.5

# ---- Noise ----
TEMP_NOISE_SIGMA = 0.1   # °C per 10 s of sim time
OD_NOISE_SIGMA = 0.005

# ---- Stir ----
STIR_MAX = 15

# ---- Pump defaults (mac_original/custom_script.py:41) ----
DEFAULT_VOLUME_ML = 25.0
DEFAULT_FLOW_RATES_ML_PER_SEC = np.array(
    [0.95, 1.1, 0.975, 0.85, 0.95, 1.05, 1.05, 1.05,
     1.025, 1.125, 1.0, 1.0, 1.05, 1.15, 1.1, 1.025]
)


def raw_setpoint_to_target_C(raw, temp_cal=None) -> np.ndarray:
    """Steady-state vial temperature for a given raw `xr` setpoint.

    The Arduino firmware closes the loop on the thermistor ADC, so the
    steady-state temperature is the calibration applied to the setpoint
    directly: ``temp_C = setpoint * slope + intercept``. Calibration
    slopes are NEGATIVE, so lower setpoint -> hotter target. This is the
    same calibration the experiment writer inverts to derive a raw
    setpoint from a target °C (CLAUDE.md §Calibration).

    The mock has no active cooling, so any target below T_AMBIENT_C is
    floored to ambient — this represents "heater off, vial relaxes to
    room temperature" and is the correct mock behaviour for
    HEATER_OFF_SETPOINT (~4095 -> target ~ -370 °C -> clamped to ambient).

    Without ``temp_cal``, uses a representative fallback calibration
    (slope=-0.11, intercept=82) that preserves the inverted-convention
    behaviour. Used only when the mock is run uncalibrated.
    """
    raw = np.asarray(raw, dtype=float)
    if temp_cal is not None:
        slope = temp_cal[0]
        intercept = temp_cal[1]
    else:
        slope = _FALLBACK_SLOPE
        intercept = _FALLBACK_INTERCEPT
    target = raw * slope + intercept
    # No active cooling — clamp to ambient so HEATER_OFF_SETPOINT decays
    # passively rather than asking the vial to chill.
    return np.maximum(target, T_AMBIENT_C)


# Back-compat alias for external utilities; new code should use raw_setpoint_to_target_C.
pwm_to_target_C = raw_setpoint_to_target_C


def growth_rate_factor(temp_C) -> np.ndarray:
    """E. coli cardinal-temperature factor in [0, 1]: rises linearly from
    zero at T_GROWTH_MIN_C to one at T_GROWTH_OPT_C, then falls linearly
    to zero at T_GROWTH_MAX_C."""
    temp_C = np.asarray(temp_C, dtype=float)
    rising = (temp_C - T_GROWTH_MIN_C) / (T_GROWTH_OPT_C - T_GROWTH_MIN_C)
    falling = (T_GROWTH_MAX_C - temp_C) / (T_GROWTH_MAX_C - T_GROWTH_OPT_C)
    return np.clip(np.minimum(rising, falling), 0.0, 1.0)


class MockSerialManager:
    def __init__(
        self,
        # TIME MULTIPLIERS HERE#
        time_multiplier: float = 100.0,
        tick_seconds: float = 1.0,
        seed: Optional[int] = None,
    ) -> None:
        self.time_multiplier = float(time_multiplier)
        self.tick_seconds = float(tick_seconds)
        self._rng = np.random.default_rng(seed)
        self._lock = threading.Lock()

        self.temp_C = np.full(N_VIALS, INITIAL_TEMP_C)
        # Init heaters to OFF (HEATER_OFF_SETPOINT, not 0 — see module
        # docstring for the inverted convention).
        self.temp_setpoint_raw = np.full(N_VIALS, HEATER_OFF_SETPOINT, dtype=int)
        self.od_abs = np.full(N_VIALS, INITIAL_OD)
        self.mu_max = self._rng.uniform(MU_MAX_RANGE[0], MU_MAX_RANGE[1], N_VIALS)
        self.stir_speed = np.zeros(N_VIALS, dtype=int)

        self.pump_log: list[dict] = []
        self._pump_cursor = 0
        self.flow_rate = DEFAULT_FLOW_RATES_ML_PER_SEC.copy()
        self.volume_ml = DEFAULT_VOLUME_ML

        self.temp_cal: Optional[np.ndarray] = None
        self.od_cal: Optional[np.ndarray] = None

        self.sim_time = 0.0

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------
    # Stored for parity with the real SerialManager and for downstream
    # code that introspects it (e.g. app.py's raw-ADC inversion in API
    # responses). The mock simulation itself works in °C and OD600
    # directly and does not consume calibration.

    def load_calibration(self, temp_cal_path: str, od_cal_path: str) -> None:
        temp_cal = np.genfromtxt(temp_cal_path, delimiter=",")
        od_cal = np.genfromtxt(od_cal_path, delimiter=",")
        if temp_cal.shape != (2, N_VIALS):
            raise ValueError(
                f"temp calibration shape {temp_cal.shape}, expected (2, {N_VIALS})"
            )
        if od_cal.shape != (4, N_VIALS):
            raise ValueError(
                f"OD calibration shape {od_cal.shape}, expected (4, {N_VIALS})"
            )
        with self._lock:
            self.temp_cal = temp_cal
            self.od_cal = od_cal

    # Calibration helpers retained for the round-trip test and any
    # external utility that wants to convert between physical units and
    # raw ADC counts. _advance / read_* do not call these.

    def _temp_C_to_raw(self, temp_C: np.ndarray) -> np.ndarray:
        slope = self.temp_cal[0]
        intercept = self.temp_cal[1]
        return (temp_C - intercept) / slope

    def _temp_raw_to_C(self, raw: np.ndarray) -> np.ndarray:
        slope = self.temp_cal[0]
        intercept = self.temp_cal[1]
        return raw * slope + intercept

    def _od_abs_to_raw(self, od: np.ndarray) -> np.ndarray:
        mn, mx, c, d = self.od_cal[0], self.od_cal[1], self.od_cal[2], self.od_cal[3]
        return mn + (mx - mn) / (1.0 + np.power(10.0, d * (c - od)))

    def _od_raw_to_OD(self, raw: np.ndarray) -> np.ndarray:
        mn, mx, c, d = self.od_cal[0], self.od_cal[1], self.od_cal[2], self.od_cal[3]
        with np.errstate(invalid="ignore", divide="ignore"):
            return c - np.log10((mx - mn) / (raw - mn) - 1.0) / d

    # ------------------------------------------------------------------
    # Simulation tick (must hold self._lock)
    # ------------------------------------------------------------------

    def _advance(self) -> None:
        sim_dt = self.tick_seconds * self.time_multiplier
        self.sim_time += sim_dt

        # 1. Apply pump dilutions for influx events that have now completed.
        while self._pump_cursor < len(self.pump_log):
            event = self.pump_log[self._pump_cursor]
            end_time = event["sim_time"] + event["seconds"]
            if end_time > self.sim_time:
                break
            if event["direction"] == "influx":
                vial = event["vial"]
                F = self.flow_rate[vial]
                V = self.volume_ml
                self.od_abs[vial] *= math.exp(-F * event["seconds"] / V)
            self._pump_cursor += 1

        # 2. Temperature: first-order lag toward the setpoint-driven steady-state.
        target_C = raw_setpoint_to_target_C(self.temp_setpoint_raw, self.temp_cal)
        alpha = 1.0 - math.exp(-sim_dt / THERMAL_TAU_SECONDS)
        noise_T = self._rng.normal(
            0.0, TEMP_NOISE_SIGMA * math.sqrt(sim_dt / 10.0), N_VIALS
        )
        self.temp_C = self.temp_C + alpha * (target_C - self.temp_C) + noise_T

        # 3. OD: logistic growth, gated by stir > 0 (mixing / oxygenation)
        # and scaled by the cardinal-temperature factor for E. coli.
        mu = np.where(self.stir_speed > 0, self.mu_max, 0.0)
        mu = mu * growth_rate_factor(self.temp_C)
        hours = sim_dt / 3600.0
        self.od_abs = self.od_abs * np.exp(
            mu * hours * (1.0 - self.od_abs / CARRYING_CAPACITY_OD)
        )
        self.od_abs = self.od_abs + self._rng.normal(0.0, OD_NOISE_SIGMA, N_VIALS)
        self.od_abs = np.clip(self.od_abs, 1e-4, CARRYING_CAPACITY_OD * 1.05)

    # ------------------------------------------------------------------
    # Public reads — direct physical units, no ADC indirection.
    # ------------------------------------------------------------------

    def read_temperature(self) -> list[float]:
        with self._lock:
            self._advance()
            return self.temp_C.tolist()

    def read_od(self, led_power: int = 2125) -> list[float]:
        with self._lock:
            self._advance()
            return self.od_abs.tolist()

    # ------------------------------------------------------------------
    # Public writes
    # ------------------------------------------------------------------

    def _C_to_raw(self, target_C: np.ndarray) -> np.ndarray:
        if self.temp_cal is not None:
            slope, intercept = self.temp_cal[0], self.temp_cal[1]
        else:
            slope, intercept = _FALLBACK_SLOPE, _FALLBACK_INTERCEPT
        return np.rint((target_C - intercept) / slope).astype(int)

    def _raw_floor_per_vial(self) -> np.ndarray:
        if self.temp_cal is None:
            return np.full(N_VIALS, DEFAULT_RAW_FLOOR, dtype=int)
        floor = self._C_to_raw(np.full(N_VIALS, MAX_SAFE_TEMP_C))
        return floor.astype(int)

    def set_temperature_celsius(self, temps_c: list[float]) -> list[float]:
        """Set 16 target temperatures in Celsius (primary heater API)."""
        if self.temp_cal is None:
            raise RuntimeError(
                "set_temperature_celsius requires calibration; "
                "call load_calibration() first or use set_temperature_raw"
            )
        temps = np.asarray(temps_c, dtype=float)
        if temps.shape != (N_VIALS,):
            raise ValueError(
                f"set_temperature_celsius expects {N_VIALS} values, "
                f"got shape {temps.shape}"
            )
        temps = np.clip(temps, T_AMBIENT_C, MAX_SAFE_TEMP_C)
        raw = self._C_to_raw(temps)
        with self._lock:
            self.temp_setpoint_raw = raw
        return self.read_temperature()

    def set_temperature_raw(self, setpoints: list[int]) -> list[float]:
        """Escape hatch — see SerialManager.set_temperature_raw."""
        raw = np.asarray(setpoints, dtype=int)
        if raw.shape != (N_VIALS,):
            raise ValueError(
                f"set_temperature_raw expects {N_VIALS} values, "
                f"got shape {raw.shape}"
            )
        floor = self._raw_floor_per_vial()
        raw = np.maximum(raw, floor)
        raw = np.clip(raw, None, HEATER_OFF_SETPOINT)
        with self._lock:
            self.temp_setpoint_raw = raw
        return self.read_temperature()

    def set_stir(self, speed_values: list[int]) -> None:
        speeds = np.asarray(speed_values, dtype=int)
        if speeds.shape != (N_VIALS,):
            raise ValueError(
                f"set_stir expects {N_VIALS} values, got shape {speeds.shape}"
            )
        speeds = np.clip(speeds, 0, STIR_MAX)
        with self._lock:
            self.stir_speed = speeds

    def pump_command(self, vial: int, direction: str, seconds: float) -> None:
        if not (0 <= vial < N_VIALS):
            raise ValueError(f"vial must be in 0..{N_VIALS - 1}, got {vial}")
        if direction not in ("influx", "efflux"):
            raise ValueError(
                f"direction must be 'influx' or 'efflux', got {direction!r}"
            )
        if seconds < 0:
            raise ValueError(f"seconds must be >= 0, got {seconds}")
        # Mirror the real SerialManager's sub-second handling so mock-backed
        # tests catch callers that would silently no-op on hardware.
        whole_seconds = int(round(seconds))
        if seconds > 0 and whole_seconds == 0:
            logging.getLogger(__name__).warning(
                "pump_command vial=%d %s: %.3fs rounds to 0s — no pump will fire. "
                "Sub-second pump times are not supported by the legacy firmware.",
                vial, direction, seconds,
            )
            return
        with self._lock:
            self.pump_log.append(
                {
                    "sim_time": self.sim_time,
                    "vial": int(vial),
                    "direction": direction,
                    "seconds": float(whole_seconds),
                }
            )

    def stop_all_pumps(self) -> None:
        with self._lock:
            self.pump_log.append(
                {
                    "sim_time": self.sim_time,
                    "vial": -1,
                    "direction": "stop_all",
                    "seconds": 0.0,
                }
            )

    def emergency_shutdown(self) -> None:
        """Stop all pumps, zero stir, park heaters OFF (HEATER_OFF_SETPOINT, not 0)."""
        with self._lock:
            self.temp_setpoint_raw = np.full(N_VIALS, HEATER_OFF_SETPOINT, dtype=int)
            self.stir_speed = np.zeros(N_VIALS, dtype=int)
            self.pump_log.append(
                {
                    "sim_time": self.sim_time,
                    "vial": -1,
                    "direction": "stop_all",
                    "seconds": 0.0,
                }
            )
