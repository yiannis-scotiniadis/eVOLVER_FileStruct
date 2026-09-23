# Custom control mode — design

**Date:** 2026-09-09 · **Status:** DESIGN ONLY — nothing below is built
**Goal:** let a user write a Python function that fully defines a control mode, with the
ergonomics of `mac_original/custom_script.py`, without giving up anything the engine
currently guarantees.
**Read first:** `CLAUDE.md` facts 3 and 7 · `CONTROL_MODE_AUDIT.md` · `SPEC.md` §9, §10, §15, §16

---

## 1. What the legacy system actually gave the user

`mac_original/custom_script.py` is one file with one function:

```python
def test(OD_data, temp_data, vials, elapsed_time, exp_name):
```

`main_eVOLVER.update_eVOLVER` calls it once every 10 s (`root.after(10000, ...)`), after
reading OD and temperature. Inside it the user has, with no ceremony:

| Capability | How it was reached |
|---|---|
| Per-vial OD and temperature this cycle | `OD_data[x]`, `temp_data[x]` arrays |
| Full run history | `np.genfromtxt` on `{exp}/OD/vialN_OD.txt`, `pump_log`, `ODset`, `temp_config` |
| Arbitrary per-vial parameters | numpy arrays in a `##### USER DEFINED VARIABLES #####` block |
| Fire pumps | `eVOLVER_module.fluid_command(MESSAGE, ...)` — a raw UDP datagram |
| Set stir | `eVOLVER_module.stir_rate(STIR_MESSAGE)` |
| Set temperature | append a line to `vialN_tempconfig.txt`; the next `update_temp` picks it up |
| Persist its own state | append a line to a `.txt` file it chose |

**What is worth preserving:** one file, one function, plain Python with numpy, a
user-variables block at the top, per-vial decisions each cycle, and the fact that reading
and writing your own state needs no framework.

**What must not be preserved:** direct actuation. `fluid_command` writes the wire
protocol itself, which is precisely how it bypassed everything: no duration cap, no media
debit, no consumables interlock, no `pump_log` row, no maintenance-mode gate, no dilution
boundary for the growth service, no alert. Every defect `CONTROL_MODE_AUDIT.md` found lived
in that bookkeeping layer, not in the algorithms.

**The design in one sentence:** the user keeps the function, and loses only the ability to
touch hardware directly — the function *returns a decision* and the engine executes it,
exactly as the three built-in modes already do.

---

## 2. The seam that already exists

The engine does not know what a control mode is. It talks to controllers through a small
duck-typed protocol, and everything else applies uniformly regardless of type.

**Required by the engine** (a custom controller must provide all five):

| Member | Called from | Contract |
|---|---|---|
| `push_od(od: float)` | `experiment_engine.py:1579` | one valid OD sample; NaN never reaches it |
| `decide(now: float) -> PumpAction \| None` | `experiment_engine.py:1580` | the whole control decision |
| `to_state() -> dict` | `_save_state_locked` (3225) | JSON-serialisable, every OD tick |
| `restore_state(state, now=None)` | `resume_on_startup` (3388) | tolerant of missing keys |
| `flow_rate_influx_ml_s: float` | `_influx_ml_locked` (2142) | attribute, used for the media debit |

**Optional, already duck-typed** (`getattr`, so a new type needs no engine edit):

| Member | Called from | Effect |
|---|---|---|
| `requires_od` | `1566`, defaults `True` | `False` keeps the mode running through dropped/out-of-range OD (C-2) |
| `pop_events() -> list[dict]` | `_broadcast_controller_events_locked` (2442) | consume-on-read; the engine funnels them to alerts |

**Everything below is engine-owned and a custom mode inherits it for free:** the 30 s
`PUMP_DURATION_HARD_CAP_SECONDS` re-clip (1585-1590), the §15 consumables interlock, media debit
and waste credit, dilution boundaries for the growth service (§7.3), maintenance-mode
coalescing, `vialNN_pump_log.csv` rows and `experiment_event` emission (`_execute_pump_actions`),
heater safety and fault latching, `state.json` persistence and crash-resume, the sensor
dropped-read streaks, and the alert funnel (CLAUDE.md fact 4).

### The five places that are closed and must be opened

| # | Location | Today |
|---|---|---|
| 1 | `experiment_engine.py:73` `SUPPORTED_MODES` | frozenset of three; gate at `:739` returns HTTP 400 |
| 2 | `experiment_engine.py:2993` `_build_controllers` | `if` chain, `raise ValueError` at `:3006` |
| 3 | `experiment_engine.py:71` `ControllerType` | union of three classes |
| 4 | `experiment_engine.py:231` `validate_control_parameters` | `if mode in (...) / elif mode ==`; an unknown mode silently gets only the two global checks |
| 5 | `experiment_engine.py:1734` `status()` | `isinstance` chain for `target` / `avg_od` / `last_pump_age_s` |
| 6 | `frontend/templates/index.html:2213` mode cards, `:4216` allow-list, `:2253` params panes, `:4455` review rows, `:4485` payload builder |

(4) and (5) are the ones that matter. (5) should be converted from `isinstance` to `getattr`
duck-typing as part of this work, so this is the last mode that has to touch it.

---

## 3. Proposed design

### 3.1 It is a mode, not a special case

`mode: "custom"`. The user's file compiles into a `CustomController` per vial that satisfies
the protocol in §2. No new dispatch path, no second engine, no changes downstream of
`decide()`.

### 3.2 The user-facing function

```python
# ---------- USER DEFINED FIELDS ----------
LOWER = [0.20] * 16
UPPER = [0.40] * 16
PUMP_WAIT_MIN = 15
# ---------- END USER DEFINED FIELDS ----------

import math

def control(ctx):
    """Called once per vial per OD tick (60 s). Return a decision or None."""
    if ctx.n_samples < 8:                      # legacy warmup gate
        return None
    avg = ctx.mean_od(5)
    if avg is None:
        return None

    target = ctx.state.get("target", UPPER[ctx.vial])
    if avg > UPPER[ctx.vial]:
        target = LOWER[ctx.vial]
    if avg < (LOWER[ctx.vial] + UPPER[ctx.vial]) / 2:
        target = UPPER[ctx.vial]
    ctx.state["target"] = target

    if avg <= target:
        return None
    if ctx.time_since_last_pump < PUMP_WAIT_MIN * 60:
        return None

    return ctx.dilute(to_od=LOWER[ctx.vial], average_od=avg)
```

That is the shipped turbidostat, written as a user script. It is also the strongest
acceptance test for the whole feature — see §6.

**Per-vial, not whole-rig.** The legacy `test()` looped over vials itself. Per-vial is the
right primary contract here because the engine's fault latching, consumables gating,
`requires_od` suspension and `state.json` restore are all *per vial*; a whole-rig function
would have to be un-picked back into per-vial results anyway. For cross-vial logic, allow a
second, optional entry point (§7, phase 3):

```python
def control_all(run):   # -> {vial: decision_or_None}
```

implemented by a coordinator that runs once per tick and hands each per-vial
`CustomController` its pre-computed decision, so the per-vial objects — and everything that
hangs off them — survive intact. Ship `control(ctx)` first.

### 3.3 The context object

Read-only snapshots taken under the engine lock. Tuples and floats, never live deques or
numpy views into engine state, and **never** the engine, `SerialManager` or `DataLogger`.

```
ctx.vial                       int
ctx.now                        float   engine clock, seconds
ctx.elapsed_hours              float   since inoculation
ctx.od                         float | None    this tick's calibrated OD
ctx.od_history                 tuple[(t, od), ...]   engine-owned timestamped history (§7.2)
ctx.n_samples                  int     valid samples seen this run
ctx.temperature_c              float | None
ctx.growth                     dict | None   {mu_per_hour, doubling_time_min, r_squared,
                                               regime, flags} — the §17 service's output
ctx.last_pump_time             float | None
ctx.time_since_last_pump       float   +inf if never
ctx.params                     dict    the experiment's `parameters` block (a copy)
ctx.volume_ml                  float
ctx.flow_rate_influx_ml_s      float
ctx.flow_rate_efflux_ml_s      float
ctx.state                      dict    free-form, persisted, restored on resume
ctx.mean_od(n)                 helper  mean of the last n samples, or None
ctx.dilute(...)                factory the ONLY way to build a decision
ctx.emit(type=..., **fields)   -> experiment_event through the funnel
ctx.warn(msg) / ctx.info(msg)  -> alert through the funnel, deduped per vial
```

`ctx.state` is the sanctioned replacement for the legacy's ad-hoc `.txt` files. It
round-trips through `to_state()`/`restore_state()` into `state.json`, which is what makes a
custom controller survive a crash-resume the way the built-ins do — something the legacy
arrangement never had.

`ctx.growth` matters more than it looks: the §17 estimator is per-vial, segment-aware and
dilution-boundary-gated, and it is a great deal better than the 30-minute log-linear fit a
user script would otherwise write by hand (GROWTH_RATE_METHOD.md — fitting `ln(OD)` across
a dilution reads 80–99 % low). Exposing it is most of the value of doing this inside the
engine rather than beside it.

### 3.4 The return value — the one hard rule

A script may not act. It may only return a decision.

```python
return None                                   # do nothing this cycle
return ctx.dilute(seconds=8)                  # influx 8 s, efflux 8 + efflux_extra
return ctx.dilute(ml=6.0)                     # quantised via compute_pump_quantization()
return ctx.dilute(to_od=0.2, average_od=avg)  # solves the SPEC §9 formula for you
```

`ctx.dilute()` returns a `PumpAction`. Because `PumpAction` is already exactly what
`decide()` returns, **nothing downstream of the controller changes at all** — the hard cap,
the interlock, the debit, the pump log, the boundary record and the event all happen as they
do for a turbidostat.

`ctx.dilute()` validates and coerces: rejects non-numerics, negatives and NaN with a message
naming the vial and the script line; applies the same whole-second truncation the firmware
forces (SPEC §16) and, if that truncates to zero, records it as a distinct
`custom_bolus_truncated` event rather than firing nothing silently — that silent-truncation
failure is the legacy `%d` bug. Anything returned that is not a `PumpAction` or `None` is a
script error (§5).

**Temperature and stir are deliberately not writable in v1.** This is the one legacy
capability the design drops, and the reason is specific: `_handle_heater_safety_locked`
(2783) owns `_setpoint_raw` and *steps the target down by 2 °C per cycle on overrun*. A
script that re-asserts its target every cycle would undo that step-down every cycle, and
silently — the safety path would log that it acted while the target never moved. If setpoint
control is wanted later, it needs a per-vial safety ceiling that latches on the first
step-down and clamps every subsequent script request. That ceiling is the acceptance
criterion for the feature, not an optimisation of it. See §7.

### 3.5 Where the script lives

`experiments/{name}/control.py`, written at create time from the request body.

Four reasons, all of them constraints already in the repo:

1. **Deploy is `git checkout <tag>`** (DEPLOY.md — "deploy by tag, never a moving branch").
   A script anywhere in the tracked tree is either clobbered by an update or blocks the
   checkout. `calibration/*.txt` has exactly this problem today and DEPLOY.md §"Installing
   updates" documents it as an open wart. Do not add a second instance of it.
2. **`experiments/*` is already gitignored**, and is the directory the lab backs up.
3. **The exact code that ran is archived with the data it produced.** In the legacy system
   `custom_script.py` was one mutable file shared by every run; six months later there was no
   way to reconstruct what a given experiment actually did. Here, `config.json` records
   `script_sha256` and the file sits beside the CSVs.
4. **Resume needs it.** `resume_on_startup` re-reads the same directory.

`config.json` gains `parameters.script_sha256` and `parameters.script_source`
(`"inline"` or `"library:<name>"`). An optional per-rig `scripts/` directory (gitignored)
holds reusable drafts the wizard can copy from; it is a convenience, never the run's record.

**The file is frozen once the run starts.** It is read at `start_experiment` and at resume,
and not re-read while RUNNING. Hot-editing a live controller produces a run nobody can
reconstruct. To change it: stop, clone, edit, start. (The legacy system had this property by
accident, since the module was imported once at startup; make it deliberate.) If a reload
action is ever added, it must log an event carrying both hashes.

**`data_export.py` needs one line.** The ZIP builder writes an explicit file list
(`data_export.py:770–789`), not the directory, so `control.py` would *not* be included
automatically. Add it beside the `config.json` write at `:785`. Without that, point 3 above
is false and the feature loses most of its scientific value.

### 3.6 Loading

At `start_experiment` and on resume, `_build_custom_controllers`:

```python
source = (exp_dir / "control.py").read_text(encoding="utf-8")
module  = types.ModuleType(f"evolver_custom_{name}")
module.__file__ = str(path)
exec(compile(source, str(path), "exec"), module.__dict__)
```

Compiling with the real path means tracebacks and `SyntaxError` line numbers point at
`control.py`, which is what makes the error surface usable.

Validated at load: exactly one of `control` / `control_all` is defined and callable;
its arity matches; optional `PARAMS` / `describe()` (wizard rendering, §3.7) and optional
`validate(params, vials) -> list[str]` (the script's own create-time check) are callable if
present. `math`, `statistics` and `numpy as np` are pre-bound into the namespace so legacy
scripts read naturally, but a plain `import` works too — this is a real module exec.

**On sandboxing, plainly:** the script runs in-process with full interpreter access. There
is no sandbox and there should not be a pretence of one. RestrictedPython breaks numpy,
and a subprocess boundary costs a serialisation round-trip per vial per tick for no gain
against the actual threat model — the person writing the script already has SSH to the Pi,
and the server has no authentication at all (ROADMAP Session AE, deferred). **The goal is
containment of mistakes, not of malice.** That is §5, and it is the part of this design that
carries real risk.

### 3.7 API and UI

- `SUPPORTED_MODES` gains `"custom"`.
- `POST /api/experiments/create` accepts `params.script` (inline source) or
  `params.script_path` (a name under `scripts/`). The engine writes `control.py`, hashes it,
  and records the hash and source in `config.json`.
- `validate_control_parameters(mode="custom", ...)` — see §4.
- `status()`: a custom controller exposes `target`, `average_od()` and
  `time_since_last_pump()` (target read from `ctx.state["target"]` when the script sets it,
  else `None`), and the `isinstance` chain at `:1734` becomes `getattr`-based.
- Wizard step 5 gains a **Custom** card; step 6 shows a monospace editor
  (a `<textarea>` with a line-numbered gutter — do not vendor CodeMirror; `index.html` is a
  single file whose only third-party assets are a vendored uPlot and socket.io from a CDN),
  prefilled from a template.
  Create-time syntax errors and warnings render inline against their line numbers.
  A script that declares `PARAMS`/`describe()` gets ordinary form inputs for those, so a
  parameterised script can be re-used without editing code.
- Review step shows the script hash and the first ~15 lines.

---

## 4. Create-time validation — a custom mode must not be a validation hole

`validate_control_parameters` exists because of C-3: before it, a band too narrow to produce
a fireable bolus, or a bolus interval below the firmware's resolution, started happily and
delivered nothing for the length of the run. A `"custom"` branch that only does the two
global checks (`efflux_extra_seconds`, positive influx rate) would reopen exactly that hole.

The `"custom"` branch does four things, all at create time so failures are HTTP 400 and land
in the wizard, not in an overnight run:

1. **Compile.** `SyntaxError` → 400 carrying `lineno`, `offset` and `text`.
2. **Structural check.** Entry point present, callable, correct arity.
3. **The script's own check.** If `validate(params, vials)` is defined, call it and fold its
   returned strings into the existing warnings list, so a script can reject its own bad
   parameters in the same channel the built-in modes use.
4. **Dry-run smoke test.** Instantiate the controllers and drive them against a short
   synthetic OD ramp — the same culture model `verify_control_modes.py` already uses — and
   require that:
   - nothing raises;
   - every non-`None` return is a valid `PumpAction`;
   - no vial exceeds the per-vial time budget (§5);
   - `to_state()` output is JSON-serialisable.

   A few hundred simulated cycles costs milliseconds and catches the overwhelming majority
   of what would otherwise be discovered at 03:00 with a culture in the vial.

---

## 5. Containment — the part that carries real risk

`decide()` is called **inside `self._lock`** (an `RLock`) from `run_cycle`, and *both* sensor
lanes take that lock. That single fact determines everything here.

| Failure | Consequence if unmitigated | Mitigation |
|---|---|---|
| **Script raises** | it propagates out of `run_cycle`. `app.py:2336` catches it and raises a critical alert, so the loop survives and the watchdog is still pet — but **the rest of that tick is lost**: heater safety for every vial after the raising one, the growth recompute, the stir re-send, and the `state.json` write. One bad vial silently disarms §10 for the vials above it. | catch inside `CustomController.decide`; return `None`; alert once with a stable `dedup_key`; latch a per-vial `script_error` fault after 3 consecutive failures (`DEFAULT_SENSOR_FAILURE_THRESHOLD`, the same threshold as the sensor streaks) |
| **Script hangs** — infinite loop, `time.sleep`, a blocking read | holds the engine `RLock` → the fast lane blocks → **heater safety stops running** → `watchdog.pet()` (app.py:2394) is never reached → 30 minutes to `emergency_shutdown`, on a rig where the Arduino closes the heater loop itself and `xr=0` requests ~82 °C. The held lock also blocks every Flask thread that calls `engine.status()`, so the dashboard freezes at the same moment — the operator sees a dead UI, not an alert. | **the only genuinely new way this feature can hurt a culture.** See below. |
| **Script mutates engine state** | corrupts OD history or media books | hand it copies: tuples, floats, plain dicts. The engine, manager and logger are not reachable from `ctx`. |
| **Script returns garbage** | a malformed `PumpAction` reaching the pump layer | `ctx.dilute()` is the only constructor and it validates; a non-`PumpAction` return is a script error |
| **`ctx.state` not JSON-serialisable** | `_save_state_locked` throws every OD tick → **resume silently broken** | validate/coerce at the end of each cycle; drop the offending key, alert once, never let it break the state write |
| **Script writes files** | fills the SD card, which `PI3B_HEADED_OS_ASSESSMENT.md` §5.1 already names as what breaks first | documented as forbidden; `ctx.state` and `ctx.emit()` are the sanctioned outputs; `_check_disk_space` already alerts |

### The hang, specifically

Four options, and three of them do not work here:

- **`signal.setitimer` / `SIGALRM`** — Python delivers signals only on the main thread. The
  sensor loop is not the main thread. **Does not work.**
- **Worker thread with `.join(timeout)`** — you learn that it overran, but you cannot kill
  it, and it keeps burning CPU on a Pi 3B. Useful only as an outer measurement.
- **Moving the `decide()` call outside the engine lock** — tempting and useless: the script
  still needs a coherent snapshot, and it is the *sensor-loop thread* that hangs either way,
  so heater safety and the watchdog pet stop regardless. **The fix has to be a time budget,
  not a lock change.**
- **`sys.settrace` deadline hook** — install a trace function for the duration of the call
  that raises `ScriptTimeout` once `time.monotonic() - t0` exceeds the budget (propose
  **0.5 s per vial per cycle**; a 16-vial pass then costs at most 8 s of a 60 s OD tick).
  Costs a 2–5× slowdown of the script, which is irrelevant for code this small.
  **Recommended.**

Its one blind spot: a trace hook cannot interrupt a single long C-level call — one enormous
numpy operation, a socket read. So pair it with the outer measurement: time the whole
custom pass, and if it exceeds a hard ceiling (propose 15 s), latch the run into maintenance
mode and raise a critical alert. Maintenance already exists, already coalesces pending
dilutions per vial, and already has the 30-minute auto-resume failsafe — it is the right
landing place for "the controller is not answering."

Everything else on that table degrades to *no dilution*, which the existing alert and
event paths already surface to the operator.

---

## 6. Testing

Following the conventions already in `server/`:

- **`server/test_custom.py`** — pure-function, no I/O, no mocks, in the style of
  `test_turbidostat.py`: loader accepts/rejects, `ctx` snapshot immutability, `ctx.dilute()`
  coercion and truncation, `state` round-trip through `to_state`/`restore_state`, the
  timeout hook fires, the exception path latches after three failures and not before.
- **`server/verify_control_modes.py`** — add a fourth check: **run the shipped turbidostat
  algorithm as a user script** (the §3.2 listing) against the same simulated culture and
  require its OD trajectory and dilution count to match `TurbidostatController` within
  tolerance. This is the single most valuable test in the set. It proves the `ctx` surface is
  expressive enough to express a mode that already exists, and it fails loudly the first time
  someone changes the ctx contract.
- **`server/test_experiment_engine.py`** — create → start → resume in custom mode;
  `control.py` written and hashed into `config.json`; the file frozen mid-run; state
  round-trips; export ZIP contains the script.
- **Mock-mode run**: a full `--mock` experiment on a custom script, checked for pump events
  in `vialNN_pump_log.csv` and boundaries in the growth service.

---

## 7. Sequencing

| Phase | Content | Effort (ROADMAP vocabulary) |
|---|---|---|
| **1** | `server/control_modes/custom.py` (loader, `CustomController`, `ScriptContext`, timeout); engine registration (1–5 of §2, including the `status()` duck-typing refactor); script written to the experiment dir at create; `data_export` line; `validate_control_parameters` branch with the dry run; tests | **DEEP, 2–3 h** |
| **2** | Wizard mode card, editor pane, inline error surface, `describe()`-driven params, `scripts/` library, review-step hash | **MEDIUM, 60–90 min** |
| **3** | `control_all(run)` whole-rig hook; `ctx.set_temperature` / `ctx.set_stir` **with the latching safety ceiling of §3.4** | **MEDIUM, 90 min** — do not start without the ceiling |

### Interactions with the existing roadmap

- **Session Y (vial groups)** — orthogonal. Once groups exist, `"custom"` is a per-group mode
  like any other with no extra work. Either order is fine.
- **Session Z (multi-phase protocols)** — **partly subsumed.** A custom script can implement
  its own phase transitions in twenty lines. Z should be re-scoped to "phases for the
  built-in modes, for people who do not want to write code," or deferred further.
- **Session Q (templates)** — the `scripts/` library is the same idea; build them together
  if Q is done first.
- **Session S (supervised override)** — SPEC §21's controller-state-coherence requirement
  applies here too: a manual dilution must be pushed into a custom controller's state.
  Since `ctx.state` is opaque to the engine, the mechanism is a documented optional hook,
  `def on_manual_pump(ctx, direction, seconds, ml)`, called through the same path that
  updates `last_pump_time` for the built-ins. Worth reserving the name now.

---

## 8. Summary of the trade

| Legacy `custom_script.py` | This design |
|---|---|
| Arbitrary Python, numpy, one function | unchanged |
| Reads run history off disk with `genfromtxt` | `ctx.od_history`, timestamped, in memory |
| Growth rate: hand-rolled if at all | `ctx.growth` — the §17 segment-aware estimator |
| State in ad-hoc `.txt` files | `ctx.state`, persisted in `state.json`, restored on resume |
| Fires pumps by raw UDP datagram | returns a `PumpAction`; the engine fires it |
| No duration cap, no interlock, no pump log | 30 s hard cap, §15 interlock, `vialNN_pump_log.csv`, growth boundaries, event log — all inherited |
| Sets temperature and stir directly | **not in v1** (§3.4); phase 3, gated on the safety ceiling |
| One shared mutable file for every run | one immutable file per run, hashed into `config.json`, shipped in the export |
| An error kills the loop | caught, alerted, latched per vial |
| A hang kills the loop | trace-hook time budget + maintenance-mode fallback (§5) |

The only capability genuinely surrendered is direct actuation, and that is the capability
every finding in `CONTROL_MODE_AUDIT.md` was downstream of.
