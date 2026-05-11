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
[Auxiliary Board: 48 peristaltic pumps (3 per vial: influx, efflux, spare)]
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
- 48 peristaltic pumps total (3 per vial)
- Per vial: influx pump (fresh media in), efflux pump (culture out), spare
- Controlled by a separate SAMD21 on the Auxiliary Board
- Pump addressing uses binary codes: influx = 2^vial, efflux = 2^(vial+16)

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

Stir and fluidics are write-only — Arduinos acknowledge but don't return sensor data for these.

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
- `fan_config.txt` — stir PWM values
- `temp_config.txt` — temperature PWM values
- `OD_config.txt` — OD LED power values
- `fluid_config.txt` — pump commands

Data files (written by exp_manager, read by UDP servers):
- `temp_data.txt` — raw ADC temperature readings (16 values, comma-separated)
- `OD_data.txt` — raw ADC OD readings (16 values, comma-separated)

All files are in `/home/pi/eVOLVER_UDP/`.

### UDP port mapping

| Port | Process       | Subsystem  | Responds? | Notes                                        |
|------|---------------|------------|-----------|----------------------------------------------|
| 5551 | UDP_FAN       | Stir       | No        | Write-only, response line is commented out    |
| 5552 | UDP_FLUIDICS  | Fluidics   | Yes       | Returns "Message Recieved" (sic)              |
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
- Conversion: `temp_celsius = (raw_adc * slope[vial]) + intercept[vial]`
- Inverse (for setting): `pwm_value = (target_celsius - intercept[vial]) / slope[vial]`

### OD calibration (`OD_cal.txt`)
- 4 rows x 16 columns
- 4-parameter logistic conversion:
```python
OD = od_cal[2,vial] - (log10((od_cal[1,vial] - od_cal[0,vial]) / (raw_adc - od_cal[0,vial]) - 1)) / od_cal[3,vial]
```
- Row 0: dark reading (LEDs off)
- Row 1: saturation reading (LEDs on, no culture)
- Row 2, Row 3: curve fit parameters

## Pump command format

```python
# Binary addressing: each pump has a power-of-2 address
control = np.power(2, range(0, 32))
# Vial N influx: control[N] = 2^N
# Vial N efflux: control[N+16] = 2^(N+16)

# Command to run both influx and efflux for vial 0 for 10 seconds:
MESSAGE = "{0:b}".format(control[0] + control[16]) + ",0,10,"
# = "10000000000000001,0,10,"

# Efflux only for vial 0 for 5 seconds:
MESSAGE = "{0:b}".format(control[16]) + ",0,5,"
# = "10000000000000000,0,5,"

# Stop all pumps:
MESSAGE = "t,11111111111111111111111111111111,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,"
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
1. Average the last 5 OD readings
2. Hysteresis: if avg OD > upper_thresh, set target = lower_thresh
3. Hysteresis: if avg OD < midpoint, set target = upper_thresh (stop diluting)
4. If avg OD > current target:
   - Calculate pump time: `time_in = -(ln(lower_thresh/avg_OD) * volume) / flow_rate`
   - Cap at 20 seconds max
   - Check that `pump_wait` minutes have elapsed since last pump event
   - Fire influx + efflux for `time_in` seconds
   - Fire efflux alone for `time_out` additional seconds

## What to build

### Phase 1: Web dashboard on RPi
- Flask/FastAPI server running on the RPi
- Replaces all 5 current processes (4 UDP servers + exp_manager) with a single app
- Directly reads/writes RS485 serial (no file-based IPC, no UDP)
- Serves a web GUI at http://192.168.1.2:5000
- Real-time display of temperature and OD for all 16 vials (WebSocket push every 10s)
- Manual controls: set temperature, stir rate, trigger pump, stop all
- Mobile-friendly responsive design

### Phase 2: Experiment engine
- Config-driven experiment system (JSON/YAML config files, not hardcoded Python)
- Support multiple control modes: turbidostat, chemostat, morbidostat, growth rate control
- Independent experiment groups: partition 16 vials into groups with different parameters
- Phase-based protocols: sequential experiment phases with trigger conditions
- Real-time growth rate estimation from OD sliding window
- Automatic data logging to structured CSV/JSON files

### Phase 3: Experiment designer GUI
- Visual drag-and-configure interface for experiment design
- Vial group assignment (click to assign vials to experiment groups)
- Parameter configuration with sliders and validated inputs
- Protocol timeline builder for multi-phase experiments
- Preset templates for common experiment types
- Config export/import for sharing experiment designs

### Phase 4: Monitoring and alerting
- Historical data plots (OD, temperature, growth rate over time)
- Pump event log visualization
- Configurable alerts: OD stagnation, temperature deviation, excessive pump cycling
- Slack/email notification integration

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

- Stir: send address `zv` + 16 comma-separated PWM values + ` !`. Values 0-15 typical. 0=off.
- Temperature: send address `xr` + 16 PWM values + ` !`. 0=off, 200=mild, 500=hot. Read response with prefix `temp`.
- OD: send address `we` + 16 LED power values + ` !`. 2125 is standard. Read response with prefix `turb`.
- Fluidics: send address `st` + pump binary code + ` !`. Verify water flows.
- All 16 vials have been hardware-verified as functional (stir, temp, OD, fluidics all pass).

## Repository structure

```
evolver-gui/
  CLAUDE.md                  # This file
  rpi_original/              # Original RPi scripts (reference only, do not modify)
    evolver_UPD.py
    UDP_TEMP.py
    UDP_OD.py
    UDP_FAN.py
    UDP_FLUIDICS.py
    RS485_TEST.py
  mac_original/              # Original Mac client scripts (reference only)
    eVOLVER_module.py
    main_eVOLVER.py
    custom_script.py
  calibration/               # Calibration data files
    OD_cal.txt
    temp_calibration.txt
  server/                    # New Flask server (to be built)
    app.py
    serial_manager.py
    experiment_engine.py
    ...
  frontend/                  # New web GUI (to be built)
    ...
```
