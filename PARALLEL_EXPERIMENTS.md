# PARALLEL_EXPERIMENTS.md — Two operators, one eVOLVER

**Status:** design brainstorm, 2026-08-27. Supersedes the framing of `ROADMAP.md` item 18 and
`SPEC.md` §14 Q6. No code written.

**Driving scenario (confirmed with Yiannis):** two people sharing one machine, staggered start
and stop dates, fully separate media bottles and waste carboys per experiment.

---

## 0. Thesis

The roadmap frames parallel experiments as a choice between cheap vial groups (Session Y) and a
risky concurrency refactor (Session AB). That is a false binary. The two-operator case needs
**logical independence**, not **execution concurrency**, and those come apart cleanly.

Recommendation: **multiplex, don't parallelize.** Keep one sensor loop, one `SerialManager`, one
control tick. Replace the engine's scalar `_name` / `_config` / `_vials` with `dict[str, Run]`.

> **Revised 2026-08-27** after the design-space study in `MULTIPLEX_OPTIONS.md`. The structure below
> stands, with three additions that came out of exploring the alternatives: ownership lives on the
> **vial** rather than as a list on the run; **all actuator writes go through one reducer** rather
> than runs writing the bus directly; and **hot state is machine-scoped** with per-run snapshots, so
> the one measured cost that scaled with N goes away. See §4 and §6.

---

## 1. Why vial groups do not cover this case

`ROADMAP.md` item 18 argues ~80 % of demand is met by vial groups inside one experiment. That is
right for one operator running several arms; it is wrong for two operators, and the reason is
mechanical rather than philosophical.

A group is a subdivision of one experiment object: one `state.json`, one `DataLogger` activation,
one directory, one start, one stop. Consequences:

- Suren cannot start on Thursday without editing Yiannis's running experiment.
- Suren cannot stop his half without `stop_experiment`, which parks Yiannis's heaters and zeroes
  his stir (`_zero_experiment_actuators_locked`).
- One data directory means no clean per-operator record, export, or provenance.

Groups and parallel runs are complementary, not alternatives. **Groups are how one operator
structures the inside of their own run; parallel runs are how two operators stay out of each
other's way.** Groups should eventually live *inside* a run.

---

## 2. What the hardware actually constrains

The eVOLVER is already a 16-channel parallel instrument. Every actuator is addressed per vial on
the wire; every sensor read is one broadcast returning all 16 values. The singleton is entirely a
software choice.

One OD read serving every run is a *benefit*, not a conflict — there is no bus contention to
arbitrate because there is nothing extra to ask for.

| Resource | Granularity on the wire | Under two runs | Verdict |
|---|---|---|---|
| Heater setpoint | per vial (`xr` 16-vector) | compose from ownership map | free |
| Stir | per vial (16-vector) | same; engine stores one scalar today | free |
| Pump firing | per vial (32-bit mask) | independent; bus writes serialize | free |
| Pump **stop** | all-or-nothing as written | one run's stop truncates another's dilution | **T1** |
| Temperature read | one broadcast, 16 values | one sample feeds every run | free |
| OD read | one broadcast, 16 values | sample shared; *acquisition settings* cannot diverge | **T2** |
| OD / temp calibration | machine-global fit | shared by definition; lockout gate now wrong | **T3** |
| Per-run OD blank | per-vial coefficients | composes cleanly; the gate around it does not | **T3** |
| RS485 bus | half-duplex, one lock | already serialized | free |
| Media & waste | per-experiment config | correct given separate carboys, but unverified | **T8** |
| Maintenance mode | engine-level flag | conflates "pause my run" with "lid is off" | **T4** |
| E-stop & watchdog | machine-global | kills every run — and should | keep |

Nothing in the fluidics or sensor path needs arbitration. The work is in the handful of places
where the software collapsed "the machine" and "the experiment" into one object because, until
now, they were the same object.

---

## 3. Multiplex, don't parallelize

Session AB rates true parallelism DEEP with "high regression risk in the heater path". That is
right about the design it describes — per-experiment engine instances on their own threads. It is
not the design needed here.

Two axes are bundled together:

- **Logical independence** — separate identities, lifecycles, directories, owners, media, modes.
  This is everything the two-operator case asks for.
- **Execution concurrency** — separate threads driving hardware. This is where the heater races
  come from, and it buys nothing.

16 vials on a 60 s control period is a workload measured in milliseconds. The sensor loop already
serves all 16 vials on one thread and has no reason to care that they belong to two experiments.

### The codebase is already shaped for this

1. **Actuator composition already exists.** `_apply_temperature_locked` and `_resend_stir_locked`
   read the current 16-vector off the `SerialManager`, overwrite only their own vials, and write
   it back — docstrings say "preserving non-experiment vials' values". That read-modify-write was
   written for manual control of unowned vials; it generalizes to "vials owned by another run" for
   free. Under a supervisor it should become one explicit composition per tick.

2. **Per-run scheduling already exists twice.** If runs ever need different control cadences, the
   pattern is `next_od_due` (sensor loop) and `_growth_due` (growth service) — both schedule by due
   time on a shared fast tick. A per-run due time is the third instance of an idiom the codebase
   already trusts. This closes the one argument that might have required threads.

3. **The refactor follows an existing seam.** `ExperimentEngine` state divides cleanly into per-run
   fields (`_config`, `_vials`, `_controllers`, `_setpoint_raw`, streak counters, growth deques,
   media and maintenance state) and per-machine collaborators (`_manager`, `_data_logger`,
   `_experiments_root`, thresholds).

**The honest cost is the tests, not the engine.** `test_experiment_engine.py` is 2,355 lines and
every one assumes a singleton. That is the real budget line — and the safety net that makes the
refactor survivable, which is why §6 keeps refactor and behaviour change strictly apart.

---

## 4. Options

| | Option A — Session Y | Option B — **recommended** | Option C — Session AB |
|---|---|---|---|
| Shape | groups inside one experiment | multi-run supervisor, one thread | one engine instance per experiment |
| Gets | independent params and modes | lifecycles, directories, media, N-way resume | nothing more than B here |
| Fails / costs | staggered starts, independent stop, separate dirs/owners | mechanical engine split, fan-out, masked stop, large test rewrite | every risk in the DEEP rating, for unused headroom |
| Verdict | build it — inside a run | **build this** | retire this framing |

`SPEC.md` §14 Q6 currently concludes groups are "same practical capability, far less concurrency
risk". The first half is wrong for staggered two-operator use; the second half is right only
against Option C. Option B is not on the list at all. Worth updating both docs.

**Eleven designs were checked against these three — see `MULTIPLEX_OPTIONS.md`.** No alternative
structure beat Option B, but two refinements changed it materially:

- **Intents and one reducer (axis A2).** Five writers already reach the bus outside `SerialManager`
  (engine, manual endpoints, calibration, watchdog, pump executor); two runs makes seven, each doing
  a read-modify-write on the same 16-vector. Route them all through one reducer with an explicit
  priority order — safety > calibration > manual > run intents. This makes **T1 and the bus-time
  concern structurally impossible rather than merely avoided**, and it completes a pattern the pump
  path already uses (`run_cycle` returns `[(vial, PumpAction)]`; only heater and stir are written
  directly). Caution: the every-tick stir re-send is deliberate re-assertion, not redundancy —
  compose every tick, write every tick.
- **Machine-scoped hot state (axis A3).** One `machine.json` fsynced per tick, per-run `state.json`
  written on transitions only. Removes the per-run fsync measured in §9 while keeping experiment
  directories self-contained.

Two smaller findings from the same study: **ownership belongs on the vial** (one owner field cannot
be double-claimed, turning three separate conflict checks into an invariant), and **bottles should
be first-class objects referenced by runs** rather than owned by them — a bottle is an aggregate
over the vials that draw from it, and that set need not equal a run's vial list.

---

## 5. The traps

### T1 (sharp) — stopping one run stops everyone's pumps

`_zero_experiment_actuators_locked` calls `stop_all_pumps()`. Its own comment accepts this as a
Phase-1 trade-off. Under two runs, Suren pressing Stop truncates an in-flight dilution on Yiannis's
vials — and the pump event was already logged at its *intended* duration, so nothing downstream
knows the volume was short.

**Fix.** `STOP_ALL_PUMPS_BODY` is the multi-fire sub-mode with all 32 pump bits set and 16 zero
times; a masked stop is the same command with only owned bits set. Small, and independently useful
today. *Verify the firmware honours a partial mask on the bench before relying on it.*

Under the reducer (§4) this trap disappears rather than being fixed: a run that cannot reach the bus
cannot call `stop_all_pumps()`. It emits "my vials should not be pumping" and the reducer masks.
Build the masked stop anyway — the reducer needs it as its primitive.

### T3 (blocker) — nobody can ever start a second run

`_cal_route(mutating=True)` returns 409 whenever `engine.is_running`. Starting a run requires a
per-run OD blank (§19.2, hard-blocked when missing), and taking a blank is a mutating calibration
call. While Yiannis's run is going, Suren cannot take his blank, so he cannot start. **The workflow
never gets off the ground.**

**Fix.** Change the gate from "is anything running" to "does this operation touch vials owned by a
run other than the caller's". A per-run blank on unowned vials is safe while another run continues —
it is vial-scoped and the coefficients compose. Full OD/temperature recalibration stays machine-wide
and still locks everyone out, but the 409 should name the blocking runs and their operators.

### T2 — OD acquisition settings cannot diverge

One OD read serves all 16 vials, so `n_samples`, `agg` and `dark_subtract` are machine properties
wearing per-experiment clothing. `dark_subtract` is worse than a preference: `create_experiment`
hard-errors if it is on without a calibration fit on dark-subtracted signal.

**Fix.** Take the max of `n_samples` across active runs (more samples is strictly better, just
slower); treat a mismatch in `dark_subtract` or `agg` as a hard conflict at create time; surface the
machine's current setting in the wizard. **This trap applies to vial groups too.**

### T4 — two different things are called maintenance

`_maintenance_active` means both "pause this run's pumps while I swap its media" and "a human has
the machine open". Those coincided with one experiment. With two, the first is per-run and the
second must stop everything — and there is currently no way to express the second at all.

**Fix.** Two states. Per-run maintenance keeps its 30 min auto-resume failsafe and coalesced pump
queue. A machine-level *physical intervention* state suspends every run, shows on every run's card,
and needs its own timeout policy — decide it deliberately rather than inheriting 30 min.

### T5 (nearly free) — N-way resume is one deletion away

`resume_on_startup` already scans every `experiments/*/state.json`, collects **all** candidates with
`status == RUNNING`, then deliberately demotes all but the most-recently-started to ERROR with
`stop_reason="resume_conflict"`.

**Fix.** Stop demoting; resume them all. Only new logic is vial-ownership conflict detection across
resumed runs. Recommendation: if two state files claim the same vial, refuse **both** to ERROR and
alert loudly — a conflict there means something already went wrong, and guessing a winner writes
heater setpoints on a guess.

### T6 — data and events route to "the" active experiment

`DataLogger` holds scalar `_active_name` / `_active_dir` / `_active_vials` and refuses a second
activation. `EventLog`'s CSV fan-out appends to "the active experiment's events.csv".

**Fix.** Both keyed by run. Write the routing rule down explicitly, because getting it wrong makes a
run's post-hoc record silently incomplete: **vial-scoped events route to the owning run;
machine-scoped events (bus, disk, watchdog, e-stop) broadcast to every active run** plus the ring
buffer.

### T7 — the manual actuator API is a 16-vector

`POST /api/actuators/temperature` takes a dense 16-float array and rejects it if any
experiment-owned vial's value differs from what the engine set. With two runs, a caller would have
to echo back both runs' setpoints exactly to change one free vial.

**Fix.** Move manual control to a sparse map (`{"4": 37.0}`) and let the server compose. The dense
array is itself a single-experiment assumption.

### T8 — separate carboys are a claim, not a fact

Per-experiment media/waste config is correct given genuinely separate consumables, but nothing
verifies it. If two runs each declare a 4 L waste container and it is physically the same carboy,
both interlocks pass while the real vessel overflows at half the reported fill. Silent failure, and
it is a floor spill.

**Fix.** Make the operator declare a physical vessel ID per bottle and carboy; refuse or loudly warn
when two running experiments name the same one.

### T9 — emergency stop kills every run, correctly

`emergency_shutdown()` zeroes the whole machine; `handle_emergency_stop` transitions the one loaded
experiment to ERROR. Global behaviour is right — an e-stop asserts something is physically wrong
with the instrument — but the state transition must fan out.

**Fix.** Fan the ERROR transition to all active runs, write the event into every run's `events.csv`,
and make the UI confirm say how many experiments it is about to end. Same for the watchdog. **Do not
make e-stop selective.**

---

## 6. Sequencing — keeping the heater path safe

The roadmap's fear of regression in the heater path is legitimate. Defuse it structurally rather
than by care: **never mix the refactor with the behaviour change.** Stages 1–2 are pure refactors
whose correctness is proved by the existing suite passing unchanged.

| Stage | Work | Gate |
|---|---|---|
| 0 | Masked pump stop in `SerialManager`. No engine changes; improves single-experiment behaviour today. | Firmware honours a partial 32-bit mask |
| 1 | Extract `Run`; engine becomes a supervisor holding a dict of size one. Behaviour identical by construction. | Every existing engine test passes **unmodified** |
| 2 | Ownership pointer onto the vial; **all actuator writes behind one reducer** with explicit priority (safety > calibration > manual > runs); 16-vectors composed once per tick. Still one run. | No behavioural diff in mock mode over a simulated 24 h run; reducer priority table unit-tested |
| 3 | Allow N > 1: lift create/start gates, delete the resume demotion, machine-scoped hot state, logger and event fan-out, split maintenance, fix the calibration gate. T1 and the bus-time concern are already gone by stage 2; the rest resolved here. | Two runs, different modes, staggered start and stop, in mock mode |
| 4 | UI: allocation map, run lens, ownership prompts, operator names, expected end dates. | Second browser mid-run shows correct ownership |
| 5 | Groups inside a run (Session Y), now a per-run concern. | Existing single-mode configs load as one implicit group |

---

## 7. UI reframe

**The machine becomes the top-level object; the experiment becomes a lens onto it.** With two runs
the first question is no longer "how is my culture doing" but "what is free, and what is not mine".

- **Vial allocation map as home screen** — 16 sleeves coloured by owning run, free vials visibly
  free, faults visible regardless of owner.
- **Machine header, run-independent** — bus health, disk, watchdog, calibration age, e-stop.
- **Run lens** — select a run and plots/controls/events scope to its vials. Do not render two runs'
  OD traces on one axis; different modes, different targets, no shared meaning.
- **Ownership on every destructive control** — `app.py` already blocks manual writes to
  experiment-owned vials and names the experiment in the 409; it just needs to name *which* run.
- **Operator name per run** — free text, not auth. Real auth is Session AE. This is a lab-etiquette
  problem; a name plus a confirm dialog gets most of the benefit for almost no code.
- **Expected end date** — with staggered runs the machine may never be idle again, so "when do vials
  8–13 free up" and "when can I calibrate" become scheduling questions the UI should answer.

**Physical adjacency.** Allocation should prefer physically contiguous blocks — two people reaching
into interleaved sleeves is how lines get crossed and cultures cross-contaminated. That needs the
logical-to-physical vial map still open as `SPEC.md` §14 Q2, which upgrades that question from
housekeeping to a dependency of this feature.

---

## 8. Open questions for the lab

1. **Does the bench actually fit two waste carboys and two media sets?** 16 efflux lines across two
   vessels plus two bottle racks is a space question; the answer decides whether T8's vessel ID is a
   nicety or a necessity.
2. **When does calibration happen on a machine that is never idle?** Either agreed maintenance
   windows, or declared end dates the UI can plan against — probably both.
3. **Is a shared e-stop acceptable to both operators?** It should be, but say it out loud once.
4. **Should stopping a run leave its vials warm?** For back-to-back handoffs, parking heaters off and
   reheating wastes an hour. Small change, but it changes what an idle vial means.
5. **How much does the physical vial map matter now?** Worth deciding whether to run the stirrer
   identification script before Stage 4.

---

## 9. Computational feasibility on the Pi

Measured 2026-08-27 with `server/bench_parallel_runs.py` (added this session).

**Headline: the control loop is not the constraint, and it is not close.**

Per-tick work is **O(vials), not O(experiments)**. Sixteen vials are sixteen vials whether they
belong to one experiment or three — `run_cycle` iterates vials, and everything expensive inside it
(control decision, streak counters, media debit, growth history) is per-vial. Only a small per-run
constant multiplies by N.

Measured on x86 (Xeon 2.1 GHz), using **independent engine instances — the Option C worst case**.
The recommended supervisor (Option B) shares actuator composition, so its real cost is strictly
lower than this:

| Configuration | fast tick | OD tick | OD p95 | engine state |
|---|---|---|---|---|
| 1 run × 16 vials | 0.026 ms | 0.95 ms | 1.4 ms | 148 kB |
| 2 runs × 8 vials | 0.035 ms | 1.51 ms | 1.9 ms | 156 kB |
| 3 runs (6/5/5 vials) | 0.044 ms | 3.14 ms | 4.2 ms | 191 kB |

**Marginal cost per extra run: +0.6 to +1.6 ms on the OD tick, +8 to +35 kB resident.** The fast
lane — the one carrying heater safety — barely moves at all.

### Where that millisecond goes

Almost entirely one thing: `_save_state_locked` writes `state.json` with an `fsync` on every OD
tick, and N runs means N of them. Decomposed at the measured 8 kB payload:

| | ms |
|---|---|
| `json.dumps` only (CPU) | 0.19 |
| + write + atomic replace | 0.36 |
| + fsync — full `_save_state_locked` | 0.66 |

The marginal cost of a second run and the cost of one state persist are the same number. That is
the whole story.

### Scaling to the Pi 3B

Using the ×7 CPU factor already established in `PI3B_HEADED_OS_ASSESSMENT.md` §3:

- **CPU:** ~2.5 ms per extra run per OD tick — i.e. per *sixty seconds*.
- **fsync:** does **not** scale by ×7. It is device latency. A small-file fsync on a class-10 SD
  card is typically 5–50 ms and can spike into the hundreds during wear-levelling. This is the one
  number here that is extrapolated rather than measured — run the bench on the Pi to pin it.
- **Memory:** tens of kB per run, against 905 MB usable.

Against the established baseline — whole sensor loop at ~0.5 % of one of four cores, and a 10 s
tick that already spends **~2.3 s asleep on the UART with the GIL released** — a second and third
experiment are invisible.

### The three things that do get worse — none of them the control loop

**1. SD write amplification (the real steady-state cost).** Each run rewrites `state.json` with
fsync every OD tick: ~11.6 MB/day logical at the measured size, on top of the 10 s CSV writes that
§8 of the Pi assessment already flags. Physical wear is worse, because an SD erase block is far
larger than 8 kB. Two runs doubles it.

*Mitigation, and it removes the multiplier entirely:* **debounce the persist.** Nothing in
`state.json` needs 60 s granularity — the CSVs carry the science, `state.json` carries only the
resume point. Persist on meaningful change with a floor of a few minutes and the N× fsync term
collapses to noise. Worth doing before N > 1 regardless of this feature.

**2. One composed actuator write per tick, not N.** A naive per-run implementation has every run
call `_apply_temperature_locked` and `_resend_stir_locked`, so N runs means N `xr` writes plus N
`zv` writes per tick on a **9600 baud half-duplex bus** — roughly 140 ms of bus time per extra run
per tick, stolen from the OD acquisition window. This is the one place a careless multi-run
implementation costs something genuinely scarce. Supervisor-level composition avoids it entirely.
**Design requirement, not an optimisation** — and another argument for Option B over Option C.

**3. The two user-triggerable bombs now have two fingers on the trigger.**
`PI3B_HEADED_OS_ASSESSMENT.md` §5.2 measures the Plots "All" range at ~8 min CPU and ~81 MB
transient on a Pi 3B, and a whole-run export bundle at ~2.5 min. With two operators these can fire
**concurrently and unaware** — ~160 MB transient against 135–355 MB headroom on a headed box,
served by Werkzeug (§6, "a real weak link").

**This is the actual feasibility risk of parallel operation, and it is a request-path problem, not
a control-loop problem.** The run lens (§7) helps structurally, since each request then covers half
the vials, but the real fix is a server-side cap or queue on those two endpoints.

### One accumulating cost that arrives sooner

§7.1's pump-log index is O(total pump events across the whole experiments tree), never trimmed,
with a ~90 s cold call on the Pi. Parallel runs do not change the per-event cost, but they fill the
tree roughly twice as fast in wall-clock time, so both the resident growth and the cold-start stall
arrive sooner. It arms the moment Tier 2 pump calibration lands. Bound it before parallel runs
ship, not after.

### Verdict

Two or three concurrent experiments cost, on the Pi, **a few milliseconds of CPU per minute and
tens of kilobytes of RAM**. The feature is comfortably affordable. Budget the engineering effort
against the test suite and the request path, not against the control loop.


---

*Companion study: `MULTIPLEX_OPTIONS.md`. Findings drawn from `server/experiment_engine.py`, `server/app.py`, `server/serial_manager.py`, `server/data_logger.py`, `server/event_log.py` at the working tree of 2026-08-27, read against `SPEC.md` §2, §9, §14, §15, §19, `ROADMAP.md` item 18 / Sessions Y and AB, and `PI3B_HEADED_OS_ASSESSMENT.md` §3, §5.2, §7 and §8. §9 figures measured this session with `server/bench_parallel_runs.py` on x86; Pi figures are scaled by the ×7 factor from the Pi assessment, except fsync, which is device latency and must be measured on the box. The masked-pump-stop mechanism in T1 is inferred from `STOP_ALL_PUMPS_BODY` and the fluidics sub-protocol notes in `CLAUDE.md`; it has not been tested against firmware.*
