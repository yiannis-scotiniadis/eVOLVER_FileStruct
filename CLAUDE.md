# CLAUDE.md — eVOLVER Control System

## Project overview

This project modernizes the control software for a 2016-era eVOLVER continuous culture platform built by the Boston University Engineering Design Facility for the Khalil Lab. The eVOLVER supports 16 independent culture vials with individual control over temperature, optical density (OD) sensing, stirring, and fluidics (pumps). The goal is to replace the current split Mac/RPi architecture with a single web-based server running on the Raspberry Pi, accessible from any browser.

## Hardware architecture

```
[Browser on any device]
        |
        | HTTP / WebSocket
        v
[Raspberry Pi (192.168.1.2)]  ← runs the new Flask server
        |
        | RS485 serial (/dev/ttyAMA0, 9600 baud)
        |
        v
[4x SAMD21 Arduino microcontrollers on shared RS485 bus]
        |
        | Analog/digital I/O via motherboard PCBs
        v
[16 Smart Sleeves: heaters, thermistors, LEDs, photodiodes, stir motors]
        |
[Auxiliary Board: 32 peristaltic pumps (2 per vial: influx + efflux)]
```

### Raspberry Pi
- Model: Raspberry Pi (B8:27:EB MAC prefix)
- OS: Raspbian (Python 2.7 environment)
- Static IP: 192.168.1.2
- Serial port: /dev/ttyAMA0 (hardware UART, directly wired to RS485 transceiver)
- SSH access: user `pi`
- Process manager: supervisor

### SAMD21 Arduinos
- Four SAMD21 mini breakout boards on the motherboard
- All connected in parallel on a single RS485 bus
- Each Arduino is responsible for one subsystem
- They listen for commands with their specific address prefix and respond when addressed

### Motherboard
- Houses the SAMD21 boards and custom PCBs
- PWM boards: amplify SAMD21 output to drive actuators (heaters, LEDs, stir motors)
- ADC boards: 16:1 demultiplexer that reads 16 sensors, filters, and sends to SAMD21
- RS485 board: enables serial communication between RPi and all Arduinos

### Smart sleeves (x16)
Each vial sleeve contains:
- Heating element (resistive heater, PWM controlled)
- Thermistor (temperature sensor, read via ADC)
- IR LED (for OD measurement, PWM controlled intensity)
- Photodiode (detects transmitted light for OD)
- Stir motor (drives magnetic stir bar, PWM speed control)

### Fluidics
- 32 peristaltic pumps total (2 per vial: influx + efflux)
- Controlled by a separate SAMD21 on the Auxiliary Board
- Pump addressing uses binary codes: influx = 2^vial, efflux = 2^(vial+16) — 32 addresses total (range 0..31)

## RS485 serial protocol

All communication goes through /dev/ttyAMA0 at 9600 baud. The exp_manager (evolver_UPD.py) is the only process that reads/writes the serial port. Messages are ASCII strings terminated with ` !` (space then exclamation mark).

### Command format
```
{address_prefix}{comma_separated_values} !
```

### Address prefixes (RPi → Arduino)
| Subsystem  | Address | Example command                           |
|------------|---------|-------------------------------------------|
| Fluidics   | `st`    | `st{binary_pump_code},0,{seconds}, !`     |
| Stir/Fan   | `zv`    | `zv8,8,8,8,8,8,8,8,8,8,8,8,8,8,8,8, !`  |
| Temperature| `xr`    | `xr200,200,200,...(x16), !`               |
| OD         | `we`    | `we2125,2125,2125,...(x16), !`            |

### Response format (Arduino → RPi)
```
{response_prefix}{comma_separated_values}end
```

### Response prefixes (Arduino → RPi)
| Subsystem   | Prefix | Example response                            |
|-------------|--------|---------------------------------------------|
| Temperature | `temp` | `temp516,429,429,431,...(x16),end`          |
| OD          | `turb` | `turb57711,58056,55568,...(x16),end`        |

The exp_manager parses responses by checking: `response[:4] == prefix` and `response[-3:] == 'end'`, then extracts the data as `response[4:-3]`.

Stir and fluidics are write-only on the active code path — Arduinos may acknowledge but the exp_manager never reads a response for these. (A `Fluidic_status()` function exists in `evolver_UPD.py` but is only called from a commented-out "without queue" branch.)

### Fluidics sub-protocol

The `st` address prefix is a namespace; the first character of the payload selects one of three sub-modes. Only `mac_original/` actually writes these — the legacy single-fire form is the only one that turbidostat experiments use.

| Sub-mode             | Wire format                                                   | Used by                                          |
|----------------------|---------------------------------------------------------------|--------------------------------------------------|
| Single fire          | `st<binary_pump_code>,0,<seconds>, !`                         | `custom_script.py` turbidostat (line 112)        |
| Stop / multi-fire    | `stt,<32-bit pump mask>,<time0>,<time1>,…,<time15>, !`        | `eVOLVER_module.stop_all_pumps` (line 199)       |
| Chemostat (rates)    | `stc,<rate0>,…,<rate15>,<bolus>, !`                           | `eVOLVER_module.update_chemo` (lines 92–101)     |

Note: `extras/pump_rs485.py` also sends a bare `st !` between commands; its meaning isn't documented in the legacy and the Arduino firmware source is not available, so treat it as undefined behaviour.

## Current software architecture (being replaced)

### RPi side — 5 supervisor-managed processes

```
[supervisor]
  ├── exp_manager (evolver_UPD.py) — RS485 loop, reads config files, writes data files
  ├── UDP_TEMP (UDP_TEMP.py)       — UDP server on port 5553
  ├── UDP_OD (UDP_OD.py)           — UDP server on port 5554
  ├── UDP_FAN (UDP_FAN.py)         — UDP server on port 5551
  └── UDP_FLUIDICS (UDP_FLUIDICS.py) — UDP server on port 5552
```

### File-based IPC between UDP servers and exp_manager

```
Mac ──UDP──> UDP_*.py ──writes──> config file ──read by──> exp_manager ──RS485──> Arduino
Arduino ──RS485──> exp_manager ──writes──> data file ──read by──> UDP_*.py ──UDP──> Mac
```

Config files (written by UDP servers, consumed by exp_manager):
- `fan_config.txt` — stir PWM values (overwritten each UDP write)
- `temp_config.txt` — temperature setpoint values (overwritten each UDP write)
- `OD_config.txt` — OD LED power values (overwritten each UDP write)
- `fluid_config.txt` — pump commands, **appended** by UDP_FLUIDICS (queue). exp_manager drains up to 3 lines per loop iteration and `delete_line()`s each. This is the one config file that behaves as a queue rather than a single-slot mailbox.

Data files (written by exp_manager, read by UDP servers):
- `temp_data.txt` — raw ADC temperature readings (16 values, comma-separated)
- `OD_data.txt` — raw ADC OD readings (16 values, comma-separated)

All files are in `/home/pi/eVOLVER_UDP/`.

### UDP port mapping

| Port | Process       | Subsystem  | Responds? | Notes                                        |
|------|---------------|------------|-----------|----------------------------------------------|
| 5551 | UDP_FAN       | Stir       | No        | Write-only, response line is commented out    |
| 5552 | UDP_FLUIDICS  | Fluidics   | Yes (UDP) | UDP server replies "Message Recieved" (sic); the RS485/Arduino side is write-only |
| 5553 | UDP_TEMP      | Temperature| Yes       | Non-'clear' msg: writes config, returns data  |
| 5554 | UDP_OD        | OD         | Yes       | Non-'clear' msg: writes config, returns data  |

### UDP_TEMP and UDP_OD protocol detail

These two servers have identical logic:
- Receiving `'clear'`: reads the CONFIG file, sends its contents back, then empties the config file
- Receiving anything else: writes the message to the CONFIG file, then reads the DATA file and sends its contents back

So to read sensor data without issuing a command, send any benign string (e.g. `'read'`). The exp_manager will try to send it as a command but the Arduino will ignore the malformed message.

### Mac side — 3 Python files

- `eVOLVER_module.py` — UDP client library with functions: `read_OD()`, `update_temp()`, `fluid_command()`, `update_chemo()`, `stir_rate()`, `stop_all_pumps()`, `parse_data()`, `initialize_exp()`, `save_var()`
- `main_eVOLVER.py` — Tkinter GUI + 10-second measurement loop calling `update_eVOLVER()`
- `custom_script.py` — experiment-specific logic (turbidostat, chemostat, etc.)

### Exp_manager main loop (evolver_UPD.py)

Runs continuously in this order:
```python
while (1):
    Arduino_Fluidic(Fluid_Name, 'st', ' !', 're !', 'nc !')
    Arduino_Stir(Fan_Name, 'zv', ' !', 'wq !')
    Arduino_Temperature(Temp_Name, 'xr', ' !', 'pf !', 'qe !', 'em !')
    Arduino_OD(OD_Name, 'we', ' !', 'oq !', 'tr !', 'cd !')
```

Each function:
1. Reads the corresponding config file
2. If empty, prints "Config Empty" and skips
3. If populated, prepends the address prefix, appends ` !`, sends over RS485
4. For temp/OD: waits for serial response, parses it, writes to data file
5. Clears the config file after processing

## Calibration

### Temperature calibration (`temp_calibration.txt`)
- 2 rows x 16 columns (slope and intercept per vial)
- Row 0 = slopes (typically ≈ −0.11, **negative**), Row 1 = intercepts (typically ≈ 80–86)
- Reading (raw thermistor ADC → °C): `temp_celsius = (raw_adc * slope[vial]) + intercept[vial]`
- Setting (target °C → `xr` setpoint integer): `setpoint = (target_celsius - intercept[vial]) / slope[vial]`
- Because slope is negative, **larger setpoint corresponds to a colder target**. The Arduino's closed loop drives the heater PWM until the thermistor ADC reading reaches this setpoint, so the `xr` value is best thought of as a "target ADC reading," not as a PWM duty cycle. See the warning at the top of the Testing section.

### OD calibration (`OD_cal.txt`)
- 4 rows x 16 columns
- 4-parameter logistic conversion:
```python
OD = od_cal[2,vial] - (log10((od_cal[1,vial] - od_cal[0,vial]) / (raw_adc - od_cal[0,vial]) - 1)) / od_cal[3,vial]
```
- Row 0: dark reading — ADC value when LEDs are off (lower asymptote of the sigmoid)
- Row 1: saturation reading — ADC value with LEDs on and no culture present (upper asymptote)
- Row 2: OD value at the sigmoid inflection point (the centre of the calibration curve)
- Row 3: Hill coefficient — slope/steepness of the logistic (larger = sharper transition)

## Pump command format

Each Mac-side `MESSAGE` shown below is the *payload* written into `fluid_config.txt`. exp_manager prepends the address prefix `st` before sending over RS485, so e.g. payload `t,11…,0,…` goes on the wire as `stt,11…,0,…, !`.

```python
# Binary addressing: each pump has a power-of-2 address
control = np.power(2, range(0, 32))
# Vial N influx: control[N] = 2^N
# Vial N efflux: control[N+16] = 2^(N+16)

# --- Single-fire sub-mode (wire: st<binary>,0,<sec>, !) ---

# Both influx and efflux for vial 0 for 10 seconds:
MESSAGE = "{0:b}".format(control[0] + control[16]) + ",0,10,"
# payload = "10000000000000001,0,10,"  →  wire = "st10000000000000001,0,10, !"

# Efflux only for vial 0 for 5 seconds:
MESSAGE = "{0:b}".format(control[16]) + ",0,5,"
# payload = "10000000000000000,0,5,"  →  wire = "st10000000000000000,0,5, !"

# --- Stop / multi-fire sub-mode (wire: stt,<32-bit mask>,<16 times>, !) ---

# Stop all pumps (mask all-ones, all times zero):
MESSAGE = "t,11111111111111111111111111111111,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,"
# wire = "stt,<32 ones>,<16 zeros>, !"
# Sub-prefix "t," tells the pump Arduino "this is the multi-fire/all-stop form."

# --- Chemostat sub-mode (wire: stc,<16 rates>,<bolus>, !) ---

# Set continuous chemostat rates per vial plus a bolus volume:
MESSAGE = "c," + ",".join(str(int(r)) for r in chemo_rates) + "," + str(int(bolus)) + ","
# wire = "stc,<r0>,<r1>,…,<r15>,<bolus>, !"
# See `eVOLVER_module.update_chemo` for the exact dispatch in legacy code.
```

## Turbidostat control logic (from custom_script.py)

Key parameters:
- `lower_thresh` — OD below which dilution stops (per vial, np.array of 16)
- `upper_thresh` — OD above which dilution triggers (per vial, 99999 = disabled)
- `pump_wait` — minimum minutes between pump events (default: 15)
- `flow_rate` — ml/sec per pump (np.array of 16, from pump calibration)
- `volume` — vial volume in mL (default: 25)
- `time_out` — extra seconds for efflux pump to prevent overflow (default: 5)

Algorithm per vial per cycle:
0. Warmup gate: dormant until `len(OD_history) > 7` — turbidostat does nothing for the first 8 measurement cycles (`custom_script.py:83`).
1. Average the last 5 OD readings (most recent 5, equal weight).
2. Hysteresis: if avg OD > upper_thresh, set target = lower_thresh
3. Hysteresis: if avg OD < midpoint, set target = upper_thresh (stop diluting). Midpoint = `lower_thresh + (upper_thresh - lower_thresh)/2`.
4. If avg OD > current target:
   - Calculate pump time: `time_in = -(ln(lower_thresh/avg_OD) * volume) / flow_rate`
   - Cap at 20 seconds max
   - Cast to integer seconds — the legacy formats `time_in` with `%d`, so sub-second pump targets truncate to 0 and no pump fires.
   - Check that `pump_wait` minutes have elapsed since last pump event
   - Fire influx + efflux for `time_in` seconds
   - Fire efflux alone for `time_out` additional seconds

## What has been built

**Phase 1 shipped and is deployed.** The Flask server replaces all 5 legacy supervisor
processes, owns `/dev/ttyAMA0` directly, and serves the dashboard at
`http://192.168.1.2:5000`. Most of Phase 2 is done as well.

| Subsystem | Status | Where |
|---|---|---|
| Flask + socketio server, 10 s sensor loop, watchdog, shutdown handler | Built | `server/app.py`, `watchdog.py` |
| `SerialManager` + `MockSerialManager` (`--mock` runs with no hardware) | Built | `server/serial_manager.py`, `mock_serial_manager.py` |
| Dashboard, 4×4 vial grid, per-vial modal with uPlot charts | Built | `frontend/templates/index.html` (single file) |
| Manual controls (temperature in °C, stir, pump, emergency stop) | Built | `/api/actuators/*` |
| 7-step experiment wizard (Name → Media → Vials → Waste → Mode → Params → Review) | Built | `index.html` `#exp-modal` |
| Media bottles, vial→bottle map, waste tracking, low/high alerts, refill | Built | `server/experiment_engine.py` |
| Maintenance mode with 30 min auto-resume failsafe | Built | `experiment_engine.py` |
| Control modes: turbidostat, chemostat, morbidostat | Built | `server/control_modes/` |
| Data export (ZIP), exports browser, disk monitoring | Built | `server/data_export.py` |
| Crash recovery / resume from `state.json` | Built | `experiment_engine.py` |
| Deployment: systemd unit, `install.sh`, Tailscale keepalive | Built | `DEPLOY.md`, `deploy/` |

**Not yet built** (in rough priority order — see `ROADMAP.md`):
consumables safety interlock, volume-based manual pumping, structured logging and a
unified event log, a general per-vial growth-rate service, calibration wizards of any kind
(**there are currently no `/api/calibration/*` routes at all**), hygiene/sterilisation
records, experiment templates, supervised per-vial override, anomaly detection,
notifications, off-box backup, vial groups, multi-phase protocols, and authentication.

### Where to look for what

- `ROADMAP.md` — **current prioritised work plan.** Derived from the August 2026 lab
  meeting; triages every outstanding feature by urgency, utility, and difficulty, and
  records which items were deliberately deferred and why.
- `SPEC.md` — technical specification. §15–§25 cover the subsystems listed as not-yet-built.
- `SESSION_MASTER_PLAN.md` — the original Session A–J plan. A/B/C/F/G are built; read the
  status header at the top before following any of its prompts.
- `DEPLOY.md` — operator runbook for the RPi.

### Two facts worth carrying into any new work

1. **`xr` is a closed-loop setpoint, not a PWM, and the slope is negative.** The Arduino
   already closes the temperature loop. `xr=0` requests ~82 °C. See the Testing warning
   below and `SPEC.md` §10. `SESSION_MASTER_PLAN.md` Session H originally got this wrong;
   it now carries a correction.
2. **All volume tracking is open-loop inference**, computed as `duration × flow_rate` and
   accumulated. `pump_flow_rates` is currently a hardcoded default array that has never
   been measured on this machine. Media levels, the planned consumables interlock, and one
   of the two planned growth-rate estimators are all downstream of it — nothing built on
   those numbers is more accurate than that array. Gravimetric pump calibration
   (`ROADMAP.md` Session O) is the fix.

## Technical constraints

- RPi runs Python 2.7 (Raspbian). The new server should use Python 3 if available, falling back to Python 2.7 if needed. Check with `python3 --version`.
- Serial port /dev/ttyAMA0 can only be opened by one process at a time. The new server must be the sole serial user — the old supervisor processes must be stopped first.
- RS485 is half-duplex: you send a command, then wait for a response. Commands and responses are newline-terminated (`ser.readline()`).
- Serial timeout is 5 seconds. If an Arduino doesn't respond, the system should log the failure and continue, not hang.
- The RS485 bus is shared — all 4 Arduinos hear all messages but only respond to their address prefix. Do not send commands faster than the Arduinos can process them (50ms minimum between commands).
- The 2016 Arduino firmware is not being changed. The new server must speak the exact same serial protocol.
- Calibration files must remain compatible with the existing format.
- The lab Mac Mini runs macOS 10.12.6 (Sierra) with Python 2.7, but it only needs to run a browser to access the new web GUI.
- The router is a Netgear N300 at 192.168.1.1. The RPi is at 192.168.1.2. The Mac gets its IP via DHCP (typically 192.168.1.3).

## Stopping the old system before running the new one

```bash
# SSH into RPi
ssh pi@192.168.1.2

# Stop all old processes
sudo supervisorctl stop all

# Verify nothing is using the serial port
sudo fuser /dev/ttyAMA0

# Now start the new server
cd /home/pi/evolver-gui
python3 app.py  # or python app.py
```

## Testing

> ⚠ **Heater control convention is inverted.** The `xr` value is **not a raw PWM** — it is a setpoint that the temperature Arduino's closed loop drives the thermistor ADC reading toward. The calibration slope is **negative**, so **lower `xr` = hotter target**. `xr=0` requests ~82 °C (heater pinned to MAX); `xr=4095` is unreachably cold and is the only definitive "off." Any code or doc that treats `xr` like a PWM (where 0 means off) is wrong.

- Stir: send address `zv` + 16 comma-separated PWM values + ` !`. Values 0-15 typical. 0=off (this one really is a raw PWM).
- Temperature: send address `xr` + 16 setpoint integers + ` !`. Practical operating range ≈ 400 (≈ 37 °C) to 700 (≈ 14 °C); 4095 = off; 0 = drive heater to ~82 °C (avoid). Read response with prefix `temp` (16 raw thermistor ADC values).
- OD: send address `we` + 16 LED power values + ` !`. 2125 is standard. Read response with prefix `turb` (16 raw photodiode ADC values).
- Fluidics: send address `st` + pump binary code + ` !`. Verify water flows. See "Fluidics sub-protocol" for the three command sub-modes.
- All 16 vials have been hardware-verified as functional (stir, temp, OD, fluidics all pass).

## Repository structure

```
eVOLVER_FileStruct/
  CLAUDE.md                  # This file — hardware facts and serial protocol
  SPEC.md                    # Technical specification (§15-§25 = planned subsystems)
  ROADMAP.md                 # Current prioritised work plan (Aug 2026 lab meeting)
  SESSION_MASTER_PLAN.md     # Original Session A-J plan; see its status header
  DEPLOY.md                  # RPi operator runbook

  rpi_original/              # Original RPi scripts (reference only, do not modify)
    evolver_UPD.py
    UDP_TEMP.py  UDP_OD.py  UDP_FAN.py  UDP_FLUIDICS.py
    RS485_TEST.py
    extras/
  mac_original/              # Original Mac client scripts (reference only)
    eVOLVER_module.py  main_eVOLVER.py  custom_script.py

  calibration/               # Calibration data files (2016-era, UNVERIFIED)
    OD_cal.txt               # 4 x 16 sigmoid parameters
    temp_calibration.txt     # 2 x 16 slope/intercept
    # pump_calibration.json  — planned, ROADMAP Session O
    # stir_calibration.json  — planned, SPEC §25

  server/
    app.py                   # Flask routes, socketio, sensor loop, shutdown handler
    serial_manager.py        # RS485 (real hardware); owns /dev/ttyAMA0
    mock_serial_manager.py   # Simulated hardware — run app.py --mock
    experiment_engine.py     # Lifecycle, run_cycle, media tracking, maintenance, resume
    data_logger.py           # Per-vial CSV writers
    data_export.py           # ZIP bundles, filtering
    watchdog.py              # Heartbeat -> emergency shutdown
    control_modes/
      turbidostat.py  chemostat.py  morbidostat.py
    test_*.py                # pytest suite, all runnable against the mock

  frontend/
    templates/index.html     # Entire GUI in one file (dashboard, wizard, plots)
    static/js/uPlot.iife.min.js

  experiments/{name}/        # Runtime output
    config.json  state.json
    vial00_OD.csv  vial00_temp.csv  vial00_pump_log.csv  ...
    # events.csv             — planned, SPEC §20
  exports/                   # Server-side export bundles (outside experiments/ by design)
  deploy/                    # systemd unit, Tailscale keepalive
```

Run locally with no hardware: `python server/app.py --mock`
Run the tests: `cd server && python -m pytest`
