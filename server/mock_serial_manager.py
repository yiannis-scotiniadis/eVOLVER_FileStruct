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
  efflux is tracked but does not change OD. Frames queue and run one at a
  time, like the firmware (FLUIDICS_FIRMWARE_AUDIT.md FW-1): a frame starts
  when the previous one ends, and every pump in its mask starts together
  (FW-2). ``pump_log`` holds one entry per pump per frame.
- Stir: stored as plain integer state; the only dynamical effect is
  gating growth.
"""

from __future__ import annotations

import logging
import math
import threading
from typing import Optional

import numpy as np

from fluidics import Frame, FluidicsDispatchError, check_mask, pump_index
from serial_manager import (
    DEFAULT_RAW_FLOOR,
    HEATER_OFF_SETPOINT,
    MAX_SAFE_TEMP_C,
    OD_AGG_DEFAULT,
    OD_DEFAULT_N_DARK,
    OD_DEFAULT_N_SAMPLES,
    OD_LED_MAX,
    ODReading,
    OdCurveMixin,
    SerialManager,
    apply_floor_override,
    led_power_vector,
    od_reading_from_samples,
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


class MockSerialManager(OdCurveMixin):
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
        # Sim time at which the firmware's frame queue drains (FW-1).
        self._fluidics_free_at = 0.0
        self.flow_rate = DEFAULT_FLOW_RATES_ML_PER_SEC.copy()
        self.volume_ml = DEFAULT_VOLUME_ML

        self.temp_cal: Optional[np.ndarray] = None
        # The OD curve, per vial -- same state and methods as SerialManager.
        self._init_od_curve_state()
        # Synthetic ADC model for collect_od_raw (the sim has no real ADC):
        # dark floor + read noise, in counts.
        self.mock_dark_counts = 1200.0
        self.mock_dark_noise_sd = 30.0
        self.mock_signal_noise_sd = 150.0
        self.mock_uncal_signal_counts = 58000.0

        # --- Ground truth for the Tier 3 wizards (Session AA) -------------
        # The true thermistor response, i.e. what a perfect two-point
        # calibration would recover. Pinned at the FIRST load_calibration and
        # never moved by a later one: the heater physics runs on it, while
        # the INSTALLED temp_cal only converts raw counts to reported °C and
        # °C targets to setpoints. If the truth followed each install, every
        # post-install test would pass by construction.
        self._true_temp_cal: Optional[np.ndarray] = None
        self.mock_temp_raw_noise_sd = 1.0  # thermistor ADC counts per read
        self.last_temperature_raw = np.full(N_VIALS, np.nan)
        # OD truth overrides (set_true_od / set_true_od_curve). Opt-in: with
        # neither set, OD behaves exactly as before.
        self._frozen_od: dict[int, float] = {}
        self._true_od_rows: Optional[np.ndarray] = None

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
        dark_subtracted = SerialManager._read_cal_dark_subtracted(od_cal_path)
        with self._lock:
            self.temp_cal = temp_cal
            self._set_default_od_curve_locked(od_cal, dark_subtracted)
            if self._true_temp_cal is None:
                self._true_temp_cal = temp_cal.copy()

    # ------------------------------------------------------------------
    # Ground-truth controls (tests of the Tier 3 wizards)
    # ------------------------------------------------------------------

    def set_true_temp_cal(self, slope, intercept) -> None:
        """Make the simulated thermistors respond as ``raw * slope +
        intercept`` (per vial), independently of the installed calibration.
        A two-point calibration against a perfect reference thermometer
        should recover exactly these numbers."""
        cal = np.vstack([
            np.broadcast_to(np.asarray(slope, dtype=float), (N_VIALS,)),
            np.broadcast_to(np.asarray(intercept, dtype=float), (N_VIALS,)),
        ])
        with self._lock:
            self._true_temp_cal = cal

    def set_true_od(self, vials, od: Optional[float]) -> None:
        """Hold the listed vials at a fixed true OD600 -- no growth, no
        noise, no carrying-capacity clip -- as a loaded turbidity standard
        would sit. ``od=None`` releases them back to the growth model."""
        with self._lock:
            for v in vials:
                if od is None:
                    self._frozen_od.pop(int(v), None)
                else:
                    self._frozen_od[int(v)] = float(od)
                    self.od_abs[int(v)] = float(od)

    def set_true_od_curve(self, rows, dark_counts=None) -> None:
        """Synthesize ``collect_od_raw``'s lit signal as ``dark +
        S_true(OD)`` from a 4x16 DARK-SUBTRACTED logistic ``rows`` -- the
        curve an OD dilution series should recover. ``dark_counts`` (scalar
        or 16-list) sets the per-vial dark floor too."""
        rows = np.asarray(rows, dtype=float)
        if rows.shape != (4, N_VIALS):
            raise ValueError(f"rows shape {rows.shape}, expected (4, {N_VIALS})")
        with self._lock:
            self._true_od_rows = rows
            if dark_counts is not None:
                self.mock_dark_counts = np.broadcast_to(
                    np.asarray(dark_counts, dtype=float), (N_VIALS,)
                ).copy()

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
        # The firmware closes its loop on the thermistor ADC, so where a
        # setpoint lands physically is set by the TRUE thermistor response,
        # not by whatever calibration the server has installed.
        true_cal = self._true_temp_cal if self._true_temp_cal is not None else self.temp_cal
        target_C = raw_setpoint_to_target_C(self.temp_setpoint_raw, true_cal)
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
        # A loaded turbidity standard does not grow (set_true_od).
        for vial, od in self._frozen_od.items():
            self.od_abs[vial] = od

    # ------------------------------------------------------------------
    # Thermistor ADC model (must hold self._lock)
    # ------------------------------------------------------------------

    def _true_temp_raw_locked(self) -> np.ndarray:
        """The raw counts the thermistors would report at the simulated
        temperature, through the TRUE response (fallback when uncalibrated)."""
        true_cal = self._true_temp_cal if self._true_temp_cal is not None else self.temp_cal
        if true_cal is None:
            slope, intercept = _FALLBACK_SLOPE, _FALLBACK_INTERCEPT
        else:
            slope, intercept = true_cal[0], true_cal[1]
        return (self.temp_C - intercept) / slope

    def _reported_temps_locked(self, raw: np.ndarray) -> np.ndarray:
        """°C as the server would compute it from ``raw`` with the INSTALLED
        calibration. While that is the true response (always, until a Tier 3
        test sets a different truth or installs a new curve) this is the
        simulated temperature itself, exactly as before Session AA."""
        if self.temp_cal is None:
            return self.temp_C.copy()
        if self._true_temp_cal is None or np.array_equal(self.temp_cal, self._true_temp_cal):
            return self.temp_C.copy()
        return raw * self.temp_cal[0] + self.temp_cal[1]

    # ------------------------------------------------------------------
    # Public reads — direct physical units, no ADC indirection.
    # ------------------------------------------------------------------

    def read_temperature(self) -> list[float]:
        with self._lock:
            self._advance()
            raw = self._true_temp_raw_locked()
            self.last_temperature_raw = raw
            return self._reported_temps_locked(raw).tolist()

    def read_temperature_with_raw(self) -> tuple[list[float], list[float]]:
        """Parity with SerialManager.read_temperature_with_raw."""
        with self._lock:
            self._advance()
            raw = self._true_temp_raw_locked()
            self.last_temperature_raw = raw
            return self._reported_temps_locked(raw).tolist(), raw.tolist()

    def collect_temp_raw(self, n_samples: int = 5) -> dict:
        """Parity with SerialManager.collect_temp_raw. Advances the sim ONCE
        (n advances at the default 100x would be minutes of simulated time
        per capture) and synthesizes ``n`` reads with count noise."""
        n = max(1, int(n_samples))
        with self._lock:
            self._advance()
            raw = self._true_temp_raw_locked()
            self.last_temperature_raw = raw
            samples = raw[None, :] + self._rng.normal(
                0.0, self.mock_temp_raw_noise_sd, (n, N_VIALS)
            )
        return {
            "median": np.median(samples, axis=0).tolist(),
            "sd": np.std(samples, axis=0).tolist(),
            "n_valid": [n] * N_VIALS,
        }

    def read_od(self, led_power: int = 2125) -> list[float]:
        with self._lock:
            self._advance()
            return self.od_abs.tolist()

    def collect_od_raw(self, led_power: int, n_samples: int = 5) -> dict:
        """Parity with SerialManager.collect_od_raw — the calibration-wizard
        read primitive. The sim has no real ADC, so raw counts are
        synthesized: LED 0 returns a dark floor + read noise; LED > 0 inverts
        the loaded calibration from the current simulated OD (falling back to
        a plausible constant when uncalibrated) + read noise."""
        leds = led_power_vector(led_power)
        n = max(1, int(n_samples))
        with self._lock:
            self._advance()
            dark = leds == 0
            dark_counts = np.broadcast_to(
                np.asarray(self.mock_dark_counts, dtype=float), (N_VIALS,)
            )
            if self._true_od_rows is not None:
                # Session AA ground truth: a lit read is the dark floor plus
                # the true dark-subtracted curve at the true OD.
                a, b, c, d = self._true_od_rows
                od = np.asarray(self.od_abs, dtype=float)
                lit = dark_counts + a + (b - a) / (1.0 + np.power(10.0, d * (c - od)))
            elif self.od_cal is not None:
                # Invert each vial's composed curve; on a dark-subtracted
                # vial that is the signal ABOVE the dark floor, as in
                # read_od_enhanced's default mode.
                lit = self._od_abs_to_raw(np.asarray(self.od_abs, dtype=float))
                lit = lit + np.where(self._od_ds_by_vial, dark_counts, 0.0)
            else:
                lit = np.full(N_VIALS, float(self.mock_uncal_signal_counts))
            # Per vial: a dark LED reads the dark floor, a lit one the signal.
            base = np.where(dark, dark_counts, lit)
            noise_sd = np.where(dark, self.mock_dark_noise_sd, self.mock_signal_noise_sd)
            self.last_collect_led_powers = leds.tolist()
            samples = base[None, :] + self._rng.normal(0.0, 1.0, (n, N_VIALS)) * noise_sd[None, :]
        return {
            "median": np.median(samples, axis=0).tolist(),
            "sd": np.std(samples, axis=0).tolist(),
            "n_valid": [n] * N_VIALS,
        }

    def read_od_enhanced(
        self,
        led_power: int = 2125,
        *,
        n_samples: int = OD_DEFAULT_N_SAMPLES,
        dark_subtract=False,
        n_dark: int = OD_DEFAULT_N_DARK,
        agg: str = OD_AGG_DEFAULT,
        record_dark: bool = False,
    ) -> ODReading:
        """Mock parity for SerialManager.read_od_enhanced: same dark plan and
        guard (per vial), same fields, and the sim advances **exactly once**
        (never ``n_samples`` times, which would over-run sim time).

        Two modes:

        - **default** -- the simulation's true OD is reported directly, and
          the raw fields are synthesized by inverting each vial's composed
          curve, i.e. what the real reader would have fed that curve (plus
          the dark floor on a dark-subtracted vial). The historical mock.
        - **truth** (``set_true_od_curve``) -- the photodiode sees
          ``dark + S_true(OD)``, and that goes through the SAME conversion as
          the real reader (``od_reading_from_samples``). A vial given the
          wrong organism's curve then reads the wrong OD, as it would on
          the rig."""
        led = int(np.clip(led_power, 0, OD_LED_MAX))
        with self._lock:
            mask, n_dark = self._resolve_dark_plan_locked(
                dark_subtract, max(0, int(n_dark))
            )
            take_dark = n_dark > 0 and bool(mask.any() or record_dark)
            self._advance()
            n = max(1, int(n_samples))
            od = np.asarray(self.od_abs, dtype=float)
            dark_counts = np.broadcast_to(
                np.asarray(self.mock_dark_counts, dtype=float), (N_VIALS,)
            ).copy()
            dark = dark_counts if take_dark else np.full(N_VIALS, np.nan)
            dark_n_valid = np.full(N_VIALS, n_dark if take_dark else 0)
            n_valid = np.full(N_VIALS, n)

            if self._true_od_rows is not None:
                a, b, c, d = self._true_od_rows
                lit = dark_counts + a + (b - a) / (1.0 + np.power(10.0, d * (c - od)))
                reading, self._last_good_dark = od_reading_from_samples(
                    lit, n_valid, dark, dark_n_valid, mask,
                    self.od_cal, self._last_good_dark, led,
                )
                return reading

            if self.od_cal is not None:
                signal = self._od_abs_to_raw(od)
                mn, mx = self.od_cal[0], self.od_cal[1]
                in_domain = (signal > mn) & (signal < mx)
            else:
                signal = np.full(N_VIALS, np.nan)
                in_domain = np.ones(N_VIALS, dtype=bool)
            if take_dark:
                self._last_good_dark = dark_counts.copy()
            lit = np.where(mask, signal + dark_counts, signal)
            dark_used = np.where(mask, dark_counts, np.nan)

            calibrated: list[float] = []
            flags: list[str] = []
            for v in range(N_VIALS):
                if in_domain[v]:
                    flags.append("ok")
                    calibrated.append(float(od[v]))
                else:
                    flags.append("out_of_range")
                    calibrated.append(float("nan"))
            return ODReading(
                calibrated=calibrated,
                raw=[float(x) for x in signal],
                dark=[float(x) for x in dark],
                n_valid=[int(x) for x in n_valid],
                flags=flags,
                lit=[float(x) for x in lit],
                dark_used=[float(x) for x in dark_used],
                dark_n_valid=[int(x) for x in dark_n_valid],
                dark_subtracted=[bool(x) for x in mask],
                led_power=led,
            )

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

    def set_temperature_raw(
        self, setpoints: list[int], floor_override: Optional[dict] = None,
    ) -> list[float]:
        """Escape hatch — see SerialManager.set_temperature_raw."""
        raw = np.asarray(setpoints, dtype=int)
        if raw.shape != (N_VIALS,):
            raise ValueError(
                f"set_temperature_raw expects {N_VIALS} values, "
                f"got shape {raw.shape}"
            )
        floor = apply_floor_override(self._raw_floor_per_vial(), floor_override)
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
        self.pump_mask_command(1 << pump_index(vial, direction), seconds)

    def pump_mask_command(self, mask: int, seconds: float) -> None:
        """Parity with SerialManager.pump_mask_command."""
        check_mask(mask)
        if seconds < 0:
            raise ValueError(f"seconds must be >= 0, got {seconds}")
        # Mirror the real SerialManager's sub-second handling so mock-backed
        # tests catch callers that would silently no-op on hardware.
        whole_seconds = int(round(seconds))
        if seconds > 0 and whole_seconds == 0:
            logging.getLogger(__name__).warning(
                "pump frame mask=%s: %.3fs rounds to 0s — no pump will fire. "
                "Sub-second pump times are not supported by the legacy firmware.",
                format(mask, "b"), seconds,
            )
            return
        with self._lock:
            self._enqueue_frame_locked(mask, whole_seconds)

    def pump_frames(self, frames) -> None:
        """Parity with SerialManager.pump_frames (all frames validated first,
        then queued back-to-back)."""
        frames = [f if isinstance(f, Frame) else Frame(*f) for f in frames]
        with self._lock:
            for sent, frame in enumerate(frames):
                try:
                    self._enqueue_frame_locked(frame.mask, frame.seconds)
                except Exception as exc:
                    raise FluidicsDispatchError(
                        f"pump frame {sent + 1}/{len(frames)} failed: {exc}",
                        frames_sent=sent,
                    ) from exc

    def _enqueue_frame_locked(self, mask: int, whole_seconds: int) -> None:
        """Queue one frame behind whatever is still running (FW-1); every pump
        in it starts at the same sim time (FW-2)."""
        start = max(self.sim_time, self._fluidics_free_at)
        for pump in range(2 * N_VIALS):
            if mask >> pump & 1:
                self.pump_log.append(
                    {
                        "sim_time": start,
                        "vial": pump % N_VIALS,
                        "direction": "influx" if pump < N_VIALS else "efflux",
                        "seconds": float(whole_seconds),
                    }
                )
        self._fluidics_free_at = start + whole_seconds

    def _log_stop_all_locked(self) -> None:
        # The real stop frame is inert (FW-10/FW-11): it queues and does
        # nothing. Queued here too, so pump_log end times stay monotone.
        self.pump_log.append(
            {
                "sim_time": max(self.sim_time, self._fluidics_free_at),
                "vial": -1,
                "direction": "stop_all",
                "seconds": 0.0,
            }
        )

    def stop_all_pumps(self) -> None:
        with self._lock:
            self._log_stop_all_locked()

    def emergency_shutdown(self) -> None:
        """Stop all pumps, zero stir, park heaters OFF (HEATER_OFF_SETPOINT, not 0)."""
        with self._lock:
            self.temp_setpoint_raw = np.full(N_VIALS, HEATER_OFF_SETPOINT, dtype=int)
            self.stir_speed = np.zeros(N_VIALS, dtype=int)
            self._log_stop_all_locked()
