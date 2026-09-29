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

from fluidics import Frame, FluidicsDispatchError, check_mask, pump_index

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


def led_power_vector(led_power) -> "np.ndarray":
    """One LED power for all sixteen vials, or a per-vial list of 16, as the
    clipped integer vector a ``we`` frame carries."""
    if isinstance(led_power, (list, tuple, np.ndarray)):
        arr = np.asarray(led_power, dtype=float)
        if arr.shape != (N_VIALS,):
            raise ValueError(f"led_power list must have {N_VIALS} entries, got {arr.shape}")
    else:
        arr = np.full(N_VIALS, float(led_power))
    return np.clip(np.rint(arr), 0, OD_LED_MAX).astype(int)


def apply_floor_override(floor: "np.ndarray", floor_override: Optional[dict]) -> "np.ndarray":
    """The per-vial raw-setpoint floor with ``{vial: floor}`` overrides
    applied, each clamped to at least DEFAULT_RAW_FLOOR -- no override can
    request hotter than the uncalibrated safety floor."""
    floor = np.asarray(floor, dtype=int).copy()
    for vial, value in (floor_override or {}).items():
        v = int(vial)
        if not (0 <= v < N_VIALS):
            raise ValueError(f"floor_override vial must be in 0..{N_VIALS - 1}, got {v}")
        floor[v] = max(DEFAULT_RAW_FLOOR, int(value))
    return floor


def _sample_stats(samples: "np.ndarray") -> dict:
    """Per-vial median / population SD / valid count of an ``(n, N_VIALS)``
    sample array, ignoring NaN (dropped) rows. The shape every calibration
    read primitive returns; a vial with no valid sample reports NaN."""
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


@dataclass(frozen=True)
class ODReading:
    """Structured result of one enhanced OD acquisition cycle.

    Every field is a length-``N_VIALS`` list:

    - ``calibrated`` — OD600; ``NaN`` when the vial was ``dropped`` or
      ``out_of_range``.
    - ``raw`` — the signal actually fed to the calibration: LED-on, minus
      the dark for a vial whose curve is dark-subtracted; ``NaN`` when
      dropped.
    - ``dark`` — this cycle's aggregated LED-off reading; ``NaN`` when no
      dark read was taken, or it dropped.
    - ``n_valid`` — surviving samples per vial after NaN/garble rejection.
    - ``flags`` — per vial, one of ``"ok" | "out_of_range" | "dropped"``.

    And, for re-applying a better curve after the fact (per-organism OD
    profiles), the counts before any interpretation:

    - ``lit`` — aggregated LED-on reading.
    - ``dark_used`` — the dark actually subtracted (this cycle's, or the
      vial's last good one if it dropped); ``NaN`` where nothing was.
    - ``dark_n_valid`` — surviving dark samples per vial.
    - ``dark_subtracted`` — per vial, whether ``raw = lit - dark_used``.
    - ``led_power`` — the LED power of the lit reads.
    """

    calibrated: list[float]
    raw: list[float]
    dark: list[float]
    n_valid: list[int]
    flags: list[str]
    lit: Optional[list[float]] = None
    dark_used: Optional[list[float]] = None
    dark_n_valid: Optional[list[int]] = None
    dark_subtracted: Optional[list[bool]] = None
    led_power: Optional[int] = None


log = logging.getLogger(__name__)


def od_reading_from_samples(
    lit,
    lit_n_valid,
    dark,
    dark_n_valid,
    mask,
    od_cal: Optional[np.ndarray],
    last_good_dark,
    led_power: int,
) -> tuple[ODReading, np.ndarray]:
    """One acquisition's aggregated counts -> an :class:`ODReading`, per
    vial. Pure; shared by :class:`SerialManager` and the mock so both apply
    the same dark rule, range guard and flags.

    ``mask[v]`` says vial ``v``'s curve wants dark-subtracted signal. Its
    dark is this cycle's reading, else its last good one; a masked vial
    with NEITHER is ``dropped`` -- subtracting 0 would feed a dark-
    subtracted curve a signal ~1200 counts high and call it "ok".
    ``od_cal`` is the composed per-vial curve (``None`` = raw passthrough).

    Returns ``(reading, updated last_good_dark)``."""
    lit = np.asarray(lit, dtype=float)
    lit_n_valid = np.asarray(lit_n_valid, dtype=int)
    dark = np.asarray(dark, dtype=float)
    dark_n_valid = np.asarray(dark_n_valid, dtype=int)
    mask = np.asarray(mask, dtype=bool)
    last_good_dark = np.asarray(last_good_dark, dtype=float)

    good_dark = np.isfinite(dark)
    new_last_good = np.where(good_dark, dark, last_good_dark)
    dark_used = np.where(mask, np.where(good_dark, dark, last_good_dark), np.nan)
    signal = np.where(mask, lit - dark_used, lit)

    if od_cal is not None:
        a, b, c, d = od_cal[0], od_cal[1], od_cal[2], od_cal[3]
        with np.errstate(invalid="ignore", divide="ignore"):
            in_domain = (signal > a) & (signal < b)
            od_all = c - np.log10((b - a) / (signal - a) - 1.0) / d
    else:
        in_domain = np.ones(N_VIALS, dtype=bool)
        od_all = signal  # no calibration -> raw passthrough

    calibrated: list[float] = []
    raw_out: list[float] = []
    flags: list[str] = []
    for v in range(N_VIALS):
        no_signal = lit_n_valid[v] <= 0 or not np.isfinite(lit[v])
        no_dark = bool(mask[v]) and not np.isfinite(dark_used[v])
        if no_signal or no_dark:
            flags.append("dropped")
            calibrated.append(float("nan"))
            raw_out.append(float("nan"))
            continue
        raw_out.append(float(signal[v]))
        if od_cal is not None and not in_domain[v]:
            flags.append("out_of_range")
            calibrated.append(float("nan"))
            continue
        flags.append("ok")
        calibrated.append(float(od_all[v]))

    reading = ODReading(
        calibrated=calibrated,
        raw=raw_out,
        dark=[float(x) for x in dark],
        n_valid=[int(x) for x in lit_n_valid],
        flags=flags,
        lit=[float(x) for x in lit],
        dark_used=[float(x) for x in dark_used],
        dark_n_valid=[int(x) for x in dark_n_valid],
        dark_subtracted=[bool(x) for x in mask],
        led_power=int(led_power),
    )
    return reading, new_last_good


class OdCurveMixin:
    """The live OD curve, composed PER VIAL (per-organism OD profiles).

    ``_od_cal_default`` / ``_od_cal_dark_subtracted`` are the machine's
    default curve (``OD_cal.txt`` and its sidecar). An experiment's organism
    profile is written over ITS vials' columns by :meth:`assign_od_curve`
    and taken back by :meth:`release_od_curve`. ``_od_cal_base`` is the
    composed curve before any per-run blank; ``od_cal`` is that plus the
    blank's row-2 re-anchors. One OD read serves all sixteen vials, which
    is why an E. coli run and a Pichia run can only share a tick this way --
    and why dark subtraction is per vial too (``_od_ds_by_vial``).

    Shared by :class:`SerialManager` and the mock; needs ``self._lock``."""

    def _init_od_curve_state(self) -> None:
        self.od_cal: Optional[np.ndarray] = None
        self._od_cal_default: Optional[np.ndarray] = None
        self._od_cal_base: Optional[np.ndarray] = None
        # The DEFAULT curve's flag (OD_cal.meta.json); per vial below.
        self._od_cal_dark_subtracted = False
        self._od_ds_by_vial = np.zeros(N_VIALS, dtype=bool)
        # Per-vial last good dark reading: a dropped dark read in a dark-
        # subtracted acquisition falls back to it (see od_reading_from_samples).
        self._last_good_dark = np.full(N_VIALS, np.nan)

    def _set_default_od_curve_locked(self, od_cal: np.ndarray, dark_subtracted: bool) -> None:
        """(Re)load the default curve. Every vial reverts to it: any assigned
        profile curve and any per-run blank are gone."""
        self._od_cal_default = np.array(od_cal, dtype=float)
        self._od_cal_base = self._od_cal_default.copy()
        self.od_cal = self._od_cal_default.copy()
        self._od_cal_dark_subtracted = bool(dark_subtracted)
        self._od_ds_by_vial = np.full(N_VIALS, bool(dark_subtracted))

    @property
    def od_cal_dark_subtracted(self) -> bool:
        """True when the DEFAULT OD curve's sidecar records dark-subtracted
        signal. A vial running an organism profile has its own flag:
        :attr:`od_dark_subtracted_by_vial`."""
        return self._od_cal_dark_subtracted

    @property
    def od_dark_subtracted_by_vial(self) -> list[bool]:
        return [bool(x) for x in self._od_ds_by_vial]

    @staticmethod
    def _check_vials(vials) -> list[int]:
        out = []
        for vial in vials:
            v = int(vial)
            if not (0 <= v < N_VIALS):
                raise ValueError(f"vial must be in 0..{N_VIALS - 1}, got {v}")
            out.append(v)
        return out

    def assign_od_curve(self, vials, rows, dark_subtracted: bool) -> None:
        """Give ``vials`` the columns of ``rows`` (a 4x16 curve -- an
        organism profile) and its dark-subtraction flag. Replaces those
        vials' curve AND any blank on them; other vials are untouched."""
        rows = np.asarray(rows, dtype=float)
        if rows.shape != (4, N_VIALS):
            raise ValueError(f"rows shape {rows.shape}, expected (4, {N_VIALS})")
        vials = self._check_vials(vials)
        with self._lock:
            if self._od_cal_base is None:
                raise RuntimeError("assign_od_curve requires a loaded OD calibration")
            base = self._od_cal_base.copy()
            od_cal = self.od_cal.copy()
            ds = self._od_ds_by_vial.copy()
            for v in vials:
                base[:, v] = rows[:, v]
                od_cal[:, v] = rows[:, v]
                ds[v] = bool(dark_subtracted)
            self._od_cal_base, self.od_cal, self._od_ds_by_vial = base, od_cal, ds

    def release_od_curve(self, vials) -> None:
        """Return ``vials`` to the default curve (and drop their blank)."""
        vials = self._check_vials(vials)
        with self._lock:
            if self._od_cal_default is None:
                return
            base = self._od_cal_base.copy()
            od_cal = self.od_cal.copy()
            ds = self._od_ds_by_vial.copy()
            for v in vials:
                base[:, v] = self._od_cal_default[:, v]
                od_cal[:, v] = self._od_cal_default[:, v]
                ds[v] = self._od_cal_dark_subtracted
            self._od_cal_base, self.od_cal, self._od_ds_by_vial = base, od_cal, ds

    def apply_od_blank(self, c_run_by_vial: dict) -> None:
        """Apply a per-run OD blank re-anchor (CALIBRATION_PROTOCOL §5.4 /
        §10.1) to the in-memory calibration: set row 2 (inflection OD) to
        ``c_run`` for each given vial. Rows 0, 1 and 3 are never touched, and
        no calibration file is modified — the persisted artefact is the run's
        ``od_blank.json``. Idempotent per vial; vials not listed keep their
        current row-2 value. ``clear_od_blank`` restores the vial's curve."""
        with self._lock:
            if self.od_cal is None:
                raise RuntimeError("apply_od_blank requires a loaded OD calibration")
            od_cal = self.od_cal.copy()
            for vial, c_run in c_run_by_vial.items():
                v = self._check_vials([vial])[0]
                od_cal[2, v] = float(c_run)
            self.od_cal = od_cal

    def clear_od_blank(self, vials=None) -> None:
        """Restore row 2 of the vial's curve before any blank -- the curve
        ASSIGNED to it (its organism profile), not the default -- for every
        vial, or only ``vials``. No-op without a calibration.

        Per-vial because a per-run blank belongs to one run: with parallel
        experiments, stopping one must restore ITS vials' row 2 and leave the
        other run's re-anchor alone, or that run's OD silently reverts to the
        offset curve mid-experiment."""
        with self._lock:
            if self._od_cal_base is None:
                return
            if vials is None:
                self.od_cal = self._od_cal_base.copy()
                return
            od_cal = self.od_cal.copy()
            for v in self._check_vials(vials):
                od_cal[2, v] = self._od_cal_base[2, v]
            self.od_cal = od_cal

    def _resolve_dark_plan_locked(self, dark_subtract, n_dark: int) -> tuple[np.ndarray, int]:
        """``(mask, n_dark)`` for one acquisition.

        ``dark_subtract``: ``None`` = auto, each vial as its curve wants
        (a dark read is then forced when any vial needs one); a bool or a
        16-list = explicit, and asking for dark subtraction on a vial whose
        curve was fit on raw signal is a hard error (SPEC §19.2 /
        CALIBRATION_PROTOCOL §13) -- silently wrong OD otherwise. An
        explicit request with ``n_dark == 0`` subtracts nothing (there is
        no dark to subtract), as before per-vial curves."""
        if dark_subtract is None:
            mask = self._od_ds_by_vial.copy()
            if mask.any():
                n_dark = max(1, n_dark)
            return mask, n_dark
        if isinstance(dark_subtract, (list, tuple, np.ndarray)):
            mask = np.asarray(dark_subtract, dtype=bool)
            if mask.shape != (N_VIALS,):
                raise ValueError(f"dark_subtract list must have {N_VIALS} entries")
        else:
            mask = np.full(N_VIALS, bool(dark_subtract))
        if n_dark <= 0:
            return np.zeros(N_VIALS, dtype=bool), n_dark
        if self.od_cal is not None:
            bad = np.flatnonzero(mask & ~self._od_ds_by_vial)
            if bad.size:
                raise ValueError(
                    "dark_subtract=True requires an OD calibration marked "
                    "dark_subtracted in OD_cal.meta.json; the loaded "
                    "calibration is not"
                    + ("" if bad.size == N_VIALS else f" on vials {bad.tolist()}")
                )
        return mask, n_dark


class SerialManager(OdCurveMixin):
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
        # The true thermistor ADC counts of the most recent temperature read
        # (NaN until the first, or for a dropped read). The public reads
        # return °C; the Tier 3 thermistor wizard needs the counts themselves.
        self.last_temperature_raw = np.full(N_VIALS, np.nan)

        self.temp_cal: Optional[np.ndarray] = None
        # The OD curve, per vial: default + organism profiles + blanks.
        self._init_od_curve_state()

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
            self._set_default_od_curve_locked(od_cal, dark_subtracted)

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

    def _read_temperature_raw_locked(self) -> np.ndarray:
        """One xr/temp transaction: re-send the current setpoints, return the
        16 raw thermistor ADC counts (NaN on a dropped read). Records them as
        ``last_temperature_raw``."""
        body = self._format_csv(self.temp_setpoint_raw)
        self._write(PREFIX_TEMPERATURE, body)
        values = self._read_response(RESPONSE_TEMP)
        if values is None:
            raw = np.full(N_VIALS, np.nan)
        else:
            raw = np.asarray(values, dtype=float)
        self.last_temperature_raw = raw
        return raw

    def _read_temperature_locked(self) -> list[float]:
        raw = self._read_temperature_raw_locked()
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

        ``led_power`` may be one power for all sixteen LEDs or a 16-list:
        with parallel experiments a blank's dark read must darken only its
        own run's vials, never another run's mid-experiment.

        Returns ``{"median": [...16], "sd": [...16], "n_valid": [...16]}``;
        a vial with zero surviving samples reports NaN median/sd."""
        leds = led_power_vector(led_power)
        n = max(1, int(n_samples))
        with self._lock:
            body = self._format_csv(leds)
            samples = self._collect_od_reads(body, n)
        return _sample_stats(samples)

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
        """Acquire one OD cycle with averaging, per-vial dark subtraction and
        a per-vial calibration range guard. See :class:`ODReading`.

        ``dark_subtract``: ``None`` follows each vial's curve (the sensor
        loop's setting: an organism profile may be dark-subtracted while the
        default curve is not); a bool or 16-list is explicit, and asking for
        it on a vial whose curve is not dark-subtracted raises
        ``ValueError`` -- mixing the two yields silently wrong OD.

        ``record_dark`` takes the dark read even when no vial subtracts it,
        so the logged raw counts can be re-interpreted by a dark-subtracted
        curve later. The dark phase comes FIRST, so the LED is left on
        between cycles exactly as before."""
        with self._lock:
            return self._read_od_enhanced_locked(
                led_power, n_samples, dark_subtract, n_dark, agg, record_dark,
            )

    def _read_od_enhanced_locked(
        self,
        led_power: int,
        n_samples: int,
        dark_subtract,
        n_dark: int,
        agg: str,
        record_dark: bool = False,
    ) -> ODReading:
        led = int(np.clip(led_power, 0, OD_LED_MAX))
        n_samples = max(1, int(n_samples))
        mask, n_dark = self._resolve_dark_plan_locked(dark_subtract, max(0, int(n_dark)))

        # --- Dark phase (LED off). The photodiode still responds, so each
        # dark read is a full request/response we must drain. ---
        if n_dark > 0 and (mask.any() or record_dark):
            dark_body = self._format_csv(np.zeros(N_VIALS, dtype=int))
            dark_samples = self._collect_od_reads(dark_body, n_dark)
            dark_agg, dark_n_valid = self._aggregate_samples(dark_samples, agg)
        else:
            dark_agg = np.full(N_VIALS, np.nan)
            dark_n_valid = np.zeros(N_VIALS, dtype=int)

        # --- Light phase ---
        light_body = self._format_csv(np.full(N_VIALS, led))
        light_samples = self._collect_od_reads(light_body, n_samples)
        signal_on, n_valid = self._aggregate_samples(light_samples, agg)

        reading, self._last_good_dark = od_reading_from_samples(
            signal_on, n_valid, dark_agg, dark_n_valid, mask,
            self.od_cal, self._last_good_dark, led,
        )
        return reading

    # ------------------------------------------------------------------
    # Public reads
    # ------------------------------------------------------------------

    def read_temperature(self) -> list[float]:
        with self._lock:
            return self._read_temperature_locked()

    def read_temperature_with_raw(self) -> tuple[list[float], list[float]]:
        """``(°C, raw ADC counts)`` from ONE transaction under one lock hold,
        so a calibration reload can never land between the two halves. °C
        is the raw counts again when no calibration is loaded."""
        with self._lock:
            raw = self._read_temperature_raw_locked()
            if self.temp_cal is None:
                return raw.tolist(), raw.tolist()
            return self._temp_raw_to_C(raw).tolist(), raw.tolist()

    def collect_temp_raw(self, n_samples: int = 5) -> dict:
        """Tier 3 thermistor-wizard read primitive (CALIBRATION_PROTOCOL
        §8.1), the temperature twin of :meth:`collect_od_raw`: ``n_samples``
        xr/temp transactions at the CURRENT setpoints (so capturing never
        changes what the heaters are doing), reduced to per-vial NaN-aware
        statistics of the raw thermistor ADC counts. No calibration is
        applied -- the fit is what produces one.

        Returns ``{"median": [...16], "sd": [...16], "n_valid": [...16]}``."""
        n = max(1, int(n_samples))
        with self._lock:
            samples = np.vstack([
                self._read_temperature_raw_locked() for _ in range(n)
            ])
        return _sample_stats(samples)

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

    def set_temperature_raw(
        self, setpoints: list[int], floor_override: Optional[dict] = None,
    ) -> list[float]:
        """Escape hatch for calibration wizard and low-level debugging.

        Sends 16 raw setpoint integers verbatim as the `xr` command payload.
        The setpoint convention is INVERTED — lower value = hotter target.
        xr=0 requests ~82 C (drives heater to max), xr=4095 = off.

        Enforces a per-vial floor derived from MAX_SAFE_TEMP_C (or a
        conservative default of DEFAULT_RAW_FLOOR if no calibration is
        loaded). Setpoints below the floor are clipped up — you cannot
        send something hotter than the safety cap via this method.

        ``floor_override`` (``{vial: raw_floor}``) replaces the floor for
        the listed vials only, and is itself clamped to at least
        DEFAULT_RAW_FLOOR. It exists for one caller: the Tier 3 thermistor
        wizard retargeting a vial whose INSTALLED calibration is the thing
        under test, with a floor derived from that vial's own reference-
        thermometer readings instead (see calibration_service)."""
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
        """Fire one pump. A single-bit :meth:`pump_mask_command`; manual
        pumping and the calibration wizard use this."""
        # CLAUDE.md: influx vial N -> bit N (2^N), efflux vial N -> bit N+16 (2^(N+16)).
        self.pump_mask_command(1 << pump_index(vial, direction), seconds)

    def pump_mask_command(self, mask: int, seconds: float) -> None:
        """One ``st<mask>,0,<seconds>, !`` frame: every pump in ``mask`` runs
        concurrently for ``seconds`` (FLUIDICS_FIRMWARE_AUDIT.md FW-2)."""
        check_mask(mask)
        if seconds < 0:
            raise ValueError(f"seconds must be >= 0, got {seconds}")
        # 2016 firmware accepts integer seconds (mac_original used %d).
        # round() preserves 0.5+ -> 1; sub-half-second requests round to 0
        # and would silently no-op, so warn the caller rather than fire blind.
        whole_seconds = int(round(seconds))
        if seconds > 0 and whole_seconds == 0:
            log.warning(
                "pump frame mask=%s: %.3fs rounds to 0s — no pump will fire. "
                "Sub-second pump times are not supported by the legacy firmware.",
                format(mask, "b"), seconds,
            )
            return
        with self._lock:
            self._write(PREFIX_FLUIDICS, self._pump_frame_body(mask, whole_seconds))

    def pump_frames(self, frames) -> None:
        """Write a pump schedule (``fluidics.plan_dilution_frames``) as
        consecutive frames under one hold of the bus lock, so no other
        command -- a manual pump from another thread -- lands mid-schedule.

        Every frame is validated before the first is written. On a write
        failure at frame k, raises ``FluidicsDispatchError(frames_sent=k)``:
        frames 0..k-1 are already queued in the firmware and cannot be
        recalled (FW-11)."""
        frames = [f if isinstance(f, Frame) else Frame(*f) for f in frames]
        with self._lock:
            for sent, frame in enumerate(frames):
                try:
                    self._write(
                        PREFIX_FLUIDICS,
                        self._pump_frame_body(frame.mask, frame.seconds),
                    )
                except Exception as exc:
                    raise FluidicsDispatchError(
                        f"pump frame {sent + 1}/{len(frames)} failed: {exc}",
                        frames_sent=sent,
                    ) from exc

    @staticmethod
    def _pump_frame_body(mask: int, whole_seconds: int) -> str:
        # The middle field is always 0: FW-8 found it inert over 12 s, and
        # probing it risks an unstoppable repeating pump (audit §6.4).
        return f"{mask:b},0,{whole_seconds},"

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
