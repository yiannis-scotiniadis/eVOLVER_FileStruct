# Control-mode audit — chemostat & turbidostat

**Date:** 2026-08-20 · **Scope:** `server/control_modes/{chemostat,turbidostat}.py`, engine
integration, SPEC §9 · **Excluded:** morbidostat (by request)
**Method:** line-by-line review + closed-loop numerical simulation driving the repository's own
controller classes unmodified. Repro: `server/verify_control_modes.py`.

## STATUS: FIXED (2026-08-21)

**Every finding below is closed except where noted. The verdict, tables and defect list are
kept as the record of what was wrong and why it mattered — read them as history, not as
current state.**

| ID | Disposition |
|----|-------------|
| T-1 | Fixed — refractory gate evaluated before the formula |
| T-2 | Fixed — moot: there is no accumulator left to reset |
| T-3 | Fixed — accumulator removed; band validated at experiment creation instead |
| T-4 | Fixed — lagged mean for threshold crossing, latest sample for bolus sizing |
| T-5 | Fixed — `server/test_control_loop.py` adds the four closed-loop tests; the six tests that encoded the windup are rewritten |
| C-1 | Fixed — boli sized from elapsed time, clamped to 4 intervals |
| C-2 | Fixed — `controller.requires_od` splits the sensor gate from the control gate |
| C-3 | Fixed — `bolus_interval < 2 s` rejected; `validate_control_parameters` added; cap binding alerts |
| C-4 | Fixed — `total_volume_ml` books delivery, `total_volume_intended_ml` books intent, `boli_fired` / `bolus_cycles` split |
| C-5 | **Partial** — `start_od` / `start_after_seconds` start gate implemented; the washout detector was deliberately deferred |
| X-1 | **Warning only** — the bench decision on `efflux_extra_seconds` is still open; the run now warns at start instead of failing silently |
| X-2 | Fixed — `restore_state(state, now=...)` re-baselines a timestamp restored from the future |
| X-3 | Unchanged — a note, not a defect |

`server/verify_control_modes.py` now exits 0 on all four checks: floor breach 0 % across
every band, D within 0.7 % of requested under every timing and dropout condition tested,
short bolus intervals rejected at construction, booked volume equal to delivered.

**One correction to the analysis below.** The 9 % residual floor undershoot attributed to
T-4 was a harness artifact, not a control defect. The original simulation discarded a fixed
first hour before measuring, but an inoculum at OD 0.2 growing at µ=0.462/h is only at 0.317
after an hour — so for the [0.35, 0.40] band the "floor" being measured was the culture still
climbing toward a band it had not yet reached. With the settling window taken from the first
dilution instead, T-1/T-2/T-3 alone hold the floor in every band, and T-4's isolated
contribution is negligible at a 5-sample, 10 s window. T-4 was still applied: the lag bias is
real and grows with slower loops, longer history windows and faster growth. It is simply not
worth 9 %.

## Verdict

*(as written 2026-08-20, before the fixes)*

Neither mode is safe to publish results from as written; both are fixable with small local
changes. The control *algorithms* are faithful ports of `mac_original/custom_script.py`. The
defects are all in the bookkeeping layered around them.

- **Turbidostat — will not hold setpoint.** The deficit accumulator added by SPEC §9 is an
  integrator with no anti-windup. Every dilution after the first over-doses; measured floor
  violation 10–47% depending on band width. Wide bands mask it completely.
- **Chemostat — delivers the wrong dilution rate.** Bolus volume is derived from the nominal
  10 s interval while the loop runs at `max(10 s, work)`, and the mode is gated on OD validity
  despite being open-loop. Measured 17–34% under-dilution under realistic loop overrun; 100%
  when the OD sensor saturates.

## Findings

| ID | Finding | Mode | Severity | Measured impact |
|----|---------|------|----------|-----------------|
| T-1 | Deficit accumulator winds up during refractory and lag cycles | Turbidostat | **Critical** | up to 2.5× over-dose |
| C-1 | Bolus volume uses nominal interval, not elapsed time | Chemostat | **Critical** | −17% to −34% D |
| C-2 | Open-loop mode is gated on OD validity | Chemostat | **Critical** | up to −100% D |
| T-2 | Deficit never reset when leaving the diluting state | Turbidostat | High | pre-charges next event |
| C-3 | `bolus_interval < 2 s` silently disables dilution forever | Chemostat | High | 0 mL, no warning |
| X-1 | Efflux flow rate validated, stored, never used | Both | High | overflow risk (already tracked, SPEC §16.2) |
| T-3 | Sub-second accumulator unreachable by construction | Turbidostat | Medium | dead mechanism |
| T-4 | Formula applies a lagged mean as if instantaneous | Turbidostat | Medium | −9% floor |
| C-4 | Persisted volume counter books intent, not delivery | Chemostat | Medium | 750 vs 720 mL |
| C-5 | No start-OD gate; washout undetected | Chemostat | Medium | design gap |
| X-2 | Wall-clock gating stalls on backward RTC step | Both | Low | RPi boot risk |
| T-5 | Tests formalise the windup as intended behaviour | Turbidostat | Low | no closed-loop test exists |

### T-1 — Deficit accumulator is an integrator with no anti-windup (Critical)

`turbidostat.py:238–244`; also **SPEC.md §9 lines 717–726** — the design specifies this
ordering, so it is a spec defect, not a coding slip.

`pump_time` is an **absolute correction** (seconds needed to bring the *current* average OD to
`od_lower`), not a per-cycle increment. It is added to a persistent accumulator on every
`decide()` where `average_od > target`, and that addition happens **before** the `pump_wait`
gate — so cycles that fire nothing still charge the integrator.

Closed-loop measurement (µ=0.46/h, V=25 mL, F=1 mL/s, 10 s tick, 48 h):

| OD band | pump_wait | floor reached | undershoot | fixed | undershoot |
|---|---|---|---|---|---|
| [0.35, 0.40] | 15 min | 0.291 | 17% | 0.318 | 9% |
| [0.35, 0.40] | 30 min | 0.186 | **47%** | 0.318 | 9% |
| [0.30, 0.40] | 15 min | 0.195 | 35% | 0.303 | 0% |
| [0.20, 0.40] | 15 min | 0.180 | 10% | 0.203 | 0% |
| [0.20, 0.60] | 15 min | 0.270 | 0% | 0.270 | 0% |

**Why unnoticed:** with a wide band the required bolus already exceeds the 20 s cap, so capped
and wound-up behaviour coincide (last row). The bug only appears when the band is tightened.

**Fix:** check the refractory gate *before* evaluating the formula; carry only the current
decision's sub-second remainder.

### T-2 — Deficit never cleared when the controller stops diluting (High)

`turbidostat.py:223–229`. Both early-return paths exit without resetting the accumulator, so
residual charge survives indefinitely and pre-loads the next dilution event. Fix: set
`pump_time_deficit_seconds = 0.0` on both paths.

### T-3 — The sub-second case cannot occur in a turbidostat (Medium)

Hysteresis guarantees the first bolus of each event is ≥ `ln(od_upper/od_lower)·V/F` seconds,
which is sub-second only when `od_upper/od_lower < exp(F/V)` ≈ 1.041 at V=25, F=1 — narrower
than the 3.9% OD step a single 1 s bolus produces. Fix: drop the accumulator here; validate the
band at experiment creation instead.

### T-4 — Lagged mean fed into the formula as instantaneous OD (Medium)

Even with T-1/T-2 fixed, [0.35, 0.40] still undershoots 9%. Inherited from the legacy script.
Fix: use the lagged mean for *threshold crossing* (noise suppression, its actual purpose) and
the latest valid sample for *bolus sizing*; or flush OD history after a dilution.

### T-5 — Tests validate arithmetic, not control behaviour (Low)

All 20 turbidostat and 10 chemostat tests pass against current code.
`test_deficit_preserves_total_dilution` pins OD at 0.203 for 50 cycles and asserts the sum is
conserved — that assertion *is* the bug. `test_deficit_capped_at_pump_duration_cap` asserts
windup is bounded, not absent. `test_full_turbidostat_oscillation` checks only
`0 < pump_time ≤ 20`.

### C-1 — Bolus volume uses nominal interval, not elapsed time (Critical)

`chemostat.py:138–143` against `app.py:2111`. The sensor loop sleeps `10 − elapsed`, so its
real period is `max(10 s, work)` — it can only ever run *slower* than nominal, never faster,
making the bias systematically negative and self-perpetuating.

Requested D = 0.500 /h, 12 h:

| Loop condition | current | error | elapsed-time fix | error |
|---|---|---|---|---|
| ideal 10.000 s | 0.497 | −0.7% | 0.497 | −0.7% |
| jitter ±0.5 s | 0.493 | −1.3% | 0.500 | 0.0% |
| overrun +2 s | 0.413 | **−17.3%** | 0.497 | −0.7% |
| overrun +5 s | 0.330 | **−34.0%** | 0.497 | −0.7% |

In a chemostat D **is** the independent variable and µ = D at steady state, so an error in D is
an equal error in every growth rate the run reports.

**Fix:** size the bolus from `now - last_bolus_time`, clamped to ~4 intervals so a resume after
an outage cannot produce one enormous catch-up bolus.

### C-2 — Open-loop mode gated on OD validity (Critical)

`experiment_engine.py:1185–1208` — the NaN `continue` paths precede `decide()` at line 1219.
The chemostat's own docstring says OD does not influence pump decisions, yet OD validity is a
hard precondition. The `out_of_range` case is sharpest: it means the culture is denser than the
calibration covers, i.e. exactly when dilution must not stop.

| sensor condition | delivered D | error |
|---|---|---|
| clean | 0.497 | −0.7% |
| 10% dropped OD | 0.450 | −10.0% |
| 30% dropped OD | 0.353 | −29.3% |
| out of range / dead | 0.000 | **−100%** |

**Fix:** split the sensor-validity gate from the control gate; call `decide()` for
OD-independent modes regardless, pushing OD only when finite. With C-1 fixed, dropped samples
then cost nothing.

### C-3 — `bolus_interval < 2 s` silently disables dilution forever (High)

`safety_cap = min(cap, max(interval − 1.0, 0.1))`; below 2 s the cap is under 1 s so
`int(deficit) >= 1` is never true. Zero media delivered, no warning, and the volume counter
still books the full intended 25 mL. The constructor only checks `> 0`, and
`create_experiment` (`experiment_engine.py:492–537`) validates mode, OD acquisition, and
flow-rate shape but **no control parameters at all**.

**Fix:** reject `< 2.0 s` in the constructor; add a control-parameter validation pass to
`create_experiment`; warn whenever the duration cap binds (`D·V·interval/3600 > safety_cap·F`).

### C-4 — Persisted volume counter records intent, not delivery (Medium)

`total_volume_ml` accumulates the *uncapped* `influx_volume_ml`. At D=5/h with a 600 s interval
(20.8 s asked, 20 s cap) state.json records 750 mL against an actual 720 mL — implying D=5.00
where the truth is 4.80. No alert when the cap engages.

Scope is contained and worth stating precisely: `pump_log.csv` records real fired durations and
is **correct**, and `_debit_media_locked` bills from `action.pump_time` so consumables tracking
is **correct**. Only the controller's own counter is wrong — which is the number a "did I
actually get D=0.5?" check reaches for, and which SPEC §17's dilution-rate estimator is slated
to consume. Also: `boli_fired` counts gate-passing cycles, including those firing nothing.

### C-5 — No start-OD gate; washout undetected (Medium)

Dilution begins on the first `decide()` at inoculation density. The legacy script explicitly
grows the culture to a start OD first; that gate is absent from both the code and SPEC §9. At
D=0.5/h against a minimal-media µ≈0.35/h the culture washes out and nothing notices.
**Fix:** optional `start_od` / `start_after_seconds`, plus a washout detector (OD monotonically
falling over hours while dilution is active → critical alert).

> **Partially done 2026-08-21.** The start gate is implemented: `start_od` and
> `start_after_seconds` are optional chemostat parameters, OR'd rather than AND'd so the
> timeout is an escape hatch for a sleeve whose OD never reads. Releasing the gate raises a
> `start_gate_released` event through the alert funnel. **The washout detector was
> deliberately deferred** — it is a detection rule rather than a control fix, and it belongs
> with the other anomaly rules in SPEC §22. Carried in `ROADMAP.md`.

### X-1 — Efflux flow rate validated, stored, never used (High, already tracked)

`flow_rate_efflux_ml_s` is range-checked and assigned in both controllers, then referenced by
nothing. Efflux duration is `pump_time + efflux_extra_seconds`, a pure time offset. With
`DEFAULT_EFFLUX_EXTRA_SECONDS = 0.0`, level stability requires F_efflux ≥ F_influx on all 16
vials, unchecked — and per-pump calibration exists because they differ. SPEC §16.2 already
documents this correctly, including why software flow balancing is the wrong fix. This audit
confirms the code matches that description and the gate is still open. Legacy used
`time_out = 5 s`. It is the one open item that risks overflow and cross-sleeve contamination
rather than merely bad data.

> **Still open 2026-08-21 — by design.** SPEC §16.2 is right that the fix is a bench
> decision (where the straw sits, whether drawing air through culture foams), not a code
> change, and software flow balancing is explicitly the wrong answer. What changed is that
> the gate is no longer silent: `create_experiment` returns a warning and
> `start_experiment` raises one through the alert funnel whenever
> `efflux_extra_seconds == 0`, naming the consequence — volume regulation disengaged, level
> an open-loop integral of flow mismatch, no level sensor to catch it. The default is
> unchanged.

### X-2 — Wall-clock gating stalls after a backward clock step (Low)

Clock is `time.time`; timestamps persist to state.json as epoch seconds. On an RPi with no RTC,
a stale boot clock puts them in the future, `now − last_bolus_time` goes negative, and both
modes block every dilution until wall time catches up. Wall time is the right choice here (a
monotonic clock wouldn't survive restart) — only the guard is missing. **Fix:** on restore,
re-baseline a future timestamp to `now` with a warning.

### X-3 — Note: engine hard cap unreachable

`PUMP_DURATION_HARD_CAP_SECONDS = 30.0` sits above the 20 s cap both controllers apply, so the
guardrail at `experiment_engine.py:1224` never engages for these modes. Not a defect, but it
provides no defence in depth as configured.

## The asymmetry worth internalising

The same accumulator design is **correct in the chemostat and wrong in the turbidostat**,
because the accumulated quantity means different things: a per-interval increment in one, an
absolute setpoint error in the other. SPEC §9 applies it uniformly to both and claims "total
dilution delivered equals total dilution prescribed" for each. That claim holds for the
chemostat and is false for the turbidostat.

## Tests the fixes need

*(All four are implemented in `server/test_control_loop.py`.)*

1. **Closed-loop band adherence** — drive the real controller against `od *= exp(µ·dt)` with
   `od *= exp(−F·t/V)` on each PumpAction; assert OD stays within
   `[od_lower×0.97, od_upper×1.03]` after settling across ≥4 bands. Fails today.
2. **Bolus proportionality** — assert each fired `pump_time` is within 1 s of
   `ln(avg_od/od_lower)·V/F` at firing time. Model-independent windup check.
3. **Chemostat rate under adverse timing** — 12 s and 15 s tick periods, 30% dropped cycles;
   assert delivered D within 2% of requested. Covers C-1 and C-2.
4. **Delivered-equals-booked invariant** — assert `total_volume_ml` equals summed fired seconds
   × flow rate in a cap-binding config. Covers C-4; catches C-3 immediately.

## Verified correct

- Washout model matches the hardware: `t = −ln(od_lower/avg)·V/F` inverts
  `OD(t) = OD₀·exp(−Ft/V)`, and `_execute_pump_actions` fires influx and efflux concurrently on
  separate pump bits, so the continuous model is the right one (a bolus-then-drain sequence
  would need `V/F·(avg/lower − 1)`, ~30% larger for a 2× dilution).
- Hysteresis matches the legacy script exactly, including the `!=` guards; the
  `average_od <= od_lower` check correctly forecloses `log` of a non-positive argument.
- Warmup gate faithful: 8 samples, 5-sample mean, NaN dropped without advancing the counter,
  counter persisted across restart.
- Chemostat steady-state arithmetic correct across D = 0.05–1.0 /h and four bolus intervals;
  the accumulator preserves the mean exactly.
- State persistence round-trips in both modes with sensible clamping of corrupt deficits.
- Pump addressing correct (influx N → bit N, efflux N → bit N+16); efflux total duration
  matches the legacy two-command sequence.
- Maintenance coalescing is right — per-vial queuing prevents a long pause from discharging
  stacked dilutions on resume, and pending actions are deliberately not persisted.

## Confidence & limits

The simulation assumes ideal mixing, exact flow rates, and noise-free OD — all of which make
the reported errors **conservative**. Real OD noise widens the T-4 lag; real flow mismatch
compounds X-1. The direction and approximate magnitude of every finding follow from the code
structure alone; the simulation supplies the numbers.

Not covered: morbidostat (excluded), temperature control, calibration services, OD acquisition,
frontend.
