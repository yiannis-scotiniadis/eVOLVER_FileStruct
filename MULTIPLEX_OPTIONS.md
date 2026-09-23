# MULTIPLEX_OPTIONS.md — design-space study for parallel experiment running

**Status:** companion to `PARALLEL_EXPERIMENTS.md`, 2026-08-27. Explores alternatives to the
multi-run supervisor recommended there.

**Result:** the structure survives. Two refinements — on axes nobody had been arguing about —
change the design materially and are folded back into `PARALLEL_EXPERIMENTS.md`.

---

## 1. The space is five choices, not a list of designs

Most proposals here are combinations of independent decisions, which is why arguing about them as
named options goes in circles. The roadmap's debate was entirely about axes A1 and A4. The two
improvements worth having are on A2 and A3.

| Axis | Choices | Decides |
|---|---|---|
| **A1** where run identity lives | engine dict / **vial owner tag** / OS process / config only | whether ownership conflicts are impossible or merely checked |
| **A2** actuator write discipline | direct writes / **intents + one reducer** | whether multi-run is safe by construction or by discipline |
| **A3** persistence topology | N run files / one machine file / **machine hot state + run snapshots** / append-only log | whether SD write cost scales with N |
| **A4** process & thread topology | **1 process 1 thread** / 1 process N threads / N processes + broker | concurrency risk, and memory on a 905 MB box |
| **A5** tick scheduling | **all runs per tick** / phase-offset slots | persist spreading, pump coincidence |

(Bold = recommended.)

---

## 2. The refinement that matters most: intents and one reducer

`run_cycle` already returns pump decisions as data — `[(vial, PumpAction)]` — and lets the sensor
loop do the hardware write, the logging and the broadcast. Its docstring says this is deliberate.
But heater and stir setpoints are written **directly** from inside the engine, via
`_apply_temperature_locked` and `_resend_stir_locked`.

**The codebase is half-converted to an intent architecture, and the converted half is precisely the
half that multiplexes cleanly.** Finishing the conversion is not importing a pattern — it is
completing one the code already started.

### The write path today, extended to N runs

Enumerating actuator call sites outside `SerialManager` itself gives **five independent writers**:
the engine, the manual actuator endpoints in `app.py`, `calibration_service.py`, `watchdog.py`, and
the pump executor. Each does a read-modify-write on the same 16-element vector. Two runs makes
seven. Last write wins.

### The write path with a reducer

Every writer emits *desired state*; one function composes the machine's 16-vectors in an explicit
priority order — **safety > calibration > manual > run intents** — and performs the single write.

Three consequences worth more than the multi-run capability itself:

- **T1 and the bus-cost concern become impossible, not merely avoided.** A run cannot call
  `stop_all_pumps()` if it cannot reach the bus; it emits "my vials should not be pumping" and the
  reducer masks. Likewise N runs cannot produce N actuator writes per tick, because only the reducer
  writes.
- **The scattered conflict guards get one home.** `POST /api/actuators/temperature` currently
  re-derives raw setpoints and compares element-wise against the engine's values to decide whether
  to reject. That is conflict arbitration reimplemented per endpoint. In the reducer it is one
  priority rule.
- **It is testable without hardware.** "Given these intents, what vector goes to the wire" is a pure
  function. Today the equivalent assertion has to drive a mock manager through the engine.

**One caution to write into the design.** The stir re-send happens every tick *unconditionally* per
SPEC §9 step 6 — deliberate re-assertion against a dropped command or an Arduino reset, not
redundancy. A reducer optimised into "write only on change" would silently delete a safety property.
Compose every tick; write every tick.

---

## 3. Three scopes, and the code only has two

Exploring the vial-as-unit model turned up something that applies whichever structure wins.

| Scope | What lives there | Modelled today as |
|---|---|---|
| **vial** | controller, setpoints, streak counters, faults, OD and dilution history | per-vial dicts on the engine — already correct |
| **bottle** | consumed volume, low/blocked latches; waste fill and its latches | **folded into the experiment** — the mismatch |
| **run** | lifecycle, status, mode, maintenance, growth-run start, operator | engine scalars — the thing being made plural |

A bottle is an aggregate over the vials that draw from it, and that set does not have to equal an
experiment's vial list. Today it always does, because there is only one experiment, so nothing has
exposed the difference. The moment two runs exist, "whose bottle is this" is a question the schema
should answer rather than a convention.

Your machine has fully separate consumables, so this changes nothing operationally now. It changes
the schema: **make the bottle a first-class object with its own identity and consumption ledger,
referenced by runs rather than owned by them.** Costs almost nothing today; it is the difference
between supporting a shared carboy later and rewriting the interlock for it.

The other half of the vial-as-unit model is worth taking in a narrower form: **put the ownership
pointer on the vial, not the vial list on the run.** One field with one value cannot be
double-claimed, which turns conflict detection in create, start and resume from three checks into an
invariant. Runs remain objects — they must, for lifecycle and maintenance — but stop being the
authority on who owns what.

---

## 4. Everything checked

Verdicts are against the confirmed case: two operators, staggered lifecycles, separate consumables,
Pi 3B.

### Organizing structures — pick exactly one

| Candidate | Verdict | Reasoning |
|---|---|---|
| Vial groups in one experiment (Session Y) | rejected | one start, one stop, one directory — fails staggered two-operator use. Still worth building later as an operator's own subdivision. |
| Groups with independent lifecycles | rejected | two operators' data interleaved in one directory breaks provenance and export; one `state.json` with two logical writers recreates the conflict; nothing answers when the containing experiment ends. |
| **Multi-run supervisor** | **adopted, modified** | survives as the structure, with ownership moved onto the vial and writes moved behind a reducer. REST is already run-addressed (`/api/experiments/<name>/…`), so the API barely moves. |
| Vial as the unit of everything | partly adopted | bottle and run scopes still need somewhere to live, so a run side-table reappears — the model hides it rather than removing it. Its good half (ownership on the vial) is taken. |
| One engine per run, on threads (Session AB) | rejected | buys nothing on a millisecond workload; pays with locks in the heater path. Measured: three runs = 4.2 ms p95 against a 10 s tick. |
| Hardware broker + one process per run | rejected | ~58 MB dependency floor per interpreter, so broker + two runs ≈ 174 MB vs ~110 MB today. Fault isolation is illusory — a dead run's vials still need safing, so the broker needs liveness detection anyway, plus IPC. And Phase 1 deliberately replaced five supervisor processes with one server. |

### Refinements — compose with whichever structure wins

| Candidate | Verdict | Reasoning |
|---|---|---|
| **Intents + one reducer** | **adopt — highest value** | makes the two sharpest traps structurally impossible, consolidates per-endpoint conflict rules, completes a pattern the pump path already uses. |
| **Machine hot state + per-run snapshots** | **adopt** | removes the one measured cost that scaled with N (the per-run fsync) while keeping experiment directories self-contained. One authoritative file for resume; a consistent machine snapshot instead of N files that can disagree. |
| Vials as a booking timeline | adopt at the UI layer | wrong as a runtime model, right as the mental model for allocation. Makes "expected end" structural and turns "when can I calibrate" into a query. |
| Phase-offset run scheduling | hold | real benefits, but both problems are better solved directly and neither is biting per the measurements. Cheap to add later; the due-time idiom exists twice already. |
| Append-only state log | rejected | solves a write-cost problem the machine file plus debounce already solves, at the price of replay and compaction — and would sit confusingly beside the existing event log. |
| A second machine | does not apply | the scarce resource is vials, not machines. Worth noting the supervisor generalises to Phase 4's multi-eVOLVER orchestration; groups would not. |

---

## 5. Revised recommendation

Same structure, three changes:

1. **One supervisor, one control thread, a dict of runs.** Unchanged.
2. **Ownership lives on the vial** — a single owner field, not a vial list per run.
3. **All actuator writes go through one reducer**, priority order safety > calibration > manual >
   run intents, composing every tick and writing every tick.
4. **Hot state is machine-scoped**; run directories get snapshots on transitions.
5. **Bottles become first-class**, referenced by runs rather than owned by them.

**Staging:** `PARALLEL_EXPERIMENTS.md` §6 still holds, but **the reducer and vial-ownership belong
together in stage 2**. Both are pure refactors validated by the existing suite, and both make stage
3 substantially less dangerous.

**What this exploration did not change.** The structural choice was already right, the concurrency
analysis was already right, and nothing here beats a single control thread. What changed is the
interface between a run and the hardware: the earlier design left runs holding the bus and relied on
the supervisor composing politely, which is a discipline. Making runs unable to reach the bus is a
property.

---

*Structural claims verified against `server/experiment_engine.py`, `server/app.py`,
`server/serial_manager.py`, `server/control_modes/*` and `server/calibration_service.py` at the
working tree of 2026-08-27; the five-writer count in §2 is an enumeration of actuator call sites
outside `SerialManager` itself. Timing figures are from `server/bench_parallel_runs.py`; the ~58 MB
per-interpreter floor is from `PI3B_HEADED_OS_ASSESSMENT.md` §3.*
