# ROADMAP.md — Post-Lab-Meeting Development Plan

**Source:** Lab meeting on prototype finalisation (Aug 2026). This document triages the
20 feature requests from that meeting against the code as it actually exists, and
sequences them into implementable sessions.

**Relationship to the other docs:**

- `CLAUDE.md` — hardware facts and serial protocol. Ground truth about the machine.
- `SPEC.md` — technical specification of the software. Ground truth about *what to build*.
- `SESSION_MASTER_PLAN.md` — the original Session A–J plan. Sessions A, B, C, F, G are
  built; D, E, H, I, J are not. Superseded for prioritisation purposes by this file.
- `ROADMAP.md` (this file) — priority, sequencing, effort, and deferral decisions.
- `DEPLOY.md` — operator runbook for getting code onto the RPi.

---

## 1. Where the code actually is (verified Aug 2026)

Reading the tree rather than the plan, the following is **built and in `main`**:

| Subsystem | Status | Where |
|---|---|---|
| Flask + socketio server, 10 s sensor loop | Built | `server/app.py` |
| SerialManager + MockSerialManager + Watchdog | Built | `server/serial_manager.py`, `mock_serial_manager.py`, `watchdog.py` |
| Dashboard, 4×4 vial grid, per-vial modal with OD/temp plots | Built | `frontend/templates/index.html` (single file, ~160 kB) |
| Manual controls: temperature (°C), stir, pump, emergency stop | Built | `app.py` `/api/actuators/*` |
| 7-step experiment wizard (Name → Media → Vials → Waste → Mode → Params → Review) | Built | `index.html` `#exp-modal` |
| Media bottles, vial→bottle map, waste tracking, low/high alerts, refill | Built | `experiment_engine.py` (`_validate_and_normalize_media`, `refill_media`) |
| Maintenance mode with 30 min auto-resume failsafe | Built | `experiment_engine.py` (`enter_maintenance` / `exit_maintenance`) |
| Control modes: turbidostat, chemostat, morbidostat | Built | `server/control_modes/` |
| Sub-second pump deficit accumulator | Built | all three control modes |
| Data export (ZIP), exports browser, disk-usage monitoring | Built | `server/data_export.py`, `/api/storage` |
| Crash recovery / resume from `state.json` | Built | `experiment_engine.py` (`resume_on_startup`) |
| 16-colour distinct plot palette | **Already built** | `index.html` `PLOT_COLORS` |
| Deployment: systemd unit, `install.sh`, Tailscale keepalive | Built | `DEPLOY.md`, `deploy/` |

**Not built** (relevant to this roadmap): calibration wizards of any kind (there are no
`/api/calibration/*` endpoints at all), growth-rate estimation outside the morbidostat
controller, anomaly/contamination detection, multi-phase protocols, experiment templates,
authentication, PID/cascade temperature control, notifications, off-box backup, and
parallel experiments (the engine explicitly holds one experiment in memory).

### Important correction carried into this roadmap

`SESSION_MASTER_PLAN.md` Session H states that temperature control works by
`pwm = (target_temp - intercept) / slope`. **This is wrong and repeats the exact
misconception `CLAUDE.md` warns about.** The `xr` value is a *closed-loop setpoint* that
the temperature Arduino drives the thermistor ADC reading toward — the Arduino already
runs the feedback loop, and the calibration slope is negative. Any "add PID" work is
therefore *cascade* control (an outer loop on the Pi trimming the setpoint of an inner
loop on the Arduino), not a replacement of a raw PWM. Session H is rewritten accordingly
and deprioritised to P3.

---

## 2. Triage of the 20 meeting items

Scoring: **Urgency** = does the prototype fail without it. **Utility** = value delivered
once shipped. **Difficulty** = engineering cost including the lab-bench work, not just
the code.

| # | Meeting item | Urgency | Utility | Difficulty | Priority | Session |
|---|---|---|---|---|---|---|
| 1 | Stop pumps when media empty / waste full | **Critical** | High | Low | **P0** | K |
| 2 | Volume, not duration, for manual pump control | High | High | Low | **P0** | L |
| 3 | Better logs | High | High | Low–Med | **P0** | M |
| 4 | Better and more intelligent error logging | High | High | Med | **P0** | M |
| 5 | Accurate per-vial growth rate calculator | High | **Critical** | Med–High | **P0** | N |
| 6 | Per-run calibration wizard | High | High | Med | **P0** | O |
| 7 | Calibration wizard (full) | High | High | High | P0/P2 | O / AA |
| 8 | Log last autoclave/sterilisation of fluidics | Med | High | **Low** | **P1** | P |
| 9 | Sterilisation wizard | Med | High | Med | **P1** | P |
| 10 | Experiment templates | Med | High | **Low** | **P1** | Q |
| 11 | Expected time to media bottle emptying | Med | High | Low–Med | **P1** | R |
| 12 | Individual vial control during an experiment | Med–High | High | Med | **P1** | S |
| 13 | Derived-statistic figures in per-vial view | Low–Med | High | Low | **P1** | T |
| 14 | Click-and-drag vial media assignment | Low | Med | **Low** | **P1** | U |
| 15 | Intelligent contamination detection | Low–Med | High | **High** | P1 / **P3** | V / deferred |
| 16 | Slack/email integration for alerts | Med | High | Low | **P1** | W |
| 17 | Backup experiment memory to OneDrive | Med | Med–High | Med–High | **P1** (rescoped) | X |
| 18 | Parallel experiment running | Med | High | **High** | P1 / **P2** | Y / AB |
| 19 | Distinct 16-colour palette | — | — | — | **Done** | AD (audit only) |
| 20 | Map stir rate to physical RPM + animation | Low | Med | Med (bench work) | **P2** | AC |

Items 3 and 4 are merged (Session M). Items 6 and 7 are split by scope (Session O covers
the per-run and pump-flow wizards; AA covers full thermistor and OD sigmoid
recalibration). Item 15 is split into a deterministic rule-based tier (P1) and a
statistical tier (P3). Item 18 is split into vial groups (P1) and true concurrency (P2).

### Deferral decisions and their reasoning

Three items are deliberately **not** planned as requested. Each is rescoped rather than
dropped, and the reasoning below is intended to be defensible at the next lab meeting.

**Item 15 — "Intelligent contamination detection" → P3, evidence-gated.**
The failure mode of a contamination detector is asymmetric and badly so: a false
positive that stops or casts doubt on a 5-day adaptation experiment costs more than the
detector saves. Tuning thresholds requires a corpus of real runs *including known
contaminated ones*, which the lab does not yet have. The plan is therefore to ship the
cheap deterministic tier now (Session V: OD stall, dilution-response failure, temperature
excursion, pump over-cycling — all rule-based, all explainable, all warn-only), and to
start recording the derived features (short/long-window growth rate, growth-rate jerk,
dilution-interval variance) into the per-experiment logs so that a corpus accumulates
passively. Revisit the statistical detector once ~10 runs with known outcomes exist.

**Item 17 — "Backup to OneDrive" → rescoped to generic off-box backup.**
The underlying requirement is *don't lose an experiment when the Pi's SD card dies*,
which is a real and fairly likely risk. Native OneDrive integration means Microsoft Graph
OAuth with token refresh, executed on a headless Pi against a Yale tenant that very
probably enforces conditional access and MFA — days of work with an institutional
dependency that can revoke it. `rclone` already solves this generically and supports
OneDrive as one of its backends, so Session X implements scheduled `rclone` sync of
`experiments/` and `exports/` to a configurable remote. If that remote is a OneDrive
remote, the meeting request is satisfied; if Yale's tenant blocks it, a lab NAS or a
Google Drive remote is a one-line config change instead of a rewrite.

**Item 18 — "Parallel experiment running" → vial groups first, true concurrency later.**
The engine is single-experiment by construction (one `state.json`, one `DataLogger`
activation, one controller set). True concurrency means per-experiment engine instances,
RS485 arbitration across them, vial-ownership conflict resolution, and N-way resume
logic — a large refactor with a real risk of introducing races into the code path that
drives heaters. However, ~80 % of the practical demand ("Yiannis runs a turbidostat on
vials 0–7 while Suren runs a chemostat on 8–15") is satisfied by **vial groups inside a
single experiment**: per-group mode, per-group parameters, per-group media. That is a
config-schema and controller-dispatch change, not a concurrency change. Session Y does
that; Session AB does true concurrency only if groups prove insufficient.

---

## 3. P0 — Prototype blockers

These gate the next unattended run and the handoff to other lab members.

### Session K — Consumables safety interlock

**Priority: P0 (highest in the list). Effort: LIGHT–MEDIUM (45 min). Depends on: nothing.**

The engine already computes `_bottle_consumed_ml` and `_waste_filled_ml` and fires low/high
alerts. It does **not** stop pumping. Today, a bottle that runs dry overnight results in the
influx pump pushing air into the culture while the efflux pump keeps removing broth — the
vial drains and the run is lost. A full waste carboy floods the bench.

**What it builds:** a hard gate in `run_cycle` evaluated *before* any pump dispatch:

- If the bottle feeding vial *v* has less than `reserve_ml` remaining → suppress that
  vial's influx, emit a `critical` alert, and mark the vial `consumables_blocked`.
- If waste is above `capacity_ml - reserve_ml` → suppress **all** efflux and influx (an
  influx without efflux overflows the vial), emit `critical`.
- When every active vial is blocked, auto-enter maintenance mode so the run pauses
  cleanly rather than spinning.
- Blocking is sticky: it clears only on an explicit `refill_media` call, never
  automatically, because the volume estimate cannot recover on its own.

**Design notes and honest caveats:**

- The tracked volume is *inferred* (`duration × flow_rate`), not measured. It drifts with
  pump wear, tubing compliance, and calibration error. `reserve_ml` must therefore be
  generous — default to `max(50 mL, 5 % of initial)` — and the UI must present the number
  as an estimate, not a measurement.
- **The interlock is only as trustworthy as the pump flow calibration**, which is
  currently a hardcoded default array. This is the strongest argument for doing Session O
  early. Note the dependency explicitly in the UI: if the experiment used default flow
  rates rather than measured ones, label the bottle levels "uncalibrated estimate".
- A float switch or a load cell under the bottles would make this a measurement instead
  of an estimate. Worth costing out — it is the single highest-value hardware addition on
  this list.

**Verification:**

- [ ] Mock run: bottle driven to reserve → influx suppressed for exactly the vials fed by it
- [ ] Waste driven to capacity → all pumping suppressed, critical alert raised
- [ ] All vials blocked → maintenance mode entered automatically
- [ ] `refill_media` clears the block; nothing else does
- [ ] Suppressed pump attempts appear in the event log with a reason

---

### Session L — Volume-based fluidics

**Priority: P0. Effort: LIGHT (30 min). Depends on: Session O for accuracy (ship anyway).**

Researchers think in millilitres; the UI asks for seconds. Every manual dilution today
requires mental arithmetic against a flow rate the user cannot see.

**What it builds:** manual pump controls accept **mL**, with seconds as an advanced
toggle. Conversion is `seconds = volume_ml / flow_rate_ml_s[vial]`, using the same
per-vial flow rates the engine already resolves.

**The gotcha that must be surfaced in the UI:** the 2016 firmware accepts whole seconds
only. With flow rates of 0.85–1.15 mL/s, the minimum deliverable bolus is roughly one
millilitre and the quantisation step is roughly one millilitre. So:

- Show the achievable volume next to the requested one ("requested 2.5 mL → will deliver
  2.0 mL (2 s)"), per vial, since flow rates differ.
- Requests below one second must be rejected with an explanation, not silently truncated
  to zero — silent truncation is exactly the legacy `%d` bug documented in SPEC §9.
- Offer efflux volume independently of influx (the common manual operation is "take 3 mL
  out for sampling", which has no influx counterpart).

**Verification:**

- [ ] 5 mL request on a vial with flow rate 1.0 mL/s fires a 5 s pump
- [ ] Sub-second requests rejected with a message naming the minimum for that vial
- [ ] Displayed "will deliver" volume matches the logged delivered volume
- [ ] Seconds mode still available and unchanged
- [ ] Volume debits the correct media bottle

---

### Session M — Structured logging and the unified event log

**Priority: P0. Effort: MEDIUM (60 min). Depends on: nothing.**

Currently `logging.basicConfig` writes to stdout, which systemd captures into the
journal, and the only persistent structured records are `log_sensor_cycle`,
`log_pump_event`, and `log_escalation_event`. When something goes wrong overnight there
is no single place to look, and correlating a journal line with an experiment cycle means
reading timestamps by eye.

**What it builds, in three parts:**

1. **Rotating file logs.** `RotatingFileHandler` to `logs/evolver.log`, 5 × 10 MB, plus a
   separate `logs/errors.log` at WARNING and above. Disk-aware (the RPi's card is small —
   `/api/storage` already exists, so refuse to grow logs below the free-space floor).
2. **A unified per-experiment event log** — `experiments/{name}/events.csv` with columns
   `timestamp, elapsed_hours, level, category, vial, message, data_json`. Every discrete
   thing that happens goes here: start/stop, pump fires *and suppressed pump attempts*,
   phase changes, alerts, maintenance enter/exit, refills, manual overrides, escalations,
   sensor failures, serial errors. This is the artefact a researcher attaches to a lab
   notebook entry, and it is what makes the event-log table in the UI possible.
3. **Intelligent error classification** (meeting item 4). Rather than logging every
   serial hiccup identically, classify and aggregate:
   - `TRANSIENT` — a single malformed RS485 frame. Counted, not alerted. The bus is
     lossy by design and `b9b135a` already tolerates this.
   - `DEGRADED` — a vial's sensor failing repeatedly. The engine already tracks
     `DEFAULT_SENSOR_FAILURE_THRESHOLD`; surface it as a per-vial health badge instead of
     burying it in the log.
   - `PERSISTENT` — the whole bus silent, serial port gone, Arduino unresponsive.
     Immediate critical alert; this is the one that ends experiments.
   - Rate-limit repeated identical errors (log first, then every Nth, with a count) so a
     stuck loop cannot fill the SD card in an hour.

**Verification:**

- [ ] Logs rotate and do not exceed the configured cap
- [ ] `events.csv` is created per experiment and included in the export ZIP
- [ ] A suppressed pump (Session K) appears in `events.csv` with its reason
- [ ] 100 identical serial errors produce a bounded number of log lines with a count
- [ ] Per-vial sensor health visible on the dashboard
- [ ] Log writes stop gracefully when disk is below the floor

---

### Session N — Per-vial growth rate service

**Priority: P0. Effort: DEEP (90 min — the difficulty is scientific, not architectural).
Depends on: nothing. Blocks: R, T, V, and the growth-rate control mode.**

This is the dependency hub of the entire roadmap. Time-to-empty forecasting, derived
statistics plots, stall detection, and the disabled `growth_rate` control mode all need
it. Right now growth-rate estimation exists only inside `MorbidostatController` as a
private sliding-window fit.

**What it builds:** `server/growth_rate.py` — a pure, I/O-free module usable by any
control mode, plus per-vial μ in `status()`, the WebSocket payload, and the CSV logs
(`data_logger.py` already has a `growth_rate_per_hour` column).

**Why this is deep — the estimator is not obvious.** Naively fitting `ln(OD)` over a
rolling window is wrong in a turbidostat, because dilution events are step decreases in
OD that have nothing to do with growth. Two estimators should be computed and both
reported:

1. **Segment regression.** Split the OD series at dilution events; fit `ln(OD) = μt + c`
   by least squares within each inter-dilution segment; take a weighted mean of recent
   segments' μ. Reflects instantaneous growth. Noisy when dilutions are frequent, because
   segments get short.
2. **Dilution-rate estimator.** At turbidostat steady state, growth rate equals dilution
   rate. Over a window containing *k* dilution events with delivered volumes *vᵢ* into
   vial volume *V*, `μ ≈ Σ ln(1 + vᵢ/V) / Δt`. Depends only on pump volumes and event
   times, not on OD noise — much lower variance, but it inherits any error in the pump
   flow calibration and is only valid when OD is genuinely stationary.

**Report both, and treat their disagreement as a diagnostic.** Persistent divergence
means something physical: biofilm/wall growth (the culture is denser than the planktonic
OD suggests), a mis-calibrated pump, or a culture not actually at steady state. That
disagreement signal feeds Session V.

**Edge cases that must be handled explicitly:**

- Low-OD regime — below ~0.05 the sigmoid calibration is at its noisy tail; return
  `None`, not a wild number.
- Insufficient data — the turbidostat is dormant for its first 8 cycles anyway; require a
  minimum sample count and time span before emitting a value.
- Lag phase and stationary phase are not exponential; report a goodness-of-fit (R²)
  alongside μ so the UI can grey out untrustworthy estimates rather than presenting a
  meaningless slope with false confidence.
- Chemostat mode: μ is imposed by the operator at steady state, so the dilution estimator
  is nearly tautological there. Segment regression is the informative one.

**Verification:**

- [ ] Synthetic exponential growth at known μ recovered within 5 %
- [ ] Simulated turbidostat with dilutions: both estimators agree within 10 % at steady state
- [ ] Dilution events do not depress the estimate
- [ ] Low OD and short history return `None` rather than a number
- [ ] R² reported; a deliberately non-exponential series produces a low R²
- [ ] μ appears in `status()`, the WebSocket payload, and the per-vial CSV

---

### Session O — Per-run and pump-flow calibration wizards

**Priority: P0. Effort: DEEP (90 min, plus bench time). Depends on: nothing.
Blocks accuracy of: K, L, N, R.**

There are currently **no calibration endpoints at all**, and `SPEC.md` §14 open question 3
notes that the existing `temp_calibration.txt` and `OD_cal.txt` came from a previous user
and have never been verified. Every OD number the system reports and every volume it
believes it pumped rests on unvalidated 2016-era constants. This is the quiet blocker
under most of the other work.

Scope for this session is deliberately the two calibrations with the best
value-to-effort ratio. Full thermistor and OD sigmoid recalibration is Session AA.

**O1 — Pump flow calibration (gravimetric).** For each of the 32 pumps: prime, fire for a
fixed 20 s into a tared vessel, user enters the mass or volume delivered, wizard computes
mL/s and writes `calibration/pump_calibration.json`. This directly improves the interlock
(K), the volume controls (L), the dilution-rate growth estimator (N), and the forecast (R).
Bench time is roughly 40 minutes for all 32; the wizard should support doing it in
batches and resuming.

**O2 — Per-run OD blank (meeting item 6).** The full 4-parameter sigmoid is a long
calibration nobody will repeat before every experiment. What *is* worth repeating is
re-establishing the two asymptotes with the actual vials, actual media, and actual sleeve
seating for this run: record the dark reading (LEDs off) and the blank reading (LEDs on,
sterile media, no cells) per vial, and update rows 0 and 1 of the OD calibration for this
run only. This corrects the dominant per-run error sources — vial-to-vial optical
variation, sleeve seating, media colour — in about 5 minutes, without touching the
inflection point and Hill coefficient that genuinely need a dilution series.

**Design notes:**

- Never overwrite `calibration/*.txt` in place. Write versioned files with a timestamp
  and a `source` field, keep the previous one, and record which calibration version an
  experiment used inside its `config.json`. Calibration provenance is part of the data.
- Per-run blanks belong to the experiment directory, not the global calibration
  directory, precisely because they are run-specific.
- The wizard needs raw-mode access (`set_temperature_raw` exists; an equivalent raw OD
  LED path is needed) — expose these under `/api/calibration/*`, not the normal actuator
  routes, so ordinary use cannot reach them.
- Refuse to run any calibration while an experiment is RUNNING.

**Verification:**

- [ ] Pump wizard writes per-pump mL/s; engine picks them up via `calibration.pump_flow_rates`
- [ ] Measured flow reproduces to within 15 % on a repeat run
- [ ] Per-run blank updates rows 0–1 only, scoped to the experiment
- [ ] Previous calibration files retained; experiment `config.json` records the version used
- [ ] Calibration endpoints refuse to run during an active experiment
- [ ] Bottle levels lose the "uncalibrated estimate" label once real flow rates exist

---

## 4. P1 — Prototype polish

Target: complete before the next lab meeting.

### Session P — Hygiene records and sterilisation wizard

**Effort: MEDIUM (60 min). Depends on: K (volume awareness), L (volume-based pumping).**

Covers meeting items 8 and 9. Item 8 is nearly free and should ship first.

**P1a — Hygiene record (LIGHT, 20 min).** `calibration/hygiene.json` (or better,
`state/hygiene.json`) recording, per fluidic line and for the vial set: what was done
(autoclave / bleach cycle / ethanol flush), when, by whom, and free-text notes. Surface
as a dashboard badge — "Fluidics last sterilised: 6 days ago" — and as a **soft gate** in
the experiment wizard: if the last sterilisation is older than a configurable threshold
(default 14 days) or absent, the review step shows a warning the user must acknowledge.
Soft, not hard: a blocking gate on a record the software cannot verify would just train
people to lie to it.

**P1b — Sterilisation wizard (MEDIUM, 40 min).** A guided service routine that runs the
standard line-cleaning sequence — bleach, dwell, water rinse ×N, ethanol, air — with
per-step volumes, timers, and confirmation prompts, and writes the hygiene record on
completion.

**Safety requirements, which are the real work here:**

- Runs only in an explicit **service mode**, never with an experiment RUNNING.
- Fluid moved during service must **not** debit media bottles or credit the waste
  container as experimental consumption — it is a distinct event category. Waste volume
  *does* still accumulate physically, so it must be credited to waste but tagged as
  service volume.
- The wizard must prompt the operator to confirm which bottle each line is currently in
  (bleach vs media), because the software cannot know — the tubing is moved by hand.
- Hard-stop button on every step.

**Verification:**

- [ ] Hygiene record persists across restarts and appears on the dashboard
- [ ] Stale-sterilisation warning appears in the wizard review step and is acknowledgeable
- [ ] Sterilisation wizard refuses to start during a running experiment
- [ ] Service volumes tagged distinctly; media consumption unaffected
- [ ] Completion writes a hygiene record with operator and timestamp

---

### Session Q — Experiment templates and cloning

**Effort: LIGHT (30 min). Depends on: nothing.**

`config.json` already fully describes an experiment, so this is mostly plumbing with a
disproportionate usability payoff — it is the single best effort-to-value item on the
list for handoff to new lab members.

- Save any experiment's configuration as a named template (`templates/{name}.json`).
- Start the wizard from a template, pre-filling every step; the user reviews and adjusts.
- "Clone previous run" as a shortcut, which is how most experiments actually get created.
- Ship 3–4 curated built-in templates: standard turbidostat (8 vials, LB, 37 °C),
  chemostat dilution series, morbidostat escalation, and an OD-only monitoring run with
  heaters parked.
- Templates must record which calibration version they assume, and warn on load if
  calibration has changed since.
- Export/import as a file so templates can be shared between labs or attached to a paper.

**Verification:**

- [ ] Round-trip: save an experiment as a template, create from it, configs match
- [ ] Built-in templates create valid experiments in mock mode
- [ ] Template referencing a stale calibration warns on load
- [ ] Import/export produces a portable file

---

### Session R — Consumables forecasting (time to empty)

**Effort: LIGHT (30 min). Depends on: K, N.**

Meeting item 11. Build it in two tiers, and present the simpler one by default.

1. **Observed rate (ship first).** mL/h consumed per bottle over a trailing window
   (default 2 h), extrapolated linearly. Requires no model, degrades gracefully, and is
   what an operator would compute by hand. Display "≈ 14 h remaining (observed)".
2. **Predictive (after N).** In turbidostat mode, dilution frequency is set by growth
   rate: at steady state a vial consumes roughly `μ × V` mL/h. Summed across the vials fed
   by a bottle, this forecasts consumption *before* enough history exists to observe it,
   and it correctly anticipates that a culture still accelerating will consume faster
   soon. Show as a range, not a point estimate, and state the assumption (steady-state
   growth at current μ).

Present both when they disagree — divergence means the culture is not at steady state,
which is information. Surface "runs dry at 03:40 Tuesday" as a wall-clock time, since
that is what determines whether someone needs to come in overnight.

**Verification:**

- [ ] Observed-rate estimate matches hand calculation on mock data
- [ ] Predictive estimate within 20 % of observed once steady state is reached
- [ ] Wall-clock depletion time displayed and correct across a DST boundary
- [ ] No estimate shown (rather than a garbage one) before enough history exists

---

### Session S — Supervised per-vial override

**Effort: MEDIUM (60 min). Depends on: M (audit trail).**

Meeting item 12. Vials in a running experiment are currently hard-locked in the UI
(`isLocked` in `index.html`). That is the right default and the wrong absolute — real
experiments need intervention: pull a sample, spike a vial, rescue a stalled culture.

**What it builds:** an explicit unlock gesture (press-and-hold ~2 s, or a typed
confirmation for destructive actions) that grants a **time-limited, audited override** on
one vial: manual influx/efflux by volume, temperature change, stir change.

**The subtle correctness requirement — this is why the session is MEDIUM not LIGHT.** A
manual dilution that the controller does not know about will corrupt control. The
turbidostat tracks `last_pump_time` and a deficit accumulator; if a user manually dilutes
and the controller is unaware, it will dilute again immediately, double-dosing the
culture. So every manual action on a controlled vial must be **pushed into the
controller's state**, not merely executed:

- Manual influx/efflux updates `last_pump_time`, debits the media bottle, credits waste,
  and appends to the vial's OD/pump history exactly as an automatic event would.
- Manual temperature or stir changes must either update the experiment's parameters
  (persisted, so a restart does not silently revert them) or be explicitly marked
  transient with a stated expiry.
- Override expires automatically (default 10 min) and re-locks, so a forgotten unlock
  cannot leave a vial unguarded.
- Every override is an `events.csv` entry with operator, action, and reason — and appears
  as a marker on that vial's plots, because an unexplained step in the data six months
  later is a research problem.

**Verification:**

- [ ] Unlock gesture required; vial re-locks on expiry
- [ ] Manual dilution updates controller state — controller does not immediately re-dilute
- [ ] Manual actions debit media and credit waste correctly
- [ ] Every override logged with operator and reason, and marked on plots
- [ ] Overrides survive a server restart in a defined way (persisted or cleanly reverted)

---

### Session T — Per-vial derived-statistics panel

**Effort: LIGHT (30 min). Depends on: N.**

Meeting item 13. The per-vial modal plots OD and temperature; the plotting infrastructure
(uPlot) and the 16-colour palette already exist. Add, as additional traces or small
multiples:

- Growth rate μ over time, with the R² confidence shading from Session N
- Cumulative dilution volume and instantaneous dilution rate
- Doubling time (derived from μ; more intuitive than μ for most biologists)
- Time between dilution events — a very legible proxy for culture health, and the plot
  where a stall or a contamination is usually visible first
- Temperature deviation from setpoint rather than absolute temperature
- Pump event markers and override markers on every trace

Grey out or annotate traces where the underlying estimate is untrustworthy rather than
hiding them; an absent line is ambiguous, an explicitly-flagged one is not.

**Verification:**

- [ ] Each derived trace matches an independent calculation from the exported CSV
- [ ] Low-confidence regions visually distinguished
- [ ] Renders acceptably with 48 h of data at a 10 s cadence (downsample as needed)

---

### Session U — Drag-to-assign media grid

**Effort: LIGHT (30 min). Depends on: nothing.**

Meeting item 14. Step 3 of the wizard currently cycles a vial through bottles on repeated
clicks ("Click a vial to cycle through media bottles"), so assigning vial 12 to the fourth
bottle takes four clicks, and doing that for 16 vials is intolerable.

Replace with a paint model, keeping the existing colour coding: select a bottle from a
palette, then click or drag across vials to paint the assignment. Add "assign all",
"assign row", "assign column", and an eraser for unassigned. Keep click-to-cycle as a
fallback for touch devices where drag is awkward, and support keyboard selection for
accessibility.

**Verification:**

- [ ] Drag across multiple vials assigns all of them to the selected bottle
- [ ] Colour coding unchanged and consistent with the dashboard
- [ ] Works with mouse, touch, and keyboard
- [ ] Bulk-assign helpers produce the same config as manual assignment

---

### Session V — Rule-based anomaly and stall detection

**Effort: MEDIUM (60 min). Depends on: N, M.**

The deterministic tier of meeting item 15 — everything that can be detected by an
explainable rule, with no statistical model and no training data required. Warn-only,
never auto-stop.

| Condition | Rule | Level |
|---|---|---|
| Stall | μ < 0.05/h for > 2 h with OD > 0.1 | warning |
| Dead culture | OD monotonically falling for > 1 h with no dilution | warning |
| Dilution-response failure | Influx fired but OD did not drop by the expected fraction | warning |
| Runaway growth | OD above upper threshold for > 3 consecutive cycles despite dilution | warning |
| Estimator divergence | Segment-regression μ and dilution-rate μ differ by > 50 % for > 1 h | info |
| Temperature excursion | \|T − setpoint\| > 2 °C for > 15 min | warning |
| Pump over-cycling | Dilution interval below `pump_wait` floor repeatedly | warning |
| Sensor degradation | Per-vial sensor failure counter above threshold | warning |

Notes: detection stays dormant below OD 0.1 and during the turbidostat's 8-cycle warmup.
Every alert must carry the evidence that triggered it (the numbers, the window) so a user
can judge it — an alert that just says "possible contamination" trains people to dismiss
alerts. Alerts land in `events.csv` and feed Session W.

The dilution-response check is worth highlighting: it catches the two most common
mechanical failures at once (a pump that is not actually pumping, and a bottle that is
empty despite what the volume estimate says) by comparing the OD drop the dilution
*should* have produced against what happened.

**Verification:**

- [ ] Each rule fires on a synthetic series constructed to trigger it
- [ ] Normal growth over 24 h simulated produces zero alerts
- [ ] Dilution events, warmup, and low-OD phases produce no false positives
- [ ] Every alert carries its triggering evidence
- [ ] No rule can stop an experiment

---

### Session W — Notifications (Slack first)

**Effort: LIGHT (30 min). Depends on: M, V.**

Meeting item 16. Route `critical` and `warning` alerts to a Slack incoming webhook: a
single POST, no OAuth, no app review. Configurable per level, with a digest option so a
flapping condition cannot spam the channel, plus a "test notification" button and a
daily heartbeat summary for running experiments (silence should not be ambiguous between
"fine" and "server dead").

**Two constraints to resolve before building:**

- **Egress.** The Pi sits behind a dedicated Netgear router. Tailscale is deployed
  (`deploy/ts-keepalive/`), so outbound connectivity may already exist — verify before
  assuming, and fail gracefully with a queued-notification buffer when offline.
- **Email is harder than it looks.** Yale's SMTP relay will likely require authenticated
  submission and may reject a headless device. Do Slack first; add email only if someone
  actually needs it, and consider a webhook-to-email bridge rather than SMTP on the Pi.

**Verification:**

- [ ] Test notification reaches the channel
- [ ] Critical alerts delivered; info alerts suppressed per configuration
- [ ] Repeated identical alerts digested, not spammed
- [ ] Server offline → notifications queued, sent on reconnect, not lost
- [ ] Webhook URL stored outside the repo (it is a credential)

---

### Session X — Off-box backup

**Effort: MEDIUM (45 min). Depends on: nothing.**

Meeting item 17, rescoped per §2. Scheduled `rclone sync` of `experiments/` and
`exports/` to a configurable remote, run from a systemd timer rather than inside the
Flask process so a backup failure can never affect the control loop.

- Configurable remote (OneDrive, Google Drive, S3, SFTP to a lab NAS — all one config line).
- Sync after each experiment stops, plus a nightly incremental.
- Never sync while the serial loop is under load; check disk and CPU first.
- Report last-successful-backup age on the dashboard — an unnoticed silently-failing
  backup is worse than no backup, because it manufactures false confidence.
- Document a restore procedure and **test it once**, because an untested backup is a
  hypothesis.
- Credentials in `rclone.conf` outside the repo, referenced by `.gitignore`.

**Verification:**

- [ ] Nightly sync runs and completes without touching the control loop
- [ ] Backup age visible on the dashboard; stale backup raises a warning
- [ ] Restore procedure tested end to end from the remote onto a clean directory
- [ ] Credentials absent from git history

---

### Session Y — Vial groups within one experiment

**Effort: MEDIUM (90 min). Depends on: Q helps, not required.**

The pragmatic 80 % of meeting item 18 (see §2 for why true concurrency is deferred).
Extend the experiment config so an experiment contains *groups*, each with its own vials,
control mode, parameters, and media assignment:

```json
{
  "groups": [
    {"name": "control",   "vials": [0,1,2,3],   "mode": "turbidostat",
     "params": {"od_lower": 0.2, "od_upper": 0.4}},
    {"name": "selection", "vials": [8,9,10,11], "mode": "morbidostat",
     "params": {"target_od": 0.4, "drug_step": 2}}
  ]
}
```

The engine already builds per-vial controllers; this changes how they are *configured*
and dispatched, not how many engines exist — the serial layer, the state file, and the
resume logic are untouched. Vials may belong to at most one group; ungrouped vials are
inactive. The dashboard colours vial cards by group, and the wizard gains a group step.

This also cleanly subsumes a common request the meeting did not name: running the same
mode at different parameters across vials (a dilution-rate series, a temperature series).

**Verification:**

- [ ] Two groups with different modes run simultaneously in mock mode without interfering
- [ ] Per-group parameters honoured; per-group media tracked separately
- [ ] A vial cannot belong to two groups
- [ ] Existing single-mode configs still load (treated as one implicit group)
- [ ] Resume restores all groups correctly

---

## 5. P2 — Post-prototype

### Session Z — Multi-phase experiment protocols

Originally Session E in `SESSION_MASTER_PLAN.md`, still unbuilt and still worth doing.
Recommendation from that document stands: build the **linear phase list** (phase → mode →
parameters → transition condition), not a graph editor. Branching logic is a
generalisation nobody has asked for. With Session Y's groups in place, phases should be
per-group. **Effort: DEEP (2–3 h).**

### Session AA — Full temperature and OD sigmoid recalibration

The remainder of meeting items 6/7 that Session O deliberately deferred: a 2-point
thermistor calibration against a reference thermometer, and a proper OD dilution series
fitting all four sigmoid parameters. Both need substantial bench time (a couple of hours
each across 16 vials) and both are prerequisites for publishable absolute OD values —
but the per-run blank from Session O covers day-to-day relative accuracy. Blocked in
practice by `SPEC.md` §14 open question 4 (the heater diagnosis). **Effort: DEEP + bench.**

### Session AB — True parallel experiments

Only if Session Y's groups prove insufficient — specifically, if the lab needs experiments
with genuinely independent lifecycles (different start times, independent stop/resume,
different operators, separate data directories). Requires per-experiment engine instances,
RS485 arbitration, vial ownership, and N-way resume. **Effort: DEEP (3+ h), high
regression risk in the heater path.**

### Session AC — Stir rate to RPM calibration

Meeting item 20. The software is trivial (a lookup table plus interpolation, and an
animation whose rotation rate is bound to it). The cost is bench work: measuring actual
RPM per sleeve across the 0–15 PWM range, by tachometer, strobe, or high-frame-rate video
of a marked stir bar. Worth doing because "600 RPM" is reportable in a methods section
and "stir setting 8" is not, and because stir bars couple inconsistently across sleeves —
the calibration will likely reveal real vial-to-vial variation worth knowing about.
Sample 4 points per sleeve and interpolate rather than all 16 settings. **Effort: MEDIUM
software, ~2 h bench.**

### Session AD — Palette audit

Meeting item 19 is **already implemented** — `PLOT_COLORS` in `index.html` holds 16
distinct colours indexed by `vialColor()`. Two residual gaps: `BOTTLE_PALETTE` has only 6
entries and repeats from the 7th bottle, and neither palette has been checked for
colour-blind safety. Audit both against deuteranopia/protanopia simulation and extend the
bottle palette. **Effort: LIGHT (20 min).**

### Session AE — Authentication and network hardening

Originally Session J. Login, per-endpoint auth (emergency stop exempt), optional HTTPS,
and concurrent-user conflict resolution. Deferred because the machine currently sits on
an isolated router where physical access is the security boundary — this becomes P1 the
moment the Pi is exposed to the Yale network or Tailscale access is widened beyond one
user. **Effort: MEDIUM (45 min).**

---

## 6. P3 — Deferred pending evidence

### Statistical contamination detection

Meeting item 15, upper tier. Deferred per §2 until a corpus of real runs with known
outcomes exists. **Concrete prerequisite:** Session V must log its derived features
(short/long-window μ, growth-rate jerk, dilution-interval variance and its trend,
estimator divergence) into `events.csv` from day one, so that the corpus accumulates
without further work. Revisit after roughly 10 runs, including at least 2 known
contaminated ones. Until then, the honest position is that the deterministic rules catch
the detectable cases and a model trained on nothing would be theatre.

### Cascade PID temperature control

Originally Session H, whose stated premise is wrong (see §1). The correct framing: the
Arduino already closes a loop on the thermistor ADC; a Pi-side controller would be an
*outer* loop that trims the `xr` setpoint based on calibrated °C error. That is a
reasonable design, but two things must happen first: (a) `SPEC.md` §14 open question 4 —
whether the heaters are electrically healthy — must be resolved, and (b) Session AA must
provide trustworthy temperature calibration, since a cascade loop tuned against a wrong
calibration will confidently converge on the wrong temperature. Also worth asking whether
it is needed at all: ±1 °C is adequate for nearly every experiment the lab runs, and the
existing bang-bang-plus-safety-clamp already achieves that. Revisit only if a specific
experiment demands tighter control.

### Native OneDrive OAuth integration

Superseded by Session X unless a hard requirement emerges for OneDrive-specific features
(sharing links, Yale tenant compliance) that `rclone`'s OneDrive backend cannot satisfy.

---

## 7. Suggested sequence

Ordered for risk reduction first, then usability, with bench work batched.

| Week | Sessions | Theme |
|---|---|---|
| 1 | **K**, **L** | Stop losing experiments to empty bottles; make pumps speak millilitres |
| 2 | **O** (+ bench) | Calibrate pumps and per-run blanks — unblocks the accuracy of everything |
| 3 | **M**, **N** | Observability and the growth-rate engine (the dependency hub) |
| 4 | **P**, **Q**, **U** | Hygiene records, templates, drag-assign — the handoff-usability block |
| 5 | **R**, **T**, **V** | Forecasting, derived plots, rule-based detection (all consume N) |
| 6 | **S**, **W**, **X** | Supervised override, Slack alerts, off-box backup |
| 7 | **Y** | Vial groups — the practical form of "parallel experiments" |
| 8+ | Z, AA, AB, AC, AD, AE | Post-prototype, reprioritised at the next lab meeting |

Weeks 1–3 are the ones that determine whether an unattended multi-day run survives.
Weeks 4–6 are what make the system usable by someone who did not write it. If time is
short, cut from week 7 onward, not from weeks 1–3.

**Bring to a design discussion before implementing:** Session N (which growth-rate
estimator is authoritative, and what the lab considers acceptable error), Session O (the
gravimetric protocol and who does the bench work), and Session Y (whether groups really
do cover the parallel-experiment need, before committing to the larger AB refactor).
