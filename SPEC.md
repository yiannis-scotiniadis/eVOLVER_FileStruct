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

### Phase 1: MVP — Live dashboard + turbidostat (target: first working experiment)

- Flask server on RPi replaces all 5 supervisor processes
- SerialManager class owns RS485 communication
- MockSerialManager for development without hardware
- Live dashboard: temperature and OD for all 16 vials, updated every 10 seconds
- Manual controls: set temperature, stir rate, trigger pumps, emergency stop
- Single turbidostat experiment: configure thresholds per vial, start/stop, log data
- CSV data export
- Watchdog: zero all actuators if no heartbeat for 30 minutes
- Shutdown handler: zero all actuators on server exit, crash, or SIGTERM

### Phase 2: Multi-experiment + experiment designer

- Assign vials to independent experiment groups
- Visual experiment designer GUI (vial assignment, parameter sliders, mode selection)
- Additional control modes: chemostat, morbidostat, growth rate feedback
- Phase-based experiment protocols (sequential phases with trigger conditions)
- Experiment templates (save/load/share experiment configurations as JSON)

### Phase 3: Calibration + monitoring

- Guided calibration wizard for temperature and OD
- Pump flow rate calibration
- Historical data plotting with zoom/pan
- Growth rate estimation (exponential fit on sliding OD window)
- Configurable alerts: OD stagnation, temperature deviation, excessive pump cycling
- Slack webhook integration for alerts

### Phase 4: Advanced features

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

  calibration/
    OD_cal.txt                  # OD calibration data (4 x 16)
    temp_calibration.txt        # Temperature calibration data (2 x 16)
    pump_calibration.json       # Pump flow rates per vial

  experiments/                  # Created at runtime
    {experiment_name}/
      config.json               # Experiment parameters
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
  // The calibration wizard (Phase 3) has a separate /api/calibration/temperature/raw
  // endpoint that exposes set_temperature_raw for advanced use.

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

### Calibration endpoints (Phase 3)

```
GET /api/calibration/temperature
  Response: {"slopes": [...], "intercepts": [...], "last_calibrated": "..."}

GET /api/calibration/od
  Response: {"params": [[...], [...], [...], [...]], "last_calibrated": "..."}

POST /api/calibration/temperature/start
POST /api/calibration/temperature/record
POST /api/calibration/temperature/finish
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
        "efflux_extra_seconds": 5
    },
    "calibration": {
        "temp_cal_file": "temp_calibration.txt",
        "od_cal_file": "OD_cal.txt",
        "pump_flow_rates": [0.95, 1.1, 0.975, 0.85, 0.95, 1.05, 1.05, 1.05,
                            1.025, 1.125, 1.0, 1.0, 1.05, 1.15, 1.1, 1.025]
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
        pump_time = -(ln(od_lower / average_od) * volume) / flow_rate
        pump_time = min(pump_time, 20)  # cap at 20 seconds

        # Sub-second deficit accumulator (hardware-floor workaround;
        # see "Sub-second pump-time deficit" note below).
        deficit = min(deficit + pump_time, 20)

        if (now - last_pump_time) >= pump_wait and int(deficit) >= 1:
            whole = int(deficit)
            deficit -= whole
            fire influx + efflux for whole seconds
            fire efflux alone for efflux_extra seconds
            log pump event
            emit experiment_event
```

### Chemostat control mode (Phase 2)

```
inputs:
    dilution_rate  — volumes per hour
    volume         — vial volume in ml
    flow_rate      — ml/sec
    bolus_interval — derived from dilution_rate

logic:
    pump_time_per_bolus = (dilution_rate * volume) / (3600 / bolus_interval) / flow_rate
    every bolus_interval seconds:
        # Sub-second deficit accumulator (hardware-floor workaround;
        # see "Sub-second pump-time deficit" note below).
        deficit = min(deficit + pump_time_per_bolus, safety_cap)
        if int(deficit) >= 1:
            whole = int(deficit)
            deficit -= whole
            fire influx for whole seconds
            fire efflux for whole + efflux_extra seconds
```

#### Sub-second pump-time deficit (hardware-floor workaround)

The 2016 firmware only accepts whole-second pump times; the legacy Mac
client formatted `pump_time` with `%d`, so any cycle whose computed
pump_time was sub-second silently truncated to zero and never fired.
For a slow chemostat (e.g. D=0.5/h, V=25, T=60: pump_time_per_bolus ≈
0.21 s) this means the firmware would deliver no dilution at all.

Each control mode keeps a per-vial **deficit accumulator**: every cycle's
formula output is added to the deficit (capped at the mode's safety
limit), and the controller fires `int(deficit)` seconds when that reaches
≥ 1 s, carrying the fractional remainder forward. Total dilution
delivered equals total dilution prescribed (modulo the < 1 s residual
still sitting in the accumulator at any moment), which the legacy
behaviour did not guarantee.

Deficit state is persisted in `state.json` and clamped to
`[0, safety_cap]` on restore so a corrupted file cannot suppress the
next pump or grant an over-cap catch-up bolus.

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

3. **Calibration validity:** The existing temp_calibration.txt and OD_cal.txt are from a previous user (Bernie/Brandon). They need to be verified against known standards before running real experiments.

4. **Heater stuck-on issue:** Diagnosis in progress. If MOSFETs are failed-on, hardware repair is needed before temperature-controlled experiments. OD and stir experiments can proceed independently. **Note:** the inverted `xr` setpoint convention (see §10 "Heater control convention") was discovered while debugging this issue — any prior code or operator command that sent `xr=0` "to turn the heaters off" was actually requesting ~82 °C, which would explain stuck-on behaviour even with healthy MOSFETs. Before concluding hardware is at fault, verify the heaters do turn off when sent `xr=4095` (or `set_temperature_celsius` with ambient target).

5. **Network access:** Currently the eVOLVER is on a dedicated Netgear router (192.168.1.x). For remote monitoring, the router could be connected to Yale's network, or a VPN tunnel could be set up. This is a Phase 4 concern.

6. **Concurrent experiments:** Phase 2 allows multiple experiment groups, but the RS485 bus is shared. The serial manager must ensure commands for different experiments don't interfere. Since all 16 vials are always read in a single command, this is primarily a software isolation concern, not a hardware one.