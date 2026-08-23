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
import functools
import io
import json
import logging
import math
import os
import shutil
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
)
from flask.json.provider import DefaultJSONProvider
from flask_socketio import SocketIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

import data_export as dx  # noqa: E402
import growth_rate as growth  # noqa: E402
import event_log as evlog  # noqa: E402
from calibration_service import (  # noqa: E402
    CalibrationConflict,
    CalibrationService,
    QCRefusal,
    SUBSYSTEMS as CAL_SUBSYSTEMS,
)
from data_logger import DataLogger  # noqa: E402
from experiment_engine import (  # noqa: E402
    ConflictError,
    ExperimentEngine,
    ExperimentStatus,
    InvalidExperimentStateError,
    compute_pump_quantization,
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
# Server-side export bundles live OUTSIDE experiments/ so they don't get swept
# up as bogus experiments by ExperimentEngine.list_experiments().
EXPORTS_DIR = PROJECT_ROOT / "exports"
# Rotating file logs (SPEC §20.1). Same filesystem as experiments/ on purpose:
# the disk floor that protects the data is the one that must protect the logs.
LOGS_DIR = PROJECT_ROOT / "logs"

SENSOR_LOOP_INTERVAL_SECONDS = 10.0
OD_LED_POWER = 2125  # CLAUDE.md: standard LED power for OD reads

# Low-disk monitor (sensor loop). The Pi's disk is finite and a multi-day run at
# 10 s cadence accumulates many rows; warn before a run fills the disk and dies.
# Checked every N cycles (not every tick) and edge-triggered so it alerts once
# per crossing. Thresholds fire on whichever of free-bytes / free-% trips first.
DISK_CHECK_EVERY_CYCLES = 30  # ~5 min at a 10 s cadence
DISK_WARN_FREE_BYTES = 1 * 1024 ** 3       # 1 GB
DISK_WARN_FREE_PCT = 10.0
DISK_CRITICAL_FREE_BYTES = 256 * 1024 ** 2  # 256 MB
DISK_CRITICAL_FREE_PCT = 3.0
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
        self.cal_service: CalibrationService | None = None  # set in create_app
        self.sensor_thread_stop = threading.Event()
        self.shutdown_done = threading.Event()
        # Low-disk monitor state (sensor loop): throttle counter + edge latches.
        self.disk_check_counter = 0
        self.disk_warned = False
        self.disk_critical = False
        # Observability (SPEC §20). The ring buffer is populated whether or not
        # an experiment is running; bus/vial health are derived from the sensor
        # arrays the loop already reads, so they work while idle too.
        self.event_log = evlog.EventLog(data_logger)
        self.bus_health = evlog.BusHealth()
        self.vial_health = evlog.VialHealth(N_VIALS)
        self.log_writes_suspended = False


def _read_temperature_pair(state: AppState) -> dict:
    values = state.manager.read_temperature()
    if state.temp_cal is not None:
        return {"calibrated": values, "raw": _temp_C_to_raw(values, state.temp_cal)}
    return {"calibrated": values, "raw": list(values)}


def _read_od_pair(state: AppState) -> dict:
    """Read OD for one cycle. Always returns a dict with keys
    ``calibrated``, ``raw``, ``dark``, ``n_valid``, ``flags``.

    When an experiment is RUNNING, use the enhanced acquisition driven by that
    experiment's ``od_acquisition`` config (median-of-N averaging, optional
    per-cycle dark subtraction, per-vial range guard). When idle, fall back to
    the naive single read so standby cycles don't spend bus time on data that
    is never recorded."""
    engine = state.engine
    if engine is not None and engine.is_running:
        r = state.manager.read_od_enhanced(
            OD_LED_POWER, **engine.od_acquisition_params()
        )
        return {
            "calibrated": list(r.calibrated),
            "raw": list(r.raw),
            "dark": list(r.dark),
            "n_valid": list(r.n_valid),
            "flags": list(r.flags),
        }
    # Naive idle path (unchanged conversion) with neutral diagnostics. NaN
    # (timed-out) values are flagged "dropped"; everything else "ok".
    values = state.manager.read_od(OD_LED_POWER)
    raw = _od_to_raw(values, state.od_cal) if state.od_cal is not None else list(values)
    flags = ["ok" if v == v else "dropped" for v in values]  # v != v -> NaN
    return {
        "calibrated": list(values),
        "raw": list(raw),
        "dark": [float("nan")] * N_VIALS,
        "n_valid": [1] * N_VIALS,
        "flags": flags,
    }


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


def _disk_alert_decision(
    free: int, total: int, warned: bool, critical: bool
) -> tuple[str | None, bool, bool]:
    """Pure band + hysteresis decision for the low-disk monitor.

    Three bands (ok / warning / critical) keyed on whichever of free-bytes or
    free-% trips first. Returns ``(alert_level_or_None, new_warned,
    new_critical)``. An alert fires only when crossing UP into a band; latches
    clear when free space recovers to ok, so a genuine re-crossing re-alerts
    while a level hovering at a threshold does not spam. Dropping from critical
    back to warning clears the critical latch silently (no new warning)."""
    free_pct = 100.0 * free / total if total else 0.0
    crit = free < DISK_CRITICAL_FREE_BYTES or free_pct < DISK_CRITICAL_FREE_PCT
    warn = free < DISK_WARN_FREE_BYTES or free_pct < DISK_WARN_FREE_PCT
    if crit:
        if not critical:
            return "critical", True, True
        return None, warned, True
    if warn:
        if not warned:
            return "warning", True, False
        return None, True, False
    return None, False, False


def _event_message(kind: str, payload: dict) -> str:
    """One-line human summary for an experiment event (SPEC §20.2).

    The structured detail still goes to the ``data_json`` column; this is the
    line a researcher reads in the drawer or scanning events.csv six months
    later, so it names the thing that happened rather than the event type.
    """
    vial = payload.get("vial")
    name = payload.get("name") or ""
    if kind == "pump":
        return (
            f"Pump {payload.get('direction', '?')} vial {vial} for "
            f"{payload.get('duration_seconds', 0):.1f}s"
        )
    if kind == "pump_suppressed":
        return (
            f"Pump suppressed for vial {vial}: "
            f"{payload.get('reason', 'unknown reason')}"
        )
    if kind in ("created", "started", "resumed"):
        return f"Experiment '{name}' {kind}"
    if kind == "stopped":
        return f"Experiment '{name}' stopped ({payload.get('reason', 'manual')})"
    if kind == "renamed":
        return f"Experiment renamed: '{payload.get('old')}' -> '{payload.get('new')}'"
    if kind == "metadata_updated":
        return f"Experiment '{name}' metadata updated"
    if kind == "maintenance_entered":
        return f"Maintenance mode entered ({payload.get('reason', 'manual')})"
    if kind == "maintenance_exited":
        return (
            f"Maintenance mode exited ({payload.get('reason', 'manual')}); "
            f"{payload.get('queued_actions', 0)} queued pump action(s)"
        )
    if kind == "refill":
        return f"Media/waste levels updated: {payload.get('bottles') or 'waste only'}"
    if kind == "escalation_proposed":
        return (
            f"Vial {vial} escalation proposed: "
            f"{payload.get('old_drug_conc')} -> {payload.get('new_drug_conc')}"
        )
    if kind == "escalation_confirmed":
        return f"Vial {vial} escalation confirmed: {payload.get('new_drug_conc')}"
    if kind == "od_blank_committed":
        return (
            f"Per-run OD blank committed for '{name}' "
            f"({payload.get('n_vials', '?')} vials, offset up to "
            f"{payload.get('max_offset_removed', '?')} OD removed)"
        )
    if kind == "calibration_installed":
        return (
            f"Calibration installed: {payload.get('subsystem', '?')} "
            f"version {payload.get('version', '?')}"
        )
    if kind == "reconciliation":
        ok = payload.get("within_tolerance")
        return (
            f"Mass reconciliation for '{name}': "
            f"{'within' if ok else 'OUTSIDE'} ±10 %"
        )
    return kind.replace("_", " ")


def _experiment_vials(name: str) -> list[int] | None:
    """Vials of an on-disk experiment from its config.json, or None if the
    experiment directory / config is missing."""
    config_path = EXPERIMENTS_DIR / name / "config.json"
    if not config_path.is_file():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return sorted(int(v) for v in config.get("vials", []))


def _resolve_export_request(parameters, vials, hours, exp_vials: list[int]):
    """Normalize + validate an export request against the experiment's vials.

    ``parameters`` / ``vials`` may each be a comma-string (query param), a list
    (JSON body), or None (=> all). Returns ``(params, vials, hours)``. Raises
    ``ValueError`` on any malformed or out-of-experiment selection (the caller
    maps these to HTTP 400)."""
    if parameters in (None, ""):
        params = list(dx.EXPORT_PARAMETERS)
    elif isinstance(parameters, str):
        params = [p.strip().lower() for p in parameters.split(",") if p.strip()]
    else:
        params = [str(p).strip().lower() for p in parameters]
    if not params:
        params = list(dx.EXPORT_PARAMETERS)
    for p in params:
        if p not in dx.EXPORT_PARAMETERS:
            raise ValueError(f"unknown parameter {p!r}; expected od/temp/pump")

    if vials in (None, ""):
        sel = list(exp_vials)
    elif isinstance(vials, str):
        sel = [int(v) for v in vials.split(",") if v.strip() != ""]
    else:
        sel = [int(v) for v in vials]
    if not sel:
        sel = list(exp_vials)
    extra = sorted(set(sel) - set(exp_vials))
    if extra:
        raise ValueError(
            f"vials {extra} are not part of experiment (vials={exp_vials})"
        )

    if hours in (None, ""):
        h = None
    else:
        h = float(hours)
        if h <= 0:
            raise ValueError(f"hours must be > 0, got {h}")
    # Preserve canonical od/temp/pump order; dedupe vials.
    params = [p for p in dx.EXPORT_PARAMETERS if p in params]
    return params, sorted(set(sel)), h


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
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
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

    # ------------------ Observability funnels (SPEC §20) ---------------------
    # Every alert and every experiment event goes through exactly one of these
    # two functions. They record to the ring buffer + events.csv and only then
    # emit to the browser, so nothing can reach a browser without also being
    # captured, and a repeating fault collapses into one entry with a count.

    # experiment_event "type" -> (level, category). Anything unmapped is
    # recorded as info/experiment rather than dropped.
    _EVENT_KINDS = {
        "created": (evlog.LEVEL_INFO, evlog.CATEGORY_LIFECYCLE),
        "started": (evlog.LEVEL_INFO, evlog.CATEGORY_LIFECYCLE),
        "stopped": (evlog.LEVEL_INFO, evlog.CATEGORY_LIFECYCLE),
        "resumed": (evlog.LEVEL_INFO, evlog.CATEGORY_LIFECYCLE),
        "renamed": (evlog.LEVEL_INFO, evlog.CATEGORY_LIFECYCLE),
        "metadata_updated": (evlog.LEVEL_INFO, evlog.CATEGORY_LIFECYCLE),
        "pump": (evlog.LEVEL_INFO, evlog.CATEGORY_PUMP),
        "pump_suppressed": (evlog.LEVEL_WARNING, evlog.CATEGORY_PUMP),
        "maintenance_entered": (evlog.LEVEL_INFO, evlog.CATEGORY_MAINTENANCE),
        "maintenance_exited": (evlog.LEVEL_INFO, evlog.CATEGORY_MAINTENANCE),
        "refill": (evlog.LEVEL_INFO, evlog.CATEGORY_MEDIA),
        "escalation_proposed": (evlog.LEVEL_INFO, evlog.CATEGORY_ESCALATION),
        "escalation_confirmed": (evlog.LEVEL_INFO, evlog.CATEGORY_ESCALATION),
        "od_blank_committed": (evlog.LEVEL_INFO, evlog.CATEGORY_CALIBRATION),
        "calibration_installed": (evlog.LEVEL_INFO, evlog.CATEGORY_CALIBRATION),
        "reconciliation": (evlog.LEVEL_INFO, evlog.CATEGORY_CALIBRATION),
    }

    def _emit_alert(
        level: str,
        message: str,
        *,
        category: str = evlog.CATEGORY_SYSTEM,
        vial=None,
        data=None,
        dedup_key=None,
        timestamp=None,
    ) -> None:
        """Record an alert, then emit it unless the rate limiter suppressed
        this occurrence as a repeat."""
        entry = state.event_log.record(
            level=level,
            category=category,
            message=message,
            vial=vial,
            data=data,
            dedup_key=dedup_key,
            timestamp=timestamp,
        )
        if entry is not None:
            socketio.emit("alert", entry)

    def _emit_alert_payload(payload: dict) -> None:
        """Adapter for engine/watchdog callbacks that already build a payload."""
        entry = state.event_log.record_alert(payload)
        if entry is not None:
            socketio.emit("alert", entry)

    def _emit_event(payload: dict) -> None:
        """Record an experiment event and mirror it to the browser.

        The socket payload is the caller's original shape -- the dashboard
        already keys off `type`, `vial`, `direction` -- so this stays additive.
        """
        payload = dict(payload or {})
        payload.setdefault("timestamp", _now_iso())
        kind = payload.get("type", "event")
        level, category = _EVENT_KINDS.get(
            kind, (evlog.LEVEL_INFO, "experiment")
        )
        detail = {k: v for k, v in payload.items()
                  if k not in ("type", "timestamp", "vial")}
        state.event_log.record(
            level=level,
            category=category,
            message=_event_message(kind, payload),
            vial=payload.get("vial"),
            data=detail or None,
            timestamp=payload.get("timestamp"),
        )
        if kind == "stopped":
            # A per-run OD blank belongs to one run (SPEC §19.2): restore the
            # pristine calibration so idle reads stop carrying its re-anchor.
            try:
                clear = getattr(state.manager, "clear_od_blank", None)
                if clear is not None:
                    clear()
                    if getattr(state.manager, "od_cal", None) is not None:
                        state.od_cal = np.asarray(state.manager.od_cal)
            except Exception:
                log.exception("clearing per-run OD blank on stop failed")
        socketio.emit("experiment_event", payload)

    def _on_watchdog_trigger(reason: str) -> None:
        _emit_alert(
            "critical", reason,
            category=evlog.CATEGORY_SYSTEM, dedup_key="watchdog",
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
        on_event=_emit_event,
        on_alert=_emit_alert_payload,
        temp_cal=temp_cal,
    )
    state.engine = engine

    # ------------------- Calibration service (SPEC §19) ----------------------
    # Versioned provenance store + the O2/O3/O4 wizard sessions. Bootstrap
    # imports the inherited 2016 .txt files as versioned envelopes on first
    # run (a no-op once calibration/current.json exists) and resumes any
    # in-flight pump calibration session from _sessions/pump.json.

    cal_service = CalibrationService(CAL_DIR, EXPERIMENTS_DIR, manager)
    state.cal_service = cal_service
    try:
        imported = cal_service.bootstrap()
        if imported:
            log.info("calibration store: imported legacy files for %s", imported)
    except Exception:
        log.exception("calibration store bootstrap failed")

    def _sync_od_cal_from_manager() -> None:
        """Keep AppState's od_cal copy (used to invert calibrated OD back to
        raw for API responses) in step with the manager's, which is the one a
        per-run blank re-anchors."""
        if getattr(state.manager, "od_cal", None) is not None:
            state.od_cal = np.asarray(state.manager.od_cal)

    def _apply_experiment_blank(name: str) -> None:
        """Re-apply a committed per-run blank to the live manager (used on
        commit and on crash-resume, so a restart keeps the re-anchored OD)."""
        blank = cal_service.load_experiment_blank(name)
        if blank is None:
            return
        c_run = blank.get("fit", {}).get("c_run") or {}
        try:
            state.manager.apply_od_blank(
                {int(k): float(v) for k, v in c_run.items()}
            )
            _sync_od_cal_from_manager()
            log.info("re-applied per-run OD blank for '%s' (%d vials)",
                     name, len(c_run))
        except Exception:
            log.exception("failed to apply per-run OD blank for '%s'", name)

    def _push_growth_context(name: str | None) -> None:
        """Give the engine the calibration facts its growth estimator needs:
        per-vial OD floors from this run's blank, the OD-calibration-suspect
        vial list, and whether pump flow rates have been calibrated.

        Called at start, at crash-resume, and after any blank or pump commit,
        so the estimator never runs on a stale view of the calibration.
        """
        try:
            state.engine.set_growth_context(cal_service.growth_context(name))
        except Exception:
            log.exception("failed to push growth context for '%s'", name)

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
        ``/api/calibration/raw/temperature`` (calibration-only escape hatch)."""
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
            _emit_alert(
                "critical",
                f"Setting heater temperature failed: {exc}",
                category=evlog.CATEGORY_HEATER,
                dedup_key="set_temperature_failed",
            )
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

    # NOTE: the raw temperature escape hatch used to live at
    # /api/actuators/temperature/raw. It moved behind /api/calibration/raw/*
    # (SPEC §19.6): calibration is the only permitted consumer of raw actuator
    # paths, and given the inverted `xr` convention a raw heater setpoint on
    # the ordinary actuator surface was an accident waiting to happen.

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
            _emit_alert(
                "warning",
                f"Setting stir rate failed: {exc}",
                category=evlog.CATEGORY_ACTUATOR,
                dedup_key="set_stir_failed",
            )
            return jsonify(error=f"set_stir failed: {exc}"), 500
        return jsonify(status="ok")

    def _resolve_vial_flow_rate(vial: int, direction: str = "influx") -> float:
        """Best-effort per-pump flow rate for mL<->seconds conversion.
        Direction-aware since Session O3a: influx and efflux are separate
        pumps with independent rates. Falls back to 1.0 mL/s only if the
        engine somehow isn't constructed yet."""
        if state.engine is None:
            return 1.0
        return state.engine.flow_rate_ml_s(vial, direction)

    @flask_app.route("/api/actuators/pump/preview", methods=["POST"])
    def api_pump_preview():
        """SPEC §16: quantisation preview, no side effects. Shown by the
        frontend before firing a mL-mode manual pump so the operator sees
        the achievable volume, not just the requested one."""
        body = request.get_json(silent=True) or {}
        vial = body.get("vial")
        direction = body.get("direction")
        volume_ml = body.get("volume_ml")
        if not isinstance(vial, int) or not (0 <= vial < N_VIALS):
            return jsonify(error=f"'vial' must be an integer in 0..{N_VIALS - 1}"), 400
        if direction not in ("influx", "efflux"):
            return jsonify(error="'direction' must be 'influx' or 'efflux'"), 400
        if not isinstance(volume_ml, (int, float)) or isinstance(volume_ml, bool) or volume_ml <= 0:
            return jsonify(error="'volume_ml' must be a positive number"), 400
        flow_rate = _resolve_vial_flow_rate(int(vial), direction)
        q = compute_pump_quantization(float(volume_ml), flow_rate)
        return jsonify(
            deliverable_ml=round(q["deliverable_ml"], 4),
            seconds=q["seconds"],
            min_ml=round(q["min_ml"], 4),
            quantised=q["quantised"],
        )

    @flask_app.route("/api/actuators/pump", methods=["POST"])
    def api_pump():
        """Fire a single manual pump. Accepts either `seconds` (legacy,
        unchanged) or `volume_ml` (SPEC §16) -- exactly one of the two.

        `volume_ml` is converted to a whole-second duration via the vial's
        resolved flow rate (`ExperimentEngine.flow_rate_ml_s`); requests
        that would quantise to 0 s are rejected rather than silently
        truncated (the legacy `%d` bug, SPEC §9).

        Either mode debits the vial's mapped media bottle / credits waste
        if one is loaded (`ExperimentEngine.record_manual_pump`) -- this
        fixes a pre-existing gap where manual pumps never touched media
        accounting at all."""
        body = request.get_json(silent=True) or {}
        vial = body.get("vial")
        direction = body.get("direction")
        seconds_in = body.get("seconds")
        volume_ml_in = body.get("volume_ml")
        if not isinstance(vial, int) or not (0 <= vial < N_VIALS):
            return jsonify(error=f"'vial' must be an integer in 0..{N_VIALS - 1}"), 400
        if direction not in ("influx", "efflux"):
            return jsonify(error="'direction' must be 'influx' or 'efflux'"), 400
        if (seconds_in is None) == (volume_ml_in is None):
            return jsonify(error="specify exactly one of 'seconds' or 'volume_ml'"), 400

        lock = _experiment_locks_vial(int(vial))
        if lock is not None:
            return jsonify(
                error=f"vial {lock[0]} is controlled by experiment '{lock[1]}'"
            ), 409

        requested_ml = None
        delivered_ml = None
        flow_rate = None
        if volume_ml_in is not None:
            if not isinstance(volume_ml_in, (int, float)) or isinstance(volume_ml_in, bool):
                return jsonify(error="'volume_ml' must be a number"), 400
            if volume_ml_in <= 0:
                return jsonify(error="'volume_ml' must be > 0"), 400
            flow_rate = _resolve_vial_flow_rate(int(vial), direction)
            q = compute_pump_quantization(float(volume_ml_in), flow_rate)
            if q["seconds"] < 1:
                return jsonify(error=(
                    f"'volume_ml'={volume_ml_in} is below the minimum "
                    f"deliverable dose for vial {vial} {direction} "
                    f"({q['min_ml']:.2f} mL at {flow_rate:.3f} mL/s) -- "
                    "request at least that much, or use 'seconds' directly"
                )), 400
            if q["seconds"] > PUMP_MAX_SECONDS:
                return jsonify(error=(
                    f"'volume_ml'={volume_ml_in} would require {q['seconds']}s, "
                    f"over the {PUMP_MAX_SECONDS:.0f}s manual pump ceiling"
                )), 400
            seconds = float(q["seconds"])
            requested_ml = float(volume_ml_in)
            delivered_ml = q["deliverable_ml"]
        else:
            if not isinstance(seconds_in, (int, float)) or isinstance(seconds_in, bool):
                return jsonify(error="'seconds' must be a number"), 400
            if not (0 < seconds_in <= PUMP_MAX_SECONDS):
                return jsonify(error=f"'seconds' must be in (0, {PUMP_MAX_SECONDS}]"), 400
            seconds = float(seconds_in)

        try:
            state.manager.pump_command(int(vial), direction, seconds)
        except Exception as exc:
            log.exception("pump_command failed")
            _emit_alert(
                "critical",
                f"Manual pump failed (vial {vial}, {direction}): {exc}",
                category=evlog.CATEGORY_PUMP, vial=int(vial),
                dedup_key=("pump_command_failed", int(vial), direction),
            )
            return jsonify(error=f"pump_command failed: {exc}"), 500

        if state.engine is not None:
            try:
                actual_flow_rate = (
                    flow_rate if flow_rate is not None
                    else _resolve_vial_flow_rate(int(vial), direction)
                )
                actual_delivered_ml = (
                    delivered_ml if delivered_ml is not None else seconds * actual_flow_rate
                )
                state.engine.record_manual_pump(int(vial), direction, actual_delivered_ml)
            except Exception:
                log.exception("record_manual_pump failed")
                _emit_alert(
                    "warning",
                    f"Manual pump on vial {vial} was not recorded in controller "
                    "state -- media totals and dilution timing may now be wrong",
                    category=evlog.CATEGORY_PUMP, vial=int(vial),
                    dedup_key="record_manual_pump_failed",
                )

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
            # Silent data loss: the pump fired but the CSV never got the row.
            log.exception("data_logger.log_pump_event failed")
            _emit_alert(
                "critical",
                f"Pump fired but was NOT logged to CSV (vial {vial}, {direction}) "
                "-- the run record is now incomplete",
                category=evlog.CATEGORY_PUMP, vial=int(vial),
                dedup_key="log_pump_event_failed",
            )
        _emit_event({
            "type": "pump",
            "vial": int(vial),
            "direction": direction,
            "duration_seconds": float(seconds),
            "timestamp": timestamp,
        })
        response = {"status": "ok"}
        if requested_ml is not None:
            response["requested_ml"] = requested_ml
            response["delivered_ml"] = round(delivered_ml, 4)
            response["seconds"] = int(seconds)
        return jsonify(**response)

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
        _emit_alert(
            "critical", "Emergency stop — all actuators zeroed",
            category=evlog.CATEGORY_ACTUATOR, timestamp=timestamp,
            dedup_key="emergency_stop",
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
        # SPEC §19.1 provenance: record the calibration versions this run
        # will use, plus the measured 32-pump flow rates when a complete pump
        # calibration exists. Caller-supplied values win (deep-merged last).
        calibration_body = dict(body.get("calibration") or {})
        try:
            versions = cal_service.store.current_versions()
            provenance = {
                sub: versions.get(sub) for sub in CAL_SUBSYSTEMS
            }
            provenance["vial_map"] = versions.get("vial_map")
            rates = cal_service.store.current_pump_rates()
            if rates is not None and "pump_flow_rates" not in calibration_body:
                provenance["pump_flow_rates"] = rates
            calibration_body = {**provenance, **calibration_body}
        except Exception:
            log.exception("calibration provenance enrichment failed")
        try:
            config = state.engine.create_experiment(
                name=body.get("name"),
                mode=body.get("mode", "turbidostat"),
                vials=body.get("vials"),
                parameters=body.get("params") or body.get("parameters") or {},
                calibration=calibration_body,
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
        # Control-parameter warnings (SPEC §9 / CONTROL_MODE_AUDIT.md C-3):
        # configurations that will run but not deliver what was asked for --
        # a clipped chemostat bolus, a band whose whole-second truncation
        # eats a large share of each dilution. Hard errors already 400'd
        # above; these are for the wizard's review step to show.
        return jsonify(
            status="created",
            name=config["name"],
            warnings=config.get("warnings") or [],
        )

    @flask_app.route("/api/experiments/<name>/start", methods=["POST"])
    def api_experiments_start(name):
        body = request.get_json(silent=True) or {}
        # Per-run OD blank hard block (CALIBRATION_PROTOCOL §13: "hard block,
        # not a warning"). Without a blank the machine reports OD 0.12-0.44
        # for sterile medium, vial-dependent. Overridable only explicitly,
        # and the override is recorded through the alert funnel.
        blank_missing = not (EXPERIMENTS_DIR / name / "od_blank.json").is_file()
        if blank_missing and not body.get("allow_missing_od_blank"):
            return jsonify(
                error=(
                    f"no per-run OD blank has been taken for '{name}' — run "
                    "the OD blank wizard (Calibration tab) immediately before "
                    "starting, or pass allow_missing_od_blank=true to start "
                    "anyway (recorded)"
                ),
                code="missing_od_blank",
            ), 409
        try:
            state.engine.start_experiment(name)
        except InvalidExperimentStateError as exc:
            return jsonify(error=str(exc)), 409
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 500
        if blank_missing:
            _emit_alert(
                "warning",
                f"Experiment '{name}' started WITHOUT a per-run OD blank — "
                "reported OD carries a vial-specific offset of up to ~0.44 "
                "(CALIBRATION_PROTOCOL §1.1)",
                category=evlog.CATEGORY_CALIBRATION,
                dedup_key=("blank_override", name),
            )
        else:
            # Belt and braces: make sure the committed blank is live on the
            # manager (commit already applied it in this process lifetime).
            _apply_experiment_blank(name)
        # After the blank is live, so the per-vial OD floors are derived from
        # the calibration the run will actually use.
        _push_growth_context(name)
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
        hours_arg = request.args.get("hours")
        try:
            hours = float(hours_arg) if hours_arg else None
        except ValueError:
            return jsonify(error="'hours' must be a number"), 400
        max_points_arg = request.args.get("max_points")
        try:
            max_points = int(max_points_arg) if max_points_arg else 2000
        except ValueError:
            return jsonify(error="'max_points' must be an integer"), 400
        try:
            data = state.engine.get_data(
                name,
                vial=vial,
                parameter=parameter,
                last_n=last_n,
                hours=hours,
                max_points=max_points,
            )
        except FileNotFoundError as exc:
            return jsonify(error=str(exc)), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(data)

    # --------------------------- Data export (SPEC §8) -----------------------
    # Read-only over the per-vial CSVs (data_export.py). Safe to run while an
    # experiment is RUNNING — append-only files make a snapshot consistent.
    # GET streams the bundle to the browser; POST writes it to exports/ for a
    # relay-resilient "generate now, fetch later" workflow.

    @flask_app.route("/api/experiments/<name>/export", methods=["GET"])
    def api_experiment_export(name):
        exp_vials = _experiment_vials(name)
        if exp_vials is None:
            return jsonify(error=f"experiment '{name}' not found"), 404
        try:
            params, vials, hours = _resolve_export_request(
                request.args.get("parameters"),
                request.args.get("vials"),
                request.args.get("hours"),
                exp_vials,
            )
            filename, blob = dx.build_bundle(
                EXPERIMENTS_DIR / name,
                name=name,
                vials=vials,
                parameters=params,
                hours=hours,
            )
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        mimetype = "application/zip" if filename.endswith(".zip") else "text/csv"
        return send_file(
            io.BytesIO(blob),
            mimetype=mimetype,
            as_attachment=True,
            download_name=filename,
        )

    @flask_app.route("/api/experiments/<name>/export", methods=["POST"])
    def api_experiment_export_save(name):
        exp_vials = _experiment_vials(name)
        if exp_vials is None:
            return jsonify(error=f"experiment '{name}' not found"), 404
        body = request.get_json(silent=True) or {}
        try:
            params, vials, hours = _resolve_export_request(
                body.get("parameters"), body.get("vials"), body.get("hours"), exp_vials
            )
            filename, blob = dx.build_bundle(
                EXPERIMENTS_DIR / name,
                name=name,
                vials=vials,
                parameters=params,
                hours=hours,
            )
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        stem, dot, ext = filename.rpartition(".")
        saved_name = f"{stem}_{stamp}.{ext}" if dot else f"{filename}_{stamp}"
        (EXPORTS_DIR / saved_name).write_bytes(blob)
        return jsonify(
            status="saved",
            filename=saved_name,
            bytes=len(blob),
            path=str(EXPORTS_DIR / saved_name),
            download_url=f"/api/exports/{saved_name}",
        )

    @flask_app.route("/api/exports", methods=["GET"])
    def api_exports_list():
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        items = []
        for p in sorted(EXPORTS_DIR.iterdir(), reverse=True):
            if not p.is_file():
                continue
            st = p.stat()
            items.append({
                "filename": p.name,
                "bytes": st.st_size,
                "created": datetime.fromtimestamp(
                    st.st_mtime, tz=timezone.utc
                ).isoformat(timespec="seconds"),
                "download_url": f"/api/exports/{p.name}",
            })
        return jsonify(exports=items)

    @flask_app.route("/api/exports/<path:filename>", methods=["GET"])
    def api_exports_download(filename):
        # send_from_directory guards against path traversal outside EXPORTS_DIR.
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        return send_from_directory(EXPORTS_DIR, filename, as_attachment=True)

    @flask_app.route("/api/events/recent", methods=["GET"])
    def api_events_recent():
        """Server-side event ring buffer (SPEC §20.4).

        Populated whether or not an experiment is running, so the browser can
        rebuild its alert drawer after a reload -- or on a second machine --
        without a per-experiment events.csv to read from.

        ``level`` is a MINIMUM severity: level=warning returns warnings and
        criticals, which is what an operator filtering for problems expects.
        """
        level = request.args.get("level") or None
        if level is not None and level not in evlog.LEVELS:
            return jsonify(
                error=f"'level' must be one of {list(evlog.LEVELS)}"
            ), 400
        category = request.args.get("category") or None
        vial_arg = request.args.get("vial")
        vial = None
        if vial_arg not in (None, ""):
            try:
                vial = int(vial_arg)
            except ValueError:
                return jsonify(error="'vial' must be an integer"), 400
            if not (0 <= vial < N_VIALS):
                return jsonify(error=f"'vial' must be in 0..{N_VIALS - 1}"), 400
        try:
            limit = int(request.args.get("limit", 100))
        except ValueError:
            return jsonify(error="'limit' must be an integer"), 400
        limit = max(1, min(limit, evlog.DEFAULT_RING_SIZE))
        unacked = str(request.args.get("unacked_only", "")).lower() in ("1", "true", "yes")
        return jsonify(
            events=state.event_log.recent(
                level=level, category=category, vial=vial,
                limit=limit, unacked_only=unacked,
            ),
            counts=state.event_log.counts(),
        )

    @flask_app.route("/api/events/<int:event_id>/ack", methods=["POST"])
    def api_events_ack(event_id):
        """Acknowledge one event. Criticals persist in the drawer until this is
        called; the acknowledgement is itself recorded as an event (SPEC §20.4)."""
        body = request.get_json(silent=True) or {}
        by = str(body.get("by") or "operator")[:64]
        entry = state.event_log.acknowledge(event_id, by=by)
        if entry is None:
            return jsonify(error=f"event {event_id} not found"), 404
        return jsonify(status="acknowledged", event=entry)

    @flask_app.route("/api/health", methods=["GET"])
    def api_health():
        """RS485 bus health, per-vial sleeve health, and file-logging state.

        Distinct from the socket.io connection the browser already tracks: Flask
        can be perfectly reachable while the serial link is dead, which is
        exactly the case that used to show 'connected' beside stale readings.
        """
        return jsonify(
            bus=state.bus_health.snapshot(),
            vials=state.vial_health.snapshot(),
            file_logging=evlog.file_log_status(),
            events=state.event_log.counts(),
        )

    @flask_app.route("/api/storage", methods=["GET"])
    def api_storage():
        return jsonify(dx.storage_report(EXPERIMENTS_DIR, EXPORTS_DIR))

    @flask_app.route("/api/experiments/<name>", methods=["DELETE"])
    def api_experiments_delete(name):
        try:
            state.engine.delete_experiment(name)
        except InvalidExperimentStateError as exc:
            return jsonify(error=str(exc)), 409
        except FileNotFoundError as exc:
            return jsonify(error=str(exc)), 404
        return jsonify(status="deleted", name=name)

    @flask_app.route("/api/experiments/<name>", methods=["GET"])
    def api_experiments_get(name):
        """Full config.json for an experiment (loaded or on-disk). Used by the
        dashboard's notes editor, which needs notes/tags that status() and the
        list endpoint don't carry."""
        config_path = EXPERIMENTS_DIR / name / "config.json"
        if not config_path.is_file():
            return jsonify(error=f"experiment '{name}' not found"), 404
        try:
            return jsonify(json.loads(config_path.read_text(encoding="utf-8")))
        except Exception as exc:
            return jsonify(error=f"failed to read config: {exc}"), 500

    @flask_app.route("/api/experiments/<name>/rename", methods=["POST"])
    def api_experiments_rename(name):
        body = request.get_json(silent=True) or {}
        new_name = body.get("new_name") or body.get("new")
        if not new_name:
            return jsonify(error="'new_name' is required"), 400
        try:
            result = state.engine.rename_experiment(name, new_name)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except InvalidExperimentStateError as exc:
            return jsonify(error=str(exc)), 409
        except FileExistsError as exc:
            return jsonify(error=str(exc)), 409
        except FileNotFoundError as exc:
            return jsonify(error=str(exc)), 404
        return jsonify(status="renamed", **result)

    @flask_app.route("/api/experiments/<name>", methods=["PATCH"])
    def api_experiments_update(name):
        body = request.get_json(silent=True) or {}
        try:
            merged = state.engine.update_metadata(
                name, notes=body.get("notes"), tags=body.get("tags")
            )
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except FileNotFoundError as exc:
            return jsonify(error=str(exc)), 404
        return jsonify(
            status="updated",
            name=name,
            notes=merged.get("notes", ""),
            tags=merged.get("tags", []),
        )

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
        except Exception as exc:
            log.exception("execute queued pump actions failed on exit")
            _emit_alert(
                "critical",
                "Dilutions queued during maintenance were NOT delivered on "
                f"resume: {exc}",
                category=evlog.CATEGORY_PUMP,
                dedup_key="queued_pump_exit_failed",
            )
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

    # ---------------------- Calibration routes (SPEC §19) --------------------
    # The wizard surface for CALIBRATION_PROTOCOL.md Part II. Every mutating
    # route rejects with 409 while an experiment is RUNNING, and these are the
    # ONLY routes permitted to reach the raw actuator paths (§19.6).

    def _cal_route(mutating: bool = True):
        """Decorator: RUNNING-experiment guard + exception -> HTTP mapping
        (ValueError 400, CalibrationConflict 409, QCRefusal 422 with the qc
        block, FileNotFoundError 404)."""
        def decorate(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                if mutating and state.engine is not None and state.engine.is_running:
                    return jsonify(
                        error=(
                            "calibration is unavailable while an experiment "
                            "is RUNNING (SPEC §19.6) — stop it first"
                        ),
                        code="experiment_running",
                    ), 409
                try:
                    return fn(*args, **kwargs)
                except QCRefusal as exc:
                    return jsonify(error=str(exc), code="qc_refused",
                                   qc=exc.qc), 422
                except CalibrationConflict as exc:
                    return jsonify(error=str(exc), code="conflict"), 409
                except FileNotFoundError as exc:
                    return jsonify(error=str(exc)), 404
                except ValueError as exc:
                    return jsonify(error=str(exc)), 400
            return wrapper
        return decorate

    def _loaded_experiment_info() -> tuple[str | None, str | None]:
        if state.engine is None:
            return None, None
        return state.engine.loaded_experiment, state.engine.status_string

    @flask_app.route("/api/calibration/", methods=["GET"])
    @_cal_route(mutating=False)
    def api_calibration_index():
        name, status = _loaded_experiment_info()
        return jsonify(cal_service.index(
            loaded_experiment=name, loaded_status=status,
        ))

    @flask_app.route("/api/calibration/history", methods=["GET"])
    @_cal_route(mutating=False)
    def api_calibration_history():
        return jsonify(cal_service.store.history())

    @flask_app.route("/api/calibration/staleness", methods=["GET"])
    @_cal_route(mutating=False)
    def api_calibration_staleness():
        name, status = _loaded_experiment_info()
        return jsonify(cal_service.store.staleness(
            loaded_experiment=name, loaded_status=status,
        ))

    @flask_app.route("/api/calibration/<subsystem>", methods=["GET"])
    @_cal_route(mutating=False)
    def api_calibration_subsystem(subsystem):
        return jsonify(cal_service.subsystem(subsystem))

    # --- per-run OD blank (§19.2 / CALIBRATION_PROTOCOL §5.4) ----------------

    @flask_app.route("/api/calibration/od/blank/start", methods=["POST"])
    @_cal_route()
    def api_blank_start():
        body = request.get_json(silent=True) or {}
        name, status = _loaded_experiment_info()
        if name is None:
            return jsonify(
                error="no experiment is loaded — create one first; the blank "
                      "is taken against a CREATED experiment immediately "
                      "before start",
                code="conflict",
            ), 409
        config_path = EXPERIMENTS_DIR / name / "config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return jsonify(error=f"failed to read {config_path}"), 500
        params = config.get("parameters", {})
        stir_pwm = body.get("stir_pwm", params.get("stir_rate", 10))
        led_power = body.get("led_power", OD_LED_POWER)
        return jsonify(cal_service.blank_start(
            experiment=name,
            config=config,
            engine_status=status,
            led_power=int(led_power),
            stir_pwm=int(stir_pwm),
            expected_led_power=OD_LED_POWER,
            n_samples=int(body.get("n_samples", 5)),
        ))

    @flask_app.route("/api/calibration/od/blank/dark", methods=["POST"])
    @_cal_route()
    def api_blank_dark():
        body = request.get_json(silent=True) or {}
        return jsonify(cal_service.blank_dark(body.get("session", "")))

    @flask_app.route("/api/calibration/od/blank/measure", methods=["POST"])
    @_cal_route()
    def api_blank_measure():
        body = request.get_json(silent=True) or {}
        return jsonify(cal_service.blank_measure(body.get("session", "")))

    @flask_app.route("/api/calibration/od/blank/commit", methods=["POST"])
    @_cal_route()
    def api_blank_commit():
        body = request.get_json(silent=True) or {}
        name, _status = _loaded_experiment_info()
        result = cal_service.blank_commit(
            body.get("session", ""),
            exclude_vials=body.get("exclude_vials"),
            override_reason=body.get("override_reason"),
            operator=str(body.get("operator", "unknown")),
        )
        # Apply the re-anchor to the live manager so every read from now on
        # (including CREATED-state idle reads) uses the blanked curve.
        try:
            state.manager.apply_od_blank(
                {int(k): float(v) for k, v in result["c_run"].items()}
            )
            _sync_od_cal_from_manager()
        except Exception:
            log.exception("apply_od_blank after commit failed")
        # A new blank changes the per-vial OD floors the growth estimator uses.
        _push_growth_context(state.engine.loaded_experiment)
        # Provenance: the run must record which blank it used (§19.1).
        if name is not None:
            try:
                state.engine.record_calibration_provenance(name, {
                    "od_blank": f"experiments/{name}/od_blank.json",
                    "od": result.get("parent_od_cal"),
                })
            except Exception:
                log.exception("recording blank provenance failed")
        offsets = [abs(v) for v in result["od_offset_removed"].values()]
        _emit_event({
            "type": "od_blank_committed",
            "name": name,
            "n_vials": len(result["c_run"]),
            "max_offset_removed": round(max(offsets), 3) if offsets else None,
            "qc_passed": result["qc"]["passed"],
        })
        return jsonify(result)

    @flask_app.route("/api/calibration/od/blank/abort", methods=["POST"])
    @_cal_route()
    def api_blank_abort():
        body = request.get_json(silent=True) or {}
        return jsonify(cal_service.blank_abort(body.get("session", "")))

    # --- pump gravimetric (§19.3 / CALIBRATION_PROTOCOL §7) ------------------

    @flask_app.route("/api/calibration/pump/start", methods=["POST"])
    @_cal_route()
    def api_pump_cal_start():
        body = request.get_json(silent=True) or {}
        return jsonify(cal_service.pump_start(body))

    @flask_app.route("/api/calibration/pump/fire", methods=["POST"])
    @_cal_route()
    def api_pump_cal_fire():
        body = request.get_json(silent=True) or {}
        pump_id = body.get("pump_id")
        if not isinstance(pump_id, int) or isinstance(pump_id, bool):
            return jsonify(error="'pump_id' must be an integer 0..31"), 400
        return jsonify(cal_service.pump_fire(pump_id))

    @flask_app.route("/api/calibration/pump/record", methods=["POST"])
    @_cal_route()
    def api_pump_cal_record():
        body = request.get_json(silent=True) or {}
        pump_id = body.get("pump_id")
        replicate = body.get("replicate")
        if not isinstance(pump_id, int) or isinstance(pump_id, bool):
            return jsonify(error="'pump_id' must be an integer 0..31"), 400
        if not isinstance(replicate, int) or isinstance(replicate, bool):
            return jsonify(error="'replicate' must be an integer"), 400
        return jsonify(cal_service.pump_record(
            pump_id, replicate, body.get("mass_g"),
        ))

    @flask_app.route("/api/calibration/pump/session", methods=["GET"])
    @_cal_route(mutating=False)
    def api_pump_cal_session():
        return jsonify(cal_service.pump_session())

    @flask_app.route("/api/calibration/pump/finish", methods=["POST"])
    @_cal_route()
    def api_pump_cal_finish():
        body = request.get_json(silent=True) or {}
        result = cal_service.pump_finish(
            override_reason=body.get("override_reason"),
            operator=body.get("operator"),
        )
        _emit_event({
            "type": "calibration_installed",
            "subsystem": "pump",
            "version": result["version"],
            "complete": result["flow_rates_complete"],
        })
        return jsonify(result)

    @flask_app.route("/api/calibration/pump/abort", methods=["POST"])
    @_cal_route()
    def api_pump_cal_abort():
        return jsonify(cal_service.pump_abort())

    # --- raw escape hatches, calibration-only (§19.6) ------------------------

    @flask_app.route("/api/calibration/raw/temperature", methods=["POST"])
    @_cal_route()
    def api_cal_raw_temperature():
        """Raw `xr` setpoints for the calibration wizard / low-level debug.

        The convention is INVERTED — lower value = hotter target;
        HEATER_OFF_SETPOINT (4095) is "off"; 0 requests ~82 °C.
        SerialManager still enforces the MAX_SAFE_TEMP_C-derived floor, so
        even here you cannot request hotter than the software cap."""
        body = request.get_json(silent=True) or {}
        setpoints = body.get("setpoints")
        err = _validate_int_array(
            setpoints, N_VIALS, 0, HEATER_OFF_SETPOINT, "setpoints"
        )
        if err is not None:
            return jsonify(error=err), 400
        try:
            current = state.manager.set_temperature_raw(
                [int(v) for v in setpoints]
            )
        except Exception as exc:
            log.exception("set_temperature_raw failed")
            return jsonify(error=f"set_temperature_raw failed: {exc}"), 500
        current_raw = np.asarray(
            getattr(state.manager, "temp_setpoint_raw",
                    np.full(N_VIALS, HEATER_OFF_SETPOINT))
        ).tolist()
        return jsonify(
            status="ok",
            current_temp_c=list(current),
            temperature_setpoint_raw=[int(v) for v in current_raw],
        )

    @flask_app.route("/api/calibration/raw/od_led", methods=["POST"])
    @_cal_route()
    def api_cal_raw_od_led():
        """Raw OD read at an arbitrary LED power (0 = dark read). Returns
        per-vial median/sd/n_valid of the raw counts — the same primitive
        the blank wizard uses."""
        body = request.get_json(silent=True) or {}
        power = body.get("power")
        if not isinstance(power, (int, float)) or isinstance(power, bool) \
                or not (0 <= power <= 2200):
            return jsonify(error="'power' must be a number in 0..2200"), 400
        n_samples = body.get("n_samples", 5)
        if not isinstance(n_samples, int) or isinstance(n_samples, bool) \
                or not (1 <= n_samples <= 25):
            return jsonify(error="'n_samples' must be an integer in 1..25"), 400
        return jsonify(state.manager.collect_od_raw(int(power), n_samples))

    # --- post-run mass reconciliation (§19.4) --------------------------------

    @flask_app.route("/api/growth_rate", methods=["GET"])
    def api_growth_rate():
        """Per-vial growth estimates (SPEC §17).

        ONE reported estimator -- the windowed log-linear fit within
        inter-dilution segments -- plus a gated ``dilution_check`` diagnostic.
        SPEC §6's older example showed ``mu_dilution`` beside ``mu_per_hour``
        as co-equal; GROWTH_RATE_METHOD.md §0 demoted the second to a
        diagnostic that stays dark until pump calibration exists, so it is
        nested rather than presented as an alternative answer.

        ``r_squared`` never travels without ``windows_searched``: the window is
        chosen by maximum R², which makes the reported R² an optimistic bound
        rather than an unbiased fit statistic.
        """
        name = state.engine.loaded_experiment
        if name is None or not state.engine.is_running:
            return jsonify(
                experiment=name,
                running=False,
                per_vial={},
                message="no experiment is running",
            )
        return jsonify(
            experiment=name,
            running=True,
            timestamp=_now_iso(),
            recompute_interval_seconds=growth.RECOMPUTE_INTERVAL_SECONDS,
            per_vial=state.engine.growth_snapshot(),
        )

    @flask_app.route("/api/experiments/<name>/reconcile", methods=["POST"])
    def api_experiment_reconcile(name):
        """O4: compare measured start/end masses against the software's
        accumulated duration x flow_rate volumes. The only check that
        validates the whole open-loop volume chain end to end."""
        if (
            state.engine is not None
            and state.engine.loaded_experiment == name
            and state.engine.is_running
        ):
            return jsonify(
                error="stop the experiment before reconciling — the masses "
                      "are end-of-run measurements",
                code="conflict",
            ), 409
        state_path = EXPERIMENTS_DIR / name / "state.json"
        exp_state: dict = {}
        if state_path.is_file():
            try:
                exp_state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                log.exception("failed to read %s", state_path)
        body = request.get_json(silent=True) or {}
        try:
            record = cal_service.reconcile(name, exp_state, body)
        except FileNotFoundError as exc:
            return jsonify(error=str(exc)), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        _emit_event({
            "type": "reconciliation",
            "name": name,
            "within_tolerance": record["within_tolerance"],
            "media": record["media"],
            "waste": record["waste"],
        })
        if not record["within_tolerance"]:
            _emit_alert(
                "warning",
                f"Mass reconciliation for '{name}' is outside ±10 % — the "
                "flow-rate array is stale, a line is occluded, or a pump is "
                "slipping; the pump calibration is now flagged stale "
                "(CALIBRATION_PROTOCOL §6)",
                category=evlog.CATEGORY_CALIBRATION,
                dedup_key=("reconcile_failed", name),
            )
        return jsonify(record)

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
            except Exception as exc:
                log.exception("pump firing failed for vial %d", vial)
                _emit_alert(
                    "critical",
                    f"Automatic dilution failed for vial {vial}: {exc}",
                    category=evlog.CATEGORY_PUMP, vial=int(vial),
                    dedup_key=("pump_fire_failed", int(vial)),
                )
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
                    _emit_alert(
                        "critical",
                        f"Dilution fired but was NOT logged to CSV (vial {vial}, "
                        f"{direction}) -- the run record is now incomplete",
                        category=evlog.CATEGORY_PUMP, vial=int(vial),
                        dedup_key="log_pump_event_failed",
                    )
                _emit_event({
                    "type": "pump",
                    "vial": int(vial),
                    "direction": direction,
                    "duration_seconds": float(seconds),
                    "average_od": action.average_od,
                    "timestamp": ts_iso,
                })

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

    def _classify_bus_reads(t: dict, o: dict) -> None:
        """Feed one cycle's reads into the health trackers and alert on the
        transitions that matter (SPEC §20.3).

        TRANSIENT is deliberately silent: the RS485 bus drops frames by design
        and commit b9b135a already tolerates it. Only a bus that has gone quiet
        for `failure_threshold` consecutive cycles is worth waking someone for.
        """
        transitions = evlog.classify_cycle(
            state.bus_health,
            state.vial_health,
            temperature=t.get("calibrated"),
            od_calibrated=o.get("calibrated"),
            od_flags=o.get("flags"),
            od_n_valid=o.get("n_valid"),
        )
        for subsystem, outcome in transitions:
            if outcome == evlog.ErrorClass.PERSISTENT:
                _emit_alert(
                    "critical",
                    f"RS485 bus silent: no valid {subsystem} response for "
                    f"{state.bus_health.failure_threshold} consecutive cycles",
                    category=evlog.CATEGORY_SERIAL,
                    dedup_key=("bus_down", subsystem),
                )
            elif outcome == evlog.ErrorClass.RECOVERED:
                _emit_alert(
                    "info",
                    f"RS485 {subsystem} reads recovered",
                    category=evlog.CATEGORY_SERIAL,
                    dedup_key=("bus_recovered", subsystem),
                )

    # Exposed on AppState so the shutdown path and the tests can drive one
    # classification cycle without standing up the sensor thread.
    state.classify_bus_reads = _classify_bus_reads

    def _check_disk_space() -> None:
        """Edge-triggered low-disk alert; band/hysteresis logic lives in the
        pure :func:`_disk_alert_decision` so it can be unit-tested."""
        try:
            usage = shutil.disk_usage(EXPERIMENTS_DIR)
        except OSError:
            log.exception("disk_usage check failed")
            return
        level, state.disk_warned, state.disk_critical = _disk_alert_decision(
            usage.free, usage.total, state.disk_warned, state.disk_critical
        )
        if level is None:
            return
        free_mb = usage.free / 1024 ** 2
        free_pct = 100.0 * usage.free / usage.total if usage.total else 0.0
        if level == "critical":
            msg = (
                f"Disk critically low: {free_mb:.0f} MB free ({free_pct:.1f}%) "
                "— data logging at risk, free space now"
            )
        else:
            msg = f"Disk getting low: {free_mb:.0f} MB free ({free_pct:.1f}%)"
        _emit_alert(
            level, msg, category=evlog.CATEGORY_STORAGE, dedup_key="disk_space",
            data={"free_bytes": usage.free, "free_pct": round(free_pct, 2)},
        )
        # SPEC §20.1: file logging suspends itself below its own (lower) floor.
        # Report the transition once so "the logs just stop" is never a mystery.
        suspended = evlog.file_log_status().get("suspended", False)
        if suspended != state.log_writes_suspended:
            state.log_writes_suspended = suspended
            _emit_alert(
                "critical" if suspended else "info",
                "File logging suspended -- free space below the log floor"
                if suspended else "File logging resumed",
                category=evlog.CATEGORY_STORAGE,
                dedup_key="log_suspension",
            )

    def sensor_loop():
        log.info("sensor loop started (interval=%.1fs)", SENSOR_LOOP_INTERVAL_SECONDS)
        while not state.sensor_thread_stop.is_set():
            tick_start = time.monotonic()
            try:
                ts_iso = _now_iso()
                t = _read_temperature_pair(state)
                o = _read_od_pair(state)

                # Feed the calibration thermal-settling tracker (SPEC §19.2's
                # "held >=10 min" guard is enforced from what this loop saw).
                cal_service.note_temperatures(t["calibrated"])

                # SPEC §20.3 classification. Done here rather than inside
                # SerialManager because this is where both subsystems' results
                # are visible in one place, and it works while idle too.
                _classify_bus_reads(t, o)

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
                        od_n_valid=o.get("n_valid"),
                        od_flags=o.get("flags"),
                        od_dark=o.get("dark"),
                    )
                except Exception:
                    log.exception("data_logger.log_sensor_cycle failed")
                    _emit_alert(
                        "critical",
                        "Sensor data is NOT being written to disk "
                        "(log_sensor_cycle failed)",
                        category=evlog.CATEGORY_STORAGE,
                        dedup_key="log_sensor_cycle_failed",
                    )

                # Run the experiment control loop (returns [] when not RUNNING).
                pump_actions: list = []
                try:
                    pump_actions = state.engine.run_cycle(
                        ts_iso, t["calibrated"], o["calibrated"],
                        od_flags=o.get("flags"),
                    )
                except Exception as exc:
                    log.exception("engine.run_cycle failed")
                    _emit_alert(
                        "critical",
                        f"Control loop cycle failed: {exc}",
                        category=evlog.CATEGORY_SYSTEM,
                        dedup_key="run_cycle_failed",
                    )

                # Execute returned pump actions via SerialManager + DataLogger.
                if pump_actions:
                    try:
                        _execute_pump_actions(pump_actions, ts_iso)
                    except Exception as exc:
                        log.exception("execute pump actions failed")
                        _emit_alert(
                            "critical",
                            f"Executing dilutions failed: {exc}",
                            category=evlog.CATEGORY_PUMP,
                            dedup_key="execute_pump_actions_failed",
                        )

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

                # Low-disk monitor: throttled (~every 5 min) so we don't stat
                # the filesystem every tick. Runs regardless of experiment state.
                state.disk_check_counter += 1
                if state.disk_check_counter % DISK_CHECK_EVERY_CYCLES == 1:
                    _check_disk_space()

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
                            "dark": o.get("dark"),
                            "n_valid": o.get("n_valid"),
                            "flags": o.get("flags"),
                        },
                        "experiment": _experiment_summary(),
                        # SPEC §17 growth estimates. Recomputed on a 60 s
                        # throttle inside the engine, so most ticks re-send an
                        # unchanged block -- cheap, and it keeps the dashboard
                        # from needing a second poll.
                        "growth": state.engine.growth_snapshot(),
                        # RS485 bus + per-vial sleeve health, so the dashboard
                        # indicators refresh at the sensor cadence rather than
                        # polling (SPEC §20.4).
                        "health": {
                            "bus": state.bus_health.snapshot(),
                            "vials": state.vial_health.snapshot(),
                        },
                    },
                )

                # Only pet on a successful sensor read — if the bus is stuck
                # we want the watchdog to actually fire.
                state.watchdog.pet()
            except Exception as exc:
                log.exception("sensor loop tick failed")
                _emit_alert(
                    "critical",
                    f"Sensor loop tick failed: {exc}",
                    category=evlog.CATEGORY_SYSTEM,
                    dedup_key="sensor_tick_failed",
                )
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
            # The blank re-anchor lives in memory; a restart must re-apply it
            # or the resumed run's OD silently reverts to the offset curve.
            _apply_experiment_blank(resumed)
            _push_growth_context(resumed)
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
    # SPEC §20.1 -- rotating, disk-aware file logs alongside the stdout stream
    # systemd captures. Failure here must not stop the server booting: stdout
    # still works, and a server that refuses to start over its logs is worse
    # than one that logs to one sink instead of three.
    try:
        evlog.setup_file_logging(LOGS_DIR, level=args.log_level.upper())
        log.info("file logging -> %s", LOGS_DIR)
    except Exception:
        log.exception("could not set up file logging in %s", LOGS_DIR)

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
