"""server/app.py — Flask + flask-socketio server for the eVOLVER web GUI.

Phase 1 MVP per SPEC §4 (architecture), §6 (REST API), §7 (WebSocket
events), and §10 (shutdown handler). A single SerialManager instance
owns the RS485 link; a background thread reads OD and temperature every
10 seconds and broadcasts `sensor_update` events to every connected
browser.

Run with simulated hardware (no eVOLVER required):
    python server/app.py --mock

Run on the RPi against the real RS485 bus:
    python server/app.py
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import math
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from flask import Flask, jsonify, render_template, request
from flask.json.provider import DefaultJSONProvider
from flask_socketio import SocketIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_logger import DataLogger  # noqa: E402
from experiment_engine import (  # noqa: E402
    ConflictError,
    ExperimentEngine,
    ExperimentStatus,
    InvalidExperimentStateError,
)
from mock_serial_manager import MockSerialManager  # noqa: E402
from serial_manager import HEATER_OFF_SETPOINT, MAX_SAFE_TEMP_C  # noqa: E402
from watchdog import Watchdog  # noqa: E402

N_VIALS = 16
STIR_MAX = 15
PUMP_MAX_SECONDS = 60.0  # arbitrary safety ceiling for manual pump bursts
# Temperature is set in Celsius via /api/actuators/temperature; raw `xr`
# integers are an internal detail of SerialManager (see SPEC.md §5/§10).
T_MIN_REQUEST_C = 22.0  # ambient — there is no active cooling
T_MAX_REQUEST_C = MAX_SAFE_TEMP_C

log = logging.getLogger("evolver.app")


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CAL_DIR = PROJECT_ROOT / "calibration"
TEMP_CAL_PATH = CAL_DIR / "temp_calibration.txt"
OD_CAL_PATH = CAL_DIR / "OD_cal.txt"
EXPERIMENTS_DIR = PROJECT_ROOT / "experiments"

SENSOR_LOOP_INTERVAL_SECONDS = 10.0
OD_LED_POWER = 2125  # CLAUDE.md: standard LED power for OD reads
WATCHDOG_TIMEOUT_MINUTES = 10  # PILOT: shortened from 30 (SPEC §10) for faster fault detection during first hardware bring-up; restore to 30 after pilot
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5000

# Minimal inline dashboard. Used until frontend/templates/index.html
# exists, so the server is testable end-to-end with no frontend files
# checked in.
INDEX_PLACEHOLDER_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>eVOLVER</title>
<style>
body{font-family:system-ui,sans-serif;margin:20px;color:#222}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;max-width:760px}
.vial{padding:10px;border:1px solid #ccc;border-radius:6px}
.vial h3{margin:0 0 4px;font-size:13px;color:#555}
.v{font-family:ui-monospace,monospace;font-size:13px}
.muted{color:#888}
</style></head>
<body>
<h1>eVOLVER live dashboard</h1>
<div class="muted">last update: <span id="ts">never</span></div>
<div class="grid" id="grid"></div>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<script>
const g=document.getElementById('grid');
for(let i=0;i<16;i++){g.insertAdjacentHTML('beforeend',
 `<div class="vial"><h3>Vial ${i}</h3>
   <div class="v">T: <span id="t${i}">—</span> °C</div>
   <div class="v">OD: <span id="o${i}">—</span></div></div>`);}
const socket=io();
socket.on('sensor_update',m=>{
 document.getElementById('ts').textContent=m.timestamp;
 m.temperature.calibrated.forEach((t,i)=>{
   document.getElementById('t'+i).textContent=Number.isFinite(t)?t.toFixed(2):'—';});
 m.od.calibrated.forEach((o,i)=>{
   document.getElementById('o'+i).textContent=Number.isFinite(o)?o.toFixed(3):'—';});});
</script></body></html>"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# SerialManager.read_temperature / read_od return calibrated values (when
# calibration is loaded) and discard the raw ADC reading. The API contract
# (SPEC §6) wants both, so we invert the calibration here. Both calibrations
# are closed-form invertible — linear for temp, 4-parameter logistic for OD —
# so raw recovery is exact.

def _temp_C_to_raw(temp_C, temp_cal) -> list[float]:
    slope = temp_cal[0]
    intercept = temp_cal[1]
    return ((np.asarray(temp_C) - intercept) / slope).tolist()


def _od_to_raw(od, od_cal) -> list[float]:
    mn, mx, c, d = od_cal[0], od_cal[1], od_cal[2], od_cal[3]
    return (mn + (mx - mn) / (1.0 + np.power(10.0, d * (c - np.asarray(od))))).tolist()


def _validate_int_array(values, n: int, lo: int, hi: int, name: str):
    """Return an error string if `values` is not a list of `n` ints in [lo, hi], else None."""
    if not isinstance(values, list):
        return f"'{name}' must be a list of {n} integers"
    if len(values) != n:
        return f"'{name}' must have exactly {n} entries, got {len(values)}"
    for i, v in enumerate(values):
        # bool is a subclass of int; reject it explicitly
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return f"'{name}[{i}]' must be a number"
        if int(v) != v:
            return f"'{name}[{i}]' must be an integer"
        if not (lo <= int(v) <= hi):
            return f"'{name}[{i}]'={v} out of range [{lo}, {hi}]"
    return None


def _validate_float_array(values, n: int, lo: float, hi: float, name: str):
    """Return an error string if `values` is not a list of `n` numbers in [lo, hi], else None."""
    if not isinstance(values, list):
        return f"'{name}' must be a list of {n} numbers"
    if len(values) != n:
        return f"'{name}' must have exactly {n} entries, got {len(values)}"
    for i, v in enumerate(values):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return f"'{name}[{i}]' must be a number"
        if not (lo <= float(v) <= hi):
            return f"'{name}[{i}]'={v} out of range [{lo}, {hi}]"
    return None


class AppState:
    def __init__(self, manager, temp_cal, od_cal, data_logger) -> None:
        self.manager = manager
        self.temp_cal = temp_cal
        self.od_cal = od_cal
        self.data_logger: DataLogger = data_logger
        self.watchdog: Watchdog | None = None  # set after socketio is created
        self.engine: ExperimentEngine | None = None  # set after socketio is created
        self.sensor_thread_stop = threading.Event()
        self.shutdown_done = threading.Event()


def _read_temperature_pair(state: AppState) -> dict:
    values = state.manager.read_temperature()
    if state.temp_cal is not None:
        return {"calibrated": values, "raw": _temp_C_to_raw(values, state.temp_cal)}
    return {"calibrated": values, "raw": list(values)}


def _read_od_pair(state: AppState) -> dict:
    values = state.manager.read_od(OD_LED_POWER)
    if state.od_cal is not None:
        return {"calibrated": values, "raw": _od_to_raw(values, state.od_cal)}
    return {"calibrated": values, "raw": list(values)}


# --- NaN/Inf JSON safety ----------------------------------------------------
# NaN and Infinity are NOT valid JSON. A bare NaN in an emitted payload
# crashes the browser's Socket.IO parser ("parse error" -> reconnect storm)
# and corrupts any jsonify() response that contains one. The control loop,
# turbidostat, and CSV logger already treat NaN as a sensor-failure signal;
# these shims make the wire format agree, at every serialization boundary.
def _json_safe(obj):
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


class _SafeJSON:
    @staticmethod
    def dumps(obj, **kw):
        return json.dumps(_json_safe(obj), **kw)

    @staticmethod
    def loads(s, **kw):
        return json.loads(s, **kw)


class _SafeJSONProvider(DefaultJSONProvider):
    def dumps(self, obj, **kwargs):
        return super().dumps(_json_safe(obj), **kwargs)


def create_app(use_mock: bool):
    if use_mock:
        manager = MockSerialManager()
        log.info("using MockSerialManager (--mock)")
    else:
        from serial_manager import SerialManager  # noqa: WPS433
        manager = SerialManager()
        log.info("using SerialManager (real RS485 hardware)")

    temp_cal = od_cal = None
    if TEMP_CAL_PATH.exists() and OD_CAL_PATH.exists():
        manager.load_calibration(str(TEMP_CAL_PATH), str(OD_CAL_PATH))
        temp_cal = np.genfromtxt(str(TEMP_CAL_PATH), delimiter=",")
        od_cal = np.genfromtxt(str(OD_CAL_PATH), delimiter=",")
        log.info("loaded calibration from %s", CAL_DIR)
    else:
        log.warning(
            "calibration files not found in %s; sensor reads will return raw ADC",
            CAL_DIR,
        )

    data_logger = DataLogger(EXPERIMENTS_DIR)
    state = AppState(
        manager=manager, temp_cal=temp_cal, od_cal=od_cal, data_logger=data_logger
    )

    flask_app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / "frontend" / "templates"),
        static_folder=str(PROJECT_ROOT / "frontend" / "static"),
    )
    flask_app.config["SECRET_KEY"] = os.environ.get(
        "EVOLVER_SECRET_KEY", "dev-not-secret"
    )
    flask_app.json = _SafeJSONProvider(flask_app)
    socketio = SocketIO(
        flask_app,
        cors_allowed_origins="*",
        async_mode="threading",
        json=_SafeJSON,
        ping_interval=25,
        ping_timeout=60,
    )

    # ---------------------- Watchdog (SPEC §10) ------------------------------
    # Runs in its own thread; the sensor loop pets it every cycle. If the
    # sensor loop dies or stalls for >30 min, the watchdog zeros actuators
    # and broadcasts a critical alert to every connected browser.

    def _on_watchdog_trigger(reason: str) -> None:
        socketio.emit(
            "alert",
            {"level": "critical", "message": reason, "timestamp": _now_iso()},
        )

    watchdog = Watchdog(
        serial_manager=manager,
        timeout_minutes=WATCHDOG_TIMEOUT_MINUTES,
        on_trigger=_on_watchdog_trigger,
    )
    state.watchdog = watchdog
    watchdog.start()

    # -------------------- Experiment engine (SPEC §9) ------------------------
    # Owns the turbidostat control loop, per-vial state, and state.json
    # persistence. Driven by sensor_loop's run_cycle() call below; emits
    # `experiment_event` and `alert` over the socketio bus.

    engine = ExperimentEngine(
        serial_manager=manager,
        data_logger=data_logger,
        experiments_root=EXPERIMENTS_DIR,
        on_event=lambda evt: socketio.emit("experiment_event", evt),
        on_alert=lambda evt: socketio.emit("alert", evt),
        temp_cal=temp_cal,
    )
    state.engine = engine

    # ------------------------------ HTTP routes ------------------------------

    @flask_app.route("/")
    def index():
        tpl = PROJECT_ROOT / "frontend" / "templates" / "index.html"
        if tpl.exists():
            return render_template("index.html")
        return INDEX_PLACEHOLDER_HTML

    @flask_app.route("/api/sensors/temperature")
    def api_sensor_temperature():
        pair = _read_temperature_pair(state)
        return jsonify(
            values=pair["calibrated"],
            raw_adc=pair["raw"],
            timestamp=_now_iso(),
        )

    @flask_app.route("/api/sensors/od")
    def api_sensor_od():
        pair = _read_od_pair(state)
        return jsonify(
            values=pair["calibrated"],
            raw_adc=pair["raw"],
            timestamp=_now_iso(),
        )

    @flask_app.route("/api/sensors/all")
    def api_sensor_all():
        t = _read_temperature_pair(state)
        o = _read_od_pair(state)
        return jsonify(
            temperature={"values": t["calibrated"], "raw_adc": t["raw"]},
            od={"values": o["calibrated"], "raw_adc": o["raw"]},
            timestamp=_now_iso(),
        )

    # ------------------------------ Actuator routes --------------------------

    @flask_app.route("/api/actuators/state")
    def api_actuator_state():
        """Current setpoints for sliders to initialise from. Also exposes the
        temperature calibration so the frontend can convert °C ↔ raw setpoint
        per vial. ``temperature_setpoint_raw`` is the raw `xr` integer the
        Arduino last received; the frontend should convert it to Celsius via
        the calibration before displaying. Under the inverted convention
        HEATER_OFF_SETPOINT (4095) means "parked off", not "max heat"."""
        temp_raw = np.asarray(
            getattr(state.manager, "temp_setpoint_raw", np.full(N_VIALS, HEATER_OFF_SETPOINT))
        ).tolist()
        stir = np.asarray(getattr(state.manager, "stir_speed", np.zeros(N_VIALS))).tolist()
        payload = {
            "temperature_setpoint_raw": [int(v) for v in temp_raw],
            "stir": [int(v) for v in stir],
            "limits": {
                "temp_min_c": T_MIN_REQUEST_C,
                "temp_max_c": T_MAX_REQUEST_C,
                "heater_off_setpoint": HEATER_OFF_SETPOINT,
                "stir_max": STIR_MAX,
                "pump_max_seconds": PUMP_MAX_SECONDS,
            },
        }
        if state.temp_cal is not None:
            payload["temp_calibration"] = {
                "slope": state.temp_cal[0].tolist(),
                "intercept": state.temp_cal[1].tolist(),
            }
        return jsonify(payload)

    def _experiment_locks_vial(vial: int) -> tuple[int, str] | None:
        """If the engine is running and `vial` is in its experiment, return
        (vial, experiment_name). Else None. Used by the actuator endpoints
        to block manual control of vials assigned to a live experiment."""
        if state.engine is None or not state.engine.is_running:
            return None
        if vial in state.engine.loaded_vials:
            return (vial, state.engine.loaded_experiment or "")
        return None

    @flask_app.route("/api/actuators/temperature", methods=["POST"])
    def api_set_temperature():
        """Set per-vial target temperatures in Celsius.

        Body: ``{"values_c": [37.0, 37.0, ...]}`` — 16 floats in
        ``[T_MIN_REQUEST_C, T_MAX_REQUEST_C]``. Internally calls
        ``SerialManager.set_temperature_celsius``, which converts to raw
        `xr` setpoints via the loaded calibration. Returns current
        readings in °C plus the raw setpoints actually sent (so the
        frontend can keep its slider position in sync without re-doing
        the calibration math).

        Raw setpoint integers are NOT accepted here — use
        ``/api/actuators/temperature/raw`` (calibration wizard / debug)."""
        body = request.get_json(silent=True) or {}
        values_c = body.get("values_c")
        err = _validate_float_array(
            values_c, N_VIALS, T_MIN_REQUEST_C, T_MAX_REQUEST_C, "values_c"
        )
        if err is not None:
            return jsonify(error=err), 400
        # Block manual override of any experiment-controlled vial. We
        # compare on the raw setpoint stored in the manager (translated
        # from the request's °C via the calibration) so the comparison
        # is exact rather than fuzzy on floating-point Celsius.
        if state.engine is not None and state.engine.is_running:
            locked = state.engine.loaded_vials
            current_raw = np.asarray(
                getattr(
                    state.manager,
                    "temp_setpoint_raw",
                    np.full(N_VIALS, HEATER_OFF_SETPOINT),
                )
            )
            if state.temp_cal is not None:
                slope = state.temp_cal[0]
                intercept = state.temp_cal[1]
                requested_raw = np.rint(
                    (np.asarray(values_c, dtype=float) - intercept) / slope
                ).astype(int)
                for v in locked:
                    if int(requested_raw[v]) != int(current_raw[v]):
                        return jsonify(
                            error=(
                                f"vial {v} is controlled by experiment "
                                f"'{state.engine.loaded_experiment}'"
                            )
                        ), 409
        try:
            current = state.manager.set_temperature_celsius(
                [float(v) for v in values_c]
            )
        except Exception as exc:
            log.exception("set_temperature_celsius failed")
            return jsonify(error=f"set_temperature_celsius failed: {exc}"), 500
        # Echo back the raw setpoints we actually sent — useful for the
        # frontend to confirm and for tests.
        current_raw = np.asarray(
            getattr(state.manager, "temp_setpoint_raw", np.full(N_VIALS, HEATER_OFF_SETPOINT))
        ).tolist()
        return jsonify(
            status="ok",
            current_temp_c=list(current),
            temperature_setpoint_raw=[int(v) for v in current_raw],
        )

    @flask_app.route("/api/actuators/temperature/raw", methods=["POST"])
    def api_set_temperature_raw():
        """Escape hatch for calibration wizard and low-level debugging.

        Body: ``{"setpoints": [<int>, ...]}`` — 16 raw `xr` setpoint
        integers. The convention is INVERTED — lower value = hotter
        target. ``HEATER_OFF_SETPOINT`` (4095) is "off"; ``0`` requests
        ~82 °C (drives heater to max).

        SerialManager enforces a per-vial floor on the integer derived
        from MAX_SAFE_TEMP_C, so even via this endpoint you cannot ask
        for a target hotter than the software cap."""
        body = request.get_json(silent=True) or {}
        setpoints = body.get("setpoints")
        err = _validate_int_array(setpoints, N_VIALS, 0, HEATER_OFF_SETPOINT, "setpoints")
        if err is not None:
            return jsonify(error=err), 400
        if state.engine is not None and state.engine.is_running:
            locked = state.engine.loaded_vials
            current_raw = np.asarray(
                getattr(state.manager, "temp_setpoint_raw", np.full(N_VIALS, HEATER_OFF_SETPOINT))
            )
            for v in locked:
                if int(setpoints[v]) != int(current_raw[v]):
                    return jsonify(
                        error=(
                            f"vial {v} is controlled by experiment "
                            f"'{state.engine.loaded_experiment}'"
                        )
                    ), 409
        try:
            current = state.manager.set_temperature_raw(
                [int(v) for v in setpoints]
            )
        except Exception as exc:
            log.exception("set_temperature_raw failed")
            return jsonify(error=f"set_temperature_raw failed: {exc}"), 500
        current_raw = np.asarray(
            getattr(state.manager, "temp_setpoint_raw", np.full(N_VIALS, HEATER_OFF_SETPOINT))
        ).tolist()
        return jsonify(
            status="ok",
            current_temp_c=list(current),
            temperature_setpoint_raw=[int(v) for v in current_raw],
        )

    @flask_app.route("/api/actuators/stir", methods=["POST"])
    def api_set_stir():
        body = request.get_json(silent=True) or {}
        values = body.get("values")
        err = _validate_int_array(values, N_VIALS, 0, STIR_MAX, "values")
        if err is not None:
            return jsonify(error=err), 400
        if state.engine is not None and state.engine.is_running:
            locked = state.engine.loaded_vials
            current_stir = np.asarray(
                getattr(state.manager, "stir_speed", np.zeros(N_VIALS))
            )
            for v in locked:
                if int(values[v]) != int(current_stir[v]):
                    return jsonify(
                        error=(
                            f"vial {v} is controlled by experiment "
                            f"'{state.engine.loaded_experiment}'"
                        )
                    ), 409
        try:
            state.manager.set_stir([int(v) for v in values])
        except Exception as exc:
            log.exception("set_stir failed")
            return jsonify(error=f"set_stir failed: {exc}"), 500
        return jsonify(status="ok")

    @flask_app.route("/api/actuators/pump", methods=["POST"])
    def api_pump():
        body = request.get_json(silent=True) or {}
        vial = body.get("vial")
        direction = body.get("direction")
        seconds = body.get("seconds")
        if not isinstance(vial, int) or not (0 <= vial < N_VIALS):
            return jsonify(error=f"'vial' must be an integer in 0..{N_VIALS - 1}"), 400
        if direction not in ("influx", "efflux"):
            return jsonify(error="'direction' must be 'influx' or 'efflux'"), 400
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
            return jsonify(error="'seconds' must be a number"), 400
        if not (0 < seconds <= PUMP_MAX_SECONDS):
            return jsonify(error=f"'seconds' must be in (0, {PUMP_MAX_SECONDS}]"), 400
        lock = _experiment_locks_vial(int(vial))
        if lock is not None:
            return jsonify(
                error=f"vial {lock[0]} is controlled by experiment '{lock[1]}'"
            ), 409
        try:
            state.manager.pump_command(int(vial), direction, float(seconds))
        except Exception as exc:
            log.exception("pump_command failed")
            return jsonify(error=f"pump_command failed: {exc}"), 500
        timestamp = _now_iso()
        # No-op if no experiment is running or `vial` is not part of it.
        try:
            state.data_logger.log_pump_event(
                timestamp_iso=timestamp,
                vial=int(vial),
                direction=direction,
                duration_seconds=float(seconds),
            )
        except Exception:
            log.exception("data_logger.log_pump_event failed")
        socketio.emit(
            "experiment_event",
            {
                "type": "pump",
                "vial": int(vial),
                "direction": direction,
                "duration_seconds": float(seconds),
                "timestamp": timestamp,
            },
        )
        return jsonify(status="ok")

    @flask_app.route("/api/actuators/emergency_stop", methods=["POST"])
    def api_emergency_stop():
        """Zero pumps, stir, and heater setpoints immediately. SPEC §10.
        If an experiment is running, also transitions it to ERROR so the
        engine stops firing pumps on its own."""
        log.warning("emergency_stop requested via API")
        timestamp = _now_iso()
        try:
            state.manager.emergency_shutdown()
        except Exception as exc:
            log.exception("emergency_stop failed")
            return jsonify(error=f"emergency_shutdown failed: {exc}"), 500
        # Notify every connected browser so a stop from one tab is visible
        # in all the others (SPEC §7 alert event).
        socketio.emit(
            "alert",
            {
                "level": "critical",
                "message": "Emergency stop — all actuators zeroed",
                "timestamp": timestamp,
            },
        )
        if state.engine is not None:
            try:
                state.engine.handle_emergency_stop()
            except Exception:
                log.exception("engine.handle_emergency_stop failed")
        return jsonify(status="ok", message="All actuators zeroed")

    # ----------------------- Experiment routes (SPEC §6) ---------------------
    # All endpoints delegate to ExperimentEngine. The engine owns lifecycle,
    # the DataLogger owns CSV writing.

    @flask_app.route("/api/experiments", methods=["GET"])
    def api_experiments_list():
        return jsonify(experiments=state.engine.list_experiments())

    @flask_app.route("/api/experiments/create", methods=["POST"])
    def api_experiments_create():
        body = request.get_json(silent=True) or {}
        try:
            config = state.engine.create_experiment(
                name=body.get("name"),
                mode=body.get("mode", "turbidostat"),
                vials=body.get("vials"),
                parameters=body.get("params") or body.get("parameters") or {},
                calibration=body.get("calibration") or {},
                notes=body.get("notes", ""),
                media=body.get("media"),
            )
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except FileExistsError as exc:
            return jsonify(error=str(exc)), 409
        except InvalidExperimentStateError as exc:
            return jsonify(error=str(exc)), 409
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 409
        return jsonify(status="created", name=config["name"])

    @flask_app.route("/api/experiments/<name>/start", methods=["POST"])
    def api_experiments_start(name):
        try:
            state.engine.start_experiment(name)
        except InvalidExperimentStateError as exc:
            return jsonify(error=str(exc)), 409
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 500
        return jsonify(status="running", name=name)

    @flask_app.route("/api/experiments/<name>/stop", methods=["POST"])
    def api_experiments_stop(name):
        if state.engine.loaded_experiment != name:
            return jsonify(error=f"experiment '{name}' is not loaded"), 400
        stopped = state.engine.stop_experiment(reason="manual")
        if stopped is None:
            return jsonify(error=f"experiment '{name}' is not running"), 400
        return jsonify(
            status="stopped",
            name=stopped,
            message=f"experiment '{stopped}' stopped — actuators zeroed for its vials",
        )

    @flask_app.route("/api/experiments/<name>/status", methods=["GET"])
    def api_experiments_status(name):
        if state.engine.loaded_experiment == name:
            return jsonify(state.engine.status())
        # Look on disk for stopped/created experiments not currently loaded.
        state_path = EXPERIMENTS_DIR / name / "state.json"
        config_path = EXPERIMENTS_DIR / name / "config.json"
        if state_path.is_file():
            try:
                return jsonify(json.loads(state_path.read_text(encoding="utf-8")))
            except Exception as exc:
                log.exception("failed to read %s", state_path)
                return jsonify(error=f"failed to read state: {exc}"), 500
        if config_path.is_file():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception as exc:
                return jsonify(error=f"failed to read config: {exc}"), 500
            return jsonify(name=name, status="stopped", config=config)
        return jsonify(error=f"experiment '{name}' not found"), 404

    @flask_app.route("/api/experiments/<name>/data", methods=["GET"])
    def api_experiments_data(name):
        try:
            vial = int(request.args.get("vial", "-1"))
        except (TypeError, ValueError):
            return jsonify(error="'vial' must be an integer"), 400
        parameter = request.args.get("parameter", "")
        last_n_arg = request.args.get("last_n")
        try:
            last_n = int(last_n_arg) if last_n_arg else None
        except ValueError:
            return jsonify(error="'last_n' must be an integer"), 400
        try:
            data = state.engine.get_data(name, vial=vial, parameter=parameter, last_n=last_n)
        except FileNotFoundError as exc:
            return jsonify(error=str(exc)), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(data)

    @flask_app.route("/api/experiments/<name>", methods=["DELETE"])
    def api_experiments_delete(name):
        try:
            state.engine.delete_experiment(name)
        except InvalidExperimentStateError as exc:
            return jsonify(error=str(exc)), 409
        except FileNotFoundError as exc:
            return jsonify(error=str(exc)), 404
        return jsonify(status="deleted", name=name)

    # ------------------------ Maintenance routes -----------------------------
    # Pauses pump execution so the user can refill bottles / empty waste
    # without disrupting the experiment. Sensor reads + CSV logging keep
    # running; pump actions are queued and fire on exit.

    @flask_app.route("/api/maintenance/enter", methods=["POST"])
    def api_maintenance_enter():
        try:
            status_block = state.engine.enter_maintenance()
        except InvalidExperimentStateError as exc:
            return jsonify(error=str(exc)), 409
        return jsonify(status="maintenance", maintenance=status_block)

    @flask_app.route("/api/maintenance/exit", methods=["POST"])
    def api_maintenance_exit():
        try:
            queued = state.engine.exit_maintenance(reason="manual")
        except Exception as exc:
            log.exception("exit_maintenance failed")
            return jsonify(error=str(exc)), 500
        try:
            if queued:
                _execute_queued_pump_actions(queued)
        except Exception:
            log.exception("execute queued pump actions failed on exit")
        return jsonify(
            status="resumed",
            fired=len(queued),
            maintenance=state.engine.status().get("maintenance"),
        )

    @flask_app.route("/api/maintenance/refill", methods=["POST"])
    def api_maintenance_refill():
        body = request.get_json(silent=True) or {}
        bottles = body.get("bottles") or None
        waste = body.get("waste") or {}
        waste_filled_ml = waste.get("filled_ml") if isinstance(waste, dict) else None
        try:
            updated = state.engine.refill_media(
                bottles=bottles, waste_filled_ml=waste_filled_ml,
            )
        except InvalidExperimentStateError as exc:
            return jsonify(error=str(exc)), 409
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(status="ok", **updated)

    # --------------------- Morbidostat-specific routes ----------------------
    # Manual-swap escalation: engine detects growth recovery and emits an
    # alert; the user physically swaps the bottle, then POSTs here to record
    # the new drug concentration (and optional updated bottle contents text).

    @flask_app.route(
        "/api/experiments/<name>/morbidostat/confirm_escalation",
        methods=["POST"],
    )
    def api_morbidostat_confirm_escalation(name):
        body = request.get_json(silent=True) or {}
        vial = body.get("vial")
        if vial is None:
            return jsonify(error="'vial' is required"), 400
        try:
            vial_int = int(vial)
        except (TypeError, ValueError):
            return jsonify(error=f"'vial' must be an integer (got {vial!r})"), 400
        new_drug_conc = body.get("new_drug_conc")
        new_bottle_contents = body.get("new_bottle_contents")
        try:
            result = state.engine.confirm_escalation(
                name=name,
                vial=vial_int,
                new_drug_conc=new_drug_conc,
                new_bottle_contents=new_bottle_contents,
            )
        except ConflictError as exc:
            return jsonify(error=str(exc)), 409
        except InvalidExperimentStateError as exc:
            return jsonify(error=str(exc)), 409
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(status="confirmed", **result)

    # --------------------- WebSocket sensor broadcast loop -------------------

    def _experiment_summary() -> dict | None:
        """Compact experiment status for the sensor_update payload.
        Returns None when nothing is loaded; otherwise a small dict the
        dashboard can read without polling /api/experiments."""
        if state.engine is None or state.engine.loaded_experiment is None:
            return None
        s = state.engine.status()
        media = s.get("media")
        media_summary = None
        if media is not None:
            media_summary = {
                "bottles": [
                    {"id": b["id"], "name": b["name"],
                     "remaining_pct": b["remaining_pct"]}
                    for b in media.get("bottles", [])
                ],
                "waste": (
                    {"filled_pct": media["waste"]["filled_pct"]}
                    if media.get("waste") else None
                ),
            }
        escalation_pending: list[int] = []
        try:
            escalation_pending = state.engine.escalation_pending_vials()
        except Exception:
            log.exception("escalation_pending_vials failed")
        # Trim to the fields the dashboard actually needs at 10 s cadence.
        return {
            "name": s.get("name"),
            "status": s.get("status"),
            "mode": s.get("mode"),
            "vials": s.get("vials", []),
            "elapsed_hours": s.get("elapsed_hours"),
            "media_summary": media_summary,
            "maintenance": s.get("maintenance"),
            "escalation_pending_vials": escalation_pending,
        }

    def _execute_pump_actions(actions, ts_iso: str) -> None:
        """Fire each (vial, PumpAction) tuple via SerialManager, log to
        the DataLogger, and broadcast experiment_event over socketio.

        Called from the sensor_loop with whatever engine.run_cycle returned.
        Each PumpAction maps to two physical pump_command calls (influx
        for pump_time, efflux for pump_time + efflux_extra_seconds)."""
        for vial, action in actions:
            pump_time = action.pump_time
            efflux_time = action.pump_time + action.efflux_extra_seconds
            try:
                state.manager.pump_command(int(vial), "influx", pump_time)
                state.manager.pump_command(int(vial), "efflux", efflux_time)
            except Exception:
                log.exception("pump firing failed for vial %d", vial)
                continue
            for direction, seconds in (("influx", pump_time), ("efflux", efflux_time)):
                try:
                    state.data_logger.log_pump_event(
                        timestamp_iso=ts_iso,
                        vial=int(vial),
                        direction=direction,
                        duration_seconds=float(seconds),
                        od_at_pump=action.average_od,
                    )
                except Exception:
                    log.exception(
                        "log_pump_event %s failed (vial=%d)", direction, vial
                    )
                socketio.emit(
                    "experiment_event",
                    {
                        "type": "pump",
                        "vial": int(vial),
                        "direction": direction,
                        "duration_seconds": float(seconds),
                        "average_od": action.average_od,
                        "timestamp": ts_iso,
                    },
                )

    def _execute_queued_pump_actions(queued) -> None:
        """Variant of _execute_pump_actions for actions returned by
        engine.exit_maintenance() / engine.check_maintenance_timeout().
        Each entry is (vial, PumpAction, original_ts_iso) — we use the
        original timestamp so the pump_log reflects when the controller
        actually decided to fire, not when the user clicked Resume."""
        # Reshape to the (vial, action) form _execute_pump_actions expects,
        # but call once per entry so each uses its own captured timestamp.
        for vial, action, captured_ts in queued:
            _execute_pump_actions([(vial, action)], captured_ts)

    def sensor_loop():
        log.info("sensor loop started (interval=%.1fs)", SENSOR_LOOP_INTERVAL_SECONDS)
        while not state.sensor_thread_stop.is_set():
            tick_start = time.monotonic()
            try:
                ts_iso = _now_iso()
                t = _read_temperature_pair(state)
                o = _read_od_pair(state)

                # log_sensor_cycle is a no-op when no experiment is running;
                # we still call it every tick so the active-vs-idle decision
                # lives in one place (the DataLogger).
                try:
                    state.data_logger.log_sensor_cycle(
                        timestamp_iso=ts_iso,
                        temperature_calibrated=t["calibrated"],
                        temperature_raw=t["raw"],
                        od_calibrated=o["calibrated"],
                        od_raw=o["raw"],
                    )
                except Exception:
                    log.exception("data_logger.log_sensor_cycle failed")

                # Run the experiment control loop (returns [] when not RUNNING).
                pump_actions: list = []
                try:
                    pump_actions = state.engine.run_cycle(
                        ts_iso, t["calibrated"], o["calibrated"]
                    )
                except Exception:
                    log.exception("engine.run_cycle failed")

                # Execute returned pump actions via SerialManager + DataLogger.
                if pump_actions:
                    try:
                        _execute_pump_actions(pump_actions, ts_iso)
                    except Exception:
                        log.exception("execute pump actions failed")

                # Maintenance-mode failsafe: if the user left maintenance
                # active for >30 min, the engine auto-exits and hands us the
                # queued pump actions to fire (with timestamps captured when
                # the controller originally decided them).
                try:
                    queued = state.engine.check_maintenance_timeout()
                    if queued:
                        _execute_queued_pump_actions(queued)
                except Exception:
                    log.exception("maintenance timeout check failed")

                # Broadcast sensor update WITH experiment status so the
                # dashboard can keep its status bar fresh without polling.
                socketio.emit(
                    "sensor_update",
                    {
                        "timestamp": ts_iso,
                        "temperature": {
                            "calibrated": t["calibrated"],
                            "raw": t["raw"],
                        },
                        "od": {
                            "calibrated": o["calibrated"],
                            "raw": o["raw"],
                        },
                        "experiment": _experiment_summary(),
                    },
                )

                # Only pet on a successful sensor read — if the bus is stuck
                # we want the watchdog to actually fire.
                state.watchdog.pet()
            except Exception:
                log.exception("sensor loop tick failed")
            elapsed = time.monotonic() - tick_start
            state.sensor_thread_stop.wait(
                timeout=max(0.0, SENSOR_LOOP_INTERVAL_SECONDS - elapsed)
            )
        log.info("sensor loop stopped")

    sensor_thread = threading.Thread(
        target=sensor_loop, name="sensor-loop", daemon=True
    )
    sensor_thread.start()

    # Recover any in-flight experiment from a previous server lifetime.
    # Runs after the sensor thread has started so the engine's run_cycle
    # ticks will pick up immediately.
    try:
        resumed = state.engine.resume_on_startup()
        if resumed:
            log.info("resumed experiment '%s' from previous server run", resumed)
    except Exception:
        log.exception("resume_on_startup failed")

    # ---------------------- Shutdown handler (SPEC §10) ----------------------

    def shutdown(*_args):
        if state.shutdown_done.is_set():
            return
        state.shutdown_done.set()
        log.info(
            "shutdown: stopping sensor loop, watchdog, engine, and zeroing actuators"
        )
        state.sensor_thread_stop.set()
        if state.watchdog is not None:
            try:
                state.watchdog.stop()
            except Exception:
                log.exception("shutdown: watchdog stop failed")
        # Stop the engine first so it doesn't fire pumps after shutdown
        # begins. engine.stop_experiment internally calls
        # data_logger.deactivate_experiment, so we don't need a separate
        # data_logger.stop_experiment call.
        if state.engine is not None:
            try:
                state.engine.stop_experiment(reason="shutdown")
            except Exception:
                log.exception("shutdown: engine.stop_experiment failed")
        try:
            manager.emergency_shutdown()
        except Exception:
            log.exception("shutdown: emergency_shutdown failed")
        # Real SerialManager has .close(); the mock doesn't.
        close = getattr(manager, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                log.exception("shutdown: serial close failed")

    def _signal_shutdown(_signum, _frame):
        shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_shutdown)
    try:
        signal.signal(signal.SIGTERM, _signal_shutdown)
    except (ValueError, AttributeError):
        # SIGTERM not installable on every platform (notably Windows in some
        # contexts); SIGINT + atexit still cover the common shutdown paths.
        pass
    atexit.register(shutdown)

    return flask_app, socketio


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="eVOLVER web control server")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use MockSerialManager instead of real RS485 (development).",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port.")
    parser.add_argument("--log-level", default="INFO", help="Logging level.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    flask_app, socketio = create_app(use_mock=args.mock)
    socketio.run(
        flask_app,
        host=args.host,
        port=args.port,
        allow_unsafe_werkzeug=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
