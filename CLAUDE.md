# CLAUDE.md — eVOLVER Control System

## Project overview

This project modernizes the control software for a 2016-era eVOLVER continuous culture platform built by the Boston University Engineering Design Facility for the Khalil Lab. The eVOLVER supports 16 independent culture vials with individual control over temperature, optical density (OD) sensing, stirring, and fluidics (pumps). The goal is to replace the current split Mac/RPi architecture with a single web-based server running on the Raspberry Pi, accessible from any browser.

## Hardware architecture

```
[Browser on any device]
        |
        | HTTP / WebSocket
        v
[Raspberry Pi (pi@evolver-001 over Tailscale)]  ← runs the new Flask server
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
- Network identity: **`pi@evolver-001` over Tailscale** — the dedicated Netgear N300 LAN
  router this rig once sat behind is decommissioned, and the `192.168.1.2` static IP it
  handed out no longer resolves to anything. Whatever LAN the Pi's Ethernet is plugged into
  now is only there to give Tailscale egress; nothing should address the rig by LAN IP.
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
- **Influx and efflux are physically separate pumps and do not share a flow rate.** Two
  pumps fired for the same duration do not move the same volume. Treat every flow rate as
  per-pump (32 values), not per-vial (16).

### Vial working volume is set by the efflux straw, not by pump balance

This is the single most important fluidics fact and it was not written down anywhere until
Aug 2026.

The efflux tube ("straw") terminates at a fixed height inside the vial. Efflux draws from
that tip. Once the liquid level reaches the tip, the pump draws **air**, and further
running removes nothing. So if efflux is run *longer* than strictly needed, working volume
is pinned to the straw height on every dilution — a closed loop implemented in hardware.

**Measured / set geometry, as of 2026-09-23:**

| Constant | Value | Where it comes from |
|---|---|---|
| Vial capacity | **40 mL** (all 16) | the vials themselves |
| Working volume | **25 mL** | the operator sets it by cutting the straw; currently 25 mL on every vial |
| Dead space (headroom) | **15 mL** | capacity − working volume |

So `volume_ml: 25.0` is no longer a guess — it is the straw height, deliberately set. Straw
height is an **operator-set parameter, not a per-vial measured constant**: the straws are
cut to a common target, so a scalar config value is correct and per-vial arrays are not
needed. If someone re-cuts a straw, they change `volume_ml` to match.

Consequences:

- **Vial volume is a geometric constant you set by cutting the straw.**
- **With adequate overrun, liquid removed per dilution equals liquid added.** Waste
  accumulation is therefore `waste += influx_volume`, exactly. Computing it as
  `efflux_seconds × efflux_flow_rate` is wrong twice over: wrong rate, and it counts the
  air-drawing portion of the overrun as liquid.
- **Without overrun, there is no volume regulation at all.** Level becomes an open-loop
  integral of the influx/efflux flow mismatch, drifting monotonically until the vial
  overflows or runs dry. Nothing in software can observe this — there is no level sensor.

`efflux_extra_seconds` is what engages the mechanism. The repo default is **0.0** (commit
`a7b408a`, "live-validated default from eVOLVER-001"), which disables it. **That gate is now
closed:** the operator has run the rig with **2.0 s** of overrun against a 25 mL straw and it
behaves. The value belongs in the experiment config. The creation wizard never wrote this key
before 2026-09-23, so every wizard-created run until then silently inherited the 0.0 default;
it now has an "Efflux overrun" field (Parameters step) defaulting to 2 s.

**The straw pins the steady state, not the transient.** A correct overrun restores the level
*after* a dilution; it does nothing about how high the level climbs *during* one. That
distinction is what caused a pilot run to overflow, and the mechanism is in the next section:
the server issued influx and efflux as two separate frames, the firmware runs frames strictly
one at a time, so nothing drained for the whole influx phase. **Fixed 2026-09-23:** dilutions
now go out as one concurrent schedule (`server/fluidics.py`). See `FLUIDICS_FIRMWARE_AUDIT.md`.

Do **not** try to fix volume drift by computing a balancing efflux duration in software.
`t_efflux = (F_in × t_in) / F_out` is generally non-integer, and the firmware accepts whole
seconds only (see the deficit-accumulator note in `SPEC.md` §9) — for typical dilutions the
truncation is the same size as the correction, so it quantises away to nothing.

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

Stir and fluidics are write-only. **Measured 2026-09-23: the fluidics Arduino never transmits
anything at all** — no receipt ack, no completion ack, and no reply to the `re !` readiness
poll either idle or mid-pump. The `Fluidic_status()` function in `evolver_UPD.py` greps a
reply for `'ready'`; it is called only from a commented-out branch, and now we know why — it
never worked. `rpi_original/extras/pump_rs485.py` prints `"Completed Command:" + received`
after a `readline()` with `timeout=1`, so that print was always empty.

### Fluidics sub-protocol

The `st` address prefix is a namespace; the first character of the payload selects a sub-mode.
**Measured on the bench 2026-09-23 — `FLUIDICS_FIRMWARE_AUDIT.md` has the full protocol, the
evidence for each claim, and what is still unmeasured.**

| Sub-mode          | Wire format                                     | Status on this rig |
|-------------------|-------------------------------------------------|--------------------|
| Single fire       | `st<binary_pump_code>,0,<seconds>, !`           | **The only working form.** Fires every pump in the mask *simultaneously* for the commanded whole seconds. |
| `t` sub-mode      | `stt,<32-bit pump mask>,<time0>,…,<time15>, !`  | **INERT — fires nothing and stops nothing.** Was documented here as "Stop / multi-fire"; both halves were inferences from the frame's shape and both are false (audit FW-10, FW-11). `eVOLVER_module.stop_all_pumps` and `SerialManager.stop_all_pumps()` both send this frame, so **neither stops anything.** |
| Chemostat (rates) | `stc,<rate0>,…,<rate15>,<bolus>, !`             | Untested. Legacy `eVOLVER_module.update_chemo` (lines 92–101); not implemented in the current server. |

The measured firmware model, in one paragraph:

> Frames are **queued** and executed **one at a time, in order, to completion**. Each frame
> runs **every pump in its mask concurrently** (verified to 14 pumps at full speed) for
> **exactly** the commanded whole seconds — no startup lag, no clamp. Durations are
> **additive** across frames on the same pump. The firmware **never preempts, never transmits
> anything, and cannot be stopped.**

Three consequences for any new work:

1. **`st<mask>,0,<sec>,` can fire influx and efflux together** by OR-ing both bits, exactly as
   `mac_original/custom_script.py:114` did. Until 2026-09-23 the server used
   `SerialManager.pump_command(vial, direction, …)`, which sets one bit, so it issued two
   frames that *serialised* and **nothing drained during the influx phase** — the
   vial-overflow mechanism. Dilutions now go through `SerialManager.pump_frames` with a
   schedule from `fluidics.plan_dilution_frames`: every diluting vial's influx and efflux
   start together, all vials run in parallel, and the efflux overrun is the tail. **Never
   fire a dilution as per-direction frames again.**
2. **There is no software stop.** The only real emergency stop is cutting power to the
   auxiliary pump board. Command duration *is* the exposure window, which makes every
   duration cap a safety parameter.
3. **Because one frame drives any mask concurrently and durations are additive**, any
   piecewise-constant pump schedule is expressible as a sequence of frames, at a bus cost of
   the schedule's *span* rather than the sum of its durations (audit §4.2).

Note: `extras/pump_rs485.py` also sends a bare `st !` between commands. It elicits no reply
(the fluidics Arduino never transmits) and its meaning remains undocumented.

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

> **Since Session O (2026-08-20):** the two `.txt` files below are **derived views**,
> regenerated by `server/calibration_service.py` from the versioned JSON envelopes under
> `calibration/{od,temperature,pump,stir}/` + `calibration/current.json`. Never hand-edit
> the `.txt` files — install a new version through the store (or the wizards) and the
> views regenerate. `SerialManager.load_calibration()` still reads the `.txt` formats
> unchanged. Additionally, each run takes a **per-run OD blank**
> (`experiments/{name}/od_blank.json`) that re-anchors row 2 of the OD calibration
> in memory for that run only — see `CALIBRATION_PROTOCOL.md` §5.4/§10.1.
>
> **Since Session AA (2026-09-28):** the Tier 3 wizards install new versions and
> `app._reload_calibration` makes them live without a restart. A blank taken on the
> previous curve is refused at start (`stale_od_blank`).
>
> **The OD photodiode front end inverts — measured 2026-10-11.** With the LED off (`we0`)
> every vial reads **~64 100–65 000, near the ADC rail, above every LED-on read** (blank
> medium 64 485 lit vs 64 987 dark; a dense vial 26 884 lit vs 64 653 dark). Counts fall as
> light reaches the photodiode. So "LED-on minus LED-off" is *negative*, and the Session AA
> series — which fitted on it and installed dark-subtracted curves — rejected every point
> on the rig. **The OD series fits and installs on the raw LED-on signal**
> (`dark_subtracted: false`), the space every working curve on this rig uses; the LED-off
> read is recorded on each point and in `vialNN_OD.csv` as a diagnostic only. Do not
> reintroduce `lit − dark` anywhere: if an offset correction is ever wanted it is
> `dark − lit`, and its curve shape is untested.
>
> **Since Session AG (2026-09-29):** OD calibration is **per organism**. `current.json`'s
> `"od"` is the machine *default* (what `OD_cal.txt` views and idle reads use — currently the
> 2016 import, labelled "Unknown organism — 2016 inherited curve (unverified)"); named
> organisms live in `"od_profiles"`, and only the OD dilution series creates or recalibrates
> one. Each experiment pins one profile version at create and reads on it, per vial, for its
> whole life. See fact 12 and `SPEC.md` §19.7.

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
- Row 0: lower asymptote of the sigmoid — a fitted parameter, **not** the LED-off reading (LED-off reads near the ADC rail, ~64 500, the *top* of the range; see the note above)
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
#
# CANONICAL PUMP INDEX (use this everywhere — calibration files, flow-rate
# arrays, per-pump records):
#     index 0..15   = influx pump for vial (index)
#     index 16..31  = efflux pump for vial (index - 16)
# i.e. pump index == the exponent in the binary address above. Any 32-element
# `pump_flow_rates` array is ordered by this convention.

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

### Where `server/control_modes/turbidostat.py` deliberately differs

The port is faithful to the legacy algorithm above except in the ordering and bookkeeping
around it, which `CONTROL_MODE_AUDIT.md` found to be where every defect lived. Carry these
three differences into any new work:

1. **The `pump_wait` gate is checked *before* the pump-time formula is evaluated**, not
   after. `pump_time` is an absolute correction (seconds to bring the current OD down to
   `lower_thresh`), so anything that accumulates it across cycles the gate then blocks is an
   integrator with no anti-windup — measured 10–47 % OD floor breach.
2. **There is no deficit accumulator in the turbidostat**, only in the chemostat. The
   whole-second remainder is discarded, which makes `int(t) ≤ t_needed` and the OD floor
   unbreachable by construction. What replaces it is a create-time check that the band can
   produce a ≥ 1 s bolus at all: `ln(upper/lower) × volume / flow_rate ≥ 1`, else HTTP 400.
3. **The bolus is sized from the newest OD sample, not the 5-sample mean.** The mean still
   decides *whether* to dilute — that is what the rolling window is for — but it lags the
   culture's present density during growth.

## What has been built

**Phase 1 shipped and is deployed.** The Flask server replaces all 5 legacy supervisor
processes, owns `/dev/ttyAMA0` directly, and serves the dashboard at
`http://evolver-001:5000`. Most of Phase 2 is done as well.

| Subsystem | Status | Where |
|---|---|---|
| Flask + socketio server, 10 s sensor loop (two-lane capable), watchdog, shutdown handler | Built | `server/app.py`, `watchdog.py` |
| Per-group stir pause before OD reads (settle, accept only the settled read; wizard toggle) | Built (software) | `server/od_stir_pause.py`, `run_config.py`, `supervisor.py`, SPEC §9.1.1 |
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
| Minimal runtime export: `.gitattributes` `export-ignore` + `deploy.sh` build the Pi's runtime directory via `git archive`, so docs/tests/dev tooling/legacy reference code never land there | Built | `.gitattributes`, `deploy.sh`, `DEPLOY.md` P3.3 |
| Consumables safety interlock (reserve/waste hard stop, auto-maintenance) | Built | `experiment_engine.py` |
| Volume-based fluidics (mL manual pumping, quantisation preview) | Built | `experiment_engine.py`, `/api/actuators/pump` |
| Concurrent dilution schedules (influx+efflux together, all vials in parallel, efflux overrun kept) | Built | `server/fluidics.py`, `app.py:_dispatch_dilutions` |
| Rotating disk-aware logs, per-experiment `events.csv`, error classification | Built | `server/event_log.py` |
| Alert drawer, RS485 bus indicator, per-vial sensor-health badge | Built | `index.html`, `/api/events/*`, `/api/health` |
| Calibration provenance store (versioned envelopes, legacy `.txt` as derived views) | Built | `server/calibration_service.py`, `calibration/` |
| Per-run OD blank wizard (re-anchors row 2 only; hard-blocks start when missing) | Built | `/api/calibration/od/blank/*`, `index.html` Calibration tab |
| Pump gravimetric wizard (32 pumps, resumable session, QC + override) | Built | `/api/calibration/pump/*` |
| 32 independent flow rates in the engine (influx ≠ efflux) | Built | `experiment_engine._as_flow_rates_32`, `control_modes/*` |
| Post-run mass reconciliation + staleness surfacing + dashboard banner | Built | `/api/experiments/{name}/reconcile`, `/api/calibration/staleness` |
| Per-vial growth-rate service (μ, doubling time, R², gated dilution diagnostic) | Built | `server/growth_rate.py`, `/api/growth_rate`, `vialNN_growth.csv` |
| Vial groups within one experiment (per-group mode/params/temp/stir, one lifecycle), preconditioning for the blank, `CONTROL_MODES` registry | Built | `server/run_config.py`, `experiment_engine.py`, SPEC §9.1 |
| Parallel experiments (independent experiments, vial ownership, shared bottles/carboys, machine hold, N-way resume) | Built | `server/supervisor.py`, `server/vessels.py`, SPEC §9.2 |
| Tier 3 calibration wizards: thermistor two-point (P2 built into the cold phase, retarget, verify before install) and OD dilution series (four-parameter fit, dark-subtracted install) | Built (software) | `server/calibration_service.py`, `server/od_curve_fit.py`, `/api/calibration/temperature/*`, `od/series/*`, SPEC §19.5 |
| Live calibration reload; machine-reserving Tier 3 sessions with their own heater watchdog | Built | `app.py:_reload_calibration`, `_calibration_tick`, `_cal_route(idle=…)` |
| Per-organism OD profiles (series creates/recalibrates; designer picks; run pins; per-vial live curve) | Built | `calibration_service.py`, `serial_manager.OdCurveMixin`, `app._experiment_od_setup`, SPEC §19.7 |
| `vialNN_OD.csv` schema 2 (raw lit/dark counts appended) and post-hoc OD recalibration (CLI, API, GUI) | Built | `data_logger.py`, `server/od_recalibrate.py`, `/api/experiments/{name}/recalibrate_od`, SPEC §8 |
| Supervised per-vial override (hold-to-unlock, time-limited, control paused, dilute/spike/drain + temp/stir pushed into controller state, plot markers); `vialNN_pump_log.csv` schema 2 (`source` column) | Built (software) | `server/vial_override.py`, `/api/vials/{vial}/override*`, SPEC §21 |

**Session N shipped 2026-08-23** — the per-vial growth-rate service: the estimator, the
engine-owned OD history and dilution boundaries, `status()` / WebSocket / API / CSV
surfaces, and the validation harness. No control mode was touched.
**Sessions K, L, M and M2 shipped 2026-08-20** — consumables interlock, volume-based manual
pumping, structured logging + the unified event log, and the operator-facing alert drawer.
**Session O shipped 2026-08-20** — calibration provenance, the per-run OD blank, the pump
gravimetric wizard, reconciliation, and the 32-flow-rate engine plumbing. The software is
bench-ready but **no Tier 1/Tier 2 procedure has yet been run on the bench**, so the
inherited-constant caveats below still stand.

**Per-group stir pause shipped 2026-10-13** — `od_stir_pause` (SPEC §9.1.1): software only,
exercised against the mock and a live mock server; no settle time has been chosen on the rig.
**Session S shipped 2026-10-11** — the supervised per-vial override and pump-log schema 2.
Software only: exercised against the mock and a live mock server, not yet on the rig.
**Session AG shipped 2026-09-29** — per-organism OD calibration profiles, the raw-count OD
log (schema 2) and post-hoc recalibration. Software only: no organism has been calibrated
on the rig, so every run still reads through the 2016 curve until one is.
**Session AA shipped 2026-09-28** — the Tier 3 thermistor and OD dilution-series wizards,
software only: neither has been run on the rig, and prerequisite P2 stays unresolved until
the thermistor wizard's cold phase is.

**Not yet built** (in rough priority order — see `ROADMAP.md`):
stir RPM calibration (`/api/calibration/stir/*`, Session AC), hygiene/sterilisation records, experiment
templates, anomaly detection, notifications, off-box
backup, multi-phase protocols, and authentication.

### Where to look for what

- `ROADMAP.md` — **current prioritised work plan.** Derived from the August 2026 lab
  meeting; triages every outstanding feature by urgency, utility, and difficulty, and
  records which items were deliberately deferred and why.
- `SPEC.md` — technical specification. §15–§25 cover the subsystems listed as not-yet-built.
  §16.3 is the overflow-safe delivery design.
- `FLUIDICS_FIRMWARE_AUDIT.md` — **what the 2016 fluidics firmware actually does**, measured
  on the bench 2026-09-23. Thirteen numbered facts with the test behind each, what they break
  in the current codebase, and the design that follows. Read it before touching anything that
  fires a pump. It supersedes inference from `mac_original/` and `rpi_original/`, two claims
  of which turned out to be wrong.
- `SESSION_MASTER_PLAN.md` — the original Session A–J plan. A/B/C/F/G are built; read the
  status header at the top before following any of its prompts.
- `GROWTH_RATE_METHOD.md` — the growth-rate estimator: what the algorithm is, what was
  measured about it, and what was deliberately not ported from the Isaacs Lab notebook.
  Plays the role for Session N that `CALIBRATION_PROTOCOL.md` plays for Session O.
  `SPEC.md` §17 is the specification of what shipped.
- `CONTROL_MODE_AUDIT.md` — line-by-line + closed-loop audit of the turbidostat and
  chemostat (Aug 2026). All findings fixed as of 2026-08-21 except the `efflux_extra_seconds`
  bench decision (X-1, warning only) and the washout detector (C-5, deferred); the document
  is kept as the record of what was wrong.
- `DEPLOY.md` — operator runbook for the RPi: hardware validation, the pilot procedure,
  update-by-tag and rollback, day-to-day operations.
- `SETUP_FROM_SCRATCH.md` — building the Pi's SD card from a freshly flashed headless OS:
  harvesting the old card, static IP on NetworkManager, the `disable-bt` UART configuration
  and how to verify `/dev/ttyAMA0` is the PL011, restoring the state git does not carry,
  Tailscale identity, and which parts of `DEPLOY.md` Phase 4 a rebuild may skip.

### Fourteen facts worth carrying into any new work

1. **`xr` is a closed-loop setpoint, not a PWM, and the slope is negative.** The Arduino
   already closes the temperature loop. `xr=0` requests ~82 °C. See the Testing warning
   below and `SPEC.md` §10. `SESSION_MASTER_PLAN.md` Session H originally got this wrong;
   it now carries a correction.
2. **All volume tracking is open-loop inference**, computed as `duration × flow_rate` and
   accumulated. The engine now carries **32 independent flow rates** (canonical pump
   index: 0..15 influx, 16..31 efflux — Session O3a) and the gravimetric wizard
   (`/api/calibration/pump/*`) exists to measure them, **but the bench work has not been
   run**: until Tier 2 is performed, the rates in use are still the hardcoded 16-value
   default array broadcast to both directions, and every mL figure the GUI shows — and
   every media level derived from one — remains an estimate with unquantified error.
   The `estimate_quality` field flips to `"calibrated"` (and the UI label drops) only
   once a complete 32-pump calibration is installed. Post-run mass reconciliation
   (`POST /api/experiments/{name}/reconcile`) is the ongoing check that the rates stay
   right.

3. **The two control modes gate on different things, and the deficit accumulator belongs to
   only one of them.** The chemostat is open-loop: it exposes `requires_od = False`, and
   `run_cycle` must keep calling `decide()` for it even when OD is NaN or out of range —
   out-of-range means the culture is *denser* than the calibration covers, i.e. exactly when
   dilution must not stop. It sizes every bolus from elapsed wall time, never the nominal
   interval, because the sensor loop's real period is `max(tick, work)` and can only run
   slow. Its `bolus_interval_seconds` must be **>= the control interval** (currently
   10 s) — `decide()` is only called on an OD tick, so a shorter interval is not honoured
   and every bolus clips against a cap sized from the nominal interval
   (`validate_control_parameters` rejects it). It keeps a deficit accumulator, correctly, because what it accumulates is a
   per-interval increment. The turbidostat is the mirror image on all four counts. See
   `SPEC.md` §9 and `CONTROL_MODE_AUDIT.md`.

4. **Every alert goes through one funnel, and nothing may bypass it.** `app.py` exposes
   `_emit_alert` / `_emit_alert_payload` (alerts) and `_emit_event` (experiment events);
   both record to the ring buffer and `events.csv` *before* emitting to the browser. A bare
   `socketio.emit("alert", ...)` would reach a browser without being captured, defeating
   both the drawer and the lab-notebook record — the only surviving direct emit is
   `sensor_update`, which is telemetry, not an event. New failure sites should call
   `_emit_alert` with a stable `dedup_key`, otherwise the rate limiter cannot collapse a
   repeating fault. See `SPEC.md` §20 and `server/event_log.py`.

5. **The per-run OD blank hard-blocks experiment start, and it lives in memory.**
   `POST /api/experiments/{name}/start` returns 409 (`code: "missing_od_blank"`) until a
   blank is committed for that experiment, overridable only with an explicit
   `allow_missing_od_blank: true` (recorded as a warning). A committed blank re-anchors
   **row 2 only** of the run's **pinned organism curve** on that run's vials — never rows
   0/1/3, which are fitted asymptotes and the reader's validity domain — and is valid only
   for exactly that profile version (`stale_od_blank` otherwise). The re-anchor is
   in-memory: it is re-applied from `experiments/{name}/od_blank.json` before a resumed run
   can be ticked (fact 12) and released with the run's curve when the experiment stops. The legacy `.txt` calibration files are now a
   **derived view** regenerated from `calibration/current.json` — never hand-edit them;
   the versioned JSON envelopes are the source of truth. (The old fact here — that
   `_as_list_of_16` rejected 32-element `pump_flow_rates` — was fixed by Session O3a;
   32/16/scalar are all accepted now. See `SPEC.md` §16.1.)

6. **There is exactly one reported growth rate, and volume never touches it.** The
   Isaacs Lab windowed log-linear fit within inter-dilution segments is *the* μ
   (`server/growth_rate.py`). The dilution-rate calculation is a **gated diagnostic**,
   off while `calibration/current.json` has `"pump": null` — which it does. Dilution
   events feed the reported μ as *timestamps only*; `delivered_ml` is read solely by the
   diagnostic, which is what keeps μ independent of the uncalibrated pump array (a test
   doubles every volume and requires the reported μ to be bit-identical). Three
   consequences for new work: **fitting `ln(OD)` across a dilution reads 80–99 % low**, so
   never do it; **`r_squared` must never be presented without `windows_searched`**, because
   max-R² selection inflates it; and growth output goes to `vialNN_growth.csv`, never a
   column in `vialNN_OD.csv` — derived values stay out of the raw sensor record. That
   file's columns are **append-only**: schema 2 (Session AG) appended the raw counts at the
   end and `config.json` carries `od_csv_schema`. Never insert or reorder a column — a
   positional parser (the lab's own scripts included) breaks silently on an insert — and
   keep the logger matching each file's own header, so a schema-1 file resumed across an
   upgrade stays seven columns.

7. **Everything is on one 10 s cycle, and the loop never blocks — stirrers pause per vial
   group, by acceptance, not by waiting.** `app.py` sets `OD_EVERY_N_TICKS = 1`, so
   temperature and all sixteen OD channels are read every tick and control decides on it.
   A group's `od_stir_pause` parameter (SPEC §9.1.1) makes its vials lower their stir, let
   the vortex settle, and **accept only the read that starts after the settle**; every
   other read of those vials is flagged `stir_settling` / `stir_unsettled` and discarded
   (never pushed, never logged to `vialNN_OD.csv`). Four rules for new work:

   - **Never implement a settle as a wait.** Blocking the sensor thread stalls the
     *temperature* lane too; over-temperature is the one real-time guarantee *not*
     delegated to firmware (`_handle_heater_safety_locked`, three consecutive reads), so a
     blocked loop multiplies its latency. That is why the old global `OD_SETTLE_SECONDS`
     knob was removed, and why the base tick must never be slowed instead.
   - **The pause lives inside the supervisor's composed stir write.** That write
     re-asserts every vial every tick (`supervisor._resend_stir_locked`), so a stir change
     made anywhere else is undone a tick later. The schedule (`server/od_stir_pause.py`) is
     synced from the engines at *both* ends of a tick — at `classify_od_read`, so the first
     read after a start or resume is already judged, and at the stir write. It runs on a
     monotonic clock; the sensor loop stamps OD acquisitions and pump-schedule ends
     (`note_dilutions`) on the same one.
   - **Acceptance is per vial, so groups and parallel experiments never merge anything.**
     Restore happens in the stir write of the accepting tick, before that tick's pump
     schedule fires, so pumps always run into a stirred vial; `stir_settling` holds every
     mode's `decide()`, chemostat included.
   - **The blank is warn-only for paused vials** (it and the organism curve are measured
     stirring). If you build a paused blank, the envelope's `stir_condition` /
     `stir_paused_reads_by_vial` are there to detect the mismatch.

   If you raise `OD_EVERY_N_TICKS`: `growth_rate.py`'s fast-tier constants are tuned to a
   10 s cadence (a series at ≥ 30 s already switches to the slow tier); per-cycle counters
   are split per lane (`_temp_nan_streak`/`_od_nan_streak`, `VialHealth`); and anything
   plotting `sensor_update` must key OD off `msg.od.timestamp` / `od.vial_timestamps`,
   never `msg.timestamp`.
8. **The fluidics firmware queues frames, runs them one at a time, and cannot be stopped.**
   Measured 2026-09-23, full evidence in `FLUIDICS_FIRMWARE_AUDIT.md`. Each frame drives
   **every pump in its mask concurrently** (verified to 14 pumps at full speed) for
   **exactly** the commanded whole seconds; durations are **additive** across frames on the
   same pump; nothing preempts; the Arduino never transmits. Three consequences that bite
   immediately:

   - **Dilutions must fire as one concurrent schedule, never per-direction frames.**
     `SerialManager.pump_command(vial, direction, seconds)` sets one bit; firing influx and
     efflux through it issues two frames that *serialise*, so nothing drains during the
     influx phase and the level rises by the whole bolus — the vial-overflow mechanism.
     `app.py:_dispatch_dilutions` (fixed 2026-09-23) plans each cycle's dilutions with
     `fluidics.plan_dilution_frames` and sends them via `pump_frames`: influx and efflux
     start together, all vials in parallel, overrun as the tail, bus time = the longest
     vial. `pump_log.csv` still gets exactly two rows per dilution (totals, not frames).
   - **`stop_all_pumps()` is inert.** It does not halt a running pump and does not flush the
     queue; no frame shape tested stops anything. `emergency_stop`,
     `watchdog.emergency_shutdown` and `_zero_experiment_actuators_locked` all rely on it.
     The only real stop is cutting power to the aux pump board (`DEPLOY.md`). **Command
     duration is the exposure window**, which makes every duration cap a safety parameter.
   - **Any piecewise-constant pump schedule is expressible** as a sequence of frames, at a
     bus cost of the schedule's *span* rather than the sum of its durations (`SPEC.md`
     §16.3.3). This is what keeps 16-vial fluidics inside the control cycle.

9. **Do not inject a fluidics frame into an open request-response transaction.** The bus is
   half-duplex and shared. A pump frame sent between an `xr`/`we` request and its
   `temp…end` / `turb…end` reply corrupts both — the read NaNs out **and** the pump command
   is lost. This is the failure the eVOLVER community documents as "dropped fluidic
   commands" and a candidate cause of this rig's NaN storms. Any threaded fluidics executor
   must hold the `SerialManager` lock across the whole transaction, not just the write.

10. **Several experiments can run at once, multiplexed on one tick.** `app.py`'s
    `state.engine` is an `ExperimentSupervisor` (`server/supervisor.py`) holding one
    `ExperimentEngine` per loaded experiment, all on ONE shared lock and ONE sensor thread.
    Carry these invariants into any new work:

    - **A vial has at most one owner** (create → stop). Anything vial-addressed — manual
      control, pump booking, flow rates, growth — routes to the owner. A call that names
      no experiment means the only one loaded and raises `AmbiguousExperimentError`
      (409 `ambiguous_experiment`) when several are.
    - **Every dilution of a tick goes out as one pump schedule**, whichever experiments
      decided it; stir is written once per tick for all of them. Never give an engine its
      own write path to the bus per tick.
    - **Bottle and carboy levels are vessels (`server/vessels.py`), not experiment
      state**: a shared carboy blocks every experiment draining into it. Owned vessels are
      created at *create*, never reset at start.
    - **Engine alerts/events carry `experiment`** and namespaced dedup keys; `events.csv`
      routing is experiment → vial owner → everyone (SPEC §20.2).
    - **Machine-wide things stay machine-wide**: emergency stop and shutdown stop every
      experiment; the machine hold holds every experiment's pumps; pump calibration and
      raw actuator routes need the whole machine. The per-run OD blank does not.

11. **A Tier 3 calibration session reserves the whole machine, and installs go live.**
    `calibration_service.TempCalSession` / `OdSeriesSession` (Session AA) drive every heater
    and stir for hours, so: at most one exists; `start`, `apply` and `finish` need an idle
    machine (nothing loaded in any state, no pump session — `_cal_route(idle=True)`); while
    one is open, anything else that drives the machine answers 409
    `calibration_in_progress` (never the emergency stop). With no experiment loaded the
    engine's heater safety never runs, so the session carries its own watchdog in
    `app._calibration_tick` on the fast lane (46 °C, 3 reads, all 16 vials). E-stop and the
    watchdog *fault* it — its stir re-send would otherwise undo an E-stop — and a restart
    reloads it *suspended*. `finish` reloads the calibration live through
    `_reload_calibration`, which is why it needs the machine idle: loaded engines cache the
    temperature curve and their persisted raw setpoints, and the reload resets every run's
    assigned OD curve. An OD series `finish` installs a version of an **organism profile**,
    never the default slot (fact 12). Any new route that drives actuators must check
    `_tier3_in_progress()`.

12. **OD calibration is per organism, pinned per run, and composed per vial.** Cells
    scatter light by their size and shape, so the counts→OD curve belongs to an organism.
    An organism exists only once the OD dilution series has calibrated it; the designer
    offers those plus the labelled 2016 curve. Create pins `calibration.od` /
    `od_profile` / `od_organism` / `od_dark_subtracted` / `od_vials_measured` — server-owned,
    a caller's `od*` key is a 400 — and the run reads on that version for life, restarts
    included (a vanished version quarantines its vials, NaN curve plus a critical alert).
    One OD read serves all sixteen vials, so the manager composes the curve per vial
    (`assign_od_curve` / `release_od_curve`) and decides dark subtraction per vial;
    runs on different organisms share a tick. Three rules for new work:

    - **A run's curve and blank go on its vials before it can be ticked**:
      `app._experiment_od_setup` runs at create, before `start_experiment`, and through
      `supervisor.resume_on_startup(prepare=…)`; the `stopped` event releases them. Never
      call `load_calibration` / `_reload_calibration` with a run loaded — it resets every
      assigned curve.
    - **Never write `current["od"]` from a series.** The default slot is the idle-read curve
      and the fallback for unmeasured vials of a new organism, not an organism.
    - **Anything that turns logged counts into OD after the fact goes through
      `od_recalibrate.py`**: it rebuilds the signal in the *target* curve's space from the
      schema-2 columns and re-anchors at the run's own blank, so re-applying a run's own
      curve reproduces its OD. Output goes to `experiments/{name}/recalibrated/`, never over
      the run's files.

13. **A frame to a different Arduino needs a quiet bus first, and the stir firmware has no
    ceiling.** Measured 2026-10-11 by replaying the 10 s tick on the rig: an `xr` sent
    straight behind a `zv` went unanswered **85/100** times; with 100 ms of quiet first,
    **0/100**. Back-to-back `we` reads to the OD Arduino never failed. All four Arduinos hear
    all traffic, and one still digesting the last frame loses bytes of the next.
    `SerialManager._wait_inter_command` waits `ADDRESS_CHANGE_QUIET_SECONDS` (100 ms) before
    any frame whose prefix differs from the previous frame's, measured from the end of that
    frame or of its reply, and `_write` drains the UART (`flush()`) so every spacing counts
    from the end of transmission rather than from when the bytes were queued. Two
    consequences:

    - **A damaged write-only frame fails silently.** The stir firmware does not clamp: on the
      bench 88 and 888 spun visibly faster and louder than 8, and `STIR_MAX = 15` in software
      is the only ceiling. A `zv10,10,…` frame that loses a comma runs one vial at 1010 until
      the next good frame — the most likely cause of the loud, self-ending runaway stir seen
      in Oct 2026. The same loss in an `st` mask shifts every higher bit down one pump. The
      temperature timeouts in the Pi's `logs/evolver.log` (395 on 2026-10-11, in streaks on
      consecutive ticks) are this failure on the one frame that has a reply.
    - **The bus is nearly saturated.** The OD Arduino starts replying ~1.1 s after a `we`
      (~1.35 s per read), so the 3 dark + 5 lit reads fill ~9.4 s of the 10 s tick and the
      loop runs back to back. Anything added to the tick pushes it past 10 s. Never win
      that time back by removing the quiet gap.

14. **An operator's action on a running vial goes through the override, and is booked like
    the controller's own.** `/api/actuators/*` keeps refusing a driven vial; the only door
    is `/api/vials/{vial}/override*` (SPEC §21, `server/vial_override.py`). While a vial is
    unlocked its automatic control is **paused** (OD still pushed, `decide()` not called).
    An override pump is queued and **booked in `run_cycle` on the tick it fires** —
    interlock re-checked, `record_manual_dilution` pushed into the controller, a growth
    boundary spanning the real interval, media/waste moved — then rides that tick's one
    schedule as an `OverridePumpAction` (`fluidics.plan_pump_schedule`). Three rules for
    new work:

    - **A new control mode must implement `record_manual_dilution(now, influx_ml)` and
      `rebaseline(now)`** if it has any timing state, or a manual dilution will be followed
      by an automatic one. Both are optional (`getattr`), so forgetting is silent.
    - **Never resolve a vial's temperature or stir target from groups alone.** Pass
      `vial_targets` (`run_config.normalise_vial_targets(config["vial_targets"])`) — an
      operator's "keep for the run" lives there, and a resolution that skips it silently
      reverts it.
    - **`vialNN_pump_log.csv` is append-only too** (schema 2 added `source` at the end;
      `config.json` carries `pump_csv_schema`). Tag every new pump-log writer with its
      `source`.


## Technical constraints

- RPi runs Python 2.7 (Raspbian). The new server should use Python 3 if available, falling back to Python 2.7 if needed. Check with `python3 --version`.
- Serial port /dev/ttyAMA0 can only be opened by one process at a time. The new server must be the sole serial user — the old supervisor processes must be stopped first.
- RS485 is half-duplex: you send a command, then wait for a response. Commands end with ` !`; replies end exactly at `end` with **no newline** (measured 2026-10-11). Read to `end`, never `readline()`, which waits out the full 5 s timeout — as the 2016 loop did on every reply.
- Serial timeout is 5 seconds. If an Arduino doesn't respond, the system should log the failure and continue, not hang.
- The RS485 bus is shared — all 4 Arduinos hear all messages but only respond to their address prefix. Do not send commands faster than the Arduinos can process them: 50 ms between frames to the same Arduino and 100 ms of quiet bus before a frame to a different one, both measured from the end of transmission (fact 13).
- The 2016 Arduino firmware is not being changed. The new server must speak the exact same serial protocol.
- Calibration files must remain compatible with the existing format.
- The lab Mac Mini runs macOS 10.12.6 (Sierra) with Python 2.7, but it only needs to run a browser to access the new web GUI.
- **The dedicated Netgear N300 router (and the `192.168.1.x` addressing it handed out) is defunct.** Reach the rig at `pi@evolver-001` over Tailscale, not by LAN IP — see "Network identity" above.

## Stopping the old system before running the new one

```bash
# SSH into RPi
ssh pi@evolver-001

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

- Stir: send address `zv` + 16 comma-separated PWM values + ` !`. Values 0-15 typical; the firmware does **not** clamp (88 and 888 spin faster and louder — fact 13). 0=off (this one really is a raw PWM).
- Temperature: send address `xr` + 16 setpoint integers + ` !`. Practical operating range ≈ 400 (≈ 37 °C) to 700 (≈ 14 °C); 4095 = off; 0 = drive heater to ~82 °C (avoid). Read response with prefix `temp` (16 raw thermistor ADC values).
- OD: send address `we` + 16 LED power values + ` !`. 2125 is standard. Read response with prefix `turb` (16 raw photodiode ADC values).
- Fluidics: send address `st` + pump binary code + ` !`. Verify water flows. OR bits together to run several pumps at once (influx vial N = bit N, efflux vial N = bit N+16). See "Fluidics sub-protocol"; only the single-fire form works, and there is **no way to stop a running pump** — bench with lines looped into one water beaker so nothing can overflow or run dry.
- All 16 vials have been hardware-verified as functional (stir, temp, OD, fluidics all pass).

## Repository structure

```
eVOLVER_FileStruct/
  CLAUDE.md                  # This file — hardware facts and serial protocol
  SPEC.md                    # Technical specification (§15-§25 = planned subsystems)
  ROADMAP.md                 # Current prioritised work plan (Aug 2026 lab meeting)
  FLUIDICS_FIRMWARE_AUDIT.md # Measured fluidics firmware behaviour (bench, Sep 2026)
  SESSION_MASTER_PLAN.md     # Original Session A-J plan; see its status header
  DEPLOY.md                  # RPi operator runbook
  .gitattributes             # `export-ignore` rules: what `git archive`/deploy.sh leave
                             #   off the Pi (this file, every doc, tests, dev tooling,
                             #   rpi_original/mac_original) — see DEPLOY.md P3.3
  deploy.sh                  # builds/updates the Pi's runtime dir from a tag via
                             #   `git archive`; run from the source clone (`evolver-src`
                             #   on the Pi), never copied into the runtime dir itself

  rpi_original/              # Original RPi scripts (reference only, do not modify) —
    evolver_UPD.py           #   never imported by server/; export-ignored, dev-tree only
    UDP_TEMP.py  UDP_OD.py  UDP_FAN.py  UDP_FLUIDICS.py
    RS485_TEST.py
    extras/
  mac_original/              # Original Mac client scripts (reference only) — same:
    eVOLVER_module.py  main_eVOLVER.py  custom_script.py   #   export-ignored, dev-tree only

  calibration/               # Versioned calibration store (Session O / SPEC §19.1)
    current.json             # pointer: subsystem -> active version; "od" is the machine
                             #   default, "od_profiles" the named organisms (Session AG)
    od/          <ver>.json  # versioned envelopes (immutable; retained forever)
    temperature/ <ver>.json  #   2016 files imported as source "legacy-import-2016"
    pump/        <ver>.json  #   written by the gravimetric wizard (none until Tier 2 runs)
    OD_cal.txt               # DERIVED VIEW of current od version (4 x 16 sigmoid params)
    temp_calibration.txt     # DERIVED VIEW of current temperature version (2 x 16)
    OD_cal.meta.json         # {"dark_subtracted": bool} sidecar, regenerated with the view
    _sessions/pump.json      # in-progress resumable wizard session (gitignored)
    _sessions/temperature.json | od_series.json
                             # an open Tier 3 session (gitignored; reloaded SUSPENDED)
    reconciliation_log.json  # per-run mass-reconciliation trend (gitignored)
    # stir/ — planned, SPEC §25 / Session AC

  logs/                      # rotating server logs (gitignored)
    evolver.log  errors.log

  server/
    app.py                   # Flask routes, socketio, sensor loop, shutdown handler
    serial_manager.py        # RS485 (real hardware); owns /dev/ttyAMA0
    mock_serial_manager.py   # Simulated hardware — run app.py --mock
    experiment_engine.py     # Lifecycle, run_cycle, media tracking, maintenance, resume
    fluidics.py              # pump frame scheduler (audit §4.1-4.2) — pure, I/O-free
    vial_override.py         # supervised per-vial override (SPEC §21): engine mixin, pump plan,
                             #   events.csv reader for the plot markers
    run_config.py            # vial groups: normalise/validate, per-vial targets — pure (SPEC §9.1)
    od_stir_pause.py         # per-vial stir pause before OD reads: the schedule — pure (SPEC §9.1.1)
    supervisor.py            # parallel experiments: one engine per experiment, one tick (SPEC §9.2)
    vessels.py               # machine-scoped bottle/carboy levels, shareable across experiments
    growth_rate.py           # SPEC §17 growth estimation — pure, I/O-free, no engine imports
    replay_growth.py         #   replay a logged run through the estimator (read-only)
    verify_growth_rate.py    #   accuracy report + `--generate` for the 1x-time fixtures
    bench_growth_rate.py     #   RUN ON THE PI: growth-service cost vs the base tick budget
    verify_cadence_growth.py #   before/after accuracy of mu at the 10 s vs 60 s OD cadence
    bench_read_paths.py      #   RUN ON THE PI: get_data / export / staleness read costs
    data_logger.py           # Per-vial CSV writers
    data_export.py           # ZIP bundles, filtering
    event_log.py             # rotating logs, event ring, error classification (SPEC §20)
    calibration_service.py   # versioned store, organism OD profiles, blank/pump/Tier 3 sessions,
                             #   staleness (SPEC §19, §19.7)
    od_recalibrate.py        # post-hoc OD recalibration of a finished run (CLI + library, §19.7)
    od_curve_fit.py          # four-parameter logistic fit for the OD dilution series — pure, numpy only
    watchdog.py              # Heartbeat -> emergency shutdown
    control_modes/
      turbidostat.py  chemostat.py  morbidostat.py
    test_*.py                # pytest suite, all runnable against the mock
    test_control_loop.py     #   closed-loop: drives the real controllers against a
                             #   simulated culture and checks the OD band / delivered D
    test_vial_groups.py      #   vial groups: run_config, engine, API (SPEC §9.1)
    test_parallel_runs.py    #   parallel experiments: supervisor + API (SPEC §9.2)
    test_od_stir_pause.py    #   stir pause: schedule, group/parallel scope, engine gates, logging, API
    test_od_profiles.py      #   organism profiles, per-vial curves, OD CSV schema 2, wiring
    test_od_recalibrate.py   #   post-hoc recalibration: identity, true curve, schema 1, HTTP
    test_vial_override.py    #   override: grant/expiry, controller coherence, books, holds,
                             #   restart, parallel runs, and the HTTP path to the wire
    test_calibration_tier3*.py, test_od_curve_fit.py
                             #   Tier 3 wizards against a mock whose TRUE thermistor/OD
                             #   response differs from the installed curve (SPEC §19.5)
    conftest.py              #   stops test apps' sensor threads; isolates each test from
                             #   the repo's runtime calibration/_sessions
    verify_control_modes.py  #   same checks as a readable report (CONTROL_MODE_AUDIT.md)

  frontend/
    templates/index.html     # Entire GUI in one file (dashboard, wizard, plots)
    static/js/uPlot.iife.min.js

  experiments/{name}/        # Runtime output
    config.json  state.json
    vial00_OD.csv  vial00_temp.csv  vial00_pump_log.csv  ...
    vial00_growth.csv        # SPEC §17 growth estimates, 60 s cadence — a PARALLEL file;
                             #   vialNN_OD.csv only ever gains columns at its end (fact 6)
    events.csv               # unified event log (SPEC §20.2)
    od_blank.json            # per-run OD blank envelope (SPEC §19.2) — run-scoped
    od_calibration.json      # the organism profile version this run pinned (SPEC §19.7)
    recalibrated/<profile>__<version>/   # post-hoc OD recalibrations; originals untouched
    reconciliation.json      # post-run mass reconciliation record (SPEC §19.4)
  exports/                   # Server-side export bundles (outside experiments/ by design)
  machine/                   # Machine-scoped runtime state (gitignored): vessels.json
                             #   (shared bottle/carboy levels), hold.json (machine hold)
  deploy/                    # systemd unit, Tailscale keepalive
```

Run locally with no hardware: `python server/app.py --mock`
Run the tests: `python -m pytest` (from repo root — testpaths is set in pytest.ini)
