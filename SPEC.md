# SPEC.md — eVOLVER Web Control System

## 1. Project summary

Replace the current terminal-based eVOLVER control system (Python 2.7 scripts + Tkinter GUI on a Mac) with a modern web application running on the Raspberry Pi inside the eVOLVER. Any lab member can open a browser, configure an experiment, and monitor it in real time — no Python editing, no terminal commands, no dedicated Mac required.

### Target users

All members of the Isaacs Lab at Yale, ranging from undergraduate researchers with no programming experience to PhD students comfortable with code. The interface must be approachable enough that a new lab member can set up a standard turbidostat experiment with minimal training, while exposing enough depth that advanced users can implement custom control logic.

### Design principles

- **No code required** for standard experiment types (turbidostat, chemostat, morbidostat)
- **Safe by default** — watchdog timers, confirmation dialogs for dangerous actions, automatic actuator shutdown on disconnection
- **Observable** — live sensor data always visible, historical plots available, alerts for anomalies
- **Recoverable** — experiments auto-save state, can resume after power loss or server restart
- **Approachable** — clean, uncluttered interface with progressive disclosure of advanced features

---

## 2. Phased build plan

> **Status note (Aug 2026).** Phase 1 is complete and deployed. Most of Phase 2 is
> complete. Phase 3 has barely started — in particular there are **no calibration
> endpoints in the codebase at all**, which is the quiet blocker under several other
> features. `ROADMAP.md` holds the current prioritised work plan derived from the
> August 2026 lab meeting; this section records the phase structure and what is done.

### Phase 1: MVP — Live dashboard + turbidostat — **COMPLETE**

- [x] Flask server on RPi replaces all 5 supervisor processes
- [x] SerialManager class owns RS485 communication
- [x] MockSerialManager for development without hardware
- [x] Live dashboard: temperature and OD for all 16 vials, updated every 10 seconds
- [x] Manual controls: set temperature, stir rate, trigger pumps, emergency stop
- [x] Single turbidostat experiment: configure thresholds per vial, start/stop, log data
- [x] CSV data export
- [x] Watchdog: zero all actuators if no heartbeat for 30 minutes
- [x] Shutdown handler: zero all actuators on server exit, crash, or SIGTERM

### Phase 2: Multi-experiment + experiment designer — **MOSTLY COMPLETE**

- [x] Additional control modes: chemostat, morbidostat
- [x] Media/waste configuration wizard, per-vial media assignment, volume tracking
- [x] Maintenance mode with auto-resume failsafe
- [x] Crash recovery / resume from `state.json`
- [ ] Growth rate feedback control mode (disabled in the UI — blocked on §17)
- [ ] Vial groups: independent modes and parameters within one experiment (ROADMAP Session Y)
- [ ] Phase-based experiment protocols (ROADMAP Session Z)
- [ ] Experiment templates (§23, ROADMAP Session Q)
- [ ] True parallel experiments — deferred, see `ROADMAP.md` §2 for the reasoning

### Phase 3: Calibration + monitoring — **STARTED**

- [x] Historical data plotting (uPlot, per-vial modal and plots view)
- [x] Calibration provenance and versioning (§19.1) — shipped 2026-08-20 (Session O)
- [x] Per-run OD blank (§19.2) — corrected spec implemented; hard-blocks start when missing
- [x] Pump flow rate calibration (§19.3) — wizard + 32-rate engine plumbing shipped; **bench Tier 2 still to run**
- [x] Post-run mass reconciliation (§19.4)
- [ ] Guided calibration wizard for temperature and OD (§19.5, Phase 4)
- [ ] Growth rate estimation service (§17)
- [x] Consumables safety interlock (§15) — Session K
- [x] Volume-based fluidics (§16) — Session L
- [x] Structured logging and unified event log (§20) — Sessions M/M2
- [ ] Hygiene records and sterilisation wizard (§18)
- [ ] Supervised per-vial override (§21)
- [ ] Rule-based anomaly and stall detection (§22)
- [ ] Slack webhook integration for alerts (§22)
- [ ] Off-box backup (§24)

### Phase 4: Advanced features

- Stir-rate-to-RPM calibration (§25)
- Statistical contamination detection (deferred — needs a corpus of real runs first)
- Cascade PID temperature control (deferred — see `ROADMAP.md` §6; note the premise in
  `SESSION_MASTER_PLAN.md` Session H is wrong)
- Authentication and network hardening
- Spectral multiplexing (multi-wavelength OD if hardware supports it)
- ML-in-the-loop adaptive experiments
- Metapopulation mode (inter-vial transfers on a programmable topology)
- Remote access through Yale VPN
- Multi-eVOLVER orchestration

---

## 3. Tech stack

### Backend (runs on RPi)

| Component       | Choice         | Rationale                                                |
|----------------|----------------|----------------------------------------------------------|
| Language        | Python 3       | Must verify availability on RPi; fall back to 2.7 if needed |
| Web framework   | Flask          | Lightweight, minimal dependencies, runs well on RPi      |
| WebSocket       | flask-socketio | Real-time sensor data push to browser                    |
| Serial          | pyserial       | Already installed on RPi for RS485 communication         |
| Task scheduling | threading.Timer or APScheduler | 10-second measurement loop           |
| Data storage    | Flat CSV files | Easy to analyze externally with pandas/Excel/R           |
| Config storage  | JSON files     | Experiment configurations, calibration data              |

### Frontend (served by Flask, runs in browser)

| Component       | Choice         | Rationale                                                |
|----------------|----------------|----------------------------------------------------------|
| Phase 1         | Vanilla HTML/CSS/JS | Minimal complexity, works on old browsers           |
| Phase 2+        | React (migrate when needed) | Experiment designer needs complex state management |
| Charts          | Chart.js or Plotly.js | Real-time updating, lightweight                   |
| WebSocket client| socket.io client | Matches flask-socketio backend                       |
| CSS             | Minimal custom CSS | Clean, readable, no heavy frameworks                |

### Development tools

| Tool            | Purpose                                                    |
|----------------|-----------------------------------------------------------|
| Claude Code     | Primary development tool (VS Code extension)               |
| Git/GitHub      | Version control, deployment pipeline                       |
| MockSerialManager | Simulates Arduino RS485 responses for local development |

---

## 4. Architecture

### System diagram

```
[Any browser on local network]
     |
     | HTTP (pages, API) + WebSocket (live data)
     |
[Flask server on RPi (192.168.1.2:5000)]
     |
     |--- SerialManager (or MockSerialManager)
     |        |
     |        | RS485 serial (/dev/ttyAMA0, 9600 baud)
     |        |
     |        v
     |    [4x SAMD21 Arduinos on shared bus]
     |        |
     |        v
     |    [16 smart sleeves + 32 pumps (2 per vial)]
     |
     |--- ExperimentEngine
     |        |
     |        |--- reads sensor data from SerialManager
     |        |--- runs control logic (turbidostat, etc.)
     |        |--- writes actuator commands to SerialManager
     |        |--- logs data to CSV
     |
     |--- DataLogger
     |        |
     |        |--- writes CSV files per vial per parameter
     |        |--- manages experiment directories
     |
     |--- Watchdog
              |
              |--- monitors server health
              |--- zeros actuators on timeout or crash
```

### File structure

```
evolver-gui/
  CLAUDE.md                     # Claude Code context document
  SPEC.md                       # This file
  ROADMAP.md                    # Current prioritised work plan
  CALIBRATION_PROTOCOL.md       # Bench SOP + calibration wizard implementation brief
  README.md                     # Project overview and setup instructions

  server/
    app.py                      # Flask application, routes, WebSocket handlers
    serial_manager.py           # RS485 communication (real hardware)
    mock_serial_manager.py      # Simulated responses for development
    experiment_engine.py        # Control loop and experiment logic
    control_modes/
      turbidostat.py            # Turbidostat control logic
      chemostat.py              # Chemostat control logic (Phase 2)
      morbidostat.py            # Morbidostat control logic (Phase 2)
      growth_rate.py            # Growth rate feedback control (Phase 2)
    data_logger.py              # CSV file management
    watchdog.py                 # Safety watchdog timer
    calibration.py              # Calibration math (ADC <-> real units)
    config.py                   # Server configuration and defaults

  frontend/
    static/
      css/
        style.css               # Main stylesheet
      js/
        dashboard.js            # Live dashboard logic
        controls.js             # Manual control panel logic
        experiment.js           # Experiment setup and monitoring
        charts.js               # Chart rendering and updating
        socket.js               # WebSocket connection management
      img/
        (icons, logos)
    templates/
      index.html                # Main dashboard page
      experiment.html           # Experiment setup page
      calibration.html          # Calibration wizard (Phase 3)

  calibration/                  # See §19.1 — versioned, never overwritten in place
    vial_map.json               # Logical vial -> physical sleeve (§14 open question 2)
    current.json                # Pointer: subsystem -> active version filename
    od/2026-08-16T142203Z.json  # Versioned artefacts, one dir per subsystem
    temperature/…json
    pump/…json                  # Per-pump mL/s, 32 entries
    stir/…json
    OD_cal.txt                  # Legacy view (4 x 16), regenerated from current.json
    OD_cal.meta.json            # {"dark_subtracted": bool} — gates §19.2 dark subtraction
    temp_calibration.txt        # Legacy view (2 x 16), regenerated from current.json
    _sessions/                  # In-progress, resumable wizard state

  experiments/                  # Created at runtime
    {experiment_name}/
      config.json               # Experiment parameters + calibration versions used
      od_blank.json             # Per-run OD blank (§19.2) — run-scoped, not global
      vial00_OD.csv
      vial00_temp.csv
      vial00_pump_log.csv
      ...
      vial15_OD.csv
      vial15_temp.csv
      vial15_pump_log.csv

  rpi_original/                 # Original scripts (reference only)
  mac_original/                 # Original scripts (reference only)
```

---

## 5. SerialManager API

The SerialManager is the single point of contact with the hardware. Only one instance exists. It owns the serial port and enforces sequential access.

### Interface

```python
class SerialManager:
    def __init__(self, port='/dev/ttyAMA0', baudrate=9600, timeout=5):
        """Open serial connection. Only one instance allowed."""

    def read_temperature(self) -> list[float]:
        """Send `xr` setpoint command, receive 16 raw thermistor ADC values.
        Returns calibrated temperatures in Celsius if calibration loaded."""

    def read_od(self, led_power=2125) -> list[float]:
        """Send LED power, receive 16 raw ADC values.
        Returns calibrated OD if calibration loaded."""

    def set_temperature_celsius(self, temps_c: list[float]) -> list[float]:
        """Set 16 target temperatures in Celsius (primary API).

        Internally converts each target to the raw `xr` setpoint via
        `temp_calibration.txt` and sends the resulting command. Caps each
        target at MAX_SAFE_TEMP_C (default 45). To park a vial's heater
        off, pass the ambient temperature (~22 C) for that vial — this
        translates to a raw setpoint the closed loop cannot reach by
        heating, so the heater idles.

        Returns the current temperature readings in Celsius."""

    def set_temperature_raw(self, setpoints: list[int]) -> list[float]:
        """Escape hatch for calibration wizard and low-level debugging.

        Sends 16 raw setpoint integers verbatim as the `xr` command
        payload. NOTE: the setpoint convention is INVERTED — lower value
        = hotter target. `xr=0` requests ~82 C (drives heater to MAX),
        `xr=4095` is unreachably cold (definitive off). This method
        enforces a per-vial floor derived from MAX_SAFE_TEMP_C; callers
        that want to bypass safety must use a separate debug method.

        Returns the current temperature readings in Celsius."""

    def set_stir(self, speed_values: list[int]) -> None:
        """Send 16 stir speed values (0-15). No response."""

    def pump_command(self, vial: int, direction: str, seconds: float) -> None:
        """Activate pump. direction = 'influx' or 'efflux'."""

    def stop_all_pumps(self) -> None:
        """Emergency stop all pumps."""

    def emergency_shutdown(self) -> None:
        """Zero all actuators immediately. Called by watchdog."""

    def load_calibration(self, temp_cal_path, od_cal_path) -> None:
        """Load calibration files for unit conversion."""
```

### RS485 command details

All commands are ASCII strings sent over serial. Format: `{prefix}{values} !`

| Method                     | Serial command sent                                            | Expected response                          |
|----------------------------|----------------------------------------------------------------|-------------------------------------------|
| `read_temperature`         | `xr{16 setpoint integers, comma-separated} !`                  | `temp{16 ADC values, comma-separated}end` |
| `read_od`                  | `we{16 LED values, comma-separated} !`                         | `turb{16 ADC values, comma-separated}end` |
| `set_stir`                 | `zv{16 speed values, comma-separated} !`                       | (none)                                    |
| `pump_command` (single)    | `st{binary_address},0,{seconds}, !`                            | (none, write-only)                        |
| `stop_all_pumps`           | `stt,{32 ones — the pump mask},{16 zeros — the per-vial times}, !` | (none)                                    |
| `chemostat_command` (P2+)  | `stc,{16 rates},{bolus}, !`                                    | (none)                                    |

The setpoint integers in `read_temperature` are NOT raw PWM values; they are the closed-loop target the Arduino drives the thermistor ADC reading toward. See "Heater control convention" in §10. `set_temperature_celsius` handles the conversion automatically; only `set_temperature_raw` exposes the raw integers.

#### Fluidics sub-protocol

The pump Arduino's `st` address is a namespace selecting one of three sub-modes by the first character of the payload:

- **Single-fire** (`st<binary_pump_code>,0,<seconds>, !`) — turbidostat-style fire of one or more pumps for a duration. Used by every turbidostat dilution event.
- **Stop / multi-fire** (`stt,<32-bit mask>,<time0>,…,<time15>, !`) — explicit per-vial times for the masked pumps. The legacy uses this only for `stop_all_pumps` (all-ones mask, all-zero times).
- **Chemostat** (`stc,<rate0>,…,<rate15>,<bolus>, !`) — continuous rates per vial plus a bolus volume. Used by `update_chemo` in the legacy and re-implemented by the chemostat control mode in Phase 2.

Timing constraints:
- Minimum 50ms between serial commands
- Serial read timeout: 5 seconds
- If an Arduino doesn't respond, log the error and continue (do not hang)

### MockSerialManager

Identical interface, returns simulated data:
- Temperature: random walk around a setpoint with noise
- OD: logistic growth curve with dilution events
- Stir: no response needed (write-only)
- Pumps: logged but no physical effect

This allows full development and testing on any machine without eVOLVER hardware.

---

## 6. REST API

All endpoints return JSON. Errors return `{"error": "message"}` with appropriate HTTP status codes.

### Sensor endpoints (read-only)

```
GET /api/sensors/temperature
  Response: {"values": [30.1, 30.2, ...], "raw_adc": [423, 419, ...], "timestamp": "..."}

GET /api/sensors/od
  Response: {"values": [0.31, 0.42, ...], "raw_adc": [52341, 48211, ...], "timestamp": "..."}

GET /api/sensors/all
  Response: {"temperature": {...}, "od": {...}, "timestamp": "..."}
```

### Actuator endpoints (write)

```
POST /api/actuators/temperature
  Body: {"values_c": [37, 37, ...]}  // 16 target temperatures in Celsius
  Response: {"status": "ok", "current_temp_c": [30.1, ...], "raw_adc": [423, ...]}
  // Internally calls set_temperature_celsius; do NOT post raw setpoint integers here.
  // The calibration surface has a separate POST /api/calibration/raw/temperature
  // endpoint that exposes set_temperature_raw (409 while an experiment is RUNNING);
  // it replaced the old /api/actuators/temperature/raw route in Session O.

POST /api/actuators/stir
  Body: {"values": [8, 8, ...]}  // 16 speed values
  Response: {"status": "ok"}

POST /api/actuators/pump
  Body: {"vial": 0, "direction": "influx", "seconds": 5.0}
  Response: {"status": "ok"}

POST /api/actuators/emergency_stop
  Body: {}
  Response: {"status": "ok", "message": "All actuators zeroed"}
```

### Experiment endpoints

```
GET /api/experiments
  Response: {"experiments": [{"name": "...", "status": "running", ...}]}

POST /api/experiments/create
  Body: {
    "name": "my_experiment",
    "mode": "turbidostat",
    "vials": [0, 1, 2, 3],
    "params": {
      "temperature": 37,
      "stir_rate": 10,
      "od_lower": 0.2,
      "od_upper": 0.4,
      "pump_wait_minutes": 15,
      "volume_ml": 25
    }
  }
  Response: {"status": "created", "name": "my_experiment"}

POST /api/experiments/{name}/start
  Response: {"status": "running"}

POST /api/experiments/{name}/stop
  Response: {"status": "stopped", "message": "All actuators zeroed for experiment vials"}

GET /api/experiments/{name}/data
  Query params: ?vial=0&parameter=od&last_n=100
  Response: {"timestamps": [...], "values": [...]}

GET /api/experiments/{name}/status
  Response: {
    "name": "...",
    "status": "running",
    "elapsed_hours": 4.2,
    "vials": {
      "0": {"od": 0.34, "temp": 37.1, "last_pump": "2h ago", "growth_rate": 0.42},
      ...
    }
  }
```

### Calibration endpoints (§19 — IMPLEMENTED 2026-08-20 except the Tier 3 block)

Everything below exists as listed (`server/calibration_service.py` + `app.py`), with one
exception: the thermistor two-point / OD dilution series / stir routes are **not built**
(ROADMAP Session AA, Phase 4). The blank-session routes operate on the currently loaded
CREATED experiment rather than taking an experiment name; QC refusals return **422** with
the qc block, overridable by re-posting with an `override_reason`.

```
GET  /api/calibration/                # index: per subsystem — current version, age,
                                      #   staleness state, qc summary
GET  /api/calibration/temperature
  Response: {"slopes": [...], "intercepts": [...], "last_calibrated": "...",
             "version": "...", "outlier_vials": [...]}
GET  /api/calibration/od
  Response: {"params": [[...], [...], [...], [...]], "dark_subtracted": bool,
             "last_calibrated": "...", "version": "..."}
GET  /api/calibration/pump
  Response: {"flow_rates_ml_s": [...32],   // flat 32: index 0..15 influx, 16..31 efflux
             "last_calibrated": "...", "version": "...", "pump_seconds_since": [...32]}
  // Ordering is the canonical pump index (CLAUDE.md, "Pump command format"). An earlier
  // revision of this line used an {"influx": [...], "efflux": [...]} object; that was
  // retracted in favour of the flat-32 already specified in CALIBRATION_PROTOCOL §11.3,
  // which matches the hardware binary addressing. See §16.1.
GET  /api/calibration/history         # all versions, for provenance
GET  /api/calibration/staleness       # what is overdue and why (§19.6)

--- per-run OD blank (§19.2) ---
POST /api/calibration/od/blank/start
  Body: {"experiment": "name", "led_power": 2125, "stir_pwm": 8,
         "target_temp_c": 37.0, "n_samples": 5}
  → validates preconditions (conditions match the run; thermally settled), returns session id
POST /api/calibration/od/blank/dark        # n reads at LED 0
  Response: {"median": [...16], "sd": [...16], "n_valid": [...16]}
POST /api/calibration/od/blank/measure     # n reads at led_power; same shape
POST /api/calibration/od/blank/commit
  Response: {"status": "ok", "updated_rows": [2], "c_run": [...16],
             "od_offset_removed": [...16], "qc": {...}}
POST /api/calibration/od/blank/abort

--- pump gravimetric (§19.3) ---
POST /api/calibration/pump/start      # {pumps, fire_seconds, replicates, fluid,
                                      #  fluid_density_g_ml, bench_temp_c}
POST /api/calibration/pump/fire       # {pump_id} — fires once, returns actual duration
POST /api/calibration/pump/record     # {pump_id, replicate, mass_g}
GET  /api/calibration/pump/session    # progress; which pumps remain (resumability)
POST /api/calibration/pump/finish     # fit, QC, write versioned file
POST /api/calibration/pump/abort

--- thermistor two-point / OD dilution series / stir (§19.5) — NOT BUILT (Session AA) ---
POST /api/calibration/temperature/start | /point | /finish | /abort
POST /api/calibration/od/series/start   | /point | /finish | /abort
POST /api/calibration/stir/record       | /finish

--- raw escape hatches, calibration-only ---
POST /api/calibration/raw/temperature      # {setpoints: [...16]} — wraps set_temperature_raw
POST /api/calibration/raw/od_led           # {power: 0..2200} — needs building
```

Note the corrected blank response: **`"updated_rows": [2]`**, not `[0]` or `[1]`. The
per-run blank re-anchors the inflection parameter only; see the warning in §19.2 for why
writing rows 0 or 1 breaks the OD path. `od_offset_removed` is the number the operator
actually needs — it says how wrong the previous run was.

All `/api/calibration/*` mutating routes reject requests while an experiment is RUNNING,
and are the only routes permitted to reach the raw actuator paths (`set_temperature_raw`,
raw OD LED power). See §19.6.

Related, though it lives on the experiment surface:

```
POST /api/experiments/{name}/reconcile     # §19.4 post-run mass reconciliation
  Body: {"media_start_g": ..., "media_end_g": ..., "waste_start_g": ...,
         "waste_end_g": ..., "density_g_ml": ...}   // each side optional as a pair
  Response (as built — one block per side that was weighed, plus the overall flag):
    {"media": {"inferred_ml": ..., "measured_ml": ..., "ratio": ..., "within_tolerance": bool},
     "waste": {... same shape ...} | null,
     "within_tolerance": bool, "pump_calibration_version": "...", "timestamp": "..."}
  // Writes experiments/{name}/reconciliation.json and appends to
  // calibration/reconciliation_log.json (the staleness trend input). 409 while
  // that experiment is RUNNING.
```

### Consumables, hygiene, override, and template endpoints (not yet implemented)

```
GET  /api/consumables                       # §15 — levels, reserves, block state, forecast
  Response: {"bottles": [{"id": "media_a", "remaining_ml": 412.0,
                          "reserve_ml": 50.0, "blocked": false,
                          "hours_remaining_observed": 14.2,
                          "hours_remaining_predicted": 11.8,
                          "empty_at": "2026-08-04T03:40:00",
                          "estimate_quality": "calibrated" | "uncalibrated"}],
             "waste": {"filled_ml": 2180.0, "capacity_ml": 5000.0, "blocked": false}}

POST /api/actuators/pump                    # §16 — extended, backwards compatible
  Body: {"vial": 0, "direction": "influx", "volume_ml": 5.0}
     or {"vial": 0, "direction": "influx", "seconds": 5.0}
  Response: {"status": "ok", "requested_ml": 5.0, "delivered_ml": 5.0, "seconds": 5}

POST /api/actuators/pump/preview            # §16 — quantisation preview, no side effects
  Body: {"vial": 0, "direction": "influx", "volume_ml": 2.5}
  Response: {"deliverable_ml": 2.0, "seconds": 2, "min_ml": 1.0, "quantised": true}

GET  /api/hygiene                           # §18
POST /api/hygiene/record
POST /api/service/sterilize/start           # service mode only; refuses while RUNNING
POST /api/service/sterilize/advance
POST /api/service/sterilize/abort

POST /api/vials/{vial}/override             # §21
  Body: {"duration_minutes": 10, "reason": "sampling"}
  Response: {"status": "ok", "expires_at": "..."}
POST /api/vials/{vial}/override/release

GET  /api/templates                         # §23
POST /api/templates                         # save current or named experiment as template
GET  /api/templates/{name}
DELETE /api/templates/{name}

GET  /api/experiments/{name}/events         # §20 — unified event log
  Query params: ?level=warning&category=pump&last_n=200
GET  /api/growth_rate                       # §17 — current per-vial estimates
  Response: {"vials": {"0": {"mu_per_hour": 0.43, "doubling_time_h": 1.61,
                             "r_squared": 0.98, "method": "segment",
                             "mu_dilution": 0.41, "diverged": false}}}
```

---

## 7. WebSocket events

Real-time data pushed from server to all connected browsers every 10 seconds.

### Server -> Client

```javascript
// Live sensor update (every 10 seconds)
socket.emit('sensor_update', {
    timestamp: "2026-05-12T14:30:00",
    temperature: {
        calibrated: [37.1, 37.0, 36.9, ...],  // Celsius
        raw: [423, 425, 427, ...]               // ADC
    },
    od: {
        calibrated: [0.34, 0.42, 0.28, ...],   // OD600
        raw: [52341, 48211, 54102, ...]          // ADC
    }
});

// Experiment event (pump fired, phase change, alert)
socket.emit('experiment_event', {
    type: "pump",
    vial: 3,
    direction: "influx",
    duration_seconds: 8.2,
    timestamp: "..."
});

// Alert
socket.emit('alert', {
    level: "warning",  // "info", "warning", "critical"
    vial: 5,
    message: "OD has not changed in 2 hours",
    timestamp: "..."
});

// Server status
socket.emit('server_status', {
    uptime_hours: 48.3,
    serial_connected: true,
    active_experiments: 1,
    watchdog_ok: true
});
```

### Client -> Server

```javascript
// Manual actuator command from UI
socket.emit('set_stir', {values: [8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8]});

// Emergency stop button
socket.emit('emergency_stop');
```

---

## 8. Data storage

### CSV file format

One file per vial per parameter per experiment. Files are append-only during an experiment.

**OD data** (`vial00_OD.csv`):
```csv
timestamp,elapsed_hours,raw_adc,calibrated_od
2026-05-12T14:30:00,0.000,52341,0.000
2026-05-12T14:30:10,0.003,52298,0.012
...
```

**Temperature data** (`vial00_temp.csv`):
```csv
timestamp,elapsed_hours,raw_adc,calibrated_temp_c
2026-05-12T14:30:00,0.000,423,37.1
...
```

**Pump log** (`vial00_pump_log.csv`):
```csv
timestamp,elapsed_hours,direction,duration_seconds,od_at_pump
2026-05-12T16:45:00,2.250,influx,8.2,0.41
2026-05-12T16:45:09,2.253,efflux,13.2,0.41
...
```

### Experiment configuration (`config.json`)

Saved when experiment is created. Serves as a complete record of parameters used.

```json
{
    "name": "adaptation_exp_001",
    "created": "2026-05-12T14:00:00",
    "mode": "turbidostat",
    "vials": [0, 1, 2, 3, 4, 5, 6, 7],
    "parameters": {
        "temperature_c": 37,
        "stir_rate": 10,
        "od_lower_thresh": [0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2],
        "od_upper_thresh": [0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4],
        "pump_wait_minutes": 15,
        "volume_ml": 25,
        "od_led_power": 2125,
        "efflux_extra_seconds": 0
    },
    "calibration": {
        "temp_cal_file": "temp_calibration.txt",
        "od_cal_file": "OD_cal.txt",
        "pump_flow_rates": [
            0.95, 1.1, 0.975, 0.85, 0.95, 1.05, 1.05, 1.05,
            1.025, 1.125, 1.0, 1.0, 1.05, 1.15, 1.1, 1.025,
            0.95, 1.1, 0.975, 0.85, 0.95, 1.05, 1.05, 1.05,
            1.025, 1.125, 1.0, 1.0, 1.05, 1.15, 1.1, 1.025
        ]
    },
    "notes": "Replicate adaptation experiment in LB, 8 vials"
}
```

---

## 9. Experiment engine

### Main loop

Runs in a background thread, ticking every 10 seconds:

```
every 10 seconds:
    1. Read OD from all active vials (SerialManager.read_od)
    2. Read temperature from all active vials (SerialManager.read_temperature)
    3. Log sensor data to CSV files
    4. Emit sensor_update to all connected WebSocket clients
    5. For each active experiment:
        a. Run the control mode logic (turbidostat, chemostat, etc.)
        b. If actuator changes needed, send commands via SerialManager
        c. Log any pump events
        d. Check alert conditions
    6. Update stir rates (resend every cycle to prevent drift)
    7. Pet the watchdog timer
```

### Turbidostat control mode (MVP)

Per vial per cycle:

```
inputs:
    od_history     — last N OD readings for this vial
    od_lower       — lower OD threshold
    od_upper       — upper OD threshold
    pump_wait      — minimum minutes between pump events
    last_pump_time — timestamp of last pump event
    flow_rate      — ml/sec for this vial's pump
    volume         — vial volume in ml
    efflux_extra   — extra seconds for efflux pump

logic:
    average_od = mean(od_history[-5:])

    if average_od > od_upper and target != od_lower:
        set target = od_lower

    if average_od < midpoint(od_lower, od_upper) and target != od_upper:
        set target = od_upper

    if average_od > target:
        # Refractory gate FIRST. Nothing is computed, and nothing is
        # remembered, on a cycle that will not fire.
        if (now - last_pump_time) < pump_wait:
            return

        # The lagged mean above decided WHETHER to dilute; the newest sample
        # sizes the bolus. The mean is a noise filter, not an estimate of the
        # culture's density right now.
        sizing_od = od_history[-1]
        pump_time = -(ln(od_lower / sizing_od) * volume) / flow_rate
        pump_time = min(pump_time, 20)  # cap at 20 seconds

        # Truncate to whole seconds and DISCARD the remainder. No accumulator
        # -- see "Sub-second pump-time deficit" below for why one belongs in
        # the chemostat and not here.
        whole = int(pump_time)
        if whole >= 1:
            fire influx + efflux for whole seconds
            fire efflux alone for efflux_extra seconds
            log pump event
            emit experiment_event
```

> **Corrected 2026-08-21 (CONTROL_MODE_AUDIT.md T-1 – T-4).** The previous
> revision of this block specified the deficit accumulator *and* placed the
> `pump_wait` check after it. That ordering was a **spec defect, not a coding
> slip**: `pump_time` is an absolute correction, so adding it to a persistent
> accumulator on every cycle — including the cycles the refractory gate then
> blocked — built an integrator with no anti-windup. Closed-loop simulation
> measured the OD floor breached by 10–47 % depending on band width, worst
> with a tight band and a long `pump_wait`. Wide bands hid it completely,
> because the required bolus already exceeded the 20 s cap and capped and
> wound-up behaviour coincided.
>
> Because `int(t) <= t_needed` always, truncating without carrying makes the
> floor unbreachable by construction. The cost is that a *second* bolus inside
> one diluting episode may be sub-second and is then dropped; that is bounded
> and safe, because `validate_control_parameters` guarantees at experiment
> creation that the first bolus of every episode is >= 1 s (see below).

### Chemostat control mode (Phase 2)

```
inputs:
    dilution_rate  — volumes per hour
    volume         — vial volume in ml
    flow_rate      — ml/sec
    bolus_interval — derived from dilution_rate

logic:
    # Optional start gate. Absent both parameters, dilution begins at
    # inoculation density as before.
    if not dilution_started:
        if (start_od is set and last_od >= start_od)
           or (start_after_seconds is set and elapsed >= start_after_seconds):
            dilution_started = True
        else:
            return

    every bolus_interval seconds:
        # Sized from the time that ACTUALLY elapsed, clamped so a resume
        # after an outage cannot fire one enormous catch-up bolus.
        elapsed = min(now - last_bolus_time, 4 * bolus_interval)
        pump_time_per_bolus = (dilution_rate * volume * elapsed / 3600) / flow_rate
        if pump_time_per_bolus > safety_cap:
            raise a bolus_cap_clipped event      # the requested D is unreachable
            pump_time_per_bolus = safety_cap

        # Sub-second deficit accumulator (hardware-floor workaround;
        # see "Sub-second pump-time deficit" note below).
        deficit = min(deficit + pump_time_per_bolus, safety_cap)
        if int(deficit) >= 1:
            whole = int(deficit)
            deficit -= whole
            fire influx for whole seconds
            fire efflux for whole + efflux_extra seconds
            total_volume_ml += whole * flow_rate    # DELIVERED, not intended
```

Constraints and bookkeeping (CONTROL_MODE_AUDIT.md C-1 – C-5):

- **`bolus_interval_seconds` must be >= 2 s.** `safety_cap` is
  `min(20, bolus_interval - 1)`; below a 2 s interval that falls under the
  firmware's 1 s resolution, so `int(deficit) >= 1` is never true and the
  controller silently delivers nothing while booking the full volume.
  Rejected in the constructor and again at experiment creation.
- **Boli are sized from elapsed time, never the nominal interval.** The sensor
  loop sleeps `interval - work`, so its true period is `max(interval, work)`:
  it can only ever run slower than nominal, which makes a nominal-interval
  bolus under-dose systematically. Measured −17 % to −34 % D under realistic
  loop overrun, −29 % at 30 % dropped cycles.
- **OD validity is not a precondition.** A chemostat is open-loop; the engine
  consults `controller.requires_od` and only suspends modes that genuinely
  close the loop on OD. See §9 "Sensor validity vs. control gating" below.
- **`total_volume_ml` books what was delivered**, `total_volume_intended_ml`
  what was prescribed. When the duration cap binds the two diverge, and that
  divergence is the honest record — booking intent is how a run reports
  D=5.00 while the truth is 4.80. `boli_fired` counts actual deliveries;
  `bolus_cycles` counts gate-passing cycles.
- **Optional `start_od` / `start_after_seconds`** hold the first bolus until
  the culture is dense enough (or enough time has passed). The two are OR'd,
  deliberately: with both set the timeout is the escape hatch for a sleeve
  whose OD never reads, so a dead sensor cannot hold the run at inoculation
  density indefinitely. Without a gate, dilution at D above the culture's µ
  washes it out from inoculation and nothing notices.

#### Sub-second pump-time deficit (hardware-floor workaround)

The 2016 firmware only accepts whole-second pump times; the legacy Mac
client formatted `pump_time` with `%d`, so any cycle whose computed
pump_time was sub-second silently truncated to zero and never fired.
For a slow chemostat (e.g. D=0.5/h, V=25, T=60: pump_time_per_bolus ≈
0.21 s) this means the firmware would deliver no dilution at all.

**The chemostat keeps a per-vial deficit accumulator; the turbidostat
deliberately does not.** This asymmetry is the single most important thing in
this section, and getting it wrong is what CONTROL_MODE_AUDIT.md T-1 found.

In the **chemostat** the accumulated quantity is a genuine per-interval
*increment* of prescribed dilution. Every bolus cycle's `pump_time_per_bolus`
is added to the deficit (capped at the mode's safety limit), and the
controller fires `int(deficit)` seconds when that reaches >= 1 s, carrying the
fractional remainder forward. Total dilution delivered equals total dilution
prescribed, modulo the < 1 s residual sitting in the accumulator at any
moment — which the legacy behaviour did not guarantee. Deficit state is
persisted in `state.json` and clamped to `[0, safety_cap]` on restore so a
corrupted file cannot suppress the next pump or grant an over-cap catch-up
bolus.

In the **turbidostat** the same-looking quantity is an *absolute setpoint
error* — the seconds needed to bring the current OD down to `od_lower` — and
accumulating it is an integrator with no anti-windup. It is therefore
truncated and the remainder discarded. Nothing is lost that matters:
hysteresis guarantees the first bolus of each diluting episode is at least
`ln(od_upper/od_lower) * V / F` seconds, which is sub-second only for a band
narrower than `exp(F/V)` ~ 1.041 at V=25, F=1 — narrower than the 3.9 % OD
step a single 1 s bolus produces, i.e. a band the machine could not track
anyway. `validate_control_parameters` rejects such a band at experiment
creation, which is what turns the legacy silent stall into a visible error.

An earlier revision of this section claimed "total dilution delivered equals
total dilution prescribed" for *both* modes. That claim holds for the
chemostat and was false for the turbidostat.

#### Sensor validity vs. control gating

A dropped or out-of-range sensor read is a *sensor* condition. Whether it
should suspend *control* depends on the mode, and the engine must not conflate
the two (CONTROL_MODE_AUDIT.md C-2):

- Each controller exposes **`requires_od`**. It is `True` for the turbidostat
  and morbidostat, which close the loop on OD, and `False` for the chemostat,
  which does not. The chemostat raises it only while an unmet `start_od` gate
  is armed.
- `run_cycle` suspends the control decision for a vial only when its OD is
  unusable **and** that vial's controller requires OD. `out_of_range` is the
  case that matters most: it means the culture is denser than the calibration
  covers, i.e. exactly when a chemostat must keep diluting.
- A dropped **temperature** read skips per-vial heater safety for that cycle
  and nothing else. Pumping does not depend on temperature, and the Arduino
  closes the heater loop on its own thermistor whether or not the Pi received
  the sample.

#### Control-parameter validation at experiment creation

`validate_control_parameters` (`experiment_engine.py`) runs inside
`create_experiment`. Hard errors become HTTP 400; warnings are returned in the
create response as `warnings: [...]` **and** raised through the alert funnel so
they land in the run's event log.

| | Condition | Result |
|---|---|---|
| turbidostat / morbidostat | `ln(od_upper/od_lower) * V / F < 1 s` | **400** — band can never dilute |
| chemostat | `bolus_interval_seconds < 2 s` | **400** — delivers nothing, books everything |
| turbidostat / morbidostat | first bolus of an episode < 2 s | warning — truncation eats a large share of each dose |
| chemostat | `D * V * interval / 3600 > safety_cap * F` | warning — every bolus clipped, names the reachable D |
| all | `efflux_extra_seconds == 0` | warning — volume regulation disengaged (§16.2) |

### Morbidostat control mode (Phase 2)

```
inputs:
    target_od       — OD setpoint to maintain
    growth_rate     — estimated from OD sliding window
    drug_conc       — current drug concentration in feed
    drug_step       — multiplication factor for escalation
    adaptation_threshold — growth rate recovery that triggers escalation

logic:
    estimate growth_rate from last 30 minutes of OD data
    if growth_rate > adaptation_threshold:
        drug_conc *= drug_step
        switch feed to higher drug concentration
        log escalation event
    standard dilution to maintain target_od
```

### Growth rate feedback control (Phase 2)

```
inputs:
    target_growth_rate — desired mu (/hr)
    od_ceiling         — max OD before forced dilution

logic:
    estimate current growth_rate from OD window
    error = target_growth_rate - current_growth_rate
    adjust dilution_rate proportionally to error (PI controller)
    if od > od_ceiling: force dilution regardless
```

---

## 10. Safety systems

### Watchdog timer

A background thread that expects a heartbeat from the experiment loop every cycle. If no heartbeat is received for 30 minutes (configurable), it zeros all actuators.

```python
class Watchdog:
    def __init__(self, serial_manager, timeout_minutes=30):
        self.timeout = timeout_minutes * 60
        self.last_heartbeat = time.time()
        self.start_monitoring()

    def pet(self):
        """Called by experiment loop each cycle."""
        self.last_heartbeat = time.time()

    def check(self):
        """Called by monitoring thread."""
        if time.time() - self.last_heartbeat > self.timeout:
            self.serial_manager.emergency_shutdown()
            self.alert("Watchdog triggered — all actuators zeroed")
```

### Shutdown handler

Registered for SIGTERM, SIGINT, and atexit:

```python
def shutdown():
    serial_manager.emergency_shutdown()
    save_experiment_state()
    log("Server shutdown — all actuators zeroed")

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)
atexit.register(shutdown)
```

### Emergency stop

- Red button always visible in the UI, regardless of which page is active
- Fires `POST /api/actuators/emergency_stop`
- Zeros heaters, stirrers, and pumps simultaneously
- Does not require confirmation — immediate action
- Logs the event

### Confirmation dialogs

Required before:
- Starting an experiment (shows summary of parameters)
- Stopping a running experiment
- Setting temperature above 45C
- Running pumps for more than 30 seconds
- Deleting experiment data

### Heater control convention (read before reasoning about heater safety)

The temperature Arduino's `xr` value is **not a PWM duty cycle**. It is a setpoint that the Arduino's closed loop drives the thermistor ADC reading toward. The temperature calibration has a **negative slope**, so:

- Lower `xr` integer → hotter target. `xr=0` requests ~82 °C (drives the heater to maximum).
- Higher `xr` integer → colder target. `xr=4095` is unreachably cold and is the only definitive "off."

Every safety bullet below is written assuming this convention. Any code that treats `xr` like a PWM (where 0 means off) will behave the opposite of intended.

### Heater safety

- Software safety cap on the **maximum target temperature in Celsius** (configurable, default 45 °C). Enforced inside `set_temperature_celsius`; `set_temperature_raw` enforces the equivalent per-vial floor on the raw setpoint integer (computed via `(MAX_SAFE_TEMP_C - intercept[vial]) / slope[vial]`).
- Closed-loop sanity check each cycle: if the measured temperature exceeds the active target by more than 5 °C, **lower the target by 2 °C** (which corresponds to *raising* the raw setpoint integer — under the inverted convention this asks the closed loop for a cooler temperature, so the Arduino backs off the heater PWM).
- If any vial reads > 50 °C, **park that vial's heater off** by setting its target to ambient (~22 °C) via `set_temperature_celsius`, OR equivalently by sending `set_temperature_raw` with that vial's setpoint = 4095. **Do not** send raw `0` to "zero" the heater — that command drives it to ~82 °C, which is the failure mode this safety bullet is supposed to prevent.

---

## 11. Frontend design

### Dashboard page (index.html)

The main page that every user sees. Always accessible, even during experiments.

```
+----------------------------------------------------------+
|  eVOLVER Control System           [Emergency Stop]  [Menu]|
+----------------------------------------------------------+
|                                                          |
|  Vial Grid (4x4)                                         |
|  +--------+ +--------+ +--------+ +--------+            |
|  | Vial 0 | | Vial 1 | | Vial 2 | | Vial 3 |            |
|  | 37.1 C | | 37.0 C | | 36.9 C | | 37.2 C |            |
|  | OD 0.34| | OD 0.42| | OD 0.28| | OD 0.51|            |
|  | [stir] | | [stir] | | [stir] | | [stir] |            |
|  +--------+ +--------+ +--------+ +--------+            |
|  +--------+ +--------+ +--------+ +--------+            |
|  | Vial 4 | | Vial 5 | | Vial 6 | | Vial 7 |            |
|  | ...    | | ...    | | ...    | | ...    |            |
|  +--------+ +--------+ +--------+ +--------+            |
|  +--------+ +--------+ +--------+ +--------+            |
|  | Vial 8 | | Vial 9 | | Vial 10| | Vial 11|            |
|  +--------+ +--------+ +--------+ +--------+            |
|  +--------+ +--------+ +--------+ +--------+            |
|  | Vial 12| | Vial 13| | Vial 14| | Vial 15|            |
|  +--------+ +--------+ +--------+ +--------+            |
|                                                          |
|  Click a vial for detail view and controls               |
|                                                          |
+----------------------------------------------------------+
|  Status: Experiment "adapt_001" running | 4.2 hrs elapsed |
+----------------------------------------------------------+
```

Each vial card shows:
- Vial number (user-assignable label in Phase 2)
- Current temperature (colored: green if at setpoint, orange if deviating, red if critical)
- Current OD
- Stir indicator (spinning icon when active)
- Experiment group color (if assigned)
- Pump activity indicator (flashes when pumping)

Clicking a vial opens a detail panel:
- Real-time OD and temperature chart (last 2 hours)
- Current setpoints
- Manual controls: temperature slider, stir slider, pump buttons (influx/efflux with duration input)
- Pump history log
- Growth rate estimate (Phase 2)

### Experiment setup page

Step-by-step wizard:

```
Step 1: Name your experiment
        [text input]
        [optional notes textarea]

Step 2: Select vials
        [4x4 grid, click to toggle, selected vials highlighted]

Step 3: Choose control mode
        [Turbidostat]  [Chemostat]  [Morbidostat]  [Growth Rate]
        (only turbidostat available in Phase 1)

Step 4: Set parameters
        Temperature: [slider 20-50 C, default 37]
        Stir rate:   [slider 0-15, default 10]
        Lower OD:    [slider 0.05-2.0, default 0.2]
        Upper OD:    [slider 0.1-3.0, default 0.4]
        Pump wait:   [slider 5-60 min, default 15]
        (per-vial overrides available via "Advanced" toggle)

Step 5: Review and start
        [summary of all parameters]
        [Confirm and Start Experiment] button
```

### Color system

- Green: parameter at setpoint or within normal range
- Yellow/orange: parameter deviating from setpoint (> 2C or > 0.1 OD from target)
- Red: critical condition (temperature > 50C, pump failure, sensor read error)
- Blue: informational (pump event, experiment phase change)
- Gray: vial not in use / no experiment assigned

### Responsive design

Must work on:
- Lab Mac Mini (1920x1080, any browser)
- Researcher's laptop (various sizes)
- Phone (for remote monitoring via local network)

---

## 12. Testing strategy

### MockSerialManager

Simulates the full RS485 protocol with realistic data:

```python
class MockSerialManager:
    """Drop-in replacement for SerialManager for development and testing.

    The mock operates in Celsius internally — it does not model the inverted
    `xr` setpoint convention. The real SerialManager handles °C ↔ raw
    setpoint conversion; the mock just stores targets in °C directly. This
    keeps the simulated control loop intuitive and avoids accidentally
    encoding the "0 = off" bug in test code.
    """

    AMBIENT_C = 22.0

    def __init__(self):
        self.target_temps_c = [self.AMBIENT_C] * 16   # start parked at ambient
        self.stir_speeds = [0] * 16
        self.simulated_temps = [self.AMBIENT_C] * 16  # start at room temp
        self.simulated_ods = [0.05] * 16              # start at low OD
        self.growth_rates = [0.4] * 16                # doublings per hour

    def set_temperature_celsius(self, temps_c):
        self.target_temps_c = list(temps_c)

    def set_temperature_raw(self, setpoints):
        # Convert raw setpoints back to Celsius via the calibration so the
        # mock's behaviour matches the real device for raw-API callers.
        self.target_temps_c = [self._setpoint_to_c(i, s)
                                for i, s in enumerate(setpoints)]

    def read_temperature(self):
        # Simulate thermal response toward the per-vial target, with noise.
        for i in range(16):
            self.simulated_temps[i] += (
                self.target_temps_c[i] - self.simulated_temps[i]
            ) * 0.1 + random.gauss(0, 0.1)
        return self.simulated_temps[:]

    def read_od(self, led_power=2125):
        # Simulate logistic growth with dilution
        for i in range(16):
            self.simulated_ods[i] *= (1 + self.growth_rates[i] * (10/3600))
            self.simulated_ods[i] += random.gauss(0, 0.005)
        return self.simulated_ods[:]

    def _setpoint_to_c(self, vial, setpoint):
        # Forward calibration: T = setpoint * slope + intercept
        return setpoint * self.slope[vial] + self.intercept[vial]
```

### Development testing (home laptop)

- All backend tests run against MockSerialManager
- Frontend tested in browser against mock backend
- No hardware required
- Run with: `python app.py --mock`

### Hardware testing (lab)

- Deploy to RPi, run against real serial
- Checklist per feature:
  - Read temperature: values match thermometer within 1C
  - Read OD: values match spectrophotometer reference within 10%
  - Set stir: visual confirmation at multiple speeds
  - Pumps: measured flow matches expected within 15%
  - Emergency stop: all actuators zero within 1 second
  - Watchdog: actuators zero after configured timeout
  - WebSocket: browser updates within 1 second of sensor read
  - CSV files: correct format, appending properly, no data loss
  - Server restart: experiment resumes with correct state

---

## 13. Deployment

### First deployment to RPi

```bash
# SSH into RPi
ssh pi@192.168.1.2

# Stop old system
sudo supervisorctl stop all

# Install dependencies
sudo apt-get update
sudo apt-get install python3 python3-pip git
pip3 install flask flask-socketio pyserial

# Clone repo
cd /home/pi
git clone https://github.com/yiannis-scotiniadis/eVOLVER_FileStruct.git evolver-gui
cd evolver-gui

# Copy calibration files
cp /home/pi/eVOLVER_UDP/temp_data.txt calibration/  # if needed
# (calibration files should already be in the repo)

# Start server
python3 server/app.py

# Access from any browser: http://192.168.1.2:5000
```

### Auto-start on boot

Create a systemd service (replaces old supervisor config):

```ini
[Unit]
Description=eVOLVER Web Control Server
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/evolver-gui
ExecStart=/usr/bin/python3 server/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Update procedure

```bash
ssh pi@192.168.1.2
cd /home/pi/evolver-gui
# DO NOT update while an experiment is running
git pull origin main
sudo systemctl restart evolver-gui
```

---

## 14. Open questions

These decisions can be deferred but should be resolved before Phase 2:

1. **Python version on RPi:** Need to verify Python 3 availability. If only 2.7, the server code must be 2.7 compatible or we install Python 3.

2. **Vial numbering:** The physical layout of vials in the eVOLVER needs to be mapped. The vial-identification script (one stirrer at a time) needs to be run to create a mapping from logical vial number to physical position.

3. **Calibration validity — partly answered, and the answer is bad.** The existing temp_calibration.txt and OD_cal.txt are from a previous user (Bernie/Brandon). They have now been audited **numerically** (§19.0): the machine reports OD 0.115–0.444 for a sterile blank, vial 0's temperature calibration is a 5-sigma outlier probably running that sleeve ~9 °C cold while displaying the setpoint, and vial 1's OD fit diverges inside its own working range. **Bench verification against known standards is still outstanding** and is now a prerequisite for anything quantitative, not a nice-to-have. Procedures: `CALIBRATION_PROTOCOL.md` Tiers 2 and 3.

4. **Heater stuck-on issue:** Diagnosis in progress. If MOSFETs are failed-on, hardware repair is needed before temperature-controlled experiments. OD and stir experiments can proceed independently. **Note:** the inverted `xr` setpoint convention (see §10 "Heater control convention") was discovered while debugging this issue — any prior code or operator command that sent `xr=0` "to turn the heaters off" was actually requesting ~82 °C, which would explain stuck-on behaviour even with healthy MOSFETs. Before concluding hardware is at fault, verify the heaters do turn off when sent `xr=4095` (or `set_temperature_celsius` with ambient target).

5. **Network access:** Currently the eVOLVER is on a dedicated Netgear router (192.168.1.x). For remote monitoring, the router could be connected to Yale's network, or a VPN tunnel could be set up. This is a Phase 4 concern.

6. **Concurrent experiments:** Phase 2 allows multiple experiment groups, but the RS485 bus is shared. The serial manager must ensure commands for different experiments don't interfere. Since all 16 vials are always read in a single command, this is primarily a software isolation concern, not a hardware one. **Update:** the recommended path is vial groups within one experiment (§2 Phase 2, `ROADMAP.md` Session Y) rather than concurrent engine instances — same practical capability, far less concurrency risk in the code path that drives heaters.

### Questions raised by the August 2026 lab meeting

7. **Pump flow calibration is the load-bearing unknown.** Media levels, the consumables interlock (§15), volume-based pumping (§16), and the dilution-rate growth estimator (§17) all rest on `pump_flow_rates`, which is currently a hardcoded default array of plausible-looking numbers. Nothing downstream is more accurate than this array. Gravimetric calibration (§19) should be scheduled before the next real run.

8. **Is bottle level worth measuring rather than inferring?** All volume tracking is open-loop: `duration × flow_rate`, accumulated. It cannot detect a disconnected tube, a kinked line, a pump that stalls, or an operator topping up a bottle without telling the software. A float switch or a load cell under each bottle would convert the interlock from an estimate into a measurement. This is the highest-value hardware addition currently on the table and should be costed.

9. **What growth-rate estimate does the lab consider authoritative?** §17 proposes computing two (segment regression on OD, and dilution-rate) and reporting both. Before the analysis pipeline hardens around one of them, the lab should decide which is quoted in figures — they answer subtly different questions, and their disagreement is itself a useful diagnostic.

10. **Does the Pi have outbound internet?** Notifications (§22) and off-box backup (§24) both need egress. The Pi sits behind a dedicated Netgear router; Tailscale is deployed (`deploy/ts-keepalive/`), which suggests egress exists, but this has not been verified for arbitrary outbound HTTPS. Confirm before building either feature.

11. **Who performs the bench calibration work?** §19 needs roughly 40 minutes of gravimetric pump work; §25 needs about two hours of stir RPM measurement; full OD/temperature recalibration (Phase 4) needs several more. None of it is software effort, and all of it blocks software accuracy. Assign an owner.

12. **Sterilisation record threshold.** §18 warns when fluidics were last sterilised longer ago than a threshold, defaulting to 14 days. The lab should set the real number — and confirm the warning stays soft, since the software cannot verify the record and a hard gate on an unverifiable claim only teaches people to click through it.
---

## 15. Consumables safety interlock

**Status: IMPLEMENTED 2026-08-20 (`ROADMAP.md` Session K).**
`experiment_engine.py` — `_consumables_block_reason`, `_handle_consumables_block`,
`_bottle_blocked_locked`, `_waste_blocked_locked`. Blocks clear only via `refill_media`.
Suppressed attempts are recorded as `pump_suppressed` events (§20.2).

The engine tracks `_bottle_consumed_ml` and `_waste_filled_ml` and raises low/high alerts,
but nothing stops pumping. An overnight bottle-empty condition currently results in the
influx pump pushing air while efflux keeps removing broth — the vial drains and the run is
lost. A full waste carboy floods the bench.

### Behaviour

A gate evaluated in `run_cycle` **before any pump dispatch**, for every candidate action:

| Condition | Action | Alert |
|---|---|---|
| `bottle.remaining_ml <= reserve_ml` | Suppress influx for every vial fed by that bottle | `critical` |
| `waste.filled_ml >= capacity_ml - reserve_ml` | Suppress **all** pumping (influx without efflux overflows the vial) | `critical` |
| Every active vial blocked | Auto-enter maintenance mode | `critical` |

```
reserve_ml (media) = max(50.0, 0.05 * initial_volume_ml)
reserve_ml (waste) = max(100.0, 0.05 * capacity_ml)
```

Blocking is **sticky**: it clears only on an explicit `refill_media` call. It must never
clear on its own, because the volume estimate has no way to recover — if the software
believes a bottle is empty, only a human can establish otherwise.

Suppressed pump attempts are logged to `events.csv` (§20) with the reason, so a run that
quietly stopped diluting is diagnosable after the fact.

### Accuracy caveat — must be surfaced in the UI

Volume tracking is open-loop inference (`duration × flow_rate`), not measurement. It
drifts with pump wear, tubing compliance, and calibration error, and it is blind to a
disconnected tube or a manually topped-up bottle.

- Present levels as estimates, never as measurements.
- Tag each bottle's estimate `calibrated` or `uncalibrated` depending on whether the
  experiment used measured `pump_flow_rates` (§19) or the hardcoded defaults, and display
  that distinction.
- **The cheap partial answer is to weigh the bottle.** §19.4 adds a post-run mass
  reconciliation — media and waste masses at start and end, compared against the inferred
  pumped volume. It is the only check that can see a kinked line, a stalled pump, or a
  manually topped-up bottle, and its drift over weeks is the tubing-wear signal that should
  trigger recalibration. Ship the estimate; use §19.4 to learn how wrong it is.
- See §14 open question 8 on replacing inference with a float switch or load cell — §19.4
  will produce the evidence for how much that hardware is actually needed.

---

## 16. Volume-based fluidics

**Status: IMPLEMENTED 2026-08-20 (`ROADMAP.md` Session L).**
`compute_pump_quantization` in `experiment_engine.py`; `volume_ml` branch of
`POST /api/actuators/pump`; mL/seconds toggle with a live "will deliver" preview in
`index.html`. Sub-second requests are rejected with the per-vial minimum rather than
truncated to zero.

> **Units are right; the numbers are only as good as `pump_flow_rates`,** which is still the
> unmeasured hardcoded default array. Session O3 (gravimetric calibration) is what makes a
> requested 5 mL actually be 5 mL.

Manual pump controls currently take a duration in seconds. Researchers work in
millilitres, so every manual dilution requires mental arithmetic against a per-vial flow
rate the UI does not display.

### Behaviour

Manual controls take **mL** by default, with seconds available behind an advanced toggle.

```
seconds = volume_ml / flow_rate_ml_s[vial][direction]
```

Influx and efflux volumes are independently settable — the most common manual operation
is removing a few mL for a sample, which has no influx counterpart.

### Firmware quantisation — the constraint that must be visible

The 2016 firmware accepts **whole seconds only**. At flow rates of 0.85–1.15 mL/s the
minimum deliverable bolus is roughly 1 mL and the quantisation step is roughly 1 mL,
differing per vial.

- Show requested vs deliverable volume before firing: *"requested 2.5 mL → will deliver
  2.0 mL (2 s)"*. `POST /api/actuators/pump/preview` provides this without side effects.
- Requests below one second must be **rejected with an explanation naming that vial's
  minimum**, never silently truncated. Silent truncation is precisely the legacy `%d` bug
  documented in §9, and it fails invisibly.
- Log both requested and delivered volume; the difference is real experimental error and
  belongs in the record.

Automatic dilutions in the control modes are unaffected — they already handle sub-second
amounts correctly via the deficit accumulator (§9). This section governs the manual path
only, where there is a human who can be told what will actually happen.

### 16.1 Per-pump flow rates — schema and the engine gap

**There are 32 physically distinct pumps and they do not share a flow rate.** Influx and
efflux are separate peristaltic heads with separate tubing; firing both for the same
duration does not move the same volume.

**Canonical schema — a flat 32-element array**, ordered by the pump index convention in
`CLAUDE.md` ("Pump command format"):

```
index 0..15   →  influx pump for vial (index)
index 16..31  →  efflux pump for vial (index - 16)
```

That is, array index equals the exponent in the hardware binary address (`2^N` influx,
`2^(N+16)` efflux), so the software index and the wire address never diverge. This matches
the format already specified in `CALIBRATION_PROTOCOL.md` §11.3.

> #### ✅ Engine gap closed 2026-08-20 (ROADMAP Session O3a)
>
> `_resolve_flow_rates` used to coerce through `_as_list_of_16`, so a 32-element array
> raised `ValueError: 'pump_flow_rates' list must have length 16, got 32` at experiment
> creation — while three documents claimed populating
> `config["calibration"]["pump_flow_rates"]` was "the whole integration". The plumbing
> has now landed and the claim is true.

Engine changes as implemented (Session O3a, 2026-08-20):

1. `_resolve_flow_rates` resolves through `_as_flow_rates_32`: a scalar broadcasts to all
   32; a length-16 list broadcasts each vial's rate to both directions (the pre-O3
   behaviour, and the correct initial state until O3 has actually measured anything); a
   length-32 list is used as-is. Validated at experiment creation.
2. All three controllers carry `flow_rate_influx_ml_s` **and** `flow_rate_efflux_ml_s`,
   initialised equal when only the deprecated `flow_rate_ml_s` kwarg is given (which
   remains accepted, and survives as a read-only alias for the influx rate). Only the
   influx rate participates in dilution timing
   (`pump_time = -(ln(od_lower/avg_od) × volume) / flow_rate_influx`).
3. `_debit_media_locked` still computes `efflux_ml` from the **influx** rate,
   deliberately, with a TODO pointing here: media consumption is influx-only and correct;
   waste accumulation stays as-is per §16.2 — the fix is not simply "use the efflux rate".
4. `test_experiment_engine.py` covers 32-length, 16-length (broadcast) and scalar inputs,
   plus the `calibration` block path and the estimate-quality flip.
5. The manual-pump mL conversion (`/api/actuators/pump` and its preview) is
   direction-aware: `ExperimentEngine.flow_rate_ml_s(vial, direction)` selects the influx
   or efflux pump's rate.

### 16.2 Volume regulation is a hardware loop, not a software one

**The efflux straw sets the working volume.** The efflux tube terminates at a fixed height
in the vial; once the level reaches that tip the pump draws air and removes nothing more.
Running efflux *longer* than strictly necessary therefore pins working volume to the straw
height on every dilution, regardless of pump mismatch, tubing wear, or viscosity. This is a
closed loop, and it is engaged by `efflux_extra_seconds`.

**Do not implement software flow balancing.** The obvious fix —
`t_efflux = (F_influx × t_influx) / F_efflux` — is defeated by the firmware's whole-second
quantisation (§9). For a representative dilution (t_in = 3 s, F_in = 1.00, F_out = 0.95)
the balanced duration is 3.16 s and truncates to 3 s: the correction is the same size as
the truncation, so it vanishes. Recovering it would need a second deficit accumulator on
the efflux side and would *still* leave sub-second residual imbalance every cycle. It is
open-loop compensation against a hardware floor, where a hardware closed loop already
exists.

Consequences once overrun is engaged:

- **Waste accumulation is `waste += influx_ml`, exactly.** Volume is pinned, so liquid
  removed per dilution equals liquid added. Computing waste as
  `efflux_seconds × efflux_flow_rate` is wrong twice: it uses a duration that deliberately
  exceeds the liquid available, and counts the air-drawing portion as liquid. This
  supersedes the naive reading of §16.1 item 3.
- **`volume_ml` becomes a measured geometric constant** — set by where the straw is cut —
  rather than the unverified `25.0` default in the config.
- **The §17 dilution-rate growth estimator gets an honest `V`.** `μ ≈ Σ ln(1 + vᵢ/V)/Δt` is
  biased by a drifting vial volume; pinning V removes that bias.

**Current state and the open gate.** `efflux_extra_seconds` defaults to **0.0** across all
three control modes (commit `a7b408a`, "live-validated default from eVOLVER-001"), which
disables the mechanism entirely and makes level an open-loop integral of flow mismatch —
undetectable in software, since there is no level sensor. **Establish why it was set to 0
before restoring it**: if the straw currently sits too deep, overrun over-drains and the
straw must be re-cut first; if the concern was foaming or aerosol from drawing air through
culture, that bounds the safe overrun. This is a bench question, not a code question, and
it gates `ROADMAP.md` Sessions K and L.

---

## 17. Growth rate estimation service

**Status: partially implemented (private to `MorbidostatController`). Priority P0
(`ROADMAP.md` Session N). Blocks: §22 detection rules, consumables forecasting, the
growth-rate control mode, and the derived-statistics plots.**

New module `server/growth_rate.py`: pure, I/O-free, importable by any control mode. Output
lands in `status()`, the WebSocket payload, and the existing `growth_rate_per_hour` column
in the per-vial CSV.

### Two estimators, both reported

Naively fitting `ln(OD)` over a rolling window is **wrong in a turbidostat**: dilution
events are step decreases in OD that have nothing to do with growth, and averaging across
them biases μ toward zero.

**1. Segment regression.** Split the OD series at dilution events; within each
inter-dilution segment fit

```
ln(OD) = μ·t + c        (least squares)
```

and take a weighted mean of recent segments. Reflects instantaneous growth. Variance
rises as dilutions become frequent, because segments become short.

**2. Dilution-rate estimator.** At turbidostat steady state, growth rate equals dilution
rate. Over a window containing *k* dilution events delivering volumes *vᵢ* into vial
volume *V*:

```
μ ≈ Σ ln(1 + vᵢ/V) / Δt_window
```

Depends only on pump volumes and event times, not on OD noise — substantially lower
variance — but inherits any error in the pump flow calibration (§19) and is valid only
when OD is genuinely stationary.

**Report both.** Persistent divergence is a physical signal, not a bug: biofilm or wall
growth (culture denser than planktonic OD suggests), a mis-calibrated pump, or a culture
not actually at steady state. §22 consumes this divergence as a detection rule.

In chemostat mode μ is imposed by the operator at steady state, so the dilution estimator
is close to tautological there; segment regression is the informative one.

### Required edge-case handling

| Case | Behaviour |
|---|---|
| OD < 0.1 | Return `None` — the sigmoid calibration is at its noisy tail |
| Fewer than N samples or shorter than the minimum span | Return `None` |
| Lag or stationary phase | Report R² alongside μ so the UI can flag low confidence |
| Turbidostat warmup (first 8 cycles, §9) | Dormant |

Never return a number without its R². A slope fitted through non-exponential data is
meaningless, and presenting it unqualified is worse than presenting nothing.

Also report **doubling time** (`ln2 / μ`), which most biologists read more fluently
than μ.

---

## 18. Hygiene records and sterilisation wizard

**Status: not implemented. Priority P1 (`ROADMAP.md` Session P).**

### 18.1 Hygiene record

Persistent record, per fluidic line and for the vial set: procedure performed (autoclave /
bleach cycle / ethanol flush), timestamp, operator, free-text notes.

- Dashboard badge: *"Fluidics last sterilised: 6 days ago."*
- **Soft gate** in the experiment wizard review step: if the record is older than a
  configurable threshold (default 14 days, see §14 open question 12) or absent, show a
  warning the user must acknowledge.
- Deliberately soft, not blocking. The software cannot verify that sterilisation actually
  happened; a hard gate on an unverifiable self-report only trains people to click
  through it.

### 18.2 Sterilisation wizard

A guided service routine running the standard line-cleaning sequence — bleach, dwell,
water rinse ×N, ethanol, air — with per-step volumes, timers, and confirmation prompts,
writing a hygiene record on completion.

**Safety requirements — these are the substance of the feature:**

- Runs only in an explicit **service mode**. Refuses to start while an experiment is
  RUNNING.
- Fluid moved during service must **not** debit media bottles or count as experimental
  consumption. It is a distinct event category. Waste volume still accumulates physically
  and must be credited, but tagged as service volume so it doesn't corrupt consumption
  statistics.
- The wizard must prompt the operator to confirm which bottle each line currently sits in
  (bleach vs media). The tubing is moved by hand; the software cannot know, and guessing
  wrong means pumping bleach into a culture.
- Hard-stop control available on every step.

---

## 19. Calibration wizards

**Status: §19.1–§19.4 and §19.6 IMPLEMENTED 2026-08-20 (`ROADMAP.md` Session O).**
`server/calibration_service.py` holds the envelope, the versioned store under
`calibration/` (with the legacy `.txt` files regenerated as a derived view), the OD-blank
and pump wizard sessions, the thermal-settling tracker, staleness, and reconciliation;
`app.py` exposes them under `/api/calibration/*` and `index.html` carries the Calibration
tab with the OD-blank, pump and reconciliation wizards. §19.5 (full thermistor and OD
sigmoid recalibration) remains **Phase 4 / Session AA**, gated on bench prerequisite P2.
The software is bench-ready; **Tier 2 gravimetric calibration and the first real blank
have not yet been run**, so the numeric caveats below still stand until they are.

> **The bench protocol is `CALIBRATION_PROTOCOL.md`.** Part I is the tiered SOP the lab
> actually follows (Tier 0 pre-flight → Tier 1 per-run → Tier 2 per-campaign pumps → Tier 3
> foundational); Part II is the endpoint-by-endpoint implementation brief, with file
> formats, guards, wizard screens and a verification checklist. This section is the
> specification; that document is the procedure and the evidence behind it.

Every OD value the system reports and every volume it believes it moved rests on
2016-era constants inherited from a previous user (§14 open question 3). Those constants
have now been audited **numerically**, and three of them are demonstrably wrong rather than
merely unverified — see §19.0. The audit changed the design of §19.2, so read it before
implementing.

### 19.0 What the audit of the inherited constants found

Full tables in `CALIBRATION_PROTOCOL.md` §1 and Appendix A; all figures computed from the
committed `calibration/*.txt`.

1. **The machine reports OD 0.115–0.444 for a sterile blank.** A plausible blank signal
   (~58 000 counts) returns a non-zero OD on every vial — a **0.33 OD spread across sleeves
   at zero cells**. Every OD threshold currently in use is compared against a number
   carrying a large, vial-specific, unmeasured offset. Removing this is what §19.2 is for.

2. **Rows 0 and 1 are fitted asymptotes, not measurements.** Every fitted upper asymptote
   is **1.4×–8.7× the observed signal level** (81 938–503 890 against readings near
   58 000) — an extrapolation the hardware never reaches, consistent with a 16-bit ADC
   capped at 65 535. This invalidates the previous specification of §19.2; see the
   correction there.

3. **Vial 0's temperature calibration is a 5-sigma outlier.** Its intercept (86.493) and
   slope sit **+5.9 SD** and **+5.4 SD** off the other fifteen (4.5 on median/MAD). To
   request 37 °C the software sends `xr = 482` to vial 0 and `xr ≈ 403` to everything else.
   If vial 0's thermistor behaves like its neighbours, **commanding 37 °C lands it at
   ≈ 28 °C while the UI displays 37.0 °C.** The other fifteen agree to ±1.5 °C at a common
   setpoint, which is itself evidence of a bad fit rather than a different sleeve. A
   20-minute bench check with a reference thermometer resolves it (§19.5).

4. **Optical sensitivity varies four-fold across sleeves** — 92 counts per 0.01 OD on
   vial 1 against 375 on vial 9. Vial 1 is additionally broken: its fitted lower asymptote
   (44 262) sits *inside* the working signal range, so its curve diverges above OD ≈ 1 and
   no per-run blank will fix it. **Consequence for §17 and the control modes:** per-vial OD
   uncertainty is not uniform and must be reported rather than assumed. A threshold
   separation of 0.02 OD is meaningful on vial 9 and is noise on vial 1.

### 19.1 Calibration provenance and versioning — P0, build first

Nothing else in this section is safe without it.

- A common JSON envelope for every artefact: `schema`, `subsystem`, `version`,
  `supersedes`, `operator`, `source`, `conditions`, `data`, `fit`, `qc`.
- `calibration/current.json` points at the active version per subsystem; per-subsystem
  directories hold every previous version (they are small — retain all of them).
- The legacy `OD_cal.txt` and `temp_calibration.txt` become a **derived view**, regenerated
  from `current.json`, so `SerialManager.load_calibration()` needs no change.
- `conditions` is mandatory, not decoration: LED power, stir PWM, setpoint, bench
  temperature, fluid and density, vial-map version. Two calibrations without their
  conditions cannot be compared. Reject a write with an empty `conditions` block.
- Every experiment's `config.json` records a version for each subsystem it used.

### 19.2 Per-run OD blank — P0 — **corrected specification**

> ⚠ **This section previously specified "update rows 0 and 1 of the OD calibration".
> That was wrong and would be actively harmful. Do not implement it that way.**
>
> Rows 0 and 1 are the fitted asymptotes of a four-parameter logistic, not measurements
> (§19.0 finding 2), and `serial_manager._read_od_enhanced_locked` additionally uses them
> as the **validity domain**: `in_domain = (corrected > mn) & (corrected < mx)`. Setting
> `mx` to a measured blank makes every reading at or above the blank return `NaN`. Early in
> a run OD sits at the blank, so noise alone would discard roughly half of all samples and
> `experiment_engine` would raise "OD out of calibrated range" on all sixteen vials.
> Substituting measured values into rows 0/1 moves reported OD at a 50 000-count signal
> from **+0.66 to −6.62** on vial 0, and negative on every vial.

The full four-parameter sigmoid is a long calibration nobody will repeat before every
experiment. What *is* worth repeating is re-anchoring the curve against this run's actual
vials, media and sleeve seating.

**Procedure (≈10 min, `CALIBRATION_PROTOCOL.md` §5.4).** Taken last, immediately before
inoculation, under final run conditions — sterile medium at working volume, sleeves seated,
stir at the run's PWM, temperature equilibrated and held ≥ 10 min, LED at the run's power.

1. **Dark read** — LED power 0, five reads, per-vial median and SD.
2. **Blank read** — LED at the run value, five reads, per-vial median and SD.
3. **Re-anchor row 2 only:**

```python
c_run = np.log10((b - a) / (blank_raw - a) - 1.0) / d    # rows 0, 1, 3 untouched
```

This is *identically* `OD_new(S) = OD_old(S) − OD_old(blank)` — a blank subtraction in OD
units, a rigid vertical shift that preserves the curve shape the dilution series paid for
and leaves the validity domain intact. Derivation: `CALIBRATION_PROTOCOL.md` Appendix B.1.

**Rows 0, 1 and 3 are never modified by a per-run blank.** They change only in §19.5.

**Correcting offset is not correcting gain.** If a vial's optical coupling has changed
enough to alter the slope of signal against OD, the blank will read 0 while every non-zero
OD is wrong. The blank-median acceptance window (±10 % of the campaign reference) is the
tripwire; §19.5 is the fix.

**The dark read is a diagnostic, not a correction.** The 2016 curve was fit on
non-dark-subtracted signal and carries no `OD_cal.meta.json` sidecar saying otherwise, so
`dark_subtract=True` against it is silently wrong — `serial_manager` logs a warning today
and this should become a hard error. The blank re-anchoring absorbs a constant dark offset
anyway. Dark subtraction switches on only when §19.5 produces a dark-subtracted curve and
its sidecar, and the blank must switch with it; a dark-subtracted curve with a
non-dark-subtracted blank is as wrong as the reverse.

Per-run blanks are written to the **experiment directory**
(`experiments/{name}/od_blank.json`), not the global calibration directory, because they
are run-specific by definition.

### 19.3 Pump flow calibration (gravimetric) — P0 — IMPLEMENTED

For each of the 32 pumps: prime the line, fire for a fixed 20 s into a tared vessel,
operator enters delivered mass, wizard computes mL/s and writes a versioned
`calibration/pump/…json`. The engine consumes it through
`experiment_engine._resolve_flow_rates`, which prefers
`config["calibration"]["pump_flow_rates"]` over the hardcoded defaults — a claim that
became true only when Session O3a's 32-rate plumbing landed (see §16.1). A **partial**
calibration (e.g. the §5.2 four-line spot-check) merges over the previous version's rates;
only a version with all 32 rates populated is fed to new experiments.

Directly improves §15 (interlock), §16 (volume controls), §17 (dilution estimator) and
consumables forecasting.

- **Resumable.** ~1 hour of bench work for all 32 and nobody does it in one sitting:
  per-pump state persisted, progress visible, abort leaves no partial file.
- **Acceptance:** 3 replicates with CV ≤ 5 %; within 15 % of the previous calibration;
  non-zero and within 2× of the manifold median; monotonic if multiple durations are tested.
- Mass→volume uses the **bench-temperature** density. Calibrate in water unless the medium
  is appreciably more viscous or dense (high-sugar YPD, glycerol), in which case use the
  medium and record which fluid was used.
- A 0.01 g balance resolves 0.05 % on a 20 s fire. The balance is nowhere near the limiting
  factor — priming, line compliance and tubing wear are, which is why replicates and the CV
  criterion matter more than balance precision.

### 19.4 Post-run mass reconciliation — P0, small

Weigh the media bottle and waste carboy at the start and end of a run; compare the mass
deltas against the software's accumulated `duration × flow_rate`. Agreement within ±10 %
passes.

This is the only check that validates the entire open-loop volume chain end to end — it is
the one thing that can see a kinked line, a stalled pump, or a bottle someone topped up
without telling the software (§14 open question 8). Logged per run, the ratio's drift over
weeks *is* the peristaltic tubing-wear signal, and should trigger recalibration in place of
a fixed calendar interval.

Needs one endpoint (`POST /api/experiments/{name}/reconcile`), two number fields in the UI,
and a stored record. It also gives §15's `uncalibrated` label an evidence-based exit
condition.

### 19.5 Full temperature and OD recalibration — Phase 4

Two-point thermistor calibration against a reference thermometer, and an OD dilution series
fitting all four sigmoid parameters. Procedures: `CALIBRATION_PROTOCOL.md` §8.1 and §8.2.
Prerequisites for publishable absolute OD values. Blocked in practice by §14 open question 4
(heater electrical health) — though note that historical "stuck-on" behaviour may have been
the inverted-`xr` command bug rather than failed MOSFETs, which is 30 minutes of testing to
establish.

No longer optional for temperature: §19.0 finding 3 means vial 0 cannot be trusted until
this runs. For OD, vials 0, 1, 6, 14 and 15 all show the signature of a poorly constrained
fit; if bench time is short, recalibrate those five rather than spreading effort evenly.

**Efficiency note that makes the temperature half tractable:** ambient is a *free 16-way
simultaneous calibration point*. With heaters off and stir running, all sixteen vials
equilibrate to the same room temperature, so one reference reading serves all of them; only
the hot point needs the probe moved vial to vial (~35 min).

**Acceptance — temperature:** two points spanning ≥ 15 °C; fitted slope within ±10 % and
intercept within ±2 °C of the pack median; and — the check that actually matters — an
*independent* verification at an intermediate target (e.g. 30 °C) agreeing with the
reference thermometer within 0.5 °C on at least four vials. A two-point fit reproduces its
own two points trivially; only the middle reveals whether the response is linear.

**Acceptance — OD:** R² ≥ 0.99 per vial against true OD600 from a benchtop
spectrophotometer; residual < 0.02 OD or 5 %, whichever is larger; fitted lower asymptote
clearly below the lowest measured signal; inflection point inside the measured range;
monotonic across the working range. Fit on **dark-subtracted** signal and write
`calibration/OD_cal.meta.json` with `{"dark_subtracted": true}` — that sidecar is what lets
§19.2's dark read graduate from diagnostic to correction. Use a non-growing turbidity source
(killed cells, heat-fixed cells, or beads) so the series does not drift over the two hours
it takes to measure 16 vials.

### 19.6 Cross-cutting requirements

- **Never overwrite calibration files in place.** Versioned files carrying timestamp,
  operator and `source`; the previous version retained; the version recorded in each
  experiment's `config.json`. Calibration provenance is part of the dataset — a plot whose
  calibration cannot be reconstructed is not reproducible.
- **Calibrate in the state you will run in.** Same vial type, fill volume, medium, sleeve
  position, stir PWM, thermally equilibrated. A blank taken with the stirrer off is not a
  blank for a run with the stirrer at 8. Reject a blank commit whose LED power or stir PWM
  differs from the run's.
- Calibration is the **only** consumer of raw actuator paths. *(Done 2026-08-20:
  `POST /api/actuators/temperature/raw` moved to `POST /api/calibration/raw/temperature`,
  and `POST /api/calibration/raw/od_led` was built; both 409 while RUNNING.)* Ordinary
  operation cannot reach a raw heater setpoint, which matters more than usual given the
  inverted `xr` convention (§10).
- All calibration routes refuse to run while an experiment is RUNNING.
- **Refuse to save an obviously bad fit** — non-monotonic pump response, R² below the floor,
  a thermistor fit spanning under 15 °C — overridable only with a recorded `override_reason`
  in `qc.overridden_by`.
- **Surface staleness.** Pump calibration goes stale on age, on cumulative pump-seconds (the
  pump log already holds them — sum them), or on a failed §19.4 reconciliation. A missing
  per-run blank should hard-block the run rather than warn.
- **Review screens are the point.** Before committing any calibration the operator must see
  the new value, the previous value, the delta, and which acceptance criteria passed. A
  wizard that only says "done" reproduces exactly the situation that let §19.0 finding 3 sit
  unnoticed since 2016.
- **Prerequisite: the vial→sleeve map** (§14 open question 2). Every per-vial constant is
  attached to a logical index; if index 7 is not the sleeve you think it is, the whole
  calibration set is scrambled. Record the `vial_map` version in every calibration write and
  warn loudly when it is null.

---

## 20. Observability: structured logging and the event log

**Status: IMPLEMENTED 2026-08-20 (`ROADMAP.md` Sessions M and M2).**
`server/event_log.py` holds the rotating disk-guarded handlers, the event ring buffer, the
rate limiter, and the `BusHealth` / `VialHealth` classifiers. `DataLogger.log_event` writes
`events.csv`; `app.py` funnels every alert and experiment event through one pair of functions
so nothing can reach a browser without also being captured.

### 20.1 Rotating file logs

`RotatingFileHandler` to `logs/evolver.log` (5 × 10 MB), plus `logs/errors.log` at WARNING
and above. Disk-aware: `/api/storage` already reports free space; log writes must degrade
gracefully rather than filling the SD card that the experiment is also writing data to.

### 20.2 Unified per-experiment event log

`experiments/{name}/events.csv`:

```csv
timestamp,elapsed_hours,level,category,vial,message,data_json
```

Every discrete occurrence is recorded here: experiment start/stop, pump fires **and
suppressed pump attempts** (§15), phase changes, alerts, maintenance enter/exit, refills,
manual overrides (§21), drug escalations, sensor failures, and serial errors.

This is the artefact a researcher attaches to a lab-notebook entry, the backing store for
the event-log table in the UI, and part of the export bundle.

### 20.3 Error classification

Logging every serial hiccup identically buries the one that matters. Classify:

| Class | Meaning | Handling |
|---|---|---|
| `TRANSIENT` | Single malformed RS485 frame | Counted, not alerted — the bus is lossy by design and commit `b9b135a` already tolerates this |
| `DEGRADED` | One vial's sensor failing repeatedly (engine already tracks `DEFAULT_SENSOR_FAILURE_THRESHOLD`) | Per-vial health badge on the dashboard |
| `PERSISTENT` | Bus silent, serial port gone, Arduino unresponsive | Immediate `critical` alert — this is the class that ends experiments |

Rate-limit repeated identical errors: log the first, then every Nth with an occurrence
count. A stuck loop must not be able to fill the disk in an hour.

### 20.4 Operator-facing error surface

**Status: IMPLEMENTED 2026-08-20 (`ROADMAP.md` Session M2).**

§20.1–§20.3 are about *capturing* errors. This section is about an operator seeing them
without SSH-ing into the Pi.

#### What was built

- **Alert drawer** — persistent, collapsible, filterable by level / vial /
  unacknowledged-only, with an unacked-count badge in the header. Three visually distinct
  levels, none of which reuses the success colour. `index.html` `#alert-drawer`.
- **Reload persistence** — `GET /api/events/recent?level=&category=&vial=&limit=` over a
  500-entry server-side ring, populated whether or not an experiment is running.
- **Acknowledgement** — `POST /api/events/<id>/ack`. The acknowledgement is itself recorded
  as an event, so `events.csv` shows who cleared what and when.
- **RS485 bus health** — `#bus-conn`, fed by the `health` block on every `sensor_update`
  and by `GET /api/health`. Amber while frames are being dropped, red after
  `DEFAULT_SENSOR_FAILURE_THRESHOLD` consecutive failures. Distinct from the socket.io dot.
- **Per-vial sleeve badge** — `.vial-health` on each card, from the streak counters the
  engine now exposes in `status()` (`nan_streak`, `od_range_streak`, `sensor_health`).
- **Escalation** — every actuator-command, serial, and data-logger failure now raises an
  alert as well as logging, rate-limited so a repeating fault is one entry with a rising
  count rather than hundreds.

The toast is retained for transient confirmations of user-initiated actions. It is a
reasonable acknowledgement channel and was never an acceptable *error* channel.

#### What the surface looked like before (the audit that motivated this)

Before Session M2, an `alert` WebSocket event originated from **13 sites**: three in `app.py` (watchdog
trigger `:404`, emergency stop `:711`, disk-low `:1190`) and ten in `experiment_engine.py`
via `_broadcast_alert` (`experiment_engine.py:2265`) covering media low, waste high, OD
range and NaN streaks, latched vial faults, escalations, and maintenance transitions.
Engine alerts reached the socket through an `on_alert` lambda; the browser handled the
event at `index.html` `socket.on("alert")` and called `showToast` and nothing else.

*(Line numbers above are pre-Session-M2 and will not resolve against current `main`.)*

#### The four defects this section was written to fix

**All four are now closed.** They are kept as the record of what was wrong and why it
mattered -- read them as history, not as current state.

1. **Warnings are styled as success.** The handler reads
   `const kind = msg.level === "critical" ? "error" : "ok"`, and `.toast.ok` is
   `--status-ok: #16a34a` — green. **Seven of the engine's ten `_broadcast_alert` sites
   pass `level="warning"` literally** (two are `critical`, one is computed at `:1677`), so
   the majority of alerts the system raises — media bottle low, waste nearly full, OD out
   of range — render in the same green as "Stir applied". One-line fix; it should not wait
   for the rest of the session.

2. **Alerts are ephemeral.** `showToast` clears after 3500 ms and nothing retains the
   message. An alert raised at 03:00 is gone by morning: not on screen, not in a file
   (`events.csv` per §20.2 did not exist yet), recoverable only from the systemd journal
   over SSH. This defeated the entire purpose of raising it.

3. **Nothing survives a page reload.** There is no client- or server-side history, so
   refreshing the dashboard — or opening it on a second machine — shows a clean slate
   regardless of what has happened.

4. **Most failures never reach the browser at all.** There are 73 `log.exception` /
   `log.error` / `log.warning` sites across the server against 13 alert-raising sites. The
   silent ones include the failures most likely to ruin a run:
   `pump_command failed` (`app.py:671`), `pump firing failed for vial %d`
   (`app.py:1129`), `set_temperature_celsius failed` (`app.py:563`),
   `execute queued pump actions failed on exit` (`app.py:1017`), and
   `data_logger.log_pump_event failed` (`app.py:683`) — the last being **silent data
   loss**, a pump row that never reaches the CSV.

Additionally, the header connection indicator reflects the **socket.io** connection, not
the RS485 bus. If the serial link fails while Flask stays up, the browser continues to
show "connected" beside stale or NaN readings, and all 13 `log.exception` sites in
`serial_manager.py` are invisible.

#### Required behaviour

**Alert drawer.** A persistent, collapsible panel with an unacknowledged-count badge in
the header. Each row: timestamp, level, subsystem/category, vial (if applicable), message,
and expandable detail. Filterable by level and vial. Three visually distinct levels —
info, warning, critical — none of which may reuse the success colour.

**Persistence across reload.** `GET /api/events/recent?level=&limit=` backed by a
server-side ring buffer (default 500 entries) that is populated **whether or not an
experiment is running** — calibration faults, manual-control failures, and serial errors
all occur while idle, so a per-experiment `events.csv` is not sufficient on its own. When
an experiment *is* running, the drawer reads through to `events.csv` for full history.

**Acknowledgement.** `critical` entries persist until explicitly acknowledged rather than
ageing out; the acknowledgement (who, when) is recorded as an event. Warnings may age out
of the drawer but never out of `events.csv`.

**Escalation of log-only failures.** Every actuator-command failure, serial read/write
failure, and data-logger write failure must emit an alert in addition to logging. The
triage rule: *if a human would want to know within the hour, it is an alert; otherwise it
is a log line.* Apply the §20.3 rate-limiting so a repeating fault produces one alert with
a rising count, not hundreds.

**RS485 bus health.** An indicator distinct from the socket.io indicator, showing time
since the last successful read per subsystem (temperature, OD), amber after one missed
cycle and red after `DEFAULT_SENSOR_FAILURE_THRESHOLD` consecutive failures.

**Per-vial health badge.** Surface the existing per-vial sensor-failure counter on the
vial card, so a single degraded sleeve is visible without opening anything.

The existing toast is retained for transient confirmations of user-initiated actions
("Stir applied"). It is a reasonable acknowledgement channel; it is simply not an
acceptable *error* channel.

---

## 21. Supervised per-vial override

**Status: not implemented — vials in a running experiment are hard-locked in the UI
(`isLocked`). Priority P1 (`ROADMAP.md` Session S).**

Hard-locking is the right default and the wrong absolute. Real experiments need
intervention: pull a sample, spike a vial, rescue a stalled culture.

### Behaviour

An explicit unlock gesture (press-and-hold ≈2 s; typed confirmation for destructive
actions) grants a **time-limited, audited override** on one vial — manual influx/efflux by
volume, temperature change, stir change. Default expiry 10 minutes, after which the vial
re-locks automatically so a forgotten unlock cannot leave a vial unguarded.

### Controller-state coherence — the correctness requirement

A manual action the controller does not know about will corrupt control. The turbidostat
tracks `last_pump_time` and a deficit accumulator; a manual dilution invisible to it will
be followed immediately by an automatic one, double-diluting the culture.

Every manual action on a controlled vial must therefore be **pushed into controller
state**, not merely executed:

- Manual influx/efflux updates `last_pump_time`, debits the media bottle, credits waste,
  and appends to the vial's pump history exactly as an automatic event would.
- Manual temperature or stir changes either update the experiment's persisted parameters
  (so a restart does not silently revert them) or are explicitly marked transient with a
  stated expiry. Ambiguity here produces experiments whose actual conditions cannot be
  reconstructed.
- Every override writes an `events.csv` entry with operator, action, and reason, **and is
  drawn as a marker on that vial's plots**. An unexplained step in the data six months
  later is a research problem, not a UI problem.

---

## 22. Anomaly detection and notifications

**Status: not implemented. Priority P1 for the rule-based tier and Slack
(`ROADMAP.md` Sessions V and W); statistical contamination detection deferred.**

### 22.1 Rule-based detection (P1)

Deterministic, explainable, warn-only. No model, no training data. Dormant below OD 0.1
and during the turbidostat's 8-cycle warmup.

| Condition | Rule | Level |
|---|---|---|
| Stall | μ < 0.05/h for > 2 h with OD > 0.1 | warning |
| Dead culture | OD monotonically falling > 1 h with no dilution | warning |
| Chemostat washout | OD monotonically falling > 2 h *while dilution is active* | critical |
| Dilution-response failure | Influx fired but OD did not drop by the expected fraction | warning |
| Runaway growth | OD above upper threshold for > 3 consecutive cycles despite dilution | warning |
| Estimator divergence | Segment-regression μ and dilution-rate μ differ > 50 % for > 1 h (§17) | info |
| Temperature excursion | \|T − setpoint\| > 2 °C for > 15 min | warning |
| Pump over-cycling | Dilution interval below `pump_wait` floor repeatedly | warning |
| Sensor degradation | Per-vial failure counter above threshold (§20) | warning |

**No rule may stop an experiment.** Every alert must carry the evidence that triggered it —
the numbers, the window, the threshold. An alert that only says "possible contamination"
trains users to dismiss alerts, which is worse than having none.

The **chemostat washout** rule is the deferred half of CONTROL_MODE_AUDIT.md C-5. The
other half — the optional `start_od` / `start_after_seconds` gate that stops a run diluting
at inoculation density — shipped 2026-08-21. Washout is the failure that gate does not
cover: D set above the culture's actual µ (e.g. D=0.5/h against a minimal-media µ≈0.35/h)
dilutes faster than the culture grows, and the vial empties of cells over hours with every
subsystem reporting healthy. It is `critical` rather than `warning` because the run is
already unrecoverable by the time it is visible, and it is listed here rather than in the
control mode because it is a detection rule, not a control decision — the chemostat must
keep delivering the rate it was asked for.

The dilution-response check deserves emphasis: by comparing the OD drop a dilution
*should* have produced against what actually happened, it catches the two most common
mechanical failures at once — a pump not actually pumping, and a bottle empty despite
what the volume estimate claims (§15).

### 22.2 Statistical contamination detection — deferred

The failure mode is asymmetric: a false positive that halts or casts doubt on a five-day
adaptation experiment costs more than the detector saves. Tuning requires a corpus of real
runs including known-contaminated ones, which does not yet exist.

**Concrete prerequisite:** §22.1 must log its derived features (short/long-window μ,
growth-rate jerk, dilution-interval variance and trend, estimator divergence) into
`events.csv` from day one, so the corpus accumulates passively. Revisit after ~10 runs
with known outcomes, including at least 2 contaminated.

### 22.3 Notifications

Route `critical` and `warning` alerts to a **Slack incoming webhook** — a single POST, no
OAuth, no app review. Per-level configuration, digesting so a flapping condition cannot
spam the channel, a "send test notification" button, and a daily heartbeat for running
experiments (silence must not be ambiguous between "fine" and "server dead").

Egress is unverified (§14 open question 10) — fail gracefully with a queued buffer and
send on reconnect. Email is deliberately second: Yale's SMTP relay will likely require
authenticated submission and may reject a headless device; a webhook-to-email bridge is
preferable to running SMTP on the Pi. The webhook URL is a credential and must live
outside the repository.

---

## 23. Experiment templates

**Status: not implemented. Priority P1 (`ROADMAP.md` Session Q).**

`config.json` already fully describes an experiment, so this is largely plumbing with a
disproportionate payoff for handing the system to new lab members.

- Save any experiment's configuration as a named template (`templates/{name}.json`).
- Start the wizard from a template, pre-filling every step for review and adjustment.
- "Clone previous run" — how most experiments actually get created.
- Curated built-ins: standard turbidostat (8 vials, LB, 37 °C), chemostat dilution series,
  morbidostat escalation, OD-only monitoring with heaters parked at ambient.
- Templates record the calibration version they assume and warn on load if calibration has
  changed since (§19.1, §19.6).
- Export/import as a portable file, so a protocol can be shared between labs or attached
  to a paper.

---

## 24. Off-box backup

**Status: not implemented. Priority P1 (`ROADMAP.md` Session X).**

The requirement is *don't lose an experiment when the Pi's SD card dies* — a real and
fairly likely failure mode. Implemented generically via `rclone`, driven by a **systemd
timer rather than the Flask process**, so a backup failure can never perturb the control
loop.

- Configurable remote: OneDrive, Google Drive, S3, or SFTP to a lab NAS — a one-line
  config change between them. (The lab meeting asked for OneDrive specifically; `rclone`'s
  OneDrive backend satisfies that without hand-rolling Microsoft Graph OAuth on a headless
  Pi against a tenant that likely enforces conditional access. See `ROADMAP.md` §2.)
- Triggers: on experiment stop, plus a nightly incremental.
- Never sync while the serial loop is under load; check disk and CPU first.
- **Report last-successful-backup age on the dashboard.** A silently failing backup is
  worse than none, because it manufactures confidence.
- Document *and test* the restore path onto a clean directory. An untested backup is a
  hypothesis.
- `rclone.conf` lives outside the repo and is git-ignored.

---

## 25. Stir rate to RPM calibration

**Status: not implemented. Priority P2 (`ROADMAP.md` Session AC).**

Stir is currently a raw PWM value 0–15 (this one really *is* a PWM, unlike `xr` — see
§10). "Stir setting 8" is not reportable in a methods section; "600 RPM" is.

- Measure actual RPM per sleeve across the 0–15 range by tachometer, strobe, or
  high-frame-rate video of a marked stir bar. Sample 4 points per sleeve (PWM 4, 7, 10, 13)
  and interpolate rather than measuring all 16 settings. Procedure:
  `CALIBRATION_PROTOCOL.md` §8.3.
- **Currently blocked on equipment, not time.** The lab has a balance, a reference
  thermometer and a spectrophotometer, but no tachometer, strobe or high-frame-rate camera.
  Cheapest unblock: a phone shooting 240 fps with a white paint dot on one end of the stir
  bar, or an inexpensive optical tachometer.
- Store as `calibration/stir/…json`, per vial, following the versioning rules in §19.1
  and §19.6.
- **Coupling worth noting even if RPM is never calibrated:** stir setting affects the OD
  blank, because vortex geometry and entrained air change the optical path. That is why
  §19.2 requires the per-run blank to be taken at the run's stir PWM and rejects a commit
  where the two differ.
- Display RPM alongside the PWM setting in the UI and record it in `config.json`.
- Optional: bind an animation's rotation rate to the calibrated RPM in the experiment
  designer. Cosmetic, and last.

The software is trivial; the cost is ~2 hours of bench work (§14 open question 11). Worth
doing partly for reportability and partly because stir bars couple inconsistently across
sleeves — the calibration will likely expose real vial-to-vial variation worth knowing
about before it shows up as unexplained variance in a growth experiment.
