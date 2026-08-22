# CALIBRATION_PROTOCOL.md — eVOLVER calibration and verification

**Status:** bench procedures ready to run. **Part II is IMPLEMENTED (2026-08-20,
`ROADMAP.md` Session O)** — the provenance store, per-run OD blank, pump gravimetric
wizard, reconciliation, guards and staleness all exist in `server/calibration_service.py`
+ `/api/calibration/*` + the GUI's Calibration tab; only the Tier 3 wizards (§8) and the
dedicated Tier 0 / §5.1–§5.3 screens are still pending (Session AA; run sheet meanwhile).
No Tier 1 or Tier 2 procedure has yet been **run on the bench** — the audit findings in §1
remain live until one is.
**Applies to:** the 2016 BU/EDF eVOLVER, 16 sleeves, driven by the Flask server in `server/`.
**Companion documents:** `SPEC.md` §19 and §25, `ROADMAP.md` Session O / AA / AC, `CLAUDE.md` §Calibration.

---

## 0. How to use this document

Part I is the bench SOP. It is written to be followed by someone who did not write the
software. Part II specifies the calibration wizard that should eventually drive Part I from
the browser; until that exists, Part I is run by hand with a notebook and the existing
`/api/sensors/*` and `/api/actuators/*` routes.

Everything is organised in **tiers by cadence**, because "per-run calibration" is only
meaningful once you decide what is *not* per-run:

| Tier | Cadence | Duration | Contents |
|---|---|---|---|
| **0 — Pre-flight** | Every run | ~10 min | Function and safety checks. No numbers are changed. |
| **1 — Per-run** | Every run | ~25 min + 5 min post-run | OD dark and blank; pump spot-check; temperature verification; post-run mass reconciliation. |
| **2 — Per-campaign** | Monthly, or after 40 h cumulative pump time, or after any tubing change | ~60 min | Full 32-pump gravimetric calibration. |
| **3 — Foundational** | Annually, after hardware work, or when Tier 1 keeps failing | ~4–6 h | Two-point thermistor calibration; OD dilution series (all four sigmoid parameters); stir PWM→RPM. |

Read §1 before running anything. The inherited calibration constants have never been
verified, and the audit in §1 changes what the per-run procedure should do.

---

## 1. What the audit of the inherited constants found

`calibration/OD_cal.txt` and `calibration/temp_calibration.txt` came from a previous user
and have never been checked (`SPEC.md` §14 Q3). I evaluated them numerically. Four findings
shape the protocol below; full tables are in Appendix A.

### 1.1 The machine currently reports OD ≈ 0.12–0.44 for a sterile blank

Feeding a plausible blank signal (~58 000 counts) through the current curves returns a
non-zero OD on every vial, ranging from **+0.115 (vial 10) to +0.444 (vial 1)** — a
**0.33 OD spread across sleeves at zero cells**. Any turbidostat threshold set today is
being compared against a number carrying a large, vial-specific, unmeasured offset.

This is the single strongest argument for a per-run blank, and it sets the per-run
procedure's job: **remove the offset**.

### 1.2 Overwriting rows 0 and 1 with measured dark/blank readings would break the curve

`ROADMAP.md` Session O2 and `SPEC.md` §19.2 both specify the per-run blank as *"update rows
0 and 1 of the OD calibration"*. **Do not implement it that way.** Rows 0 and 1 are not
measurements — they are the fitted asymptotes of a four-parameter logistic. Two concrete
consequences:

- **Row 1 sits far above any signal the hardware produces.** The fitted upper asymptotes run
  81 938 → 503 890, i.e. **1.4× to 8.7× the observed signal level** (~58 000 counts, per the
  example `turb` response in `CLAUDE.md`). It is an extrapolation, not a reading — consistent
  with a 16-bit ADC that cannot exceed 65 535, though the argument does not depend on the bit
  depth. A measured blank is a point *on* the curve at OD ≈ 0; the model places the asymptote
  at OD → −∞. They are not the same quantity.
- `serial_manager._read_od_enhanced_locked` uses rows 0 and 1 as the **validity domain**:
  `in_domain = (corrected > mn) & (corrected < mx)`. Setting `mx` to the measured blank makes
  every reading at or above the blank fail the guard and return `NaN`. Early in a run, OD sits
  at the blank, so roughly half of all early samples would be discarded by noise alone — and
  `experiment_engine` would start emitting "OD out of calibrated range" alerts on every vial.

Substituting measured values into rows 0/1 moves reported OD at 50 000 counts from
**+0.66 to −6.62** on vial 0, and to negative values on all 16. §5.4 gives the correct
procedure and Part II §10.1 gives the software correction.

### 1.3 Vial 0's temperature calibration is a 5-sigma outlier and is probably wrong

Measured against the other fifteen vials, vial 0's intercept (86.493 vs a pack of 80.66–83.20)
is **+5.9 SD** out and its slope is **+5.4 SD** out; on the outlier-resistant median/MAD
measure both are **4.5** out. Consequence: to request 37 °C the software sends `xr = 482` to
vial 0 and `xr ≈ 403` to everything else. **If vial 0's thermistor actually behaves like its
neighbours, commanding 37 °C is landing it at ≈ 28 °C while the dashboard reads 37.0 °C.**

Note that the remaining fifteen agree tightly with each other (±1.5 °C at a common setpoint),
which is itself evidence that vial 0 is a bad *fit* rather than a genuinely different sleeve.

This is a 20-minute test: put the reference thermometer in vial 0, command 37 °C, and compare
(§5.3). Do it before the next run, independently of everything else here.

### 1.4 Optical sensitivity varies four-fold across sleeves

Counts per 0.01 OD at OD 0.3 range from **92 (vial 1) to 375 (vial 9)**. At an assumed
±200 count read noise that is ±0.022 OD on vial 1 versus ±0.005 OD on vial 9. Vial 1 is
additionally compromised: its fitted lower asymptote (44 262) sits inside the normal working
signal range, so its curve diverges above OD ≈ 1 — at a signal of 45 000 counts it reports
**OD ≈ 3.2–3.7** where its neighbours report 0.3–0.8 for the same signal, and no per-run blank
will fix that.

**Practical consequence:** vial 1 should not carry a condition whose result depends on
absolute OD, and per-vial OD noise should be reported rather than assumed uniform. Tier 3
recalibration is the fix; until then this is a known, documented limitation.

---

## 2. Principles

These are the rules that make the rest of the document consistent. They are worth reading
once even if you skip to the procedures.

1. **Calibrate in the state you will run in.** Same vial type, same fill volume, same media,
   same sleeve position, same stir PWM, thermally equilibrated. A blank taken with the stirrer
   off is not a blank for a run with the stirrer at 8.
2. **A per-run calibration corrects offset, not shape.** The dilution series buys you the
   shape (the Hill coefficient and the inflection point); the blank re-anchors it. If the
   shape has changed, no blank will save you — that is a Tier 3 trigger, not a Tier 1 fix.
3. **Never overwrite a calibration file in place.** Write a new versioned file, keep the old
   one, and record which version an experiment used in its `config.json`. A plot whose
   calibration cannot be reconstructed is not reproducible.
4. **Per-run artefacts live with the run.** Blanks belong in `experiments/{name}/`, not in
   `calibration/`. They describe one run's vials, not the instrument.
5. **Record the conditions, not just the numbers.** LED power, stir PWM, setpoint, ambient
   temperature, media lot, operator, timestamp. A calibration without its conditions cannot
   be compared to the next one.
6. **A failed acceptance check is a finding, not an obstacle.** Record it and either exclude
   the vial or escalate a tier. Do not adjust the criterion to make it pass.
7. **Verification is not calibration.** Tier 0 and most of Tier 1 confirm the machine still
   behaves as its stored constants claim. Only Tier 1's OD blank, and Tiers 2–3, change a
   number.

> ⚠ **Heater convention — read before touching temperature.** `xr` is **not** a PWM. It is a
> setpoint that the Arduino's closed loop drives the thermistor ADC toward, and the
> calibration slope is **negative**: **lower `xr` = hotter**. `xr = 0` requests ≈ 82 °C with the
> heater pinned on. `xr = 4095` is the only definitive off. Never type a raw setpoint you
> have not converted through the per-vial calibration, and never assume 0 means off.

---

## 3. Prerequisites — do these once, before any of the tiers are meaningful

### P1. Resolve the vial-to-sleeve physical mapping

`SPEC.md` §14 Q2 is still open. Every per-vial constant in this document is attached to a
logical index; if index 7 is not the sleeve you think it is, the entire calibration set is
scrambled and no acceptance criterion will make sense.

**Procedure (~15 min, no risk):** with the machine idle and no vials loaded, set stir to a
single non-zero vial at a time (`zv` with one value at 8 and fifteen zeros, or the manual
stir control in the UI) and record which physical sleeve position spins. Work through 0–15.
Photograph the final map and store it as `calibration/vial_map.json`. Use stir rather than
heaters or LEDs: it is unambiguous, visible, and cannot damage anything.

### P2. Confirm the heaters actually turn off

`SPEC.md` §14 Q4 (suspected stuck-on MOSFETs) is unresolved, and the note there is important:
the inverted `xr` convention means any historical attempt to "turn heaters off" by sending
`xr = 0` was in fact requesting ≈ 82 °C. Stuck-on behaviour may have been a command bug, not
a hardware fault.

**Procedure (~45 min):** load all 16 sleeves with 25 mL water, stir at run PWM, send
`xr = 4095` to all vials (or use the UI's off control), and log raw thermistor ADC every
minute for 30 minutes. Place the reference thermometer in one vial.

- **Pass:** raw ADC drifts monotonically toward the ambient value and settles; reference probe
  reads room temperature ± 1 °C at the end.
- **Fail:** any vial's temperature rises or holds above ambient. Record which vials, stop, and
  treat it as a hardware fault. Do not run temperature-controlled experiments on those sleeves.

**P2 gates all of Tier 3's temperature work and all temperature-controlled experiments.** OD
and stir work can proceed without it.

### P3. Establish campaign reference values

Several Tier 1 acceptance criteria are stated relative to a *campaign reference* — the values
recorded on the first clean run of a campaign. On the first run, record the values and mark
them as the reference; thereafter compare against them. Where this document gives an absolute
threshold with no reference available, the threshold is provisional and flagged as such.

---

# PART I — Bench SOP

## 4. Tier 0 — Pre-flight verification (every run, ~10 min)

No numbers change. This catches the failures that would otherwise be discovered six hours
into an overnight run.

| # | Check | Method | Pass criterion | On failure |
|---|---|---|---|---|
| 0.1 | Server healthy, correct calibration loaded | `GET /api/calibration/` (or server log line "loaded calibration from …") | Server reports the expected calibration versions and no "calibration files not found" warning | Do not start. Sensor reads fall back to raw ADC silently. |
| 0.2 | No experiment already running | Dashboard status | Status is IDLE | Calibration routes will refuse anyway; resolve first. |
| 0.3 | Serial bus responsive on all 16 | One temperature and one OD read | 16 values returned, no `dropped` flags | Check RS485 wiring; a partially responsive bus corrupts everything downstream. |
| 0.4 | Heaters off at start | Confirm all setpoints at 4095 | All 16 report off | See P2. |
| 0.5 | Stir bars present and turning | Set run PWM, visually inspect all 16 | Every vial shows a visible vortex or a moving bar | Reseat or replace the bar. A stationary bar invalidates that vial's blank *and* its growth. |
| 0.6 | Tubing intact, no kinks, all lines seated | Visual, follow each line in use end to end | No kinks, no disconnections, all fittings tight | Fix before priming. |
| 0.7 | Waste carboy has headroom | Visual / weight | Headroom exceeds the run's projected waste volume | Empty it. Overflow is the most common failure mode on this platform. |
| 0.8 | Media bottle weighed and recorded | Balance | Mass recorded in the run sheet | Needed for §6 reconciliation. |

---

## 5. Tier 1 — Per-run calibration (every run)

Run these in the order given. The order matters: the OD blank must be the **last** thing
before inoculation, taken under final run conditions.

### 5.1 Prime and leak-check the fluidics (~5 min)

1. Place all influx lines in the media bottle and all efflux lines in the waste carboy.
2. Fire each influx line in use for 10 s. Watch the line fill.
3. Fire each efflux line in use for 10 s.
4. Repeat any line still carrying visible air.

**Pass:** every line in use runs bubble-free, no drips at fittings, flow visibly starts within
1 s of the command.
**Fail:** a line that will not clear of bubbles usually means a loose fitting on the suction
side. Air in the line makes the stored flow rate a fiction — fix it before continuing.

### 5.2 Pump spot-check (~8 min)

Full gravimetric calibration is Tier 2. Per-run, verify a **rotating subset of four lines**
against their stored rates, so that all 32 are checked every eight runs. Record which four in
the run sheet.

1. Tare a beaker on the balance.
2. Fire the line for **10 s** into the beaker.
3. Weigh. Convert to volume with the water/media density at bench temperature (Appendix B.3).
4. Compute mL/s and compare with the stored rate for that pump.

**Pass:** measured rate within **±15 %** of the stored rate.
**Marginal (10–15 %):** record and continue; if the same line is marginal twice running,
escalate to Tier 2.
**Fail (>15 %):** escalate that pump's whole manifold to Tier 2 before running. A pump reading
15 % low will over-dilute by 15 %, and every downstream number — bottle level, consumables
interlock, dilution-rate growth estimate — inherits the error.

> Until Tier 2 has been run at least once, **there is no stored rate to compare against** —
> `DEFAULT_FLOW_RATES_ML_PER_SEC` in `experiment_engine.py` is a hardcoded array of plausible
> numbers that has never been measured on this machine. Run Tier 2 before the next
> quantitative experiment.

### 5.3 Temperature verification (~20 min, mostly unattended)

1. Load all vials at final working volume with the run's medium (or water, if the blank is
   being taken separately — but see Principle 1: media colour matters to OD, not to
   temperature, so water is acceptable here only if the vials are then emptied and refilled).
2. Stir at run PWM.
3. Set the run's target temperature via the °C control (never a raw setpoint).
4. Place the reference thermometer in one vial — **rotate which vial each run**, so all 16 are
   checked over time. Record which.
5. Wait for the reported temperature to stabilise (typically 15–20 min), then hold 5 min.

**Pass:**
- Reported °C for the probed vial agrees with the reference thermometer within **±0.5 °C**.
- All 16 reported temperatures are within **±1.0 °C** of the setpoint and stable to
  **±0.2 °C** over the 5-minute hold.
- No vial is an outlier by more than 1 °C from the pack median.

**Fail:** a vial whose reported temperature disagrees with the reference by more than 0.5 °C
has a bad thermistor calibration — exclude it or run Tier 3.1 for that vial.
**Expected finding:** per §1.3, **vial 0 is the prime suspect**. Probe vial 0 on the first run.

### 5.4 OD dark and blank — the core per-run calibration (~10 min)

This is the step that actually changes a number. Take it **last**, under exactly the
conditions the run will use, immediately before inoculation.

**Preconditions (all mandatory):**

- Vials contain the run's **sterile medium**, at final working volume, no cells.
- Sleeves seated as they will be for the run; nothing will be unseated after this point.
- Stir running at the **run's PWM**.
- Temperature equilibrated at the **run's setpoint**, held ≥ 10 min (§5.3 satisfies this).
- Bubbles settled — wait 5 min after any handling.
- LED power set to the run's value (**2125** standard, hard max 2200).

**Procedure:**

1. **Dark read.** LED power 0. Take **5 reads**. Record the median and SD per vial.
2. **Blank read.** LED power at the run value. Take **5 reads**. Record the median and SD per
   vial.
3. Compute the per-vial re-anchored inflection parameter:

   ```
   c_run[v] = log10( (b[v] - a[v]) / (B[v] - a[v]) - 1 ) / d[v]
   ```

   where `a, b, d` are rows 0, 1 and 3 of the current `OD_cal` and `B[v]` is the blank median
   for vial v. **Rows 0, 1 and 3 are not modified.** Only `c` (row 2) changes, and only for
   this run.

   This is exactly equivalent to `OD_reported(S) = OD_legacy(S) − OD_legacy(B)` — a blank
   subtraction in OD units, which preserves the curve shape the dilution series paid for.
   Derivation in Appendix B.1; the software correction is Part II §10.1.

4. Write `experiments/{name}/od_blank.json` with the dark medians, blank medians, `c_run`,
   and the full condition block. Record the parent `OD_cal` version.

**Acceptance criteria:**

| Quantity | Criterion | Meaning of a failure |
|---|---|---|
| Dark SD (per vial, 5 reads) | ≤ 150 counts *(provisional)* | Electrical noise, or ambient light leaking into the sleeve. Close the enclosure. |
| Dark median | Within ±500 counts of campaign reference | LED leakage, ambient light, or ADC drift. |
| Blank SD (per vial, 5 reads) | ≤ 300 counts *(provisional)* | Bubbles, a wobbling stir bar, or a poorly seated vial. Worst-vial equivalent ≈ 0.03 OD. |
| Blank median | Within ±10 % of campaign reference | Different vial glass, sleeve seating, media colour, or LED ageing. |
| Implied offset `c_run − c_previous_run` | Within 0.15 OD | Something about the optical path changed between runs. Investigate before trusting the run. |
| Vials passing | ≥ the number of vials the run needs | Reseat and repeat failures once; then exclude the vial. |

> **Thresholds marked provisional** are first-principles estimates derived from the
> sensitivity table in Appendix A.1, not from measured repeatability on this machine. Record
> the actual dark and blank SDs for the first five runs, then replace these numbers with
> mean + 3 SD of what the machine actually does. This is the single highest-value edit to
> make to this document once data exists.

**On dark subtraction.** `serial_manager` supports subtracting the dark read from the signal
before applying the calibration (`dark_subtract=True`). **Leave it off.** The 2016 curve was
fit on non-dark-subtracted signal, there is no `OD_cal.meta.json` sidecar asserting otherwise,
and mixing the two produces silently wrong OD — the code logs a warning to that effect. The
blank re-anchoring in step 3 absorbs any constant dark offset anyway. Use the dark read as a
**diagnostic**, not as a correction, until Tier 3.2 produces a dark-subtracted curve and its
sidecar. When that happens, dark subtraction and the per-run blank must both switch over
together; a dark-subtracted curve with a non-dark-subtracted blank is as wrong as the reverse.

### 5.5 Record and start

Before starting the run, confirm `config.json` records: `OD_cal` version, `temp_calibration`
version, `pump_calibration` version, the per-run blank file, LED power, stir PWM, and the
operator. If the run cannot say which calibration produced its numbers, its numbers are not
publishable.

---

## 6. Tier 1 (post-run) — mass reconciliation (~5 min)

Cheap, and it is the only check that validates the entire open-loop volume chain end to end.
All volume tracking in this system is inferred as `duration × flow_rate`, accumulated; it
cannot see a kinked line, a stalled pump, or a bottle someone topped up (`SPEC.md` §14 Q8).

1. Weigh the media bottle and the waste carboy at the end of the run.
2. Convert the mass deltas to volumes using media density.
3. Compare with the software's accumulated pumped volume for that bottle.

**Pass:** agreement within **±10 %**.
**Fail:** the flow-rate array is stale, a line is partially occluded, or a pump is slipping.
Escalate to Tier 2 and treat the run's volume-derived quantities — dilution rate, dilution-based
growth rate, consumables forecasts — as unreliable.

Log the reconciliation ratio each run. A slow drift over weeks is peristaltic tubing wear and
is exactly the signal that should trigger Tier 2 recalibration.

---

## 7. Tier 2 — Full gravimetric pump calibration (~60 min)

**Trigger:** monthly, or after 40 h cumulative pump-on time on any line *(provisional — set the
real number once §6 reconciliation has produced a few months of drift data)*, or after any
tubing change, or on any Tier 1 §5.2 / §6 failure.

**Equipment:** analytical or top-pan balance (0.01 g resolution is ample — see Appendix B.3),
beaker, thermometer for bench temperature, the calibration fluid (see note).

**Fluid choice.** Calibrate in water for reproducibility, unless the medium is appreciably
more viscous or denser than water — high-sugar YPD, glycerol-containing media, anything with
> 5 % w/v solutes. In that case calibrate in the actual medium and record which fluid was used
in the calibration file. Peristaltic pumps are close to volumetric and largely insensitive to
viscosity, but the density correction from mass to volume is not optional either way.

**Procedure, per pump (32 total: 16 influx, 16 efflux):**

1. Prime the line until bubble-free.
2. Tare the receiving vessel on the balance.
3. Fire the pump for exactly **20 s**.
4. Weigh; record mass in grams.
5. Repeat for **3 replicates**.
6. Rate = mean mass / density / 20 s, in mL/s.

For **efflux** pumps, the receiving vessel is the waste side: place the efflux inlet in a
source beaker of water and weigh the receiving vessel, or weigh the source beaker and use the
mass loss. Either works; be consistent and record which, since a partially drained line
biases the first method and not the second.

**Acceptance criteria:**

| Quantity | Criterion | On failure |
|---|---|---|
| Replicate CV (3 reps) | ≤ 5 % | Re-prime and repeat. Persistent scatter means air ingress or a worn tube. |
| Rate vs previous calibration | Within 15 % | Not necessarily a fault — tubing wears. Record the delta; a line that has drifted > 15 % twice in a row should be re-tubed. |
| Absolute rate | Non-zero, and within a factor of 2 of the manifold median | A pump an order of magnitude off is stalled or mis-addressed. Check the binary address mapping before blaming the pump. |
| Monotonic response *(if multiple durations are tested)* | Volume increases with duration | Non-monotonic means a stalling pump — reject the fit. |

**Batching.** 32 pumps is about an hour and nobody will do it in one sitting. The session must
be resumable: record per-pump results as you go, and note which pumps remain. The software
wizard (Part II) makes this explicit.

**Output:** a new versioned `calibration/pump/…json`, and the engine reads the rates from
`config["calibration"]["pump_flow_rates"]` (see `experiment_engine._resolve_flow_rates`, which
prefers `parameters` → `calibration` → hardcoded defaults).

> **✅ Resolved 2026-08-20:** the engine now consumes 32 values. `_resolve_flow_rates`
> resolves through `_as_flow_rates_32` (scalar → 32, length-16 → both directions,
> length-32 → as-is), the three controllers carry per-direction rates, and a complete
> pump calibration's rates are written into each new experiment's
> `config["calibration"]["pump_flow_rates"]` at creation. Ordering is the canonical pump
> index in `CLAUDE.md`: `0..15` influx, `16..31` efflux. A partial (spot-check) session
> merges over the previous version's rates; only a complete 32 feeds the engine.

---

## 8. Tier 3 — Foundational recalibration (~4–6 h, annually or on trigger)

### 8.1 Two-point thermistor calibration

**Gated by prerequisite P2.** Do not run this until the heaters are confirmed controllable.

The efficient trick here is that **ambient is a free 16-way simultaneous calibration point**:
with all heaters off and stir running, every vial equilibrates to the same room temperature,
so a single reference reading serves all sixteen. Only the hot point needs the probe moved
vial to vial.

**Procedure:**

1. Fill all 16 vials with 25 mL water. Stir at a mid PWM. Heaters off (`xr = 4095`).
2. Equilibrate ≥ 45 min. Confirm equilibrium by raw ADC stability, not by the clock: all 16
   raw values stable to ±2 counts over 10 min.
3. **Cold point.** Record the reference thermometer reading (in a vial, not in air) and all 16
   raw ADC values simultaneously. This gives point 1 for every vial at once.
4. **Hot point.** Command a target near the top of the working range — **40 °C**, not higher;
   `MAX_SAFE_TEMP_C` is 45 °C and there is no reason to approach it. Equilibrate 30 min.
5. Move the reference probe vial to vial. At each: insert, allow 2 min to settle, record the
   reference °C and that vial's raw ADC simultaneously. ~35 min for all 16.
6. Fit per vial: `slope = (T_hot − T_cold) / (raw_hot − raw_cold)`, `intercept = T_cold − slope × raw_cold`.

**Acceptance criteria:**

- Two points span **≥ 15 °C**. A narrower span makes the slope badly conditioned.
- Fitted slope within **±10 %** of the pack median (expect ≈ −0.11).
- Fitted intercept within **±2 °C** of the pack median (expect ≈ 82).
- Back-substituting either point reproduces the reference reading to **< 0.2 °C** (trivially
  true for a two-point fit — the real check is the next line).
- **Independent verification:** after installing the new calibration, command an intermediate
  target (e.g. 30 °C) and confirm the reference thermometer agrees within **0.5 °C** on at
  least four vials. A two-point fit that fails in the middle means the thermistor response is
  not linear over the range and you need a third point.

A vial failing the slope or intercept criterion should be flagged in the calibration file
rather than silently accepted — that is how §1.3's vial 0 problem survived since 2016.

### 8.2 OD dilution series — all four sigmoid parameters

**Equipment:** benchtop spectrophotometer (the reference for true OD600), the run's medium,
a dense culture or a turbidity standard.

**Procedure:**

1. Prepare a dilution series in the run's medium spanning the intended working range with at
   least **8 points**, log-spaced, from blank to above the highest OD the experiment will
   reach — e.g. 0, 0.05, 0.1, 0.2, 0.4, 0.8, 1.2, 1.6, 2.0.
   Use a **non-growing** turbidity source (killed cells, heat-fixed cells, or polystyrene
   beads) so the series does not drift during the two hours it takes to measure 16 vials.
2. For each point: read true OD600 on the spectrophotometer **immediately** before loading;
   load into the vial; stir at run PWM; equilibrate at run temperature; wait 5 min; record the
   eVOLVER raw signal (5 reads, median) **and** the dark read (5 reads, median).
3. Fit the four-parameter logistic per vial to (raw, true OD600) pairs.
4. **Fit on dark-subtracted signal** and write `calibration/OD_cal.meta.json` with
   `{"dark_subtracted": true}` — this is the migration point that lets
   `serial_manager`'s dark-subtraction path be enabled correctly (see §5.4).

**Acceptance criteria:**

- **R² ≥ 0.99** per vial against true OD600.
- Residual **< 0.02 OD** or 5 % of the value, whichever is larger, at every point.
- Fitted lower asymptote `a` **below the lowest measured signal** by a clear margin. Vial 1's
  current fit violates this (§1.4) and that is precisely why it misbehaves.
- Fitted upper asymptote `b` reported honestly as an extrapolation if it exceeds 65 535, and
  flagged in the file. The domain guard depends on it.
- Inflection point `c` **inside the measured range**. A `c` of −3.58 (vial 0 today) means the
  fit is extrapolating its own centre.
- Curve **monotonic** across the working range.

Refuse to install a fit that fails R² or monotonicity without an explicit, recorded override.

### 8.3 Stir PWM → RPM

**Equipment gap.** This needs a tachometer, a strobe, or a high-frame-rate camera, none of
which are currently on hand. The cheapest route is a **phone at 240 fps** with a white paint
dot on one end of the stir bar; count rotations frame by frame. A basic optical tachometer is
also inexpensive. Until one is available this tier stays deferred and stir remains reported as
a raw PWM.

**Procedure when equipment is available:**

1. Vials at working volume with water, sleeves seated as for a run.
2. Per vial, measure RPM at **PWM 4, 7, 10, 13** — four points, interpolate the rest
   (`SPEC.md` §25).
3. Record 3 measurements per point; take the median.
4. Store as `calibration/stir_calibration.json`, per vial, versioned per §2 rule 3.

**Acceptance:** monotonic RPM with PWM per vial; replicate spread < 5 %; report the vial-to-vial
spread at each PWM, which is the actual finding of interest — stir bars couple inconsistently
across sleeves and this is a plausible source of unexplained variance in growth experiments.

**Why bother:** "600 RPM" is reportable in a methods section; "stir setting 8" is not.

---

## 9. Run sheet

Print one per run. Fields marked ⟨ref⟩ establish the campaign reference on the first run.

```
eVOLVER RUN SHEET                    Run name: ______________________
Operator: ____________  Date: __________  Start time: ______
Media: ________________  Lot: __________  Bench temp: ______ °C
Calibration versions in use:  OD ____________  temp ____________  pump ____________
Vials in use: ______________________  LED power: ______  Stir PWM: ______  Target: ______ °C

TIER 0  0.1 ☐  0.2 ☐  0.3 ☐  0.4 ☐  0.5 ☐  0.6 ☐  0.7 ☐  0.8 media bottle mass: ______ g

TIER 1
 5.1 Prime / leak check ....................... ☐ pass  ☐ fail: __________________
 5.2 Pump spot-check — lines checked: ____ ____ ____ ____
     measured mL/s: ______ ______ ______ ______   vs stored: within 15 %? ☐
 5.3 Temperature — probed vial: ____  reference: ______ °C  reported: ______ °C
     all 16 within ±1.0 °C of setpoint? ☐   stable to ±0.2 °C over 5 min? ☐
 5.4 OD dark/blank    dark SD max: ______ ⟨ref⟩   blank SD max: ______ ⟨ref⟩
     blank medians within ±10 % of reference? ☐   vials excluded: ______________
     od_blank.json written? ☐
 5.5 config.json records all calibration versions? ☐

POST-RUN
 6.  End media mass: ______ g   End waste mass: ______ g
     Inferred pumped volume (software): ______ mL   Measured: ______ mL
     Ratio: ______   within ±10 %? ☐

Notes / deviations: _________________________________________________
Signed: ______________________
```

---

# PART II — Software: the calibration wizard

Implementation brief for `ROADMAP.md` Session O, extending `SPEC.md` §6 and §19. The design
goal is that **every step in Part I has exactly one screen and one endpoint**, so the SOP and
the software cannot drift apart.

## 10. Corrections to the existing spec

### 10.1 The per-run blank must re-anchor `c`, not overwrite rows 0 and 1

`ROADMAP.md` Session O2 and `SPEC.md` §19.2 both say the per-run blank should *"update rows 0
and 1 of the OD calibration"*. Per §1.2 this is wrong and would be actively harmful. Replace
with:

```python
def reanchor_od_calibration(od_cal: np.ndarray, blank_raw: np.ndarray) -> np.ndarray:
    """Return a copy of od_cal whose row 2 (inflection OD) is shifted so that
    each vial reports OD == 0.0 at its measured blank signal.

    Rows 0 (lower asymptote), 1 (upper asymptote) and 3 (Hill coefficient) are
    NOT modified: they are fitted parameters of the logistic, not measurements,
    and row 1 exceeds the 16-bit ADC ceiling on every vial. Rows 0 and 1 are
    also the validity domain used by SerialManager._read_od_enhanced_locked;
    changing them changes which readings are accepted.

    Equivalent to OD_new(S) = OD_old(S) - OD_old(blank) — a blank subtraction
    in OD units, preserving the curve shape from the dilution-series fit.
    """
    a, b, _c, d = od_cal
    if np.any(blank_raw <= a) or np.any(blank_raw >= b):
        raise ValueError("blank signal outside the calibration domain (a, b)")
    c_run = np.log10((b - a) / (blank_raw - a) - 1.0) / d
    out = od_cal.copy()
    out[2] = c_run
    return out
```

Three properties worth asserting in tests:

- `OD(blank_raw) == 0.0` for every vial, to within floating-point tolerance.
- Rows 0, 1 and 3 are bitwise unchanged.
- `OD_new(S) - OD_old(S)` is a **constant** per vial for all valid `S` (it is a pure vertical
  shift). This is the property that proves shape is preserved.

The dark read is recorded but **not** subtracted while the installed curve lacks a
`dark_subtracted: true` sidecar. See the dark-subtraction coherence guard in §13.

### 10.2 `SPEC.md` §6 endpoint list needs the blank response corrected

The documented response `{"status": "ok", "raw": [...16], "updated_rows": [0] | [1]}` should
become `"updated_rows": [2]`, and should return the computed `c_run` and the implied per-vial
OD offset — the offset is the number the operator actually needs to see, because it tells them
how wrong the previous run was.

## 11. Data model

### 11.1 Common envelope

Every calibration artefact — global or per-run — uses one envelope:

```json
{
  "schema": "evolver.calibration/1",
  "subsystem": "od | temperature | pump | stir | od_blank",
  "version": "2026-08-16T14:22:03Z",
  "supersedes": "2026-06-01T09:14:55Z",
  "operator": "yiannis",
  "source": "gravimetric-20s-x3",
  "conditions": {
    "bench_temp_c": 22.1,
    "fluid": "water",
    "fluid_density_g_ml": 0.99777,
    "led_power": 2125,
    "stir_pwm": 8,
    "target_temp_c": 37.0,
    "vial_map_version": "2026-08-10T…"
  },
  "data":  { "raw measurements, per vial or per pump" },
  "fit":   { "derived parameters" },
  "qc":    { "passed": true, "warnings": [], "failures": [], "overridden_by": null }
}
```

`conditions` is not optional decoration — it is what makes two calibrations comparable
(Principle 5). Reject a write with an empty `conditions` block.

### 11.2 Layout

```
calibration/
  vial_map.json                       # prerequisite P1
  current.json                        # pointer: subsystem -> version filename
  od/       2026-08-16T142203Z.json
  temperature/ 2026-08-16T…json
  pump/     2026-08-16T…json
  stir/     2026-08-16T…json
  OD_cal.txt                          # legacy view, regenerated from current, never hand-edited
  OD_cal.meta.json                    # {"dark_subtracted": bool, "version": "..."}
  temp_calibration.txt                # legacy view, regenerated from current
  _sessions/pump-2026-08-16.json      # in-progress, resumable

experiments/{name}/
  od_blank.json                       # per-run; envelope with subsystem "od_blank"
  config.json                         # records every version used
```

**Back-compatibility:** `SerialManager.load_calibration()` takes file paths and expects the
two `.txt` shapes. Keep writing them, generated from `current.json`, so nothing in the serial
layer needs to change. The `.txt` files become a derived view; the JSON is the source of truth.
Retain every previous version — they are small.

### 11.3 `config.json` provenance block

```json
"calibration": {
  "od": "2026-08-16T142203Z",
  "temperature": "2026-06-01T091455Z",
  "pump": "2026-08-14T110300Z",
  "stir": null,
  "od_blank": "experiments/pichia-ale-03/od_blank.json",
  "pump_flow_rates": [ ...32 ],
  "vial_map": "2026-08-10T…"
}
```

`pump_flow_rates` is a **flat 32-element array** ordered by the canonical pump index defined
in `CLAUDE.md` ("Pump command format"): indices `0..15` are the influx pumps for vials 0–15,
indices `16..31` are the efflux pumps for the same vials. Array index equals the exponent in
the hardware binary address, so the software index and the wire address cannot diverge.

> **✅ Correction resolved 2026-08-20.** This section previously flagged that
> `_resolve_flow_rates` coerced through `_as_list_of_16` and would reject 32 values.
> Session O3a landed the plumbing: `_resolve_flow_rates` is 32-aware, the three
> controllers carry per-direction rates (`flow_rate_influx_ml_s` /
> `flow_rate_efflux_ml_s`, dilution timing on influx only), and the 32/16/scalar cases
> are covered by tests. Populating this block IS now the integration, and the server
> populates it automatically at experiment creation when a complete pump calibration is
> current.
>
> The 16 hardcoded defaults are still applied to **both** directions until Tier 2 runs on
> the bench, so the system continues to assume influx and efflux move identical volumes
> per second. They are separate pumps and generally do not. Establishing the
> per-direction difference is one of the things this Tier 2 calibration exists to
> measure — see `SPEC.md` §16.1–§16.2 for what follows from it, including why the answer
> is *not* to compute a balancing efflux duration in software.

## 12. Endpoints

All under `/api/calibration/*`. All reject with **409** while an experiment is RUNNING, except
the read-only ones. These are the **only** routes permitted to reach raw actuator paths.

> **Implementation status (2026-08-20):** everything below is built except the Tier 3
> blocks (`temperature/*`, `od/series/*`, `stir/*` — Session AA). As built: the blank
> routes operate on the loaded CREATED experiment (no experiment name in the body); QC
> refusals return **422** carrying the qc block and are overridable by re-posting with an
> `override_reason`; `GET /api/calibration/pump/session` reports `{"active": false}`
> rather than 404 when idle.

```
GET  /api/calibration/                      index: per subsystem — current version, age,
                                            staleness state, qc summary
GET  /api/calibration/{subsystem}           current values + fit + conditions
GET  /api/calibration/history               all versions, for provenance
GET  /api/calibration/staleness             what is overdue and why  (§13)

--- per-run OD blank (Tier 1 §5.4) ---
POST /api/calibration/od/blank/start        {experiment, led_power, stir_pwm, target_temp_c,
                                             n_samples: 5}
                                            → validates preconditions, returns a session id
POST /api/calibration/od/blank/dark         → fires n reads at LED 0; returns per-vial
                                              median, sd, n_valid
POST /api/calibration/od/blank/measure      → fires n reads at led_power; same shape
POST /api/calibration/od/blank/commit       → computes c_run, runs acceptance checks,
                                              writes experiments/{name}/od_blank.json
                                            → {"updated_rows": [2], "c_run": [...16],
                                               "od_offset_removed": [...16], "qc": {...}}
POST /api/calibration/od/blank/abort

--- pump gravimetric (Tier 2 §7) ---
POST /api/calibration/pump/start            {pumps: [...], fire_seconds: 20, replicates: 3,
                                             fluid, fluid_density_g_ml, bench_temp_c}
POST /api/calibration/pump/fire             {pump_id} → fires once, returns actual duration
POST /api/calibration/pump/record           {pump_id, replicate, mass_g}
GET  /api/calibration/pump/session          → progress; which pumps remain  (resumability)
POST /api/calibration/pump/finish           → fit, QC, write versioned file
POST /api/calibration/pump/abort

--- thermistor two-point (Tier 3 §8.1) ---
POST /api/calibration/temperature/start     {points: ["cold","hot"], target_hot_c: 40}
POST /api/calibration/temperature/point     {label, reference_c, vials: [...]}
                                            → captures raw ADC for the listed vials now
POST /api/calibration/temperature/finish    → per-vial linear fit + residuals + outlier flags
POST /api/calibration/temperature/abort

--- OD dilution series (Tier 3 §8.2) ---
POST /api/calibration/od/series/start       {points_expected, dark_subtracted: true}
POST /api/calibration/od/series/point       {true_od600, vials: [...]} → captures raw + dark
POST /api/calibration/od/series/finish      → 4-param logistic fit per vial, R², residuals
POST /api/calibration/od/series/abort

--- stir (Tier 3 §8.3) ---
POST /api/calibration/stir/record           {vial, pwm, rpm}
POST /api/calibration/stir/finish           → interpolation table per vial

--- raw escape hatches, calibration-only ---
POST /api/calibration/raw/temperature       {setpoints: [...16]}   (wraps set_temperature_raw)
POST /api/calibration/raw/od_led            {power: 0..2200}       (needs building)
```

`POST /api/actuators/temperature/raw` used to sit on the normal actuator surface; it has
been **moved** to `POST /api/calibration/raw/temperature` (and `raw/od_led` built), so
ordinary operation cannot reach a raw heater setpoint — this matters more than usual given
the inverted convention. Both raw routes 409 while an experiment is RUNNING.

## 13. Guards

Each of these corresponds to a way the protocol can be silently violated.

| Guard | Rule | Rationale |
|---|---|---|
| Experiment running | All mutating calibration routes → 409 | `SPEC.md` §19.4 |
| Condition match | Blank commit rejected if `led_power` or `stir_pwm` differ from the values the experiment will run with | Principle 1. A blank at a different LED power is not a blank. |
| Thermal settling | Blank commit rejected unless the temperature has been within ±0.3 °C of setpoint for ≥ 10 min | §5.4 preconditions, enforced rather than trusted |
| Domain | Blank commit rejected if any `B[v] ≤ a[v]` or `≥ b[v]` | The re-anchor formula is undefined outside the domain |
| Dark-subtraction coherence | Reject enabling `dark_subtract` unless the installed curve's sidecar says `dark_subtracted: true`; reject a blank taken dark-subtracted against a curve that is not | §5.4. Currently a log warning; should be a hard error. |
| Fit quality | Refuse to save a pump fit that is non-monotonic or has replicate CV > 5 %; refuse an OD fit with R² below 0.99; refuse a thermistor fit spanning < 15 °C — each overridable only with an explicit `override_reason` recorded in `qc.overridden_by` | `SPEC.md` §19.4 |
| Immutability | Writing a calibration never overwrites an existing version file | Principle 3 |
| Provenance | An experiment cannot start unless `config.json` names a version for every subsystem it uses | Principle 3 |
| Vial map | Any calibration write records `vial_map` version; warn loudly if it is null | Prerequisite P1 |

**Staleness surfacing** (`GET /api/calibration/staleness`, and a dashboard banner):

| Subsystem | Stale when | Note |
|---|---|---|
| Pump | > 30 days, **or** > 40 h cumulative pump-on time since calibration, **or** last reconciliation ratio outside ±10 % | Pump-seconds are already in the pump log — sum them; this is the wear signal §6 is designed to catch |
| OD blank | Not taken for the current experiment | Hard block, not a warning |
| OD curve | > 365 days, or never verified against a spectrophotometer | Today: **never verified** |
| Temperature | > 365 days, or any vial flagged as an outlier fit | Today: **never verified**, vial 0 flagged |
| Stir | Absent | Today: absent |

Until Tier 2 has run at least once, bottle levels and every volume-derived quantity must carry
the **"uncalibrated estimate"** label already specified in `ROADMAP.md` §K / `SPEC.md` §15.

## 14. Wizard screens

One screen per SOP step. The screen shows the SOP text for that step, so the operator does not
need the printed document in hand — but the run sheet stays, because a signed paper record is
worth something a database row is not.

> **Implementation status (2026-08-20):** the §5.4 OD blank, §7 pump grid (which also
> serves the §5.2 spot-check by selecting the four due lines), §6 reconciliation, and the
> staleness banner are built in the GUI's Calibration tab. The dedicated Tier 0
> checklist, §5.1 prime, §5.2 rotation state, and §5.3 temperature-verification screens
> were deliberately deferred — those steps stay on the printed run sheet, driven by the
> existing manual controls. Tier 3 screens are Session AA.

| SOP step | Screen | Endpoint(s) | Writes |
|---|---|---|---|
| §4 Tier 0 | Pre-flight checklist — live status for 0.1–0.4, manual tick for 0.5–0.8 | `GET /api/calibration/`, existing sensor routes | run sheet entry |
| §5.1 Prime | Prime & leak — fire-each-line buttons, per-line ✓/✗ | `POST /api/calibration/pump/fire` | session log |
| §5.2 Spot-check | Shows the four due lines (rotation state persisted); fire, enter mass, live pass/fail vs stored rate | `pump/fire`, `pump/record` | spot-check log; escalation flag |
| §5.3 Temp verify | Live 16-vial temperature strip, stability indicator, reference-reading entry field, rotation state for which vial to probe | existing sensor routes | verification record |
| §5.4 OD blank | 3 sub-steps: dark → blank → review. Review screen shows per-vial blank median, SD, implied OD offset removed, delta from campaign reference, and pass/fail per criterion in §5.4 | `od/blank/start` → `dark` → `measure` → `commit` | `experiments/{name}/od_blank.json` |
| §5.5 | Provenance summary shown in the experiment wizard's Review step | — | `config.json` |
| §6 Post-run | Reconciliation — enter end masses, shows inferred vs measured and the ratio | new: `POST /api/experiments/{name}/reconcile` | reconciliation record; staleness input |
| §7 Tier 2 | Pump grid: 32 tiles, each showing state (pending / firing / awaiting mass / done / failed) and the replicate values. Resumable across sessions. | `pump/*` | `calibration/pump/…json` |
| §8.1 Tier 3 temp | Cold point (all 16 at once) → hot point (vial-by-vial, probe prompt) → fit review with residuals and outlier flags | `temperature/*` | `calibration/temperature/…json` |
| §8.2 Tier 3 OD | Dilution series: per point, enter spectrophotometer OD600 → capture → repeat; fit review with per-vial R² and residual plot | `od/series/*` | `calibration/od/…json` + `OD_cal.meta.json` |
| §8.3 Tier 3 stir | Per-vial PWM 4/7/10/13, enter measured RPM | `stir/*` | `calibration/stir/…json` |

**Review screens are the point.** For each calibration the operator must see, before
committing: the new value, the previous value, the delta, and which acceptance criteria passed.
A wizard that just says "done" reproduces exactly the situation that let §1.3's vial 0 sit
unnoticed for a decade.

## 15. Verification checklist

Extends the checklist in `ROADMAP.md` Session O. Ticked items are verified by the test
suite (`test_calibration_service.py`, `test_calibration_api.py`,
`test_experiment_engine.py`, `test_serial_manager.py`) as of 2026-08-20; unticked items
are bench work or Tier 3 software.

**Correctness**

- [x] `reanchor_od_calibration` yields `OD(blank) == 0.0` for all 16 vials
- [x] Rows 0, 1, 3 bitwise unchanged by a blank commit; only row 2 differs
- [x] `OD_new(S) − OD_old(S)` is constant in `S` per vial (shape preservation)
- [x] Blank commit rejected when any `B[v]` falls outside `(a[v], b[v])`
- [x] A blank taken at a different LED power or stir PWM than the run is rejected
- [x] Pump wizard writes per-pump mL/s; engine consumes them via `calibration.pump_flow_rates`
- [ ] Measured flow reproduces within 15 % on a repeat run *(bench — Tier 2 not yet run)*
- [ ] Thermistor fit reproduces an intermediate reference temperature within 0.5 °C *(bench, Tier 3)*
- [ ] Thermistor fit flags vial 0 as an outlier if it remains one *(Tier 3; the legacy
      import already carries vial 0's 5-sigma flag in its qc warnings)*

**Provenance and safety**

- [x] No calibration write ever overwrites an existing version file
- [x] `.txt` legacy views regenerate correctly from `current.json` and load in `SerialManager`
- [x] Experiment `config.json` records a version for every subsystem in use
- [x] All mutating calibration routes return 409 during a RUNNING experiment
- [x] Raw temperature and raw LED routes are unreachable from the normal actuator surface
- [x] Enabling `dark_subtract` against a curve without the sidecar is a hard error, not a warning
- [x] Bottle levels drop the "uncalibrated estimate" label only once a real pump calibration exists
- [x] Pump-seconds accumulate per line and drive the staleness warning
- [x] A stale calibration produces a visible dashboard banner, not just an API field

**Resumability**

- [x] Pump session survives a server restart mid-calibration and resumes with the right pumps pending
- [x] Aborting any wizard leaves no partial file in `calibration/`

## 16. Suggested build order

Items 1–5 shipped 2026-08-20 (ROADMAP Session O); item 6 remains (Session AA).

1. ✅ **Envelope, versioning, `current.json`, legacy `.txt` regeneration, provenance in `config.json`.**
   Nothing else is safe to build first — it is the part that makes every later calibration
   reconstructable.
2. ✅ **Per-run OD blank** (§5.4 / §10.1). Highest accuracy return per line of code: it removes a
   0.12–0.44 OD error that is present in every reading today.
3. ✅ **Pump gravimetric wizard** (§7). Unblocks the consumables interlock, volume-based pumping,
   and the dilution-rate growth estimator.
4. ✅ **Post-run reconciliation** (§6). Small, and it is the only thing that will tell you when
   step 3 has gone stale.
5. ✅ **Staleness surfacing** (§13).
6. **Tier 3 wizards** — after prerequisite P2 is resolved.

---

# Appendices

## Appendix A — Audit of the inherited constants

Computed from `calibration/OD_cal.txt` and `calibration/temp_calibration.txt` as committed.
Blank-dependent columns assume a blank signal of 58 000 counts, chosen to match the example
`turb` response in `CLAUDE.md` (57 711, 58 056, 55 568, …). These are illustrative of scale,
not measurements — the point is the spread across vials, which is scale-independent.

### A.1 OD calibration, per vial

| Vial | a (row 0) | b (row 1) | c (row 2) | d (row 3) | OD reported at blank | Practical OD ceiling | counts per 0.01 OD @ OD 0.3 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 17 531 | 456 270 | −3.582 | −0.259 | **+0.253** | 8.12 | 188 |
| 1 | 44 262 | 218 820 | −2.208 | −0.403 | **+0.444** | 3.04 | **92** |
| 2 | 36 408 | 93 067 | −0.062 | −0.695 | +0.241 | 2.41 | 180 |
| 3 | 26 344 | 134 490 | −0.379 | −0.725 | +0.149 | 2.66 | 288 |
| 4 | 25 819 | 174 400 | −0.719 | −0.664 | +0.121 | 2.86 | 286 |
| 5 | 23 229 | 136 720 | −0.435 | −0.614 | +0.144 | 3.31 | 278 |
| 6 | 15 595 | 453 750 | −2.057 | −0.443 | +0.133 | 4.92 | 302 |
| 7 | 30 952 | 106 060 | −0.137 | −0.729 | +0.206 | 2.51 | 238 |
| 8 | 31 207 | 101 200 | −0.073 | −0.810 | +0.183 | 2.27 | 251 |
| 9 | 20 503 | 107 910 | −0.017 | −0.919 | **+0.118** | 2.40 | **375** |
| 10 | 20 720 | 132 120 | −0.280 | −0.756 | **+0.115** | 2.82 | 342 |
| 11 | 29 449 | 116 520 | −0.249 | −0.701 | +0.196 | 2.65 | 249 |
| 12 | 37 769 | 81 938 | +0.163 | −0.976 | +0.238 | **1.73** | 208 |
| 13 | 34 787 | 99 113 | −0.109 | −0.681 | +0.256 | 2.52 | 194 |
| 14 | 15 288 | 503 890 | −2.718 | −0.353 | +0.169 | 6.19 | 257 |
| 15 | 13 109 | 494 060 | −2.480 | −0.375 | +0.152 | 6.07 | 282 |

Column definitions: *OD reported at blank* = what the current curve returns for a 58 000-count
signal. *Practical OD ceiling* = the OD at which the signal reaches 2 % above the fitted lower
asymptote `a` — beyond this the curve is effectively vertical and the vial cannot resolve
further density. *Counts per 0.01 OD* is evaluated at OD 0.3 after blank re-anchoring.

Notes:

- **Every `b` is 1.4×–8.7× the observed signal level** (minimum 81 938, maximum 503 890,
  against readings near 58 000). These are extrapolated asymptotes, not readings, and on a
  16-bit ADC they are unreachable outright.
- **Blank offset spread: 0.115 → 0.444, i.e. 0.329 OD.** This is what §5.4 removes.
- **Vial 1**: `a` = 44 262 sits inside the working signal range; the curve diverges just above
  OD 1. At a signal of 45 000 it reports OD 3.24 where neighbours report 0.3–0.8.
- **Vial 12**: practical ceiling ≈ 1.73 OD — the lowest on the machine.
- **Vials 0, 6, 14, 15** have `|c| > 2` and shallow `|d|`, the signature of a poorly
  constrained fit. They are the first candidates for Tier 3.2.

### A.2 Temperature calibration, per vial

Slope row × 16, intercept row × 16. `T = raw × slope + intercept`.

| Vial | slope | intercept | slope z¹ | intercept z¹ | `xr` for 37 °C |
|---:|---:|---:|---:|---:|---:|
| **0** | **−0.10267** | **86.493** | **+3.16** | **+3.24** | **482** |
| 1 | −0.11112 | 81.779 | −0.14 | −0.30 | 403 |
| 2 | −0.11212 | 82.161 | −0.53 | −0.02 | 403 |
| 3 | −0.11394 | 83.200 | −1.24 | +0.76 | 405 |
| 4 | −0.11000 | 81.310 | +0.30 | −0.66 | 403 |
| 5 | −0.10994 | 81.210 | +0.32 | −0.73 | 402 |
| 6 | −0.11082 | 81.722 | −0.02 | −0.35 | 404 |
| 7 | −0.11276 | 82.484 | −0.78 | +0.23 | 403 |
| 8 | −0.10970 | 82.287 | +0.41 | +0.08 | 413 |
| 9 | −0.11088 | 81.642 | −0.05 | −0.41 | 403 |
| 10 | −0.11312 | 82.636 | −0.92 | +0.34 | 403 |
| 11 | −0.11347 | 82.817 | −1.06 | +0.48 | 404 |
| 12 | −0.10920 | 80.657 | +0.61 | −1.15 | 400 |
| 13 | −0.11023 | 81.060 | +0.21 | −0.85 | 400 |
| 14 | −0.10947 | 80.847 | +0.50 | −1.01 | 401 |
| 15 | −0.11273 | 82.645 | −0.77 | +0.35 | 405 |

¹ z-scores against the full 16-vial distribution, which vial 0 itself inflates. Excluding
vial 0, the pack is slope −0.11130 ± 0.00159 and intercept 81.897 ± 0.778, against which
vial 0 is **+5.4 SD** (slope) and **+5.9 SD** (intercept); on median/MAD, 4.5 on both.

- **Vial 0 is a 5-sigma outlier on both parameters.** If its true response matches the pack,
  the `xr = 482` sent when 37 °C is requested lands it at **≈ 28 °C** while the UI reads 37.0 °C.
- Spread at a common raw setpoint of 420: 34.76 → 43.37 °C (8.6 °C, driven by vial 0; 34.76 →
  36.21 °C, i.e. 1.45 °C, excluding it). Excluding vial 0 the pack is tight, which is itself
  evidence that vial 0 is a bad fit rather than a genuinely different sleeve.

### A.3 What "overwrite rows 0 and 1" would do

Reported OD at a 50 000-count signal, current curve versus the O2-as-written substitution
(`a` ← measured dark ≈ 1 200, `b` ← measured blank ≈ 58 000):

| Vial | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|---|
| now | +0.66 | +1.44 | +0.66 | +0.38 | +0.35 | +0.40 | +0.36 | +0.51 |
| after O2 | −6.62 | −4.16 | −1.19 | −1.46 | −1.90 | −1.71 | −3.83 | −1.21 |

| Vial | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 |
|---|---|---|---|---|---|---|---|---|
| now | +0.46 | +0.30 | +0.31 | +0.48 | +0.59 | +0.64 | +0.45 | +0.40 |
| after O2 | −1.04 | −0.87 | −1.32 | −1.37 | −0.64 | −1.26 | −4.94 | −4.57 |

Plus: with `b` set to the measured blank, `in_domain = (corrected > mn) & (corrected < mx)`
rejects every reading at or above the blank. Early in a run, OD sits at the blank, so noise
alone discards roughly half the samples and `experiment_engine` raises "OD out of calibrated
range" on all 16 vials.

## Appendix B — Derivations and reference values

### B.1 Blank re-anchoring

The installed model, per vial:

```
OD(S) = c − (1/d) · log10[ (b − a)/(S − a) − 1 ]
```

Require `OD(B) = 0` at the measured blank `B`:

```
c_run = (1/d) · log10[ (b − a)/(B − a) − 1 ]
```

Substituting back:

```
OD_run(S) = c_run − (1/d)·log10[(b−a)/(S−a) − 1]
          = (1/d)·log10[(b−a)/(B−a) − 1] − (1/d)·log10[(b−a)/(S−a) − 1]
          = OD_legacy(S) − OD_legacy(B)
```

So re-anchoring `c` is **identically** a blank subtraction in OD units: a rigid vertical shift
that leaves the curve's shape — and therefore the meaning of `d` and the validity domain
`(a, b)` — untouched. This is why it is safe to do every run, and why overwriting the
asymptotes is not.

**Limitation, stated plainly:** this corrects *offset*, not *gain*. If a vial's optical
coupling has changed enough to alter the slope of signal against OD, the blank will still read
0 while every non-zero OD is wrong. The blank-median acceptance window in §5.4 (±10 % of the
campaign reference) is the tripwire for that case, and Tier 3.2 is the fix.

### B.2 Temperature setpoint conversion

```
raw_setpoint = (target_C − intercept[v]) / slope[v]
reported_C   = raw_adc × slope[v] + intercept[v]
```

Slope is negative, so **larger setpoint = colder target**. `xr = 4095` is the off sentinel; it
does not correspond to a physical temperature (back-substituting gives ≈ −370 °C, which is
just the linear fit extrapolated far outside its range — do not display it).

### B.3 Gravimetric conversion

```
flow_rate_mL_s = mass_g / density_g_mL / duration_s
```

Water density (use bench temperature, not 4 °C):

| °C | g/mL | | °C | g/mL |
|---:|---:|---|---:|---:|
| 18 | 0.99860 | | 24 | 0.99730 |
| 20 | 0.99821 | | 26 | 0.99678 |
| 22 | 0.99777 | | 28 | 0.99623 |

Resolution check: at ~1 mL/s a 20 s fire delivers ≈ 19.96 g at 22 °C. A balance reading to
0.01 g resolves 0.0005 mL/s, i.e. **0.05 %** — three orders of magnitude better than the 15 %
acceptance band. Even a 10 s fire on a 0.01 g balance gives 0.1 % resolution. **The balance is
not the limiting factor; priming, line compliance, and tubing wear are.** That is why
replicates and the CV criterion matter more than balance precision.

### B.4 Read-noise sensitivity

Counts per 0.01 OD (Appendix A.1) converts read noise into OD noise. At an assumed ±200 count
SD:

| Vial | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|---|
| ±OD | 0.011 | **0.022** | 0.011 | 0.007 | 0.007 | 0.007 | 0.007 | 0.008 |

| Vial | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 |
|---|---|---|---|---|---|---|---|---|
| ±OD | 0.008 | **0.005** | 0.006 | 0.008 | 0.010 | 0.010 | 0.008 | 0.007 |

The ±200 count figure is an assumption, not a measurement. **Measuring it is free**: the SD
recorded in §5.4 step 2 across 5 blank reads *is* this number. After five runs, replace this
table with the real one, and use it to set per-vial turbidostat thresholds that are actually
resolvable — a threshold separation of 0.02 OD is meaningful on vial 9 and is noise on vial 1.

---

## Open items

1. **Provisional thresholds** in §5.4 need replacing with measured repeatability after ~5 runs.
2. **Prerequisite P2** (heater controllability) gates Tier 3.1 and all temperature-controlled
   experiments. Unresolved since `SPEC.md` §14 Q4.
3. **Prerequisite P1** (vial map) gates the meaning of every per-vial number here.
4. **Stir tachometry equipment** — one inexpensive purchase, or a 240 fps phone, unblocks
   Tier 3.3.
5. **Who owns the bench work** — `SPEC.md` §14 Q11 is still unanswered. Tier 2 is ~1 h,
   Tier 3 is ~5 h.
6. **Turbidity standard for Tier 3.2** — decide between killed cells, heat-fixed cells, or
   polystyrene beads. Beads are the most stable and the least like a real culture; a
   non-growing cell suspension is the better match if it holds for two hours.
7. **Confirm the OD ADC bit depth.** 16-bit is inferred from the example `turb` values in
   `CLAUDE.md` (~58 000), not documented. Check by reading an empty sleeve at LED 2200 and
   seeing where the value clips. It does not change any conclusion here, but it sets the real
   ceiling on the Tier 3.2 fit's upper asymptote.

---

*The four correctness assertions in §15 were checked against the committed `OD_cal.txt`
before this document was written: blank maps to OD 0.000 on all 16 vials, rows 0/1/3 are
bitwise unchanged, the shift is constant in S to 8×10⁻¹⁷, and the domain guard rejects a blank
below vial 1's lower asymptote. Every number in Appendix A is computed from the committed
calibration files rather than quoted.*
