# eVOLVER Development Sessions — Master Plan

> **STATUS (Aug 2026) — read this before using the plan below.**
>
> This document was written before Phase 1 shipped. Sessions **A, B, C, F, and G are
> built and in `main`**. Sessions **D, E, H, I, and J are not.** The session prompts
> below remain useful as design context, but they describe a codebase that no longer
> exists in the state they assume — several of them tell you to build things that are
> already there.
>
> **For current priorities, use `ROADMAP.md`**, which triages the August 2026 lab-meeting
> feature requests against the code as it actually is, and sequences the remaining work
> (Sessions K onward) by urgency, utility, and difficulty.
>
> | Session | Status | Notes |
> |---|---|---|
> | A — Setup Mode & Media Configuration | **Built** | Wizard steps 2–4; `_validate_and_normalize_media` |
> | B — Waste & Media Volume Tracking | **Built (partially)** | Tracking and alerts done; time-to-empty forecast **not** done → `ROADMAP.md` Session R |
> | C — Alternative Control Modes | **Built (partially)** | Chemostat and morbidostat done; growth-rate mode still disabled in the UI, blocked on `SPEC.md` §17 |
> | D — Contamination & Stall Detection | **Not built** | Rescoped: rule-based tier → `ROADMAP.md` Session V; statistical tier deferred, see `ROADMAP.md` §6 |
> | E — Visual Experiment Designer | **Not built** | → `ROADMAP.md` Session Z (P2). The linear-phase-list recommendation below still stands |
> | F — Media Replacement Mode | **Built** | `enter_maintenance` / `exit_maintenance` / `refill_media`, 30 min failsafe |
> | G — Data Management & Export | **Built** | ZIP export, plots, disk monitoring; unified event log shipped 2026-08-20 (`ROADMAP.md` Session M, `server/event_log.py`) |
> | H — PID Temperature Control | **Not built** | **Premise below is factually wrong — see the correction in that section.** Deferred to P3 |
> | I — Documentation & Troubleshooting | **Not built** | Still worth doing; schedule after the P0 block in `ROADMAP.md` |
> | J — WiFi Independence & Security | **Not built** | → `ROADMAP.md` Session AE (P2). Tailscale is deployed (`deploy/ts-keepalive/`), which covers part of the access need |

## Dependency map

```
Session A: Setup Mode & Media Awareness
    |
    v
Session B: Waste & Media Tracking ----------> Session F: Replacement Mode
    |
    v
Session C: Alternative Control Modes -------> Session H: PID Control
    |
    v
Session D: Contamination & Stall Detection
    |
    v
Session E: Visual Experiment Designer (Block Charts)
    |
    v
Session G: Data Management & Export
    |
    v
Session I: Documentation & Troubleshooting
    |
    v
Session J: WiFi Independence & Security
```

Sessions A-D are the functional core. Session E is the big UI effort.
Sessions F-J are polish and hardening. Each session below includes
depth guidance (light/medium/deep), estimated time, and exact prompts.

---

## Session A: Setup Mode & Media Configuration

**Depth: DEEP (90 min, use Plan Mode carefully)**
**Why deep:** This restructures how experiments are initialized and
touches the data model, the API, the frontend, and the serial layer.
Everything downstream depends on getting this right.

### What it builds

A setup wizard that runs before every experiment, capturing:
- Which vials are active
- Which media bottle connects to which vials (input line routing)
- Starting volume of each media bottle
- Starting volume of waste container
- Media type labels (e.g., "LB", "LB + 2ug/mL Amp")

This information becomes part of the experiment config.json and is
displayed on the dashboard throughout the experiment.

### Data model addition

```json
{
  "media_sources": [
    {
      "id": "media_A",
      "label": "LB",
      "initial_volume_ml": 1000,
      "connected_vials": [0, 1, 2, 3, 4, 5, 6, 7]
    },
    {
      "id": "media_B",
      "label": "LB + 2ug/mL Amp",
      "initial_volume_ml": 500,
      "connected_vials": [8, 9, 10, 11, 12, 13, 14, 15]
    }
  ],
  "waste": {
    "container_volume_ml": 5000,
    "initial_level_ml": 0
  },
  "vial_media_map": {
    "0": "media_A",
    "1": "media_A",
    "8": "media_B"
  }
}
```

### Plan Mode prompts

**Explore:**
```
Read @SPEC.md sections 8 and 6. I need to add a setup mode that
captures media configuration before an experiment starts.

The eVOLVER has multiple media input bottles. Different vials can
be connected to different bottles via separate tubing. The setup
mode must capture: which media bottles exist, what they contain,
their starting volumes, which vials connect to which bottles, and
the waste container capacity.

Before planning, ask me clarifying questions about:
- How media routing works physically (tubing connections)
- Whether media can be switched mid-experiment
- How this interacts with the pump command format
```

**Answers to likely questions:**

Q: How does the physical media routing work?
A: Each vial has one influx tube. That tube runs to one media bottle.
   To change media, you physically move the tube. The software just
   needs to know which bottle is currently connected to which vial.

Q: Can media assignments change mid-experiment?
A: Not automatically. The user would need to pause, physically move
   tubes, then update the software. This is the "replacement mode"
   we'll build later.

Q: Does the pump command format change based on media type?
A: No. The pump hardware doesn't know about media types. The software
   just tracks which media is being consumed based on which vial's
   pump fires and which media bottle that vial is connected to.

**Plan:**
```
Plan the setup mode implementation. It should be a step-by-step
wizard in the experiment setup UI that comes BEFORE the existing
parameter configuration:

Step 1: Define media sources (add/remove bottles, name them,
        set initial volume)
Step 2: Assign vials to media sources (click vial, click media source)
Step 3: Set waste container capacity
Step 4: Proceed to existing experiment parameter setup

The media configuration must be saved in config.json and used by
the experiment engine to track consumption.

Plan the API changes, data model changes, and frontend changes.
Do not write code yet.
```

**Implement:**
```
Implement the setup mode. Start with the data model and API changes
to server/experiment_engine.py, then the new API endpoints, then
the frontend wizard steps.
```

### Verification
- [ ] Can define multiple media sources with labels and volumes
- [ ] Can assign vials to different media sources
- [ ] Config.json includes complete media configuration
- [ ] Dashboard shows which media each vial is using

---

## Session B: Waste & Media Volume Tracking

**Depth: MEDIUM (45 min)**
**Why medium:** The data model exists from Session A. This session
adds tracking logic to the experiment engine and dashboard display.

### What it builds

Real-time tracking of media consumed and waste produced per bottle,
based on pump events. Dashboard warnings when media is running low
or waste is nearly full.

### Prompts

**Plan Mode:**
```
Read the media configuration data model from Session A. I need the
experiment engine to track media and waste volumes in real time.

Every time a pump fires:
- Calculate volume pumped = duration_seconds * flow_rate[vial]
- Subtract from the media bottle connected to that vial
- Add to the waste container total

The dashboard should show:
- Each media bottle with a fill level indicator (green/yellow/red)
- Waste container fill level
- Estimated time until each bottle runs out (based on current
  consumption rate over last hour)

Alerts should fire when:
- Any media bottle drops below 15% remaining
- Waste container exceeds 85% capacity

Plan the changes to experiment_engine.py, the WebSocket data format,
and the dashboard UI. Do not write code yet.
```

**Implement:**
```
Implement the media and waste tracking. Add volume tracking to the
experiment engine run_cycle, include volumes in the WebSocket
sensor_update event, and add fill level indicators to the dashboard.
Include the alert thresholds.
```

### Verification
- [ ] Pump events decrease media volume and increase waste volume
- [ ] Dashboard shows fill levels for all bottles
- [ ] Alerts fire at correct thresholds in mock mode
- [ ] Estimated time-to-empty is displayed and reasonable

---

## Session C: Alternative Control Modes

**Depth: MEDIUM (60 min)**
**Why medium:** The control mode interface exists from the turbidostat.
Each new mode is a self-contained module following the same pattern.

### What it builds

Three additional control modes alongside turbidostat:
- Chemostat (constant dilution rate)
- Morbidostat (adaptive drug escalation)
- Growth rate feedback control (PD controller targeting mu)

### Prompts

**Plan Mode:**
```
Read @SPEC.md section 9 for the chemostat, morbidostat, and growth
rate control algorithms. Also read
@server/control_modes/turbidostat.py to understand the existing
interface.

Each new control mode must follow the same pattern:
- A class with a decide() method
- Takes current sensor data, history, config, per-vial state
- Returns a list of pump actions and updated state
- No I/O, no side effects, fully testable

Plan all three control modes. For each one, specify:
- The decide() algorithm as pseudocode
- What config parameters it needs
- What per-vial state it tracks
- Edge cases to handle

Also plan how the experiment setup UI offers mode selection and
shows mode-specific parameter controls.
```

**Implement (one at a time):**
```
Implement server/control_modes/chemostat.py following the plan.
Write a unit test that verifies pumps fire at the correct interval
for a given dilution rate. Run the test.
```

Then:
```
Implement server/control_modes/morbidostat.py. This needs growth
rate estimation from OD history to detect adaptation events. Write
a test that simulates growth rate recovery and verifies drug
escalation triggers.
```

Then:
```
Implement server/control_modes/growth_rate.py. This uses a
proportional controller to adjust dilution rate based on the error
between target and estimated growth rate. Write a test that
verifies the controller converges toward the target growth rate
in simulation.
```

Finally:
```
Update the experiment setup UI to offer all four control modes.
When a mode is selected, show only the relevant parameter controls.
Update the experiment create API to accept mode-specific configs.
```

### Verification
- [ ] Each mode runs independently in mock mode
- [ ] Mode selection in UI shows correct parameters
- [ ] Chemostat pumps at constant intervals regardless of OD
- [ ] Morbidostat escalates drug when growth rate recovers
- [ ] Growth rate controller converges in simulation

---

## Session D: Contamination & Stall Detection

**Depth: DEEP (90 min, novel algorithm design)**
**Why deep:** This is genuinely new functionality that requires
careful algorithm design, tuning, and testing against false
positives and false negatives.

### What it builds

An anomaly detection system that monitors OD growth patterns and
flags two conditions:
1. **Contamination**: sudden, abnormal increase in growth rate
   ("too fast, too suddenly")
2. **Stall**: growth rate plateau or decline suggesting dead culture
   or exhausted selection

### Algorithm design considerations

This is the session where you should bring questions HERE (to this
Claude chat) before implementing in Claude Code. Key questions:

- What growth rate qualifies as "too fast"? Need species-specific
  baselines. E. coli max growth rate ~2 doublings/hr in rich media.
  A sudden jump from 0.5/hr to 2.5/hr is suspicious.
- How to distinguish contamination from adaptation? Adaptation is a
  gradual increase. Contamination is a step change.
- Window size matters: too short = noisy, too long = slow detection.
  Start with 30-minute windows for growth rate estimation.
- False positive cost: stopping a good experiment is very bad.
  Alerts should warn, not auto-stop. Leave the decision to the user.

### Prompts

**Plan Mode (ask for algorithm design):**
```
I need a contamination and stall detection system for the eVOLVER.
It monitors OD data in real time and flags anomalies.

Design the detection algorithm. Consider:

1. Growth rate estimation: fit exponential to OD over a sliding
   window. What window size? How to handle pump dilution events
   (OD drops are not growth, they are interventions)?

2. Contamination detection: define "too fast too suddenly." This
   means a step change in growth rate, not a gradual increase.
   How to detect step changes vs gradual trends? Consider using
   the derivative of growth rate (jerk), or comparing short-window
   vs long-window growth rate estimates.

3. Stall detection: growth rate drops below a threshold for an
   extended period. What threshold? How long before alerting?

4. Alert levels: info (minor anomaly), warning (likely problem),
   critical (definite problem). What thresholds for each?

5. How to handle the startup phase where OD is low and noisy?
   Detection should probably only activate after OD exceeds
   a minimum threshold (e.g., 0.1).

Before planning implementation, propose the detection algorithm
with specific threshold values and ask me to review them.
```

**Key design decisions you'll need to make:**

- Window sizes: 30 min for short window, 2 hours for long window
- Contamination trigger: short_window_growth_rate > 2 * long_window_growth_rate
  AND short_window_growth_rate > species_max_growth_rate
- Stall trigger: growth_rate < 0.05/hr for > 2 hours
- Minimum OD for detection: 0.1
- Alerts are warnings only, never auto-stop

**Implement:**
```
Implement server/anomaly_detector.py with the agreed-upon algorithm.
It should:
- Accept OD history and pump event history for a vial
- Return an alert level (none, info, warning, critical) with a message
- Exclude OD drops caused by pump events from growth rate calculation
- Be configurable (window sizes, thresholds) via experiment config
- Have no I/O dependencies

Write comprehensive tests:
1. Normal growth: no alerts
2. Simulated contamination (sudden growth rate doubling): warning
3. Simulated stall (growth rate drops to zero): warning after 2 hours
4. Pump event doesn't trigger false positive
5. Low OD noise doesn't trigger false positive
6. Gradual adaptation doesn't trigger contamination alert
```

**Frontend:**
```
Add anomaly alerts to the dashboard:
- Vial cards show a warning icon when an alert is active
- Clicking the warning shows the alert message and detection details
- Alert history is visible in the experiment status page
- Alerts are also emitted as WebSocket events for potential Slack
  integration later
```

### Verification
- [ ] Normal growth produces no alerts
- [ ] Simulated contamination triggers warning within 30 minutes
- [ ] Simulated stall triggers warning after 2 hours
- [ ] Pump events don't cause false positives
- [ ] Low OD phase doesn't cause false positives
- [ ] Alerts visible on dashboard with correct severity

---

## Session E: Visual Experiment Designer (Block Charts)

**Depth: DEEP (2-3 hours, likely needs 2 sub-sessions)**
**Why deep:** This is the most complex frontend feature. It requires
a drag-and-drop block-based interface with conditional logic.

### What it builds

A visual experiment designer where users build experiment protocols
by connecting blocks in a flow chart:

```
[Turbidostat] --> [After 12 hours] --> [Max OD Test] --> [After 2 hrs] --> [Turbidostat]
     |                                                                          |
     |---- OD thresh: 0.2-0.4                                    OD thresh: 0.3-0.6
     |---- Temp: 37C                                              Temp: 37C
```

### Architecture decisions (discuss HERE first)

Before starting this in Claude Code, we should decide:

1. **Block types:**
   - Control mode blocks: Turbidostat, Chemostat, Morbidostat, Growth Rate, Custom
   - Transition blocks: Time elapsed, OD reached, Growth rate reached, Manual trigger
   - Action blocks: Change temperature, Change media, Sample reminder
   - Each block has editable parameters

2. **Tech choice:** This is where React becomes justified. A drag-and-drop
   flow chart with connected blocks is painful in vanilla JS. Consider:
   - React Flow (react-flow.dev) for the flow chart
   - Or keep it simpler: a linear phase list (not a graph) where each
     phase has a mode, parameters, and a transition condition to the
     next phase. This covers 90% of use cases without the complexity
     of a full graph editor.

3. **Recommendation:** Start with the linear phase list. Build the
   full graph editor as a future upgrade only if researchers actually
   need branching logic (most won't).

### Sub-session E1: Phase-based protocol engine (45 min)

**Prompts:**
```
Read @SPEC.md section 9 and @server/experiment_engine.py.

I need to add multi-phase experiment support. An experiment is a
sequence of phases. Each phase has:
- A control mode (turbidostat, chemostat, etc.)
- Mode-specific parameters
- A transition condition to the next phase

Transition conditions can be:
- Time elapsed (e.g., "after 12 hours")
- OD reached (e.g., "when average OD > 0.8")
- Growth rate reached (e.g., "when mu > 0.5/hr")
- Manual trigger (user clicks "advance to next phase")

The experiment engine should:
- Track which phase is currently active
- Evaluate transition conditions each cycle
- When a transition fires, switch to the next phase's control mode
  and parameters
- Log phase transitions as experiment events
- Support looping (last phase can transition back to an earlier phase)

Plan the changes to experiment_engine.py and the config.json format.
```

**Config format for phases:**
```json
{
  "phases": [
    {
      "name": "Initial Growth",
      "mode": "turbidostat",
      "params": {"od_lower": 0.2, "od_upper": 0.4, "temp": 37},
      "transition": {"type": "time_elapsed", "hours": 12}
    },
    {
      "name": "Max OD Measurement",
      "mode": "turbidostat",
      "params": {"od_lower": 0.8, "od_upper": 99, "temp": 37},
      "transition": {"type": "od_reached", "target_od": 1.0}
    },
    {
      "name": "Selection Phase",
      "mode": "morbidostat",
      "params": {"target_od": 0.4, "drug_start": 1.0, "drug_step": 2},
      "transition": {"type": "time_elapsed", "hours": 48}
    }
  ]
}
```

### Sub-session E2: Visual phase builder UI (60-90 min)

**This is where to consider React.** If the rest of the frontend is
still vanilla JS, you can build the phase editor as an isolated
React component embedded in the page, or keep it as a well-designed
vanilla JS interface with add/remove/reorder functionality.

**Prompts:**
```
Build a visual phase editor for the experiment setup page.

The editor shows a vertical timeline of experiment phases. Each phase
is a card showing:
- Phase name (editable)
- Control mode (dropdown)
- Mode-specific parameters (sliders/inputs)
- Transition condition (dropdown + parameter)
- Up/down buttons to reorder
- Delete button

An "Add Phase" button appends a new phase to the timeline.

Between each phase card, show the transition condition as a connector
label (e.g., "After 12 hours" or "When OD > 0.8").

The editor generates the phases array in the experiment config JSON
and sends it with the create experiment API call.

Keep it simple and usable. Vanilla JS is fine if you can make the
drag-and-reorder work cleanly, otherwise use a small React component.
```

### Verification
- [ ] Can create multi-phase experiments
- [ ] Phases execute in sequence during mock simulation
- [ ] Transition conditions trigger correctly (time, OD, growth rate)
- [ ] Phase transitions are logged
- [ ] UI shows current phase and progress toward transition
- [ ] Can reorder and delete phases in the editor

---

## Session F: Media Replacement Mode

**Depth: LIGHT (30 min)**
**Why light:** Small, focused feature building on Sessions A and B.

### What it builds

A "Maintenance Mode" button that:
1. Pauses pumping (inhibits all pump commands)
2. Shows clear instructions: "Replace media/waste now"
3. User enters new volumes after replacement
4. Includes a failsafe: auto-exits maintenance mode after 30 minutes
   with a warning, in case the user forgets

### Prompts

```
Add a Maintenance Mode to the dashboard. When activated:
1. All pump commands are suppressed (experiment keeps reading
   sensors and logging, but decide() pump actions are queued,
   not executed)
2. A prominent banner shows "MAINTENANCE MODE - Pumps disabled"
3. A form allows updating media bottle volumes and waste level
4. A "Resume" button re-enables pumping and processes any queued actions
5. Auto-resume after 30 minutes with a warning alert

Add a POST /api/maintenance/enter and POST /api/maintenance/exit
endpoint. The experiment engine checks a maintenance flag before
executing pump commands.

Include a failsafe: if maintenance mode has been active for 30
minutes, emit a critical alert and auto-resume to prevent the
experiment from stalling indefinitely.
```

### Verification
- [ ] Pumps stop firing in maintenance mode
- [ ] Sensor reading and logging continues
- [ ] Volume update form works
- [ ] Auto-resume fires after 30 minutes
- [ ] Queued pump actions execute on resume

---

## Session G: Data Management & Export

**Depth: MEDIUM (45 min)**
**Why medium:** The CSV structure exists. This adds export, cleanup,
and visualization improvements.

### What it builds

- Download experiment data as a ZIP of all CSVs
- Interactive OD/temp/growth rate plots on the experiment page
- Event log (pump events, phase transitions, alerts, maintenance)
- Disk usage awareness (RPi has limited storage)
- Data export reminder when experiment ends

### Prompts

```
Add data management features:

1. GET /api/experiments/{name}/export — returns a ZIP file containing
   all CSVs, config.json, and a summary.txt with experiment metadata

2. Historical plots on the experiment detail page:
   - OD vs time for all experiment vials (overlaid, with legend)
   - Temperature vs time
   - Growth rate vs time (estimated from OD)
   - Pump events marked as vertical lines on the OD plot
   - Phase transitions marked as shaded regions
   Use Chart.js or Plotly.js. Must handle thousands of data points
   efficiently (downsample for display if needed).

3. An event log table showing all experiment events (pumps, phase
   changes, alerts, maintenance) with timestamps

4. Disk usage indicator in the server status bar showing RPi free
   space. Warning when below 500MB.

5. When an experiment is stopped, prompt: "Download experiment data?"
   with a one-click ZIP download button.
```

### Verification
- [ ] ZIP download contains all CSVs and config
- [ ] Plots render correctly with mock data
- [ ] Pump events visible as markers on OD plot
- [ ] Disk usage displayed and warning fires at threshold
- [ ] Download prompt appears on experiment stop

---

## Session H: Cascade Temperature Control — DEFERRED (P3)

> ### ⚠ CORRECTION — the original premise of this session was wrong
>
> This session was written on the assumption that "temperature PWM is calculated from
> calibration: `pwm = (target_temp - intercept) / slope`" and that there is "no feedback
> loop." **Both statements are false**, and they repeat exactly the misconception that
> `CLAUDE.md` and `SPEC.md` §10 warn about.
>
> The `xr` value is **not a PWM duty cycle.** It is a *setpoint* that the temperature
> Arduino's closed loop drives the thermistor ADC reading toward. The feedback loop
> already exists — it runs on the Arduino, in firmware from 2016 that is not being
> changed. The calibration slope is **negative**, so a lower `xr` means a *hotter*
> target, and `xr=0` requests roughly 82 °C rather than "off."
>
> Any implementation following the original prompt below would have written a controller
> that clamps "PWM" to 0–600 and treats 0 as safe. On this hardware that is a command to
> drive the heater to maximum. **Do not implement the prompt as originally written.**

### The correct framing, if this is ever built

What is actually available is **cascade control**: an outer loop on the Pi that trims the
`xr` setpoint based on the error between the calibrated temperature reading in °C and the
target in °C, wrapped around the inner loop the Arduino already closes on raw ADC.

- Outer-loop output is a **setpoint adjustment in °C**, converted to `xr` by
  `set_temperature_celsius`. It never touches raw values directly.
- Safety clamps stay in Celsius (`MAX_SAFE_TEMP_C`), where they are legible; the existing
  per-vial raw floor in `set_temperature_raw` remains the backstop.
- The outer loop must be much slower than the inner loop — the sensor cycle is 10 s and
  the thermal time constant of a sleeve is minutes, so an outer loop updating faster than
  roughly once a minute will fight the Arduino rather than help it.

### Why it is deferred to P3

Two prerequisites, plus a live question about whether it is worth doing at all:

1. **`SPEC.md` §14 open question 4** — whether the heaters are electrically healthy — is
   unresolved. Note that the inverted-setpoint discovery may itself explain the
   stuck-on symptom: any earlier code or operator that sent `xr=0` "to turn the heaters
   off" was in fact requesting ~82 °C. Verify the heaters turn off on `xr=4095` before
   concluding hardware is at fault, and certainly before adding a controller on top.
2. **Temperature calibration is unverified** (`ROADMAP.md` Session AA). A cascade loop
   tuned against a wrong calibration will converge confidently on the wrong temperature —
   worse than the current arrangement, because it will look well-controlled.
3. **It may not be needed.** ±1 °C is adequate for essentially every experiment the lab
   currently runs, and the existing arrangement plus the §10 safety clamps already
   achieves that. Revisit only if a specific experiment (thermotolerance evolution, say)
   demands tighter control, and quantify how much tighter before starting.

### Verification, if built
- [ ] Outer loop expressed entirely in °C; raw setpoints only ever produced by `set_temperature_celsius`
- [ ] Outer-loop update period ≥ 60 s; no fighting between the two loops in simulation
- [ ] Never requests a target above `MAX_SAFE_TEMP_C`; `xr=0` is unreachable by construction
- [ ] Converges without steady-state oscillation in mock simulation
- [ ] Direct mode still works unchanged and remains the default
- [ ] Gains configurable per experiment

---

## Session I: Documentation & Troubleshooting Manual

**Depth: MEDIUM (45 min)**
**Why medium:** This is a writing task, not a coding task. Use Claude
Code to generate documentation from the codebase, but review carefully.

### What it builds

- User manual (how to run experiments, end to end)
- Troubleshooting guide (common problems and fixes)
- Hardware reference (vial numbering, wiring, board layout)
- Developer guide (how to add new control modes, API reference)

### Prompts

```
Generate documentation for the eVOLVER system. Read all source files
and create:

1. docs/USER_MANUAL.md — Step-by-step guide for running an experiment:
   - Starting the server
   - Opening the dashboard
   - Setup mode (media, vials, waste)
   - Creating an experiment
   - Monitoring a running experiment
   - Maintenance mode (replacing media/waste)
   - Stopping an experiment
   - Downloading data
   Write for a biology researcher with no programming experience.

2. docs/TROUBLESHOOTING.md — Common problems and solutions:
   - "UDP Timeout" errors (network issues)
   - Heaters won't turn off (stuck MOSFET, emergency shutdown)
   - OD readings are zero or saturated (LED/photodiode alignment)
   - Pumps not firing (check fluid_config, tubing, pump calibration)
   - Server won't start (port in use, serial port busy)
   - Dashboard not loading (check IP, check server running)
   - Experiment data missing (check disk space, check CSV paths)
   Include the diagnostic scripts we wrote during the original
   troubleshooting (udp_port_test.py, hardware_verify.py).

3. docs/DEVELOPER_GUIDE.md — How to extend the system:
   - Adding a new control mode (create class, register in engine)
   - Adding a new sensor type (modify SerialManager, add endpoint)
   - Modifying the dashboard (file structure, WebSocket events)
   - API reference (all endpoints with examples)
   - Testing against mock vs hardware
```

### Verification
- [ ] User manual is understandable by a non-programmer
- [ ] Troubleshooting covers every failure mode we encountered
- [ ] Developer guide is accurate to the actual code structure

---

## Session J: WiFi Independence & Security

**Depth: MEDIUM (45 min)**
**Why medium:** The web server architecture already removes Mac
dependency. This session adds WiFi access and basic security.

### What it builds

- RPi connects to Yale WiFi (or lab WiFi) directly
- Dashboard accessible from any device on the same network
- Basic authentication (username/password) to prevent unauthorized access
- HTTPS (self-signed cert is fine for internal use)

### Prompts

```
The eVOLVER RPi currently connects via a dedicated Netgear router.
I want it accessible on the lab's WiFi network so any lab member
can monitor experiments from their laptop.

Plan and implement:

1. WiFi configuration for the RPi:
   - Add WiFi credentials to wpa_supplicant.conf
   - Keep the Ethernet connection as a fallback
   - Configure a static IP or use mDNS (evolver.local)

2. Basic authentication for the web interface:
   - Username/password login page
   - Flask-Login or simple session-based auth
   - Default credentials that must be changed on first use
   - API endpoints require authentication header
   - Emergency stop endpoint does NOT require auth (safety first)

3. Optional HTTPS:
   - Self-signed certificate generation script
   - Flask serves over HTTPS
   - Browser will show a warning (acceptable for internal use)

4. Conflict resolution:
   - If two users try to control the same vial simultaneously,
     the server should reject the second command with a clear error
   - Only one user can have "control" of the experiment setup
   - Everyone can monitor (read-only) simultaneously
```

### Verification
- [ ] RPi accessible via WiFi from a laptop
- [ ] Login page prevents unauthorized access
- [ ] Emergency stop works without login
- [ ] Concurrent monitoring works (two browsers)
- [ ] Conflicting commands are rejected cleanly

---

## Session priority and timeline — SUPERSEDED

The original ordering below is kept for the record. **It no longer reflects priorities.**
Sessions A, B, C, F, and G are built; the August 2026 lab meeting added 20 further
requests that reshuffle everything that remains.

**→ Use the sequence in `ROADMAP.md` §7 instead.**

| Priority | Session | Depth | Est. Time | Dependency | Status |
|----------|---------|-------|-----------|------------|--------|
| 1        | A: Setup Mode | Deep | 90 min | MVP complete | **Built** |
| 2        | B: Media/Waste Tracking | Medium | 45 min | Session A | **Built** (forecast → ROADMAP R) |
| 3        | C: Alt Control Modes | Medium | 60 min | MVP complete | **Built** (growth-rate mode → SPEC §17) |
| 4        | D: Contamination Detection | Deep | 90 min | Session C | Not built → ROADMAP V / §6 |
| 5        | E: Visual Designer | Deep | 2-3 hrs | Sessions A, C | Not built → ROADMAP Z (P2) |
| 6        | F: Replacement Mode | Light | 30 min | Session B | **Built** |
| 7        | G: Data Management | Medium | 45 min | MVP complete | **Built** (event log → ROADMAP M) |
| 8        | H: PID Control | Light | 30 min | Session C | Not built, **premise corrected** → P3 |
| 9        | I: Documentation | Medium | 45 min | All above | Not built |
| 10       | J: WiFi & Security | Medium | 45 min | MVP complete | Not built → ROADMAP AE (P2) |

### What changed and why

The three highest-priority items now are **not** on the original list, because they only
became visible once the system was real enough to run experiments on:

1. **Consumables interlock** (`ROADMAP.md` Session K) — the system tracks bottle levels
   and warns, but does not stop pumping. That gap loses overnight runs.
2. **Pump flow calibration** (Session O) — every volume the system reports is derived
   from a hardcoded default array. Media levels, the interlock, volume-based pumping, and
   one of the two growth-rate estimators are all downstream of it.
3. **Growth-rate service** (Session N) — currently private to the morbidostat controller,
   yet four other planned features need it.

**Sessions to discuss before implementing** (the original list, updated):

- `ROADMAP.md` Session N — which growth-rate estimator is authoritative, and what error
  the lab considers acceptable
- `ROADMAP.md` Session O — the gravimetric protocol, and who does the bench work
- `ROADMAP.md` Session Y — whether vial groups genuinely cover the "parallel experiments"
  need before committing to the larger Session AB refactor
- Session E above — the block-chart vs linear-phase-list decision (recommendation in that
  section still stands: build the linear list)
- Session H above — **read the correction first**; the question is no longer "is PID worth
  it" but "is cascade control worth it, given the Arduino already closes the loop"
