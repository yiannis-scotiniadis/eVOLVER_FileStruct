# Fluidics firmware audit — eVOLVER-001

**Date:** 2026-09-23 · **Rig:** eVOLVER-001 (Boston University EDF 2016 build, Khalil Lab → Yale)
**Method:** direct RS485 probing of the fluidics SAMD21, server stopped, water only
**Status:** investigation complete. All open items closed by operator decision 2026-09-23 (§6).
**No further wet-lab work is required** — the next step is implementation (§4).

---

## 0. What this document is, and how to use it

The 2016 SAMD21 firmware source for this rig **is not available** — not in this repo, not
from the BU Engineering Design Facility that built it, and not in the public eVOLVER
firmware repository (§5 proves this). Everything the codebase believed about fluidics
behaviour was therefore inference from 2016 client code, and some of it was wrong.

This document replaces that inference with measurement. It exists because a pilot run
overflowed vials, and diagnosing that required knowing what the firmware actually does.

**If you are picking this up cold:**

- §2 is the firmware's behaviour, measured. Treat it as ground truth. Do not re-derive it
  from the repo's Python, and do not re-run these tests — they cost bench time and moved
  liquid.
- §3 is what is wrong in the codebase *today* as a result. Several are live bugs.
- §4 is the design that follows, including the key algorithmic result.
- §6 records the open items and why each was closed rather than measured.
- §7 has the reproducible test harness.

**Ground rule carried forward:** label inference as inference. Two claims in `CLAUDE.md`
were inferences that had hardened into facts by repetition, and both turned out false
(§3.1, §3.2).

---

## 1. Why this was investigated

A turbidostat bolus is capped at 20 s, which at the default flow rates is **up to 23 mL in a
single event** — well beyond the 15 mL of free space in a 40 mL vial holding a 25 mL
straw-pinned working volume.
Nothing in the codebase compares a requested influx against the vial's free headroom;
`volume_ml` (default 25.0) is only the `V` in the washout exponential.

But headroom was only half the story. `mac_original/custom_script.py:114-117` fires a
dilution as **one command with the influx and efflux bits OR'd into a single mask**, then a
second command for efflux-alone overrun:

```python
MESSAGE = "%s,0,%d," % ("{0:b}".format(control[x] + control[x+16]), time_in)   # BOTH bits
MESSAGE = "%s,0,%d," % ("{0:b}".format(control[x+16]), time_out)               # efflux alone
```

`SerialManager.pump_command(vial, direction, seconds)` (`server/serial_manager.py:604-629`)
takes a *direction* and can only ever set one bit. So `server/app.py:2113-2162` issues **two**
commands where the legacy issued one. Whether that matters depended entirely on unmeasured
firmware behaviour. It matters a great deal (§3.3).

---

## 2. Measured firmware behaviour

### 2.1 The model, in one paragraph

> The fluidics Arduino accepts `st<mask>,<ignored>,<seconds> !` frames and **queues** them.
> It executes queued frames **one at a time, in order, to completion**. Each frame runs
> **every pump named in its mask simultaneously**, at full speed, for **exactly** the
> commanded whole seconds. Durations are **additive** across frames on the same pump. It
> never preempts, never acknowledges, never reports status, and **cannot be stopped**.

### 2.2 The facts

| # | Fact | Evidence |
|---|---|---|
| **FW-1** | Frames are **queued and executed serially in order**. A frame arriving mid-run waits its turn. | E2, E3, G2, G3 |
| **FW-2** | **One frame drives every masked pump concurrently.** | A2 (2 pumps), A3 (influx+efflux same vial), A4 (4 pumps), A7 (14 pumps) |
| **FW-3** | **No concurrency ceiling up to 14 pumps** — all heads at full speed, no sag, stall or brownout. | A7 |
| **FW-4** | **Durations are exact and unclamped.** 1 s, 10 s and 30 s each ran for exactly that long. No startup lag observed. | F1, F2, F3 |
| **FW-5** | **Durations are additive across frames.** A 1 s command sent during a 20 s run produced 20 s then 1 s, separated by a small pause. | E2 |
| **FW-6** | **Nothing preempts.** Neither a same-pump nor a different-pump frame interrupts a running one. | E2, E3 |
| **FW-7** | **Masks may be zero-padded.** `0000000000000001` behaves as `1`; the parser is not width-sensitive. | A1 |
| **FW-8** | **The middle field is inert.** `st<mask>,0,…`, `,1,…` and `,9,…` behaved identically. *(Observed over a 12 s window only — see §6.4.)* | A6 |
| **FW-9** | **The firmware never transmits.** Zero bytes received in every window, including 90 s after a 14-pump fire, and in response to `re !` both idle and mid-run. No ack, no echo, no handshake. | all of Block A, C1, C2 |
| **FW-10** | **The `stt` sub-mode fires nothing.** Four duration shapes (`[5,0,…]`, `[0,0,5,…]`, `[3,7,0,…]`, all-slots) with both short and 32-char padded masks fired no pump. | D1–D4 |
| **FW-11** | **There is no software stop.** The stop frame does not halt a running pump, does not flush the queue (two queued frames still ran), a zero-duration frame for the running pump does nothing, and a targeted `t`-mode mask does nothing. | E1, E4, E5, E6 |
| **FW-12** | **The command queue holds ≥15 frames / ~270 bytes with zero loss**, buffered *while a pump is running*. The firmware reads serial concurrently with pumping — it is not blocked in `delay()`. | G2 (6 frames), G3 (15 frames) |
| **FW-13** | **No undocumented stop word found.** A single-letter sub-mode sweep (a–z) and every legacy two-letter code (`nc`, `wq`, `pf`, `qe`, `em`, `oq`, `tr`, `cd`) failed to stop a running pump. | G1, code sweep |

### 2.3 Wire format, as confirmed

```
st<mask>,<ignored>,<whole_seconds>, !
```

- `<mask>` is binary, rendered by `format(m,'b')`; **bit 0 is the rightmost character**;
  leading zeros optional (FW-7).
- Pump index → bit: **influx vial N = bit N**, **efflux vial N = bit N+16**. The bit exponent
  is the canonical pump index used throughout this codebase.
- `<ignored>` is always `0` in legacy code and has no observable effect (FW-8).
- Whole seconds only. Sub-second values are meaningless on the wire.

---

## 3. What is wrong in the codebase as a result

### 3.1 `CLAUDE.md`'s protocol table is measurably wrong

It lists `stt,<32-bit mask>,<t0>,…,<t15>, !` as **"Stop / multi-fire."** It is neither
(FW-10, FW-11). The "multi-fire" half was always an inference from the frame's *shape* — a
mask plus sixteen durations looks like it should fire things — and the only legacy use was
`stop_all_pumps`. Nobody had ever observed it doing either.

**Fix:** correct the table; mark the `t` sub-mode as inert on this firmware.

### 3.2 `stop_all_pumps()` is inert, and three safety paths depend on it

`SerialManager.stop_all_pumps()` (`serial_manager.py:631-633`) sends exactly the frame
measured to do nothing. Its callers:

- `emergency_stop` (the dashboard control)
- `watchdog.emergency_shutdown` (`server/watchdog.py:125`)
- `ExperimentEngine._zero_experiment_actuators_locked` (`experiment_engine.py:2948`)

All three currently claim a guarantee the hardware does not provide. **The only real
emergency stop on this rig is physical: cutting power to the auxiliary pump board.** That
procedure is not documented anywhere, including `DEPLOY.md`.

**Fix:** rename/redocument the method as best-effort; correct the UI copy; write the
physical procedure into `DEPLOY.md`; see §4.4 for what the software *can* offer.

### 3.3 Automatic dilutions run influx-then-efflux sequentially — this is the overflow

Compose FW-1, FW-2 and FW-6 with `app.py:2113-2162`:

1. The controller sizes a bolus of up to 20 s of influx.
2. `app.py` sends **influx** as its own frame. It runs to completion.
3. `app.py` sends **efflux** 50 ms later. It **queues**, and starts only after the influx
   finishes.
4. **Nothing drains during the influx phase.** The level rises by the *entire* influx
   volume — up to 23 mL — before a drop leaves the vial.
5. With `efflux_extra_seconds` defaulting to 0.0 (§3.4), the straw never re-pinned the level
   between events either.

The efflux overrun pins working volume back to the straw tip *afterwards*, which is far too
late. **The 2016 system was materially safer than the current server** because its single
OR'd mask really did drain concurrently — a capability the port lost.

**Fix:** restore the combined mask (§4.1).

### 3.4 The experiment wizard has never set `efflux_extra_seconds`

`frontend/templates/index.html:4498-4522` builds the parameter payload without it, so every
wizard-created run silently used the engine default of `0.0`
(`experiment_engine.py:123`) — straw-based volume regulation disabled. Only hand-edited
configs ever had it.

**Fix:** add the field to the Parameters step with a UI default of 2.0 s (the value the
operator established on the bench). Leave `DEFAULT_EFFLUX_EXTRA_SECONDS = 0.0` in the engine
so commit `a7b408a` is not silently reverted.

### 3.5 Waste accounting will break under any multi-frame scheme

`_debit_media_locked` (`experiment_engine.py:2373-2399`) books waste as
`(pump_time + efflux_extra) × F_influx`, with a standing TODO saying the physically correct
model is `waste += influx_ml`. Any scheme that increases total efflux seconds inflates the
waste books proportionally and drives the run into false `consumables` maintenance.

**Fix:** required in the same commit as §4, not as a later cleanup.

### 3.6 There is no notion of vial capacity anywhere

Grep for `overflow|headroom|max_volume|working_volume|liquid_level` across `server/` returns
only prose in docstrings. `volume_ml` is a static scalar used as `V` in dilution formulas and
is never compared to anything.

### 3.7 A live bus-collision bug, from the eVOLVER community

The forum thread **"Dropped fluidic commands"** (<https://www.evolver.bio/t/dropped-fluidic-commands/96>)
reports pump commands lost between server and Arduino, root-caused to OD/temperature traffic
interrupting the pump frame on the shared half-duplex bus — *"the previous broadcast od/temp
data serial comms getting interrupted"*, with the **first command in a batch** most likely to
go.

This is a different failure from anything measured here: every bench test in §2 ran with the
server stopped and a quiet bus. It is a hard constraint on any threaded executor:

> A fluidics frame injected between an `xr`/`we` request and its `temp…end` / `turb…end`
> reply corrupts both — the read NaNs out **and** the pump command is lost. The executor must
> hold the `SerialManager` lock across the whole request-response transaction, not merely the
> write, and should stay out of the OD acquisition window entirely.

This may be contributing to the NaN storms already on record for this rig.

---

## 4. The design that follows

### 4.1 Restore the combined mask

Add a mask-level API to `SerialManager` / `MockSerialManager`:

```python
def pump_mask_command(self, mask: int, seconds: int) -> None   # the general form
def pump_pair_command(self, vial: int, seconds: int) -> None   # (1<<vial) | (1<<(vial+16))
```

`pump_command(vial, direction, seconds)` stays as a thin wrapper so manual pumping and the
calibration wizard are untouched. A dilution then becomes the legacy shape: one combined
frame for `t`, one efflux-only frame for the overrun.

This alone changes peak level rise from `F_in · t` (the entire bolus) to `(F_in − F_out) · t`
(pump mismatch only).

### 4.2 The key result: schedule decomposition

Because one frame drives any mask concurrently (FW-2) and durations are additive and exact
across frames (FW-4, FW-5), **any piecewise-constant pump on/off schedule is expressible as a
sequence of frames.** Split the schedule at every start/stop breakpoint; each interval becomes
one frame whose mask is the set of pumps active during it.

```
Want: efflux0 for 19 s; efflux1 starting at t=2 for 5 s

  [0,2)   {0}     ->  frame  mask{eff0},      2 s
  [2,7)   {0,1}   ->  frame  mask{eff0,eff1}, 5 s
  [7,19)  {0}     ->  frame  mask{eff0},     12 s

  bus time = 19 s = the schedule SPAN, not the sum of durations (24 s)
```

The nested-mask special case — N vials needing different durations — costs
`max(durations)`, not `sum(durations)`:

```
vials A,B,C need 3 s, 5 s, 8 s      frame {A,B,C} 3 s
                                    frame {B,C}   2 s
                                    frame {C}     3 s   -> 8 s total
```

For a 16-vial run this is the difference between fluidics fitting inside the 60 s control
cycle and overrunning it by minutes. **Cost:** one pump start/stop transition per frame
boundary, plus whatever inter-frame gap the firmware imposes — both unmeasured (§6.1, §6.2).

### 4.3 Overflow safety — a stateless headroom planner

New module `server/fluidics.py`, pure and I/O-free, no engine imports (same discipline as
`server/growth_rate.py`). It sits **downstream of `decide()`**, alongside the consumables
interlock and media debit, so all three control modes *and* the planned custom controller
(`CUSTOM_CONTROLLER_DESIGN.md` §2) inherit it for free.

```
headroom_ml  = vial_capacity_ml − volume_ml − overflow_margin_ml
peak_rate    = F_in − efflux_credit          # credit 0 unless pump calibration is real
chunk_max_s  = floor(headroom_ml / peak_rate)
n_chunks     = ceil(requested_influx_s / chunk_max_s)
drain_s(c)   = ceil(c × (F_in / F_out) × (1 + pessimism))   # + efflux_extra on the last
```

**Two new config keys** in experiment `parameters`, both scalar-or-16 via the existing
`_as_list_of_16` (`experiment_engine.py:182-196`):

- `vial_capacity_ml` — default `None` = protection disabled, with create-time and start-time
  warnings. The wizard always supplies it.
- `overflow_margin_ml` — default `2.0`.

`volume_ml` keeps its meaning as the working volume and stays scalar. A per-vial `volume_ml`
is a larger change (three builders plus all three controllers) and is a deliberate follow-up
— **do not** add a second `working_volume_ml` key, which would create two sources of truth
for one number.

**Rig geometry, set by the operator (2026-09-23):**

| Constant | Value |
|---|---|
| Vial capacity | **40 mL**, all 16 |
| Working volume, straw-pinned | **25 mL** |
| Dead space / headroom | **15 mL** |
| Efflux overrun in use | **2.0 s** |

Straw height is an **operator-set parameter, not a per-vial measured constant** — straws are
cut to a common target, so a scalar `volume_ml` is correct and per-vial arrays are
unnecessary. Re-cutting a straw means updating `volume_ml` to match.

With a 2 mL margin that gives **13 mL of usable headroom**, and under zero efflux credit:

| F_in (mL/s) | `chunk_max_s` | Frames for a 20 s bolus |
|---|---|---|
| 0.85 | 15 s | 2 |
| 1.00 | 13 s | 2 |
| 1.15 | 11 s | 2 |

**The worst case is two frames**, not the three-plus assumed before the geometry was known.

> **The efflux-credit trap.** Whenever the 32 flow rates are broadcast from a 16-value array
> — the default, and also the result of any calibration that measured influx pumps only —
> **`F_efflux` is a literal copy of `F_influx`.** Anything that credits efflux at face value
> then computes a mismatch of exactly zero, and does so *because the two numbers are not
> independent measurements*. Default the credit to **zero**. Crediting efflux requires a
> calibration carrying real, distinct rates for pump indices 16–31; with it, the peak collapses
> to `(F_in − F_out) · t` and splitting stops being needed at all.

> **On `CLAUDE.md`'s "do not compute a balancing efflux duration in software":** that warning
> is about an *exact* balance `t_efflux = (F_in·t_in)/F_out`, where whole-second truncation is
> the same size as the correction and quantises it away. The drain window above is
> deliberately an **over**-estimate — pessimism factor above 1, rounded with `ceil`, never
> `int()`. Every rounding moves toward more efflux, and the straw absorbs the excess.

### 4.4 The stop story

There is no software stop (FW-11, FW-13), and none exists in the published successor firmware
family either beyond "command zero" (§5.2). What the software *can* offer:

**Bounded-latency stop.** Exposure equals the duration of whatever frame is already on the
wire. Issue each bolus as a train of short frames and let "stop" mean *stop enqueuing*:

| Max frame duration | Worst-case stop latency | Frames per 20 s bolus |
|---|---|---|
| 20 s (today) | 20 s | 1 |
| 5 s | 5 s | 4 |
| 2 s | 2 s | 10 |

Not free — every extra frame is another start/stop transition, and the resulting volume bias
is unmeasured (§6.1). Until it is, prefer ~5 s over 1 s.

**Keep the firmware queue at depth ≤ 1.** Since frames queue and never preempt (FW-1, FW-6),
anything already sent is unstoppable *in aggregate*. The executor must hold work in **its own**
queue and dispatch one frame at a time, waiting out each duration. Cancelling the server-side
queue is then a near-real stop with a bounded residual of one frame. FW-12 says the firmware
would happily buffer 15+ frames — the point of knowing that is to stay **below** it, not to
use it.

**The real fix is hardware.** A relay or solid-state contactor on the auxiliary pump board's
supply rail, driven from a spare Pi GPIO, de-energizes all 32 pumps regardless of firmware
state — and finally gives `watchdog.py` something real to act on. A physical E-stop button in
series with the same rail covers the case where the Pi is wedged, which given this rig's
NetworkManager and Tailscale history is not hypothetical. Best arrangement is both.

### 4.5 Executor requirements

- Dedicated thread. **Not** the sensor loop — that carries heater safety on a 10 s lane and
  must never block (`CLAUDE.md` fact 7).
- Dispatch one frame at a time; firmware queue depth ≤ 1.
- **Hold the `SerialManager` lock across whole transactions** and avoid the OD acquisition
  window (§3.7).
- Cancel on emergency stop, watchdog trip, heater-safety park, `stop_experiment`,
  `enter_maintenance`. With no hardware stop, queue cancellation *is* the stop.
- One delivery in flight per vial; `run_cycle` skips `decide()` for a vial already delivering.
  Both built-in modes absorb this without change — the chemostat sizes from actual elapsed
  time (capped at 4 intervals), the turbidostat re-derives an absolute correction from fresh OD.
- Persist in-flight deliveries to `state.json`; on resume **do not continue them** (hours-stale
  actions over-dilute, same reasoning as `_pending_pump_actions` at `experiment_engine.py:3406-3409`).
  Queue one normalisation efflux per affected vial instead.

### 4.6 Other integration points

- `_record_dilution_events_locked` (`experiment_engine.py:2162`) — `t_efflux_end` must span the
  whole multi-frame delivery. Fitting `ln(OD)` across a dilution reads 80–99 % low, and a
  decomposed event spans several OD samples.
- `app.py:_execute_pump_actions` — keep **exactly two `pump_log.csv` rows per dilution event**
  (influx total, efflux total). The CSV has no schema marker and the lab's own parsers are
  positional; per-frame rows would break them silently. Frame detail goes to `events.csv` only.
- `validate_control_parameters` (`experiment_engine.py:233`) — add to the **mode-independent**
  block so every future mode inherits: raise if `capacity <= volume + margin`; raise if
  `headroom / F_in < 1.0`; warn when capacity is absent; warn with predicted frame count and
  per-cycle bus time; warn when pump calibration is uncalibrated.
- `mock_serial_manager.py` — mirror the mask API. Its washout physics needs no change:
  `∏ exp(−F·cᵢ/V) = exp(−F·Σcᵢ/V)`, so a decomposed delivery is mathematically identical to a
  single one. Verify with a test rather than assume.

### 4.7 Verification

The load-bearing test is a **simulation, not an assertion about the formula**: sweep
`(F_in, F_out, headroom, requested)` over a grid, integrate vial level second-by-second
through the generated frame sequence, and assert the level never exceeds capacity — with
`F_out` swept down to 0.4× nominal to model the uncalibrated efflux pump this design must
survive. Also assert `Σ frame_influx == requested` (decomposition must not change dose).

Then `python -m pytest` from the repo root, `python server/app.py --mock` end to end, and a
water-only bench run comparing measured peak level against the planner's prediction.

---

## 5. Firmware provenance

### 5.1 The source is not published

`FYNCH-BIO/evolver-arduino` (MIT) is the official Arduino repo. Checked 2026-09-23:

| Ref | Fluidics address | Verdict |
|---|---|---|
| `master` | `String address = "pump"` | later protocol |
| tags `v1.0`, `v1.0.1` (earliest) | `"pump"` | later protocol |
| pre-purge commit `4d68b09`, `SAMD21/Rev A` and `Rev B` | `"pump"` | later protocol |
| fork `pbert5/evolver-arduino`, all branches | same history | later protocol |

The repo's **initial commit is 2019-01-03** and already uses the successor protocol:
`pumpi`/`pumpr`/`pumpa` frames, per-pump millisecond values, 48 pumps, the `evolver_si`
parsing library. Our `st`/`xr`/`zv`/`we` generation predates it and was never committed.

A commit titled *"Removing revisions - only 1 version of code"* (`182fb56`, 2020-11-30)
collapsed the `Rev A`/`Rev B` folders; its parent `4d68b09` still has both, and neither is
our vintage.

### 5.2 What the successor firmware does tell us

`SAMD21/Rev A/RS485_FLUIDICS.ino` was read in full. Three things carry over:

**It has a documented stop** — line 3 of the file:

```
/// TURN OFF ALL PUMPS WITH: pumpr,0,0,0,...,0,_!     (48 zeros)
```

It works because `setPump()` is called **unconditionally** on any new command, with no
"already running?" check:

```cpp
void setPump(float timeToPumpSet, int pumpIntervalSet) {
  timeToPump = timeToPumpSet * 1000;
  pumpInterval = pumpIntervalSet * 1000;
  previousMillis = millis();
  pumpRunning = true;
  Tlc.set(LEFT_PWM, addr, speedset[1]);
}
```

With zeros, the next `update()` — which runs every `loop()` pass — finds
`currentMillis - previousMillis > 0` and switches the pump off. `pumpInterval == 0` suppresses
the restart branch. The pump stops within about a millisecond.

**Why that does not transfer.** The successor's `loop()` is fully non-blocking and has **no
command queue** — `savedInputs[48]` is a single pending slot the next command overwrites. Ours
queues (the pre-purge tree ships `libraries/QueueList`, a FIFO linked list) and runs each frame
to completion before applying the next. That is exactly why a zero-duration frame failed on our
rig (E5): it was *appended*, not *applied*.

**Two markers that date ours as older:** the successor uses a two-phase handshake
(`pumpi`/`pumpr` saved → echoed as `pumpe,<48 values>end` → executes on `pumpa`), added per the
forum thread to make delivery verifiable; ours executes a single frame immediately and never
replies (FW-9). And the successor addresses 48 pumps with per-pump millisecond values against
our 32 with a shared whole-second duration.

### 5.3 Consequence

Do not plan on firmware modification. There is no source to modify, no known-good backup of
what is flashed on the four boards, and the published successor's stop mechanism depends on an
architecture ours does not have. If anyone ever wants to pursue it, do it on a spare SAMD21
and keep the original boards untouched.

---

## 6. Open items — all closed by decision, no wet-lab work remains

The operator closed this list on **2026-09-23**. Nothing below blocks implementation.

### 6.1 Inter-frame gap — **deliberately not measured**

Each frame boundary carries a small pause (FW-5) and a pump start/stop transient. Neither was
quantified. **Executive decision: proceed without compensating for either**, accepting the
resulting bias in delivered volume.

What this means in practice:

- Schedule decomposition (§4.2) and the frame-duration cap (§4.4) both ship without a gap
  correction term. Do not add one speculatively.
- The bias scales with **frame count**, so it is largest exactly where slicing is most
  aggressive. Keep frame counts low — with the rig's 13 mL headroom the worst case is two
  frames per dilution, so this is a small effect.
- **The detector already exists.** Post-run mass reconciliation
  (`POST /api/experiments/{name}/reconcile`, SPEC §19.4) compares inferred against actual
  consumption. If it starts showing a systematic shortfall that grows with frame count, this
  is the first thing to measure — one 20 s frame against ten 2 s frames, gravimetric.

### 6.2 Schedule decomposition validation — **deferred to the mock and reconciliation**

The decomposition rests on FW-2 (concurrent masks), FW-4 (exact durations) and FW-5
(additivity), all measured directly. End-to-end bench validation was judged unnecessary given
those three. It is covered instead by the simulation test in §4.7 and, in production, by mass
reconciliation.

### 6.3 Remaining stop candidates — **closed, not pursued**

Five untried frame shapes remain (all-ones mask with zero duration, the `stc` sub-mode with
zeros, and three format variants). E5 is strong evidence against all of them: a zero-duration
frame for the *running* pump did nothing, which means the firmware does not apply commands on
arrival at all — it dequeues them. No vocabulary defeats a queue.

**Design accordingly: there is no software stop** (§4.4). If someone later wants to spend ten
minutes on the lottery ticket, the frames are listed in the git history of this file.

### 6.4 Middle field — **accepted as inert**

FW-8 rests on a 12 s observation window, and the successor firmware's two-parameter
`setPump(timeToPump, pumpInterval)` raises the possibility that the middle field is a repeat
interval. Not pursued: probing it risks starting a recurring pump that **cannot be stopped**.

Keep sending `0`, as the 2016 client always did. Never send anything else in that position.

### 6.5 Vial geometry — **set, not measured**

Capacity 40 mL, straw-pinned working volume 25 mL, 2.0 s overrun (§4.3). These are operator
settings rather than bench measurements, and that is the intended design: the straw is cut to
a target and the config is told what the target is.

## 7. Reproducing this

### 7.1 Setup

```bash
ssh pi@192.168.1.2
sudo systemctl stop evolver
sudo fuser /dev/ttyAMA0                      # must print nothing
cd /home/pi/evolver-gui
.venv/bin/python -c "import serial;s=serial.Serial('/dev/ttyAMA0',9600,timeout=1);s.write(b'xr'+b'4095,'*16+b' !')"
```

> ⚠ **The heater convention is inverted.** `xr` is a closed-loop setpoint the Arduino drives
> the thermistor ADC toward, and the calibration slope is negative. **4095 is off; 0 requests
> ~82 °C.** Never send a low `xr` value.

Rig **both** the influx and efflux line of every vial under test into **one beaker of water**.
Liquid recirculates, nothing transfers, nothing can overflow, and no pump runs dry. Prime each
line first. Teardown is `sudo systemctl start evolver`.

### 7.2 Harness

```python
import serial, time
s = serial.Serial('/dev/ttyAMA0', 9600, timeout=0.2)

def send(body, prefix='st', listen=12):
    frame = (prefix + body + ' !').encode('ascii')
    s.reset_input_buffer(); t0 = time.monotonic()
    s.write(frame); s.flush()
    print('  0.00  TX %r' % frame)
    while time.monotonic() - t0 < listen:
        r = s.read(64)
        if r: print('%6.2f  RX %r' % (time.monotonic() - t0, r))
    print('  ---- %.0fs window closed ----' % listen)

def fire_then(second, delay=5.0, first=b'st1,0,20, !', listen=30):
    s.reset_input_buffer(); t0 = time.monotonic()
    s.write(first); s.flush(); print('  0.00  TX %r' % first)
    time.sleep(delay)
    s.write(second); s.flush(); print('%6.2f  TX %r' % (time.monotonic()-t0, second))
    while time.monotonic() - t0 < listen:
        r = s.read(64)
        if r: print('%6.2f  RX %r' % (time.monotonic()-t0, r))

def frame(mask, sec):
    return b'st' + mask + b',0,' + str(int(sec)).encode() + b', !'

def seq(frames):
    s.reset_input_buffer()
    for f in frames:
        s.write(f); s.flush(); print('TX %r' % f); time.sleep(0.05)
```

`server/capture_fluidics_ack.py` is also on the Pi and does per-byte timestamped capture with
burst grouping — use it when a reply is ambiguous. (Its premise, that the fluidics Arduino
emits stray acks, is disproved by FW-9.)

### 7.3 Mask reference

```python
def mask(*pumps):          # 0..15 influx, 16..31 efflux
    m = 0
    for p in pumps: m |= 1 << p
    return format(m, 'b')
```

| Mask | Pumps |
|---|---|
| `1` | vial 0 influx |
| `11` | vials 0,1 influx |
| `1111` | vials 0–3 influx |
| `10000000000000000` | vial 0 efflux |
| `10000000000000001` | vial 0 influx + efflux |
| `110000000000000000` | vials 0,1 efflux |
| `11110000000000001111` | vials 0–3 influx + efflux |
| `0111111011111111` | influx for vials 0–7 and 9–14 (14 pumps) — the 2016 script's mask |

---

## 8. Rig quirks a new session needs

- **Heater convention inverted.** `xr` 4095 = off, 0 ≈ 82 °C, slope negative.
- **Vial 0's temperature channel is bad** — raw ADC ~700 vs ~553 on the other 15, ~5.7 °C
  cold. Do not culture in vial 0 until bench-verified. Irrelevant to fluidics.
- **Check whether the installed pump calibration carries distinct efflux rates** for pump
  indices 16–31 before crediting efflux anywhere. See the efflux-credit trap in §4.3.
- **Vial geometry: 40 mL capacity, 25 mL straw-pinned working volume, 2.0 s efflux overrun.**
- **Remote access is Tailscale and the relay path goes stale** — status reads `active` while
  ping times out; only a Pi-side `tailscale ping` revives it. A wedged NetworkManager presents
  as "SSID could not be found" with every health check clean; fix is
  `sudo systemctl restart NetworkManager`, tried **before** any reflash.
- **`journalctl` prints local time; CSVs are UTC.** Use `journalctl --utc` when correlating.

## 9. Related documents

| Path | What |
|---|---|
| `CLAUDE.md` | hardware facts and serial protocol — **§3.1 correction pending** |
| `SPEC.md` §9, §15, §16, §16.2 | control modes, consumables interlock, volume-based fluidics, "volume regulation is a hardware loop" |
| `CONTROL_MODE_AUDIT.md` | prior line-by-line audit; finding X-1 is the `efflux_extra_seconds` decision |
| `CUSTOM_CONTROLLER_DESIGN.md` §2 | the duck-typed controller seam this layer sits below |
| `DEPLOY.md` | operator runbook — **needs the physical emergency-stop procedure (§3.2)** |
| `server/capture_fluidics_ack.py` | per-byte bus capture tool |
