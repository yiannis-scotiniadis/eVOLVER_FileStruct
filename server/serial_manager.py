"""SerialManager — owns the RS485 link to the four SAMD21 Arduinos.

Drop-in for MockSerialManager (same public methods, same return shapes). See
SPEC.md section 5 and CLAUDE.md's RS485 protocol section.

Protocol (CLAUDE.md):
  RPi -> Arduino:  {prefix}{csv values} !       (' !' is literal space + bang)
  Arduino -> RPi:  {prefix}{csv values}end

  Outbound prefixes: st (fluidics), zv (stir), xr (temperature), we (OD)
  Inbound  prefixes: temp, turb

  Stir and fluidics are write-only; temperature and OD elicit a response.
  >=50 ms between commands; 5 s read timeout; on timeout / malformed
  response, log and continue (never hang).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import serial as _pyserial
except ImportError:  # allows the module to be imported on dev machines without pyserial
    _pyserial = None


N_VIALS = 16
DEFAULT_PORT = "/dev/ttyAMA0"
DEFAULT_BAUDRATE = 9600
DEFAULT_TIMEOUT_SECONDS = 5.0
MIN_INTER_COMMAND_SECONDS = 0.050  # CLAUDE.md: 50 ms minimum between commands

PREFIX_FLUIDICS = "st"
PREFIX_STIR = "zv"
PREFIX_TEMPERATURE = "xr"
PREFIX_OD = "we"
COMMAND_TERMINATOR = " !"

RESPONSE_TEMP = "temp"
RESPONSE_OD = "turb"
RESPONSE_TERMINATOR = "end"

# Heater control convention (SPEC.md §10)
#
# The `xr` value the Arduino accepts is NOT a PWM duty cycle. It is an
# inverted setpoint that the Arduino's closed loop drives the thermistor
# ADC reading toward. The temp calibration has a NEGATIVE slope, so:
#   lower setpoint integer -> hotter target (xr=0 ~= 82 C, heater pinned on)
#   higher setpoint integer -> colder target (xr=4095 ~= unreachable cold)
# All safety logic in this module is written against that convention.
HEATER_OFF_SETPOINT = 4095        # unreachably cold; the only safe "off"
MAX_SAFE_TEMP_C = 45.0            # software cap on requested target temp
# Fallback floor on raw setpoints when no calibration is loaded. The
# per-vial floor derived from MAX_SAFE_TEMP_C and a typical calibration
# (slope ~ -0.11, intercept ~ 82) is ~340; 300 is conservatively below
# that, so it rejects "asking for hotter than ~48 C" even uncalibrated.
DEFAULT_RAW_FLOOR = 300

STIR_MAX = 15
OD_LED_MAX = 2200  # eVOLVER_module.py:18 "Limit 2200"

# Enhanced OD acquisition (median-of-N raw averaging + dark subtraction +
# range guard). Defaults; per-experiment config overrides via app.py.
OD_DEFAULT_N_SAMPLES = 5     # light reads per cycle (median of these)
OD_DEFAULT_N_DARK = 3        # LED-off reads per cycle (dark is more stable)
OD_AGG_DEFAULT = "median"
OD_AGG_CHOICES = ("median", "mean", "trimmed_mean")

# Stop-all-pumps body (CLAUDE.md "Fluidics sub-protocol" / eVOLVER_module.py:199).
# st (address) + t, (multi-fire sub-mode) + 32 ones (pump mask) + 16 zeros
# (per-vial times). Full wire becomes "stt,<32 ones>,<16 zeros>, !".
STOP_ALL_PUMPS_BODY = "t," + ("1" * 32) + "," + ("0," * 16)


@dataclass(frozen=True)
class ODReading:
    """Structured result of one enhanced OD acquisition cycle.

    Every field is a length-``N_VIALS`` list:

    - ``calibrated`` — OD600; ``NaN`` when the vial was ``dropped`` or
      ``out_of_range``.
    - ``raw`` — aggregated, **dark-subtracted** photodiode signal (the value
      actually fed to the calibration); ``NaN`` when dropped.
    - ``dark`` — aggregated LED-off reading; ``NaN`` when dark subtraction is
      off.
    - ``n_valid`` — surviving samples per vial after NaN/garble rejection.
    - ``flags`` — per vial, one of ``"ok" | "out_of_range" | "dropped"``.
    """

    calibrated: list[float]
    raw: list[float]
    dark: list[float]
    n_valid: list[int]
    flags: list[str]


log = logging.getLogger(__name__)


class SerialManager:
    def __init__(
        self,
        port: str = DEFAULT_PORT,
        baudrate: int = DEFAULT_BAUDRATE,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        *,
        ser: Optional[object] = None,
    ) -> None:
        """Open the RS485 connection. Pass `ser=...` to inject a pre-built
        serial-like object (used by tests to substitute a fake)."""
        if ser is None:
            if _pyserial is None:
                raise RuntimeError(
                    "pyserial is not installed. Install it (`pip install pyserial`) "
                    "or pass an injected `ser=` for testing."
                )
            ser = _pyserial.Serial(port=port, baudrate=baudrate, timeout=timeout)
            ser.flushInput()
        self._ser = ser
        self._lock = threading.Lock()
        self._last_command_time = 0.0  # monotonic seconds; 0 means "never sent"

        # IMPORTANT: heater state defaults to OFF (4095), not 0. Under the
        # inverted convention 0 is the HOTTEST possible setpoint, so any
        # code path that runs before set_temperature_celsius() must not
        # leak zeros onto the wire.
        self.temp_setpoint_raw = np.full(N_VIALS, HEATER_OFF_SETPOINT, dtype=int)
        self.stir_speed = np.zeros(N_VIALS, dtype=int)

        self.temp_cal: Optional[np.ndarray] = None
        self.od_cal: Optional[np.ndarray] = None
        # Pristine copy of the loaded OD calibration, before any per-run
        # blank re-anchor (apply_od_blank). clear_od_blank restores from it.
        self._od_cal_base: Optional[np.ndarray] = None
        # True when the loaded OD calibration was measured on dark-subtracted
        # signal (LED-on - LED-off), per the OD_cal.meta.json sidecar. The
        # enhanced reader REFUSES dark subtraction against a calibration that
        # is NOT dark-subtracted (would yield silently wrong OD).
        self._od_cal_dark_subtracted = False

    def close(self) -> None:
        with self._lock:
            try:
                self._ser.close()
            except Exception:
                log.exception("error closing serial port")

    # ------------------------------------------------------------------
    # Calibration  (CLAUDE.md §Calibration)
    # ------------------------------------------------------------------

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
        # Provenance sidecar (optional): calibration/OD_cal.meta.json marks
        # whether this curve was fit on dark-subtracted signal. Absent ->
        # assume NOT dark-subtracted (the legacy convention).
        dark_subtracted = self._read_cal_dark_subtracted(od_cal_path)
        with self._lock:
            self.temp_cal = temp_cal
            self.od_cal = od_cal
            self._od_cal_base = od_cal.copy()
            self._od_cal_dark_subtracted = dark_subtracted

    @property
    def od_cal_dark_subtracted(self) -> bool:
        """True when the loaded OD calibration's sidecar records that it was
        fit on dark-subtracted signal. Gates ``dark_subtract`` everywhere."""
        return self._od_cal_dark_subtracted

    def apply_od_blank(self, c_run_by_vial: dict) -> None:
        """Apply a per-run OD blank re-anchor (CALIBRATION_PROTOCOL §5.4 /
        §10.1) to the in-memory calibration: set row 2 (inflection OD) to
        ``c_run`` for each given vial. Rows 0, 1 and 3 are never touched, and
        no calibration file is modified — the persisted artefact is the run's
        ``od_blank.json``. Idempotent per vial; vials not listed keep their
        current row-2 value. ``clear_od_blank`` restores the pristine curve."""
        with self._lock:
            if self.od_cal is None:
                raise RuntimeError("apply_od_blank requires a loaded OD calibration")
            od_cal = self.od_cal.copy()
            for vial, c_run in c_run_by_vial.items():
                v = int(vial)
                if not (0 <= v < N_VIALS):
                    raise ValueError(f"vial must be in 0..{N_VIALS - 1}, got {v}")
                od_cal[2, v] = float(c_run)
            self.od_cal = od_cal

    def clear_od_blank(self) -> None:
        """Restore the pristine (pre-blank) OD calibration. No-op when no
        calibration is loaded or no blank was ever applied."""
        with self._lock:
            if self._od_cal_base is not None:
                self.od_cal = self._od_cal_base.copy()

    @staticmethod
    def _read_cal_dark_subtracted(od_cal_path: str) -> bool:
        """Read the ``dark_subtracted`` flag from the OD calibration's
        ``<stem>.meta.json`` sidecar, if present. Returns False on any
        absence/parse error (the safe legacy assumption)."""
        try:
            p = Path(od_cal_path)
            meta_path = p.with_name(p.stem + ".meta.json")
            if not meta_path.is_file():
                return False
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return bool(meta.get("dark_subtracted", False))
        except Exception:
            log.exception("failed reading OD calibration meta for %s", od_cal_path)
            return False

    def _temp_raw_to_C(self, raw: np.ndarray) -> np.ndarray:
        # CLAUDE.md: temp_celsius = (raw_adc * slope[vial]) + intercept[vial]
        return raw * self.temp_cal[0] + self.temp_cal[1]

    def _od_raw_to_OD(self, raw: np.ndarray) -> np.ndarray:
        # CLAUDE.md: OD = od_cal[2,vial]
        #               - log10((od_cal[1,vial] - od_cal[0,vial])
        #                       / (raw_adc - od_cal[0,vial]) - 1)
        #                 / od_cal[3,vial]
        mn, mx, c, d = self.od_cal[0], self.od_cal[1], self.od_cal[2], self.od_cal[3]
        with np.errstate(invalid="ignore", divide="ignore"):
            return c - np.log10((mx - mn) / (raw - mn) - 1.0) / d

    # ------------------------------------------------------------------
    # Low-level RS485 (must hold self._lock)
    # ------------------------------------------------------------------

    def _wait_inter_command(self) -> None:
        elapsed = time.monotonic() - self._last_command_time
        if 0.0 <= elapsed < MIN_INTER_COMMAND_SECONDS:
            time.sleep(MIN_INTER_COMMAND_SECONDS - elapsed)

    def _write(self, prefix: str, body: str) -> None:
        self._wait_inter_command()
        message = f"{prefix}{body}{COMMAND_TERMINATOR}"
        try:
            self._ser.write(message.encode("ascii"))
        except Exception:
            log.exception("serial write failed (message=%r)", message)
            raise
        finally:
            self._last_command_time = time.monotonic()

    def _read_response(self, expected_prefix: str) -> Optional[list[float]]:
        try:
            raw = self._ser.read_until(RESPONSE_TERMINATOR.encode("ascii"))
        except Exception:
            log.exception("serial read failed (expected '%s...end')", expected_prefix)
            return None
        if not raw:
            log.warning(
                "serial read timed out (no response, expected '%s...end')",
                expected_prefix,
            )
            return None
        line = raw.decode("ascii", errors="replace").strip()
        if not (line[:4] == expected_prefix and line[-3:] == RESPONSE_TERMINATOR):
            log.warning(
                "malformed response: %r (expected '%s...end')", line, expected_prefix
            )
            return None
        payload = line[4:-3]
        try:
            values = [float(v) for v in payload.rstrip(",").split(",") if v]
        except ValueError:
            log.exception("failed to parse response payload %r", payload)
            return None
        if len(values) != N_VIALS:
            log.warning(
                "expected %d values, got %d in response %r", N_VIALS, len(values), line
            )
            return None
        return values

    @staticmethod
    def _format_csv(values: np.ndarray) -> str:
        return ",".join(str(int(v)) for v in values) + ","

    def _read_temperature_locked(self) -> list[float]:
        body = self._format_csv(self.temp_setpoint_raw)
        self._write(PREFIX_TEMPERATURE, body)
        values = self._read_response(RESPONSE_TEMP)
        if values is None:
            return [float("nan")] * N_VIALS
        raw = np.asarray(values)
        if self.temp_cal is None:
            return raw.tolist()
        return self._temp_raw_to_C(raw).tolist()

    def _read_od_locked(self, led_power: int) -> list[float]:
        led = int(np.clip(led_power, 0, OD_LED_MAX))
        body = self._format_csv(np.full(N_VIALS, led))
        self._write(PREFIX_OD, body)
        values = self._read_response(RESPONSE_OD)
        if values is None:
            return [float("nan")] * N_VIALS
        raw = np.asarray(values)
        if self.od_cal is None:
            return raw.tolist()
        return self._od_raw_to_OD(raw).tolist()

    # ------------------------------------------------------------------
    # Enhanced OD acquisition: median-of-N + dark subtraction + range guard
    # ------------------------------------------------------------------

    def _collect_od_reads(self, body: str, n: int) -> np.ndarray:
        """Issue ``n`` OD reads with payload ``body`` and stack the framed
        responses into an ``(n, N_VIALS)`` float array. A timed-out/malformed
        response (``_read_response`` returns None) becomes an all-NaN row so it
        drops out of the per-vial aggregation."""
        rows: list[list[float]] = []
        for _ in range(n):
            self._write(PREFIX_OD, body)
            values = self._read_response(RESPONSE_OD)
            if values is None:
                rows.append([float("nan")] * N_VIALS)
            else:
                rows.append([float(v) for v in values])
        return np.asarray(rows, dtype=float)

    @staticmethod
    def _aggregate_samples(samples: np.ndarray, agg: str) -> tuple[np.ndarray, np.ndarray]:
        """Per-vial aggregate of an ``(n, N_VIALS)`` sample array, ignoring NaN
        (dropped/garbled) samples. Returns ``(agg_per_vial, n_valid_per_vial)``;
        a vial with zero surviving samples aggregates to NaN."""
        agg_out = np.full(N_VIALS, np.nan)
        n_valid = np.zeros(N_VIALS, dtype=int)
        for v in range(N_VIALS):
            col = samples[:, v]
            col = col[~np.isnan(col)]
            n_valid[v] = col.size
            if col.size == 0:
                continue
            if agg == "median":
                agg_out[v] = float(np.median(col))
            elif agg == "mean":
                agg_out[v] = float(np.mean(col))
            elif agg == "trimmed_mean":
                # Drop one min + one max, average the rest; fall back to the
                # median when too few samples remain to trim meaningfully.
                if col.size >= 3:
                    agg_out[v] = float(np.mean(np.sort(col)[1:-1]))
                else:
                    agg_out[v] = float(np.median(col))
            else:
                raise ValueError(
                    f"unknown OD aggregation {agg!r}; choose from {OD_AGG_CHOICES}"
                )
        return agg_out, n_valid

    def collect_od_raw(self, led_power: int, n_samples: int = 5) -> dict:
        """Calibration-wizard read primitive (CALIBRATION_PROTOCOL §5.4):
        ``n_samples`` raw OD reads at ``led_power`` (0 = dark read), reduced
        to per-vial NaN-aware statistics. No calibration is applied.

        Returns ``{"median": [...16], "sd": [...16], "n_valid": [...16]}``;
        a vial with zero surviving samples reports NaN median/sd."""
        led = int(np.clip(led_power, 0, OD_LED_MAX))
        n = max(1, int(n_samples))
        with self._lock:
            body = self._format_csv(np.full(N_VIALS, led))
            samples = self._collect_od_reads(body, n)
        median: list[float] = []
        sd: list[float] = []
        n_valid: list[int] = []
        for v in range(N_VIALS):
            col = samples[:, v]
            col = col[~np.isnan(col)]
            n_valid.append(int(col.size))
            if col.size == 0:
                median.append(float("nan"))
                sd.append(float("nan"))
            else:
                median.append(float(np.median(col)))
                # ddof=0 population SD; with n=1 that's 0.0, not NaN.
                sd.append(float(np.std(col)))
        return {"median": median, "sd": sd, "n_valid": n_valid}

    def read_od_enhanced(
        self,
        led_power: int = 2125,
        *,
        n_samples: int = OD_DEFAULT_N_SAMPLES,
        dark_subtract: bool = False,
        n_dark: int = OD_DEFAULT_N_DARK,
        agg: str = OD_AGG_DEFAULT,
    ) -> ODReading:
        """Acquire one OD cycle with averaging, optional dark subtraction, and
        a per-vial calibration range guard. See :class:`ODReading`.

        ``dark_subtract`` requires the loaded calibration to be fit on
        dark-subtracted signal (LED-on - LED-off, per the OD_cal.meta.json
        sidecar); requesting it against any other calibration raises
        ``ValueError`` — mixing the two yields silently wrong OD."""
        with self._lock:
            return self._read_od_enhanced_locked(
                led_power, n_samples, dark_subtract, n_dark, agg,
            )

    def _read_od_enhanced_locked(
        self,
        led_power: int,
        n_samples: int,
        dark_subtract: bool,
        n_dark: int,
        agg: str,
    ) -> ODReading:
        led = int(np.clip(led_power, 0, OD_LED_MAX))
        n_samples = max(1, int(n_samples))
        n_dark = max(0, int(n_dark))

        # --- Dark phase (LED off). The photodiode still responds, so each
        # dark read is a full request/response we must drain. ---
        if dark_subtract and n_dark > 0:
            if self.od_cal is not None and not self._od_cal_dark_subtracted:
                # Hard error (SPEC §19.2 / CALIBRATION_PROTOCOL §13): the
                # curve was fit on non-dark-subtracted signal, so subtracting
                # the dark first yields silently wrong OD. Previously a
                # one-time warning; the guard at experiment creation should
                # make this unreachable, but a hand-edited config must not
                # slip through.
                raise ValueError(
                    "dark_subtract=True requires an OD calibration marked "
                    "dark_subtracted in OD_cal.meta.json; the loaded "
                    "calibration is not"
                )
            dark_body = self._format_csv(np.zeros(N_VIALS, dtype=int))
            dark_samples = self._collect_od_reads(dark_body, n_dark)
            dark_agg, _ = self._aggregate_samples(dark_samples, agg)
            # A dropped dark read subtracts 0 rather than poisoning the vial.
            dark_for_calc = np.nan_to_num(dark_agg, nan=0.0)
        else:
            dark_agg = np.full(N_VIALS, np.nan)
            dark_for_calc = np.zeros(N_VIALS)

        # --- Light phase ---
        light_body = self._format_csv(np.full(N_VIALS, led))
        light_samples = self._collect_od_reads(light_body, n_samples)
        signal_on, n_valid = self._aggregate_samples(light_samples, agg)

        corrected = signal_on - dark_for_calc  # NaN where signal_on is NaN

        od_cal = self.od_cal
        if od_cal is not None:
            mn, mx = od_cal[0], od_cal[1]
            in_domain = (corrected > mn) & (corrected < mx)
            od_all = self._od_raw_to_OD(corrected)
        else:
            in_domain = np.ones(N_VIALS, dtype=bool)
            od_all = corrected  # no calibration -> raw passthrough

        calibrated: list[float] = []
        raw_out: list[float] = []
        flags: list[str] = []
        valid = (n_valid > 0) & (~np.isnan(corrected))
        for v in range(N_VIALS):
            if not valid[v]:
                flags.append("dropped")
                calibrated.append(float("nan"))
                raw_out.append(float("nan"))
                continue
            raw_out.append(float(corrected[v]))
            if od_cal is not None and not in_domain[v]:
                flags.append("out_of_range")
                calibrated.append(float("nan"))
                continue
            flags.append("ok")
            calibrated.append(float(od_all[v]))

        dark_list = [
            float(d) if not np.isnan(d) else float("nan") for d in dark_agg
        ]
        return ODReading(
            calibrated=calibrated,
            raw=raw_out,
            dark=dark_list,
            n_valid=[int(x) for x in n_valid],
            flags=flags,
        )

    # ------------------------------------------------------------------
    # Public reads
    # ------------------------------------------------------------------

    def read_temperature(self) -> list[float]:
        with self._lock:
            return self._read_temperature_locked()

    def read_od(self, led_power: int = 2125) -> list[float]:
        with self._lock:
            return self._read_od_locked(led_power)

    # ------------------------------------------------------------------
    # Heater setpoint conversion (per-vial calibration)
    # ------------------------------------------------------------------

    def _C_to_raw(self, target_C: np.ndarray) -> np.ndarray:
        """Invert temp_cal[0]*setpoint + temp_cal[1] = C: setpoint = (C - intercept)/slope.

        Calibration slopes are NEGATIVE (~ -0.11), so a higher target C
        yields a SMALLER setpoint. E.g., 37 C -> ~482; 22 C (ambient)
        -> ~580; 45 C cap -> ~340."""
        slope = self.temp_cal[0]
        intercept = self.temp_cal[1]
        return np.rint((target_C - intercept) / slope).astype(int)

    def _raw_floor_per_vial(self) -> np.ndarray:
        """Per-vial minimum raw setpoint corresponding to MAX_SAFE_TEMP_C.

        Raw setpoints below this are equivalent to asking for a target
        hotter than the cap. Returns an array of length N_VIALS."""
        if self.temp_cal is None:
            return np.full(N_VIALS, DEFAULT_RAW_FLOOR, dtype=int)
        floor = self._C_to_raw(np.full(N_VIALS, MAX_SAFE_TEMP_C))
        return floor.astype(int)

    # ------------------------------------------------------------------
    # Public writes
    # ------------------------------------------------------------------

    def set_temperature_celsius(self, temps_c: list[float]) -> list[float]:
        """Set 16 target temperatures in Celsius (primary heater API).

        Converts via per-vial calibration to raw setpoint integers and
        dispatches. Targets are clamped to [T_AMBIENT_C, MAX_SAFE_TEMP_C]
        before conversion. To park a vial off, pass ambient (~22 C); the
        derived setpoint will be unreachably cold so the heater idles.

        Requires calibration to be loaded. Returns current temperature
        readings in Celsius (also calibration-converted by the reader)."""
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
        # Ambient is the practical lower bound — there is no active cooling.
        # MAX_SAFE_TEMP_C is the software safety cap.
        temps = np.clip(temps, 22.0, MAX_SAFE_TEMP_C)
        raw = self._C_to_raw(temps)
        with self._lock:
            self.temp_setpoint_raw = raw
            return self._read_temperature_locked()

    def set_temperature_raw(self, setpoints: list[int]) -> list[float]:
        """Escape hatch for calibration wizard and low-level debugging.

        Sends 16 raw setpoint integers verbatim as the `xr` command payload.
        The setpoint convention is INVERTED — lower value = hotter target.
        xr=0 requests ~82 C (drives heater to max), xr=4095 = off.

        Enforces a per-vial floor derived from MAX_SAFE_TEMP_C (or a
        conservative default of DEFAULT_RAW_FLOOR if no calibration is
        loaded). Setpoints below the floor are clipped up — you cannot
        send something hotter than the safety cap via this method."""
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
            return self._read_temperature_locked()

    def set_stir(self, speed_values: list[int]) -> None:
        speeds = np.asarray(speed_values, dtype=int)
        if speeds.shape != (N_VIALS,):
            raise ValueError(
                f"set_stir expects {N_VIALS} values, got shape {speeds.shape}"
            )
        speeds = np.clip(speeds, 0, STIR_MAX)
        with self._lock:
            self.stir_speed = speeds
            self._write(PREFIX_STIR, self._format_csv(speeds))

    def pump_command(self, vial: int, direction: str, seconds: float) -> None:
        if not (0 <= vial < N_VIALS):
            raise ValueError(f"vial must be in 0..{N_VIALS - 1}, got {vial}")
        if direction not in ("influx", "efflux"):
            raise ValueError(
                f"direction must be 'influx' or 'efflux', got {direction!r}"
            )
        if seconds < 0:
            raise ValueError(f"seconds must be >= 0, got {seconds}")
        # CLAUDE.md: influx vial N -> bit N (2^N), efflux vial N -> bit N+16 (2^(N+16)).
        bit = vial if direction == "influx" else vial + 16
        addr = 1 << bit
        # 2016 firmware accepts integer seconds (mac_original used %d).
        # round() preserves 0.5+ -> 1; sub-half-second requests round to 0
        # and would silently no-op, so warn the caller rather than fire blind.
        whole_seconds = int(round(seconds))
        if seconds > 0 and whole_seconds == 0:
            log.warning(
                "pump_command vial=%d %s: %.3fs rounds to 0s — no pump will fire. "
                "Sub-second pump times are not supported by the legacy firmware.",
                vial, direction, seconds,
            )
            return
        body = f"{addr:b},0,{whole_seconds},"
        with self._lock:
            self._write(PREFIX_FLUIDICS, body)

    def stop_all_pumps(self) -> None:
        with self._lock:
            self._write(PREFIX_FLUIDICS, STOP_ALL_PUMPS_BODY)

    def emergency_shutdown(self) -> None:
        """Stop all pumps, zero stir, and park heaters OFF.

        Idempotent — watchdog may call repeatedly. Logs but does not
        re-raise on per-step failure so a single broken subsystem doesn't
        prevent the others from being safed.

        Heater "off" is HEATER_OFF_SETPOINT (4095), NOT zero. Under the
        inverted convention zero would pin the heaters at maximum, which
        is the exact opposite of what an emergency stop should do."""
        zeros = np.zeros(N_VIALS, dtype=int)
        off_setpoints = np.full(N_VIALS, HEATER_OFF_SETPOINT, dtype=int)
        with self._lock:
            try:
                self._write(PREFIX_FLUIDICS, STOP_ALL_PUMPS_BODY)
            except Exception:
                log.exception("emergency_shutdown: pump stop failed")
            try:
                self.stir_speed = zeros
                self._write(PREFIX_STIR, self._format_csv(zeros))
            except Exception:
                log.exception("emergency_shutdown: stir zero failed")
            try:
                self.temp_setpoint_raw = off_setpoints
                self._write(PREFIX_TEMPERATURE, self._format_csv(off_setpoints))
                # xr elicits a temp response — drain it so the next read isn't desynced.
                self._read_response(RESPONSE_TEMP)
            except Exception:
                log.exception("emergency_shutdown: heater park-off failed")
