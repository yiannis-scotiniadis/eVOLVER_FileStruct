# ROADMAP.md — Post-Lab-Meeting Development Plan

**Source:** Lab meeting on prototype finalisation (Aug 2026). This document triages the
20 feature requests from that meeting against the code as it actually exists, and
sequences them into implementable sessions.

**Relationship to the other docs:**

- `CLAUDE.md` — hardware facts and serial protocol. Ground truth about the machine.
- `SPEC.md` — technical specification of the software. Ground truth about *what to build*.
- `CALIBRATION_PROTOCOL.md` — the tiered bench SOP (per-run / per-campaign / foundational)
  and the implementation brief for the calibration wizards. Ground truth about *what the
  numbers mean* and how they are established. Contains a numerical audit of the inherited
  2016 constants that materially changes Session O — read §1 of it before starting O.
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
| Sub-second pump deficit accumulator | Built (chemostat only) | `control_modes/chemostat.py` |
| Control-mode audit fixes (windup, elapsed-time boli, sensor/control gate split) | Built | `CONTROL_MODE_AUDIT.md`, `server/test_control_loop.py` |
| Data export (ZIP), exports browser, disk-usage monitoring | Built | `server/data_export.py`, `/api/storage` |
| Crash recovery / resume from `state.json` | Built | `experiment_engine.py` (`resume_on_startup`) |
| 16-colour distinct plot palette | **Already built** | `index.html` `PLOT_COLORS` |
| Deployment: systemd unit, `install.sh`, Tailscale keepalive | Built | `DEPLOY.md`, `deploy/` |
| Consumables safety interlock (reserve/waste hard stop, auto-maintenance) | Built (Session K) | `experiment_engine.py` (`_consumables_block_reason`, `_handle_consumables_block`) |
| Volume-based fluidics (mL manual pumping with quantisation preview) | Built (Session L) | `experiment_engine.py` (`compute_pump_quantization`), `app.py` `/api/actuators/pump` |
| Rotating disk-aware file logs, unified `events.csv`, error classification | Built (Session M) | `server/event_log.py`, `data_logger.log_event`, `/api/events/*`, `/api/health` |
| Alert drawer, three-level alert colours, RS485 bus + per-vial health badges | Built (Session M2) | `index.html` (`#alert-drawer`, `applyHealth`) |
| Calibration provenance, per-run OD blank, pump gravimetric wizard, reconciliation, staleness | Built (Session O) | `server/calibration_service.py`, `app.py` `/api/calibration/*`, `index.html` Calibration tab |

**Sessions K, L, M, M2 and O shipped on 2026-08-20** and are folded into the table above;
their per-session sections below carry the completion notes and verification results.

**Not built** (relevant to this roadmap): the Tier 3 calibration wizards (thermistor
two-point, OD dilution series, stir RPM — Session AA, gated on bench prerequisite P2),
growth-rate estimation outside the morbidostat controller, anomaly/contamination
detection, multi-phase protocols, experiment templates, authentication, PID/cascade
temperature control, notifications, off-box backup, and parallel experiments (the engine
explicitly holds one experiment in memory).

### Important corrections carried into this roadmap

**1. `xr` is a closed-loop setpoint, not a PWM.**
`SESSION_MASTER_PLAN.md` Session H states that temperature control works by
`pwm = (target_temp - intercept) / slope`. **This is wrong and repeats the exact
misconception `CLAUDE.md` warns about.** The `xr` value is a *closed-loop setpoint* that
the temperature Arduino drives the thermistor ADC reading toward — the Arduino already
runs the feedback loop, and the calibration slope is negative. Any "add PID" work is
therefore *cascade* control (an outer loop on the Pi trimming the setpoint of an inner
loop on the Arduino), not a replacement of a raw PWM. Session H is rewritten accordingly
and deprioritised to P3.

**2. The per-run OD blank must re-anchor row 2, not overwrite rows 0 and 1.**
Earlier revisions of this file (and `SPEC.md` §19.2) specified the per-run blank as
"update rows 0 and 1 of the OD calibration". Rows 0 and 1 are *fitted asymptotes* of a
four-parameter logistic, not measurements, and they double as the validity domain in
`serial_manager`. Implementing it as written would return negative OD on all sixteen vials
and reject roughly half of all early-run samples as out-of-range. The correct operation is a
one-parameter re-anchoring of row 2, which is identically a blank subtraction in OD units.
Session O carries the full correction and the derivation is in `CALIBRATION_PROTOCOL.md`
Appendix B.1.

**3. The inherited calibration constants are not merely unverified — three of them are
demonstrably wrong.** A numerical audit (`CALIBRATION_PROTOCOL.md` §1) shows the machine
reports OD 0.115–0.444 for a sterile blank, that vial 0's temperature calibration is a
5-sigma outlier likely running that sleeve ~9 °C cold while displaying the setpoint, and
that vial 1's OD fit diverges inside its own working range. These are stated as findings
against Session O, K and N throughout this document rather than as a generic "calibration
is old" caveat.

---

## 2. Triage of the 20 meeting items

Scoring: **Urgency** = does the prototype fail without it. **Utility** = value delivered
once shipped. **Difficulty** = engineering cost including the lab-bench work, not just
the code.

| # | Meeting item | Urgency | Utility | Difficulty | Priority | Session |
|---|---|---|---|---|---|---|
| 1 | Stop pumps when media empty / waste full | **Critical** | High | Low | **Done** | K |
| 2 | Volume, not duration, for manual pump control | High | High | Low | **Done** | L |
| 3 | Better logs | High | High | Low–Med | **Done** | M + M2 |
| 4 | Better and more intelligent error logging | High | High | Med | **Done** | M + M2 |
| 5 | Accurate per-vial growth rate calculator | High | **Critical** | Med–High | **P0** | N |
| 6 | Per-run calibration wizard | High | High | Med | **Done** | O |
| 7 | Calibration wizard (full) | High | High | High | **Done** (O) / P2 (AA) | O / AA |
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

Items 3 and 4 are merged, then split by layer: **M** (backend capture and classification)
and **M2** (the operator-facing display). The audit behind M2 found that most alerts the
system already raises are rendered in success-green and vanish after 3.5 s, so "better
logs" was substantially a *display* problem, not only a capture one. Items 6 and 7 are split by scope (Session O covers
provenance, the per-run blank, pump flow and post-run reconciliation; AA covers full
thermistor and OD sigmoid recalibration). **Item 6's difficulty is revised from Med to
Med–High** — the audit in Session O showed the originally specified per-run blank was
incorrect, and the versioning/provenance layer it depends on had not been costed. Item 15 is split into a deterministic rule-based tier (P1) and a
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

**STATUS: DONE (2026-08-20).** Shipped in `experiment_engine.py`; covered by 16 tests in
`test_experiment_engine.py` (`test_consumables_*`, `test_refill_*`, `test_media_status_*`).

*Original triage: Priority P0 (highest in the list). Effort: LIGHT–MEDIUM (45 min). Depends on: nothing.*

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
- **The cheap partial answer already exists: weigh the bottle.** Session O4 adds a post-run
  mass reconciliation — media and waste masses at start and end, compared against the
  inferred pumped volume. It costs two number fields and it is the only check that can see a
  kinked line, a stalled pump, or a bottle someone topped up. Its drift over weeks is also
  the signal that should trigger pump recalibration. Ship K with the estimate; use O4 to
  learn how wrong the estimate actually is.
- A float switch or a load cell under the bottles would make this a continuous measurement
  instead of an estimate. Worth costing out — it is the single highest-value hardware
  addition on this list, and O4 will produce the evidence for how much it is needed.

**Verification:**

- [x] Mock run: bottle driven to reserve → influx suppressed for exactly the vials fed by it
- [x] Waste driven to capacity → all pumping suppressed, critical alert raised
- [x] All vials blocked → maintenance mode entered automatically
- [x] `refill_media` clears the block; nothing else does
- [x] Suppressed pump attempts appear in the event log with a reason *(delivered by Session M —
      `pump_suppressed` routes through the event funnel into `events.csv` with its reason)*
- [x] Bottle levels are labelled "uncalibrated estimate" until Session O3 has run

---

### Session L — Volume-based fluidics

**STATUS: DONE (2026-08-20).** `compute_pump_quantization` in `experiment_engine.py`, the
`volume_ml` branch of `/api/actuators/pump`, and the mL/seconds toggle in `index.html`.

> **Accuracy caveat still stands.** Every mL figure the UI shows is computed from
> `pump_flow_rates`, which remains the hardcoded 16-value default array that has never been
> measured on this machine. Session L made the *units* right; **Session O3 is what makes the
> numbers right.** Until then a "5 mL" dose is 5 mL only if the guessed flow rate is correct.

*Original triage: Priority P0. Effort: LIGHT (30 min). Depends on: Session O for accuracy (ship anyway).*

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

- [x] 5 mL request on a vial with flow rate 1.0 mL/s fires a 5 s pump
- [x] Sub-second requests rejected with a message naming the minimum for that vial
      (HTTP 400, "below the minimum deliverable dose for vial N")
- [x] Displayed "will deliver" volume matches the logged delivered volume
- [x] Seconds mode still available and unchanged; supplying both is rejected
- [x] Volume debits the correct media bottle (`record_manual_pump`)

---

### Session M — Structured logging and the unified event log

**STATUS: DONE (2026-08-20).** New `server/event_log.py`; 35 tests across
`test_event_log.py` (24) and `test_event_log_api.py` (11). Suite: 218 passing.

*Original triage: Priority P0. Effort: MEDIUM (60 min). Depends on: nothing.
Scope: BACKEND ONLY — the operator-facing display is Session M2.*

> **Note on the M/M2 split.** The two sessions were built together in one pass rather than by
> parallel agents, so the "an agent taking M should not open `index.html`" rule below was moot.
> The deliberate split of *capture* from *presentation* was kept in the code regardless: the
> backend raises every alert, the frontend only renders them.

> **Deliberate split.** M captures and classifies; **M2** presents. They are separated
> because M touches no frontend code at all, which lets it run as a pure-backend agent in
> parallel with N and O3a, while M2 batches with the other work that edits the single
> 159 kB `frontend/templates/index.html`. Two agents editing that file concurrently will
> conflict badly. An agent taking M should not open `index.html`.

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

- [x] Logs rotate and do not exceed the configured cap (10 MB × 5, errors 5 MB × 5)
- [x] `events.csv` is created per experiment and included in the export ZIP
- [x] A suppressed pump (Session K) appears in `events.csv` with its reason
- [x] 100 identical serial errors produce a bounded number of log lines with a count
      (11 surfaced at occurrences 1, 10, 20 … 100; **one** ring row reading `count: 100`)
- [x] Per-vial sensor-failure counter exposed in `status()` (`nan_streak`,
      `od_range_streak`, `sensor_health`)
- [x] `GET /api/events/recent` ring buffer populated whether or not an experiment runs
- [x] Log writes stop gracefully when disk is below the floor (128 MB, deliberately *below*
      `DISK_CRITICAL_FREE_BYTES` so the disk alert fires before the logs go quiet)

---

### Session M2 — Operator-facing error surface

**STATUS: DONE (2026-08-20).** Alert drawer, three-level toast colours, RS485 bus indicator,
and per-vial sleeve badge in `index.html`; backed by `/api/events/recent`,
`/api/events/<id>/ack` and `/api/health`.

*Original triage: Priority P0. Effort: MEDIUM (60 min). Depends on: M for the ring buffer; the
one-line colour fix depends on nothing. Owns `frontend/templates/index.html`.*

**All four defects below are fixed.** The audit table and defect list are kept as the record of
what was wrong and why it mattered — read them as history, not as current state.

Spec: `SPEC.md` §20.4.

An operator currently cannot debug this machine without SSH. The alert path exists but is
thinner than it looks, and in one respect it actively misleads.

**Audit of what's there** (verified against the code, Aug 2026):

| | Count | Consequence |
|---|---|---|
| `log.exception` / `log.error` / `log.warning` sites | 73 | Journal only — SSH required |
| Sites that raise an `alert` to the browser | 13 | Everything else is invisible |
| Alert display lifetime | 3500 ms | Then gone permanently |
| Alert history / persistence across reload | none | Refresh wipes the slate |

**The four defects, and the one to fix immediately:**

1. **Warnings render as success green.** `socket.on("alert")` computes
   `msg.level === "critical" ? "error" : "ok"`, and `.toast.ok` is `--status-ok` (#16a34a).
   **Seven of the engine's ten `_broadcast_alert` sites pass `level="warning"`** (two
   `critical`, one computed) — media low, waste high, OD out of range — so most alerts
   appear in the same green as "Stir applied".
   This is a one-line change; ship it ahead of the rest of the session, it needs no
   backend work.
2. **Ephemeral** — a 03:00 alert is gone by morning, recoverable only from the journal.
3. **No persistence across reload**, and no history on a second browser.
4. **The failures most likely to end a run are log-only**: `pump_command failed`
   (`app.py:671`), `pump firing failed for vial %d` (`app.py:1129`),
   `set_temperature_celsius failed` (`app.py:563`), `execute queued pump actions failed on
   exit` (`app.py:1017`), and `data_logger.log_pump_event failed` (`app.py:683`) — the last
   being silent data loss.

**What it builds:** the alert drawer (persistent, filterable, unacked badge, three distinct
level colours), reload persistence via `GET /api/events/recent`, acknowledgement for
criticals, an RS485 bus-health indicator distinct from the socket.io one, and the per-vial
sensor-health badge. Full behaviour in `SPEC.md` §20.4.

**Design note.** Keep the toast for transient confirmations of user-initiated actions —
it is a fine acknowledgement channel and a bad error channel. The drawer is the error
channel. Resist merging them.

**Verification:**

- [x] A `warning` alert is visually distinct from both success and critical
      (`--status-warn` / `--status-critical` / `--status-info`; none reuse `--status-ok`)
- [x] An alert raised with the browser closed appears in the drawer on next load
- [x] Critical alerts persist until acknowledged; acknowledgement is recorded as an event
- [x] Killing the serial link turns the bus indicator red while socket.io stays connected
      — *backend verified (bus reaches `down` after 3 missed cycles, socket.io unaffected);
      the red-dot rendering was confirmed by eye in the browser, not by a driven test*
- [x] A forced `pump_command` failure surfaces in the drawer, not just the journal
- [x] A repeating fault produces one drawer entry with a rising count, not hundreds
      (41 forced failures → 1 row, `count: 41`)
- [ ] **Drawer state is correct on a second browser opened mid-experiment — NOT VERIFIED.**
      Two browsers were never opened simultaneously. The mechanism is server-side (the ring is
      global and `refreshEvents()` runs on connect), so it should hold, but it is untested.

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
   flow calibration and is only valid when OD is genuinely stationary. Until Session O3 has
   run, that inherited error is unbounded: `pump_flow_rates` is a hardcoded array that has
   never been measured on this machine, so this estimator is currently precise and possibly
   biased, which is the worse of the two failure modes.

**Report both, and treat their disagreement as a diagnostic.** Persistent divergence
means something physical: biofilm/wall growth (the culture is denser than the planktonic
OD suggests), a mis-calibrated pump, or a culture not actually at steady state. That
disagreement signal feeds Session V.

**Edge cases that must be handled explicitly:**

- Low-OD regime — below ~0.05 the sigmoid calibration is at its noisy tail; return
  `None`, not a wild number. Note that the floor is **not the same on every vial**: the audit
  in Session O found optical sensitivity varying four-fold across sleeves (92–375 counts per
  0.01 OD), so the low-OD cutoff and any reported uncertainty should be per-vial, derived
  from the blank read noise recorded each run rather than a single global constant. Vial 1
  additionally has a divergent fit above OD ≈ 1 and should be excluded from quantitative
  comparisons until Session AA.
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

### Session O — Calibration: provenance, per-run blank, pump flow, reconciliation

**STATUS: DONE (2026-08-20).** All four sub-items (O1, O2, O3a+O3, O4) shipped:
`server/calibration_service.py` (envelope, versioned store, blank/pump sessions,
thermal-settling tracker, staleness), the `/api/calibration/*` routes in `app.py`, the
32-flow-rate engine plumbing, and the Calibration tab + OD-blank/pump/reconciliation
wizards in `index.html`. Covered by `test_calibration_service.py` (26 tests),
`test_calibration_api.py` (10 tests), and the O3a additions in
`test_experiment_engine.py` / `test_serial_manager.py`. **What did NOT ship:** the Tier 3
wizards (thermistor two-point, OD dilution series, stir RPM — Session AA, gated on bench
prerequisite P2) and dedicated screens for Tier 0 pre-flight / §5.1 priming / §5.2
spot-check rotation / §5.3 temperature verification, which stay on the printed run sheet
(the §5.2 spot-check *is* runnable through the pump wizard by selecting just the four due
lines). The software half is done; **the bench work — Tier 2 gravimetric calibration and
a first real blank — has not been run**, so every accuracy claim below still holds until
someone stands at the balance.

*Original triage: Priority P0. Effort: DEEP (2–3 h across four sub-items, plus ~1 h
bench). Depends on: nothing. Blocks accuracy of: K, L, N, R.*

> **The bench protocol is `CALIBRATION_PROTOCOL.md`.** Part I of that document
> is the tiered SOP (Tier 0 pre-flight → Tier 1 per-run → Tier 2 per-campaign pumps → Tier 3
> foundational); Part II is the endpoint-by-endpoint implementation brief for this session,
> including file formats, guards, wizard screens and a verification checklist. This section
> is the priority and scope decision; that document is the *how*.

`SPEC.md` §14 open question 3 notes that the existing `temp_calibration.txt` and
`OD_cal.txt` came from a previous user and have never been verified. Those files have been
audited **numerically** (not yet on the bench), and the situation is worse than
"unverified".

#### What the audit found — and why it changes this session's scope

Full tables in `CALIBRATION_PROTOCOL.md` Appendix A. Four findings, all computed from the
committed calibration files:

1. **The machine reports OD 0.115–0.444 for a sterile blank.** Feeding a plausible blank
   signal (~58 000 counts) through the current curves returns a non-zero OD on every vial —
   a **0.33 OD spread across sleeves at zero cells**. Every turbidostat threshold in use today
   is being compared against a number carrying a large, vial-specific, unmeasured offset. This
   is the concrete size of what O2 buys.

2. **The per-run blank as originally specified here would break the OD path.** See the spec
   correction below. This is the single most important change in this revision.

3. **Vial 0's temperature calibration is a 5-sigma outlier.** Its intercept (86.493) and slope
   sit **+5.9 SD** and **+5.4 SD** off the other fifteen (4.5 on median/MAD). To request 37 °C
   the software sends `xr = 482` to vial 0 and `xr ≈ 403` to everything else. If vial 0's
   thermistor behaves like its neighbours, **commanding 37 °C is landing it at ≈ 28 °C while
   the dashboard reads 37.0 °C.** The remaining fifteen agree to ±1.5 °C at a common setpoint,
   which is itself evidence that vial 0 is a bad *fit* rather than a different sleeve.
   This is a 20-minute bench check with a reference thermometer and should be done before the
   next temperature-controlled run, ahead of any software work here.

4. **Optical sensitivity varies four-fold across sleeves** — 92 counts per 0.01 OD on vial 1
   against 375 on vial 9. Vial 1 is additionally broken: its fitted lower asymptote (44 262)
   sits *inside* the normal working signal range, so its curve diverges above OD ≈ 1 and no
   per-run blank will fix it. Consequence for Session N and the control modes: per-vial OD
   noise is not uniform and should be reported, not assumed. A threshold separation of
   0.02 OD is meaningful on vial 9 and is noise on vial 1.

#### O1 — Calibration provenance and versioning (build this first)

Not glamorous, and everything else is unsafe without it. A common JSON envelope
(`schema`, `version`, `supersedes`, `operator`, `source`, `conditions`, `data`, `fit`, `qc`),
a `calibration/current.json` pointer, per-subsystem version directories, and the legacy
`.txt` files regenerated as a *derived view* so `SerialManager.load_calibration()` needs no
change. Experiment `config.json` records a version for every subsystem it used.

`conditions` is not decoration — LED power, stir PWM, setpoint, bench temperature, fluid,
vial-map version. Two calibrations without their conditions cannot be compared. Reject a
write with an empty `conditions` block.

#### O2 — Per-run OD blank — **correction to the previous spec**

**Previous text in this file and in `SPEC.md` §19.2 said: "update rows 0 and 1 of the OD
calibration". Do not implement it that way.** Rows 0 and 1 are the fitted asymptotes of a
four-parameter logistic, not measurements. Two concrete consequences:

- Every fitted upper asymptote is **1.4×–8.7× the observed signal level** (81 938–503 890
  against readings near 58 000) — an extrapolation the hardware never reaches, consistent
  with a 16-bit ADC that cannot exceed 65 535. A measured blank is a point *on* the curve at
  OD ≈ 0; the model places the asymptote at OD → −∞. They are not the same quantity.
- `serial_manager._read_od_enhanced_locked` uses rows 0 and 1 as the **validity domain**
  (`in_domain = (corrected > mn) & (corrected < mx)`). Setting `mx` to the measured blank
  makes every reading at or above the blank return `NaN`. Early in a run OD sits at the blank,
  so noise alone would discard roughly half the samples and `experiment_engine` would raise
  "OD out of calibrated range" on all 16 vials.

Substituting measured values into rows 0/1 moves reported OD at a 50 000-count signal from
**+0.66 to −6.62** on vial 0, and negative on all sixteen.

**Correct approach — re-anchor row 2 only:**

```python
c_run = np.log10((b - a) / (blank_raw - a) - 1.0) / d      # rows 0, 1, 3 untouched
```

This is *identically* `OD_new(S) = OD_old(S) − OD_old(blank)` — a blank subtraction in OD
units, a rigid vertical shift that preserves the curve shape the dilution series paid for and
leaves the validity domain alone. Derivation in `CALIBRATION_PROTOCOL.md` Appendix B.1; the
four correctness assertions have been checked against the committed `OD_cal.txt`.

The dark read is still taken every run, but recorded as a **diagnostic, not a correction**:
the 2016 curve was fit on non-dark-subtracted signal and there is no `OD_cal.meta.json`
sidecar saying otherwise, so `dark_subtract=True` against it is silently wrong. The blank
re-anchoring absorbs a constant dark offset anyway. Dark subtraction switches on only when
Session AA produces a dark-subtracted curve and its sidecar — and the blank must switch with
it. Make this a hard error rather than the current log warning.

Also correct in `SPEC.md` §6: the blank response should report `"updated_rows": [2]`, and
return the per-vial OD offset removed — that number is what the operator actually needs to
see, because it says how wrong the previous run was.

#### O3 — Pump flow calibration (gravimetric)

Unchanged in intent. For each of the 32 pumps: prime, fire for a fixed 20 s into a tared
vessel, operator enters delivered mass, wizard computes mL/s and writes a versioned
`calibration/pump/…json`.

> #### ✅ Correction resolved 2026-08-20: the engine plumbing (O3a) has landed
>
> Earlier revisions of this section said the engine "already prefers
> `config["calibration"]["pump_flow_rates"]` … so populating that block is the whole
> integration." That was false until O3a shipped: `_resolve_flow_rates` used to coerce
> through `_as_list_of_16`, so a 32-element array raised
> `ValueError: 'pump_flow_rates' list must have length 16, got 32`. It now resolves
> through `_as_flow_rates_32` (scalar → 32; length-16 → broadcast to both directions;
> length-32 → as-is), and the claim is finally true: a complete pump calibration's 32
> rates flow from `calibration/pump/…json` into `config["calibration"]["pump_flow_rates"]`
> at experiment creation and from there into per-direction controller rates.

**O3a — engine plumbing for 32 independent flow rates (SHIPPED with this session).**
Scope as implemented:

1. **`_resolve_flow_rates`** returns 32 values. Back-compatible: a scalar broadcasts to all
   32; a length-16 list broadcasts each vial's rate to both its influx and efflux pump
   (exactly today's behaviour, and the correct initial state — influx and efflux start
   equal until O3 measures otherwise); a length-32 list is used as-is. Ordering is the
   canonical pump index from `CLAUDE.md`: `0..15` influx, `16..31` efflux.
2. **Controllers** (`turbidostat.py:81`, `chemostat.py:42`, `morbidostat.py:79`, plus the
   delegating property at `morbidostat.py:167`) carry `flow_rate_influx_ml_s` and
   `flow_rate_efflux_ml_s`, initialised equal. Keep `flow_rate_ml_s` as a deprecated alias
   for the influx rate so `_debit_media_locked` and any external caller keep working during
   the transition. **Dilution timing uses the influx rate only** — that is already correct
   and must not change behaviour.
3. **`_debit_media_locked`** (`experiment_engine.py:1393`): media debit stays influx-only
   and is already right. Waste accumulation is wrong today, but do **not** simply swap in
   the efflux rate — see `SPEC.md` §16.2. Once efflux overrun is engaged, the physically
   correct model is `waste += influx_ml` (volume is pinned by the straw, so liquid out
   equals liquid in). Until the overrun decision is made, leave waste as-is and add a
   `TODO` referencing §16.2 rather than encoding a second wrong model.
4. **Tests.** Add coverage for a 32-length array, a 16-length array (broadcast), and a
   scalar. Every existing test pins `[1.0]*16` (`test_experiment_engine.py`, 10+ sites), so
   there is currently zero regression protection here.

This is a behaviour-preserving refactor: with influx and efflux initialised equal, every
existing test must still pass unchanged.

**Do not build software flow balancing.** `t_efflux = (F_in × t_in)/F_out` is defeated by
the firmware's whole-second quantisation — for typical dilutions the truncation is the same
magnitude as the correction. Volume regulation belongs to the efflux straw. Full reasoning
in `SPEC.md` §16.2.

Bench time is roughly an hour for all 32 and nobody will do it in one sitting, so the session
must be **resumable**: per-pump state persisted, progress visible, abort leaves no partial
file. Acceptance: 3 replicates, CV ≤ 5 %, within 15 % of the previous calibration, non-zero
and within 2× of the manifold median. Mass→volume needs the bench-temperature density, and
viscous media (high-sugar YPD, glycerol) should be calibrated in the actual medium.

Worth stating plainly: **a 0.01 g balance resolves 0.05 % on a 20 s fire.** The balance is
nowhere near the limiting factor — priming, line compliance and tubing wear are. That is why
replicates and the CV criterion matter more than balance precision.

#### O4 — Post-run mass reconciliation (new, ~20 min)

Weigh the media bottle and waste carboy at the start and end of a run; compare the mass deltas
against the software's accumulated `duration × flow_rate`. Agreement within ±10 % passes.

This is the cheapest thing in the roadmap and the only check that validates the entire
open-loop volume chain end to end — it is the one thing that can see a kinked line, a stalled
pump, or a bottle someone topped up without telling the software (`SPEC.md` §14 Q8). Logged
per run, the ratio's drift over weeks *is* the peristaltic tubing-wear signal, which is what
should trigger recalibration rather than a fixed calendar interval.

Needs one endpoint (`POST /api/experiments/{name}/reconcile`), two number fields in the UI,
and a stored record. It also gives Session K's "uncalibrated estimate" label an evidence-based
exit condition.

#### Design notes

- Never overwrite `calibration/*` in place. Versioned files with timestamp, operator and
  `source`; previous versions retained; the version recorded in the experiment's
  `config.json`. Calibration provenance is part of the dataset — a plot whose calibration
  cannot be reconstructed is not reproducible.
- Per-run blanks belong in `experiments/{name}/`, not `calibration/`, precisely because they
  are run-specific.
- Calibration is the **only** consumer of raw actuator paths. *(Done:
  `POST /api/actuators/temperature/raw` moved to `/api/calibration/raw/temperature`, and
  `/api/calibration/raw/od_led` was built.)* This matters more than usual given the
  inverted `xr` convention.
- Refuse to run any calibration while an experiment is RUNNING.
- Refuse to save a bad fit — non-monotonic pump response, R² below a floor, a thermistor fit
  spanning under 15 °C — overridable only with a recorded `override_reason`.
- **Surface staleness.** Pump calibration goes stale on age, on cumulative pump-seconds (the
  pump log already has them — sum them), or on a failed O4 reconciliation. A missing per-run
  blank should hard-block the run, not warn.
- **Review screens are the point.** Before committing any calibration the operator must see:
  new value, previous value, delta, and which acceptance criteria passed. A wizard that just
  says "done" reproduces exactly the situation that let finding 3 above sit unnoticed since
  2016.

#### Verification (2026-08-20: all software items pass in the test suite)

- [x] `reanchor_od_calibration` yields `OD(blank) == 0.0` on all 16 vials
- [x] Rows 0, 1, 3 bitwise unchanged by a blank commit; only row 2 differs
- [x] `OD_new(S) − OD_old(S)` is constant in `S` per vial (shape preservation)
- [x] Blank commit rejected when any blank falls outside the domain `(a, b)`
- [x] Blank commit rejected when LED power or stir PWM differ from the run's values
- [x] Enabling `dark_subtract` against a curve lacking the sidecar is a hard error, not a warning
- [x] Pump wizard writes per-pump mL/s; engine consumes them via `calibration.pump_flow_rates`
- [ ] Measured flow reproduces to within 15 % on a repeat run *(bench — Tier 2 not yet run)*
- [x] Pump session survives a server restart mid-calibration and resumes correctly
- [x] Aborting any wizard leaves no partial file in `calibration/`
- [x] Previous calibration files retained; experiment `config.json` records a version per subsystem
- [x] Legacy `.txt` views regenerate from `current.json` and load in `SerialManager`
- [x] Calibration endpoints refuse to run during an active experiment
- [x] Raw temperature and raw LED routes unreachable from the normal actuator surface
- [x] O4 reconciliation record written; ratio outside ±10 % marks the pump calibration stale
- [x] Bottle levels lose the "uncalibrated estimate" label once real flow rates exist

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
fitting all four sigmoid parameters. Both need substantial bench time and both are
prerequisites for publishable absolute OD values — but the per-run blank from Session O
covers day-to-day relative accuracy. **Full procedures: `CALIBRATION_PROTOCOL.md` §8.1 and
§8.2 (Tier 3). Effort: MEDIUM software, ~5 h bench.**

**Blocked in practice** by `SPEC.md` §14 open question 4 (heater diagnosis), which
`CALIBRATION_PROTOCOL.md` restates as prerequisite P2: confirm the heaters actually turn
off when sent `xr = 4095`. Note the protocol's observation that historical "stuck-on"
behaviour may have been the inverted-`xr` command bug rather than failed MOSFETs — worth
30 minutes of testing before concluding hardware is at fault.

Two things the audit has already changed about this session's scope:

- **It is no longer optional for temperature.** Vial 0's calibration is a 5-sigma outlier
  (see Session O) and probably has that sleeve running ~9 °C cold while the UI displays the
  setpoint. Either the two-point calibration fixes it or the sleeve is genuinely different
  and needs flagging — but the current file cannot be trusted on that vial.
- **Vials 0, 1, 6, 14 and 15 are the priority.** All five show the signature of a poorly
  constrained OD fit (|c| > 2 with shallow |d|, or a lower asymptote inside the working
  range). Vial 1's fit diverges above OD ≈ 1. If bench time is short, recalibrate those five
  rather than spreading effort evenly.

**Efficiency note that makes the temperature half tractable:** ambient is a *free 16-way
simultaneous calibration point*. With heaters off and stir running, all sixteen vials
equilibrate to the same room temperature, so one reference reading serves all of them; only
the hot point needs the probe moved vial to vial (~35 min). That turns a nominal
"couple of hours per subsystem" into roughly 90 minutes.

Acceptance criteria worth writing into the wizard rather than leaving to judgement: two
points spanning ≥ 15 °C, fitted slope within ±10 % and intercept within ±2 °C of the pack
median, and — the check that actually matters — an *independent* verification at an
intermediate target (e.g. 30 °C) agreeing with the reference thermometer within 0.5 °C on at
least four vials. A two-point fit reproduces its own two points trivially; only the middle
tells you whether the response is linear. For OD: R² ≥ 0.99 per vial, residual < 0.02 OD,
inflection point inside the measured range, and the fit performed on **dark-subtracted**
signal with an `OD_cal.meta.json` sidecar recording that fact — that sidecar is what lets
Session O2's dark read graduate from diagnostic to correction.

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
Sample 4 points per sleeve (PWM 4, 7, 10, 13) and interpolate rather than all 16 settings.
**Procedure: `CALIBRATION_PROTOCOL.md` §8.3. Effort: MEDIUM software, ~2 h bench.**

**Currently blocked on equipment, not time.** The lab has a balance, a reference thermometer
and a spectrophotometer, but no tachometer, strobe, or high-frame-rate camera. The cheapest
unblock is a phone shooting 240 fps with a white paint dot on one end of the stir bar, or an
inexpensive optical tachometer. Until one exists, stir stays reported as a raw PWM and this
session cannot start.

Worth noting for Session O: stir setting affects the OD blank (vortex geometry and entrained
air change the optical path), which is why `CALIBRATION_PROTOCOL.md` requires the per-run
blank to be taken at the run's stir PWM and rejects a commit where they differ. That coupling
holds whether or not RPM is ever calibrated.

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
| 0 | *(bench, ~1 h)* | **Prerequisites, before any of this:** vial→sleeve map (`SPEC.md` §14 Q2), heater off-test (§14 Q4), and the 20-minute vial-0 temperature check |
| 1 | **K**, **L** | Stop losing experiments to empty bottles; make pumps speak millilitres |
| 2 | **O** (+ ~1 h bench) | Provenance, per-run blank, pump gravimetric, reconciliation — unblocks the accuracy of everything |
| 3 | **M**, **N** | Observability (backend) and the growth-rate engine (the dependency hub) |
| 4 | **M2**, **P**, **Q**, **U** | Error surface + hygiene records, templates, drag-assign — the frontend/handoff block |
| 5 | **R**, **T**, **V** | Forecasting, derived plots, rule-based detection (all consume N) |
| 6 | **S**, **W**, **X** | Supervised override, Slack alerts, off-box backup |
| 7 | **Y** | Vial groups — the practical form of "parallel experiments" |
| 8+ | Z, AA, AB, AC, AD, AE | Post-prototype, reprioritised at the next lab meeting |

Week 0 is not software and is short, but skipping it invalidates week 2: every per-vial
constant is attached to a logical index, and if index 7 is not the sleeve you think it is,
the whole calibration set is scrambled. The vial-0 temperature check in particular should
happen before the next temperature-controlled run regardless of where the software gets to.

Weeks 1–3 are the ones that determine whether an unattended multi-day run survives.
Weeks 4–6 are what make the system usable by someone who did not write it. If time is
short, cut from week 7 onward, not from weeks 1–3.

**Bring to a design discussion before implementing:** Session N (which growth-rate
estimator is authoritative, and what the lab considers acceptable error), Session O (the
gravimetric protocol is now written — the open question is *who does the bench work*,
`SPEC.md` §14 Q11), and Session Y (whether groups really do cover the parallel-experiment
need, before committing to the larger AB refactor).

**Open items inherited from `CALIBRATION_PROTOCOL.md`** that need a decision rather than
code: who owns the bench work (≈1 h for Tier 2, ≈5 h for Tier 3); whether to buy a
tachometer or use a 240 fps phone for Session AC; which turbidity standard to use for the
Session AA dilution series (killed cells, heat-fixed cells, or beads); and replacing the
protocol's provisional OD noise thresholds with measured repeatability after about five
runs — the blank SDs recorded each run *are* that measurement, so it costs nothing but
patience.
