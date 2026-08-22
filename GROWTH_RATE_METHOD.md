# GROWTH_RATE_METHOD.md — Porting the Isaacs Lab growth analysis to eVOLVER

**Scope:** the estimator behind `ROADMAP.md` Session N and `SPEC.md` §17.
**Source algorithm:** `Growth analysis New HTX 2025_03_05.ipynb`, SECTION 4
(`Doubling_time`), by YC/MN/KKG, after Lajoie *et al.* 2013 and Kuznetsov *et al.* 2017.
**Status:** design brief, not yet implemented. Written to be executed by Claude Code.
**Revised 2026-08-22** after the scope decisions in §0.

**Relationship to the other docs.** `SPEC.md` §17 says *what the service must expose*;
`ROADMAP.md` Session N says *when it gets built and what blocks on it*; this document says
*what the estimator is and why*, the same role `CALIBRATION_PROTOCOL.md` plays for
Session O. §11 lists the corrections §17 and Session N need as a result — four of their
claims no longer match either the code or the decisions below.

---

## 0. Decisions already taken — do not re-litigate these

| Decision | Consequence |
|---|---|
| **The Isaacs Lab algorithm is *the* growth rate.** One reported μ per vial, from windowed log-linear regression within inter-dilution segments. | §17's "two estimators, both reported" no longer holds. See §11.1. |
| **The dilution-rate calculation survives only as a gated diagnostic**, never as a reported μ. | §4.4. Off by default; enabled only when pump calibration exists. |
| **Session N touches no control mode.** | All state lives in `ExperimentEngine`. `turbidostat.py` and `chemostat.py` are not edited. §7. |
| **Morbidostat is out of scope** (and may be removed entirely). | No migration of its private growth fit. `growth_rate.py` becomes the tree's only growth-rate implementation, not a replacement for one. |
| **The plate-reader band and window constants are not ported.** | They were cadence and phase-selection artefacts, not physical constants. §1.1, §4.2. |
| **Growth output goes to a parallel `vialNN_growth.csv`.** | `vialNN_OD.csv` keeps its current header, so positional parsers (including the lab's own analysis scripts) do not break. §7.7. |

---

## 1. The source algorithm, stated precisely

Per well, given a time series of calibrated OD600:

1. **Band selection.** `CI[0] = argmin|OD − 0.1|`, `CI[1] = argmin|OD − 0.5|`. Only
   indices in `[CI[0], CI[1]]` are candidates.
2. **Log transform.** `y = log₂(OD)`. Base 2, so the slope is *doublings per hour*.
3. **Sliding window.** Fixed width of 8 samples (2 h at the plate reader's 15-min
   cadence). Slide from `CI[0]` to `CI[1] − 8`.
4. **Selection by max R².** `np.polyfit(t, y, 1)` per window; keep the window with the
   highest R². **Break early** as soon as R² > 0.998.
5. **Fallback.** If the band is narrower than the window, fit the whole band instead.
6. **Warn** if the winning R² < 0.95.
7. **Outputs.** `DT = 60 / slope` (minutes), `LagTime = (log₂(0.001) − intercept) / slope`
   (the extrapolated crossing of the detection floor), `Max OD`, `R²`, the fitted index
   range, and the OD at each end of that range.

Upstream of this, the notebook does three things that are *not* part of the estimator and
must not be ported (§3.2): per-well background subtraction by the well's **minimum** OD
over the whole run, clamping OD to a floor of 0.001, and a plate-reader-specific quadratic
absorbance→OD600 calibration.

### 1.1 Neither of the two tunable constants transfers

The notebook's comments and code disagree on both — comments say band 0.2–0.7 (rich) /
0.025–0.15 (M9) and a 6-point window; the code does **0.1–0.5** and **8 points**. That
drift is worth resolving for the lab's own records, but **it does not block this build**,
because neither constant is ported:

- **The band is a phase selector, not a measurement range.** On a batch curve it answers
  "which stretch do we call exponential", excluding lag below and stationary above. The
  numbers are specific to the plate reader's cuvette-equivalent scale and to the lab's
  strains and media. The eVOLVER derives the equivalent range from the run's own config
  and calibration instead (§4.2).
- **The window is a cadence artefact.** 8 samples × 15 min = 2 h. The same 8 samples at
  the eVOLVER's 10 s cadence is 80 s, measured at 64–339 % error (§2 #5). It is respecified
  as a *duration* with a measured basis (§5).

The one place the original constants are still used verbatim is the round-trip acceptance
test (§10), which reproduces a notebook number and must therefore run the notebook's own
values.

---

## 2. What the algorithm actually does — measured, not asserted

Every figure below is from a Monte-Carlo re-implementation of `Doubling_time` (200–400
replicates per cell) run against synthetic series with known μ. Reproduce with
`server/verify_growth_rate_method.py`; every figure quoted here is one of its printed lines.

| # | Question | Result |
|---|---|---|
| 1 | Is the estimator itself biased on truly exponential data? | **No.** Bias +0.38 % / +0.28 % / −0.08 % / +0.16 % at μ = 0.4/0.7/1.0/1.4 h⁻¹; sd 2.2–3.7 %. Passes Session N's ±5 % criterion outright. |
| 2 | What does the band do to the answer? | On a logistic with K = 1.5, band 0.1–0.5 reports **−14.6 %** below μ_max; band 0.2–0.7 reports **−25.3 %**; band 0.025–0.15 reports **−2.9 %**. Tracks the logistic factor (1 − ŌD/K). |
| 3 | Does the R² > 0.998 early exit matter? | Fires on 0.3–30 % of wells depending on μ; shifts the estimate by **< 0.25 pp**. Harmless at plate-reader cadence — but see #4 and #5. |
| 4 | Is the reported R² an honest confidence measure? | **No.** Selecting max-R² over *k* windows inflates R² by +0.0008 (k = 5) to +0.0022 (k = 200) on data that is *exactly* log-linear. |
| 5 | What happens at eVOLVER's 10 s cadence with an 8-*sample* window? | 8 samples = 80 s. μ error sd **64 % / 159 % / 339 %** at OD noise 0.002 / 0.005 / 0.010. Unusable. |
| 6 | How long a span does 10 s data actually need? | sd 2.4 % / 6.0 % / 11.6 % at **10 min**; 0.4 % / 1.0 % / 2.2 % at **30 min**, for the same three noise levels. |
| 7 | Reversed band (flat OD ≈ 0.30, as a turbidostat holds it)? | `CI[1] < CI[0]` → empty slice → **`TypeError: expected non-empty vector for x`**. The notebook has no guard. |
| 8 | Culture stalls below the band top (max OD 0.26)? | Returns **DT = 131 min at R² = 0.991** — a confident-looking number off a decelerating curve, and no warning, because 0.991 is far above the 0.95 threshold. The high R² is the problem: the fit is excellent, and the quantity fitted is not what the caller thinks. |

**Read #1 and #2 together.** The algorithm is unbiased; the −14.6 % is the *band* folding
deceleration into the answer. The number this method reports is a **growth rate at the
operating density**, not μ_max. That is the right quantity for comparing strains under a
fixed protocol, and it is the right quantity for eVOLVER control — but it must be labelled
as such, and the fitted OD span recorded with every value.

---

## 3. Why it cannot be dropped in unchanged

### 3.1 The contexts differ in the one variable the algorithm keys on

| | Plate reader (notebook) | eVOLVER |
|---|---|---|
| Culture | Batch; one full lag→exponential→stationary curve | Continuous; OD deliberately pinned in a band |
| Cadence | 15 min, ~96 points per well | 10 s, ~360 points per hour |
| Dilutions | None | Every few minutes to hours; step drops in OD |
| Analysis | Post-hoc, whole curve visible | Causal, online, past data only |
| Output | One number per well per run | A time series of μ, updated each cycle |
| Band role | **Selects** exponential phase out of a curve that also contains lag and stationary | **Not needed** — dilution events segment the series, and each segment is exponential by construction |

Finding #7 above is the band's irrelevance showing up as an exception rather than a wrong
number: pointed at a turbidostat-shaped series, the band search does not merely fail to
help, it crashes.

### 3.2 Do not port the preprocessing

- **Background = per-well minimum OD.** A batch-only trick: it needs the whole run, and in
  a turbidostat the minimum is a post-dilution trough, not a blank. The eVOLVER's correct
  equivalent is the per-run blank of `SPEC.md` §19.2 — now built (`OdBlankSession` and
  `reanchor_od_calibration` in `calibration_service.py`). Use that.
- **Clamping OD to 0.001.** Creates a plateau of identical values that a log-linear fit
  reads as zero growth. Return `None` below the validity floor instead (§8).
- **Quadratic absorbance→OD600 curve.** Plate-reader-specific. The eVOLVER has its own
  four-parameter logistic.

### 3.3 The naive alternative is far worse — the number that justifies segmenting

Fitting `ln(OD)` over a trailing 30-min window *across* dilution events, on a simulated
turbidostat at μ = 0.35 / 0.70 / 1.20 h⁻¹, gives **−80.3 % / −94.1 % / −98.6 %** error.
Splitting the same series at dilution events and fitting within segments gives
**−2.3 % / +0.2 % / −0.4 %**.

---

## 4. The adopted design

The notebook method **is** the growth-rate estimator (§0). What follows is where each of
its pieces earns its place, and where it does not.

### 4.1 The window search buys transient rejection, not accuracy

Simulated turbidostat, whole-segment OLS vs. max-R² sub-window, as a function of an
**unknown-length post-dilution mixing transient**:

| Transient | Whole-segment OLS | max-R² window |
|---|---|---|
| 0 s | **+0.11 %** (sd 1.67) | +2.57 % (sd 3.50) |
| 30 s | +2.63 % (sd 1.77) | **+2.02 %** (sd 3.70) |
| 60 s | +3.86 % (sd 1.82) | **+2.16 %** (sd 3.75) |
| 120 s | +7.09 % (sd 1.79) | **+2.80 %** (sd 3.62) |
| 240 s | +12.63 % (sd 1.67) | **+3.75 %** (sd 5.13) |

Inside a *clean* inter-dilution segment the window search is not merely unnecessary, it is
mildly harmful — it selects the luckiest noise realisation and pays ~2.5 % bias and double
the variance for nothing. Its value is entirely as a **transient-rejection mechanism**: it
finds the exponential stretch when you do not know how long mixing, sensor recovery, or a
stalling culture contaminated the ends of the segment. That is the same job it does in the
notebook, where the contaminant is lag and stationary phase rather than a mixing transient.

Keep it, and document that this is what it is for. Do not present it as a general accuracy
improvement, because on clean data it is not one.

### 4.2 Two regimes, one module

**Regime A — batch / startup, inoculation to the first dilution.** A genuine
plate-reader-shaped growth curve, and the notebook algorithm applies almost directly:
range gate, windowed max-R² fit, lag time by extrapolation, time-to-threshold.

The range gate does **not** use a ported constant. It is derived per run:

```
lo = max(od_lower_thresh[vial] * 0.5, per_vial_od_floor)   # floor from the run's blank SD
hi = min(od_upper_thresh[vial] * 1.5, od_calibration_domain_max[vial])
```

`od_lower_thresh` / `od_upper_thresh` are already in `config.json` `parameters` — they are
literally the density range the operator chose to run at, which is exactly what the band
was approximating in the notebook. The ceiling is bounded by the OD calibration's own
validity domain, which `serial_manager._read_od_enhanced_locked` already enforces.

**Regime B — continuous culture, after the first dilution.** Split the OD series at
dilution events. Within each segment, run the windowed max-R² fit. Take a weighted mean of
recent segments' μ, weighted by segment span and R². No range gate is applied beyond the
per-vial validity floor — the segment is exponential by construction.

### 4.3 Numerical conventions

- Fit in **natural log** internally; μ is reported in h⁻¹, as §17 requires.
- Report **doubling time** as `ln2 / μ` in **minutes**, matching the lab's existing plate
  reader output so the two are comparable without arithmetic.
- The notebook's log₂ slope converts as `μ = slope_log2 · ln2`; equivalently
  `DT_min = 60 / slope_log2`. Document this in the module docstring — someone will
  eventually compare a GUI number against a notebook number, and the factor of 0.693 is
  exactly the kind of thing that gets silently absorbed as a "calibration difference".
- Use **real timestamps**, never nominal cadence. The notebook assumes exactly 15 min per
  read (`Time_course = i*15/60 + 15/60`); the eVOLVER's loop runs at `max(10 s, work)`, so
  its cadence genuinely varies (`CONTROL_MODE_AUDIT.md` C-1 is that same assumption
  failing elsewhere).

### 4.4 The dilution-rate calculation — diagnostic only, and gated

Not a reported growth rate (§0). Retained because it is the **only independent check on
the OD path**: it depends on pump flow and elapsed time, not on optics, so it is blind to
the failure modes the OD fit is vulnerable to. Concretely it detects a pump that is not
actually pumping, and biofilm or wall growth making the culture denser than the planktonic
OD suggests.

**Gate it on calibration.** `CalibrationStore.current_pump_rates()` returns `None` today —
`calibration/current.json` has `"pump": null`, and `calibration/_sessions/pump.json` is a
started session in which all 32 pumps still show `masses_g: []`, `fired: 0`. Until that
bench work is done, every flow rate is a hardcoded default and this diagnostic would
false-alarm. So:

```python
enabled = calibration_store.current_pump_rates() is not None
```

Off by default; no divergence warning may be raised while it is off; the UI must say why
it is unavailable rather than showing a blank.

**Use `v/V`, not `ln(1 + v/V)`.** `SPEC.md` §17 and Session N both specify
`μ ≈ Σ ln(1 + vᵢ/V) / Δt`, which is the factor for **bolus-add-then-overflow** mixing. The
eVOLVER turbidostat fires influx and efflux **simultaneously** (`turbidostat.py`: influx
for `pump_time`, efflux for `pump_time + efflux_extra`), with volume pinned by the straw —
that is continuous perfusion, whose factor is `v/V`. A third convention, efflux-then-influx,
gives `−ln(1 − v/V)`.

| Bolus into V = 25 mL | `ln(1+v/V)` (§17) | `v/V` (perfusion) | `−ln(1−v/V)` | spread |
|---|---|---|---|---|
| 2 mL | 0.0770 | 0.0800 | 0.0834 | 8.3 % |
| 4 mL | 0.1484 | 0.1600 | 0.1744 | 17.5 % |
| 6 mL | 0.2151 | 0.2400 | 0.2744 | 27.6 % |
| 8 mL | 0.2776 | 0.3200 | 0.3857 | 38.9 % |

At the turbidostat's typical 5–8 mL bolus the §17 formula reads 9–15 % low against the
mixing model this machine implements. Since nothing *reported* depends on this any more,
it is no longer a blocker — but a diagnostic built on the wrong factor is a diagnostic that
cries wolf, so fix it. Record the assumed mixing model in the returned payload.

### 4.5 How dilution events enter the calculation

**Pump events feed the reported μ as timestamps only.** Volume never touches it — only the
gated diagnostic (§4.4) reads `delivered_ml`. This is deliberate and load-bearing: a
segment boundary is knowable exactly (the pump either fired or it did not), whereas a bolus
volume is `duration × an unmeasured flow rate`. Keeping volume out of the reported path is
what makes the reported growth rate independent of the uncalibrated pump array. One deque,
two consumers with different needs.

**A boundary is an interval, not an instant.** Influx runs for `pump_time`; efflux for
`pump_time + efflux_extra_seconds`; mixing continues after that. With `pump_time` capped at
20 s against a 10 s loop, a single dilution can span two or three sensor cycles, so the
implementation must not assume one OD sample per event. Each event excises a gap:

```
segment_end   = last OD sample with t <= pump_start
segment_start = efflux_end + POST_DILUTION_SKIP_SECONDS
```

`run_cycle` reads OD *before* it calls `decide`, so the sample at the firing cycle is
genuinely pre-dilution and belongs to the ending segment. The max-R² window search then
trims whatever mixing transient survives the nominal skip — that is exactly the job §4.1
measured it doing, and the reason the skip can be a rough constant rather than a tuned one.

**Which events count as boundaries.** Any pump that actually fired, in either direction:

- Automatic dilutions — appended in `_debit_media_locked`, which runs *after* the §15
  consumables gate, so a suppressed pump correctly creates no boundary.
- Manual and override pumps (§21) — these disturb the culture exactly as automatic ones do
  and must cut the series. `record_manual_pump` is the hook.
- **Efflux-only operations** (the common "take 3 mL for a sample") — removing culture does
  not change its concentration, so this is not a dilution, but it changes working volume and
  can perturb the optical path. Treat it as a boundary; contribute **zero** to the dilution
  diagnostic.

**The refractory gate makes segmentation viable by construction.** `pump_wait` (default
`DEFAULT_PUMP_WAIT_MINUTES`, 15 min) is checked *first* in `TurbidostatController.decide`,
before any dilution arithmetic. Turbidostat segments therefore can never be shorter than
`pump_wait`, however fast the culture grows — comfortably above `MIN_FIT_SPAN_SECONDS`
(10 min). Natural intervals `ln(od_upper/od_lower)/μ`, before the gate clamps them:

| μ /h | band 0.2–0.4 | band 0.2–0.6 | band 0.3–0.4 |
|---|---|---|---|
| 0.35 | 119 min | 188 min | 49 min |
| 0.70 | 59 min | 94 min | 25 min |
| 1.40 | 30 min | 47 min | 12 min |
| 2.00 | 21 min | 33 min | 9 min |

Only a deliberately narrow band on a fast culture falls under 15 min, and there the gate
takes over. Warn (do not fail) if a run configures `pump_wait_minutes` below
`MIN_FIT_SPAN_SECONDS / 60` — that is the one configuration where segmentation starves.

### 4.6 Chemostat needs a different route

`ChemostatController` fires a bolus every `bolus_interval_seconds` — tens of seconds, not
tens of minutes. Segmenting there produces segments of a few samples and regime B would
return `None` indefinitely. Use the chemostat mass balance instead:

```
μ = D + d(ln OD)/dt
```

where `D` is the configured `dilution_rate_per_hour` and the drift term is a long-window
(≥ 1 h) log-linear fit on OD, which needs no segmentation because the dilution is
continuous rather than stepwise. At steady state the drift term is ~0 and μ = D; a drifting
OD is exactly the informative case (the culture is out-running or washing out of the
imposed rate).

Flag every chemostat estimate `assumes_commanded_D`: a *commanded* dilution rate is only a
*delivered* one once O3's bench work exists.

**Explicitly rejected as the default:** fitting `ln(OD) + Σ vᵢ/V` across dilution
boundaries. This analytically removes the step drops, fits the whole history as one line,
and is more statistically efficient than segmenting — but it reintroduces pump calibration
into the *reported* growth rate, which is precisely the dependency §0 decided to keep out
of it. Implement it if wanted, behind the same gate as the diagnostic; never as the
default path.

---

---

## 5. Constants, defaults, and their provenance

| Constant | Value | Provenance / basis |
|---|---|---|
| `MIN_FIT_SPAN_SECONDS` | 600 (10 min) | Measurement #6: 10 min gives μ sd 2.4–11.6 %; 80 s gives 64–339 %. |
| `PREFERRED_FIT_SPAN_SECONDS` | 1800 (30 min) | Measurement #6: sd 0.4–2.2 %. Use when the segment is long enough. |
| `MIN_SAMPLES` | 30 | 5 min at 10 s cadence; a floor under `MIN_FIT_SPAN` for lossy sleeves. |
| `WINDOW_FRACTION` | 0.6 | Window = 60 % of segment span, floored at `MIN_FIT_SPAN`. Replaces the notebook's fixed 8 samples. |
| `POST_DILUTION_SKIP_SECONDS` | 60, then let the window search refine | §4.1; a nominal skip plus the search beats either alone. |
| `MIN_R2_REPORT` | 0.90 | Below this, return μ flagged `low_confidence`, never silently. |
| `MIN_R2_TRUST` | 0.95 | The notebook's warning threshold; keep it for continuity with lab practice. |
| Early exit | **removed** | Measurement #3: costs < 0.25 pp at plate-reader cadence; measurement #4: at eVOLVER cadence (hundreds of candidate windows) R² > 0.998 is reachable by luck alone. Complexity without benefit. |
| Per-vial low-OD floor | `max(0.05, 10 × blank_sd_od[vial])` | Not the single 0.1 of §17 nor the 0.05 of Session N — the audit found four-fold optical-sensitivity variation across sleeves. Source: the run's `experiments/{name}/od_blank.json`. **No blank has ever been committed**, so implement the fallback in §8. |
| Regime-A range gate | derived from `od_lower_thresh`/`od_upper_thresh` | §4.2. No ported constant. |
| Dilution factor (diagnostic) | `v/V` | §4.4. |

---

## 6. `server/growth_rate.py` — module shape

Pure, I/O-free, no imports from `experiment_engine`, `app`, or any control mode. Takes
plain data in, returns a dataclass out. That constraint is what makes it testable and what
keeps Session N out of the control modes (§0).

```python
@dataclass(frozen=True)
class GrowthEstimate:
    mu_per_hour: Optional[float]        # None when not estimable — never a guess
    doubling_time_min: Optional[float]  # ln2/mu, in minutes (lab convention)
    r_squared: Optional[float]
    method: str                         # "segment" | "batch" | "chemostat_balance"
    n_points: int
    span_seconds: float
    window_start_od: Optional[float]    # notebook provenance: which stretch was fitted
    window_end_od: Optional[float]
    windows_searched: int               # feeds the R2-inflation caveat (#4)
    flags: tuple[str, ...]              # low_od, short_span, low_r2, negative_slope,
                                        # insufficient_segments, warmup, band_not_spanned

@dataclass(frozen=True)
class DilutionDiagnostic:
    enabled: bool                       # False while pump calibration is absent
    mu_per_hour: Optional[float]
    mixing_model: str                   # "perfusion_v_over_V"
    n_events: int
    disagreement_fraction: Optional[float]   # vs the reported mu; None when disabled
    reason_unavailable: Optional[str]        # shown in the UI instead of a blank

@dataclass(frozen=True)
class GrowthReport:
    growth: GrowthEstimate              # THE growth rate (regime A or B)
    regime: str                         # "batch" | "continuous" | "chemostat"
    dilution_check: DilutionDiagnostic  # diagnostic only, never a reported mu
```

Functions:

```python
def fit_log_linear(times_s, ods) -> tuple[mu, r2, intercept]
def best_window_fit(times_s, ods, *, window_seconds, step_seconds) -> GrowthEstimate
def split_segments(times_s, ods, dilution_times_s, *, skip_seconds) -> list[segment]
def estimate_segment(...) -> GrowthEstimate            # regime B
def estimate_batch(times_s, ods, *, lo, hi) -> GrowthEstimate   # regime A
def estimate_chemostat(times_s, ods, *, dilution_rate_per_hour) -> GrowthEstimate  # 4.6
def lag_time_hours(times_s, ods, *, reference_od) -> Optional[float]
def dilution_check(events, volume_ml, window_s, *, enabled) -> DilutionDiagnostic
def estimate(...) -> GrowthReport                      # the one the engine calls
```

`lag_time_hours` is the notebook's lag extrapolation, kept because it is genuinely useful
at inoculation: it answers "when did this culture actually start growing", which is the
first thing anyone asks about an overnight run that under-performed.

---

## 7. Integration into the existing tree

Verified against the repository as it stands (2026-08-22). **Session N touches no control
mode** (§0), so every piece of state lives in `ExperimentEngine`, which already sees
everything required.

1. **`server/growth_rate.py`** — new file, per §6. No dependencies beyond `math` /
   `statistics`; the control path avoids numpy and every fit here is closed-form OLS.

2. **Engine-owned timestamped OD history.** `TurbidostatController.od_history` is a
   `deque[float]` with `maxlen=history_window` (default 5) and no timestamps — it cannot
   feed this service, and it is not to be changed. Instead add to `ExperimentEngine` a
   per-vial `deque[(t, od)]` bounded by *time* (default 3 h), appended in `run_cycle` at
   the existing `push_od` dispatch site (~line 1410) using the `now` and `od` already in
   scope there. The controller call is left exactly as it is.

3. **Engine-owned dilution-event list.** Add a per-vial bounded
   `deque[(t_start, t_efflux_end, delivered_ml)]`, appended in `_debit_media_locked`,
   which already computes `influx_ml = action.pump_time * controller.flow_rate_influx_ml_s`.
   Reuse that value — do not recompute it, or the two will drift. This runs *after* the
   §15 consumables gate, so a suppressed pump correctly contributes no event. Also append
   from `record_manual_pump` (§4.5), with `delivered_ml = 0` for efflux-only operations.
   **The reported μ consumes only the two timestamps; `delivered_ml` is read solely by the
   gated diagnostic** (§4.5).

4. **Call site.** After the decide pass in `run_cycle`, call `growth_rate.estimate(...)`
   per vial and cache the `GrowthReport`. Recompute on a longer interval than the sensor
   cadence (default 60 s) — the estimate does not change meaningfully in 10 s.

5. **`status()`** — add to the `per_vial` block (~line 1550, beside `avg_od` and
   `sensor_health`): `mu_per_hour`, `doubling_time_min`, `r_squared`, `regime`,
   `growth_flags`, and a `dilution_check` sub-object.

6. **WebSocket** — extend the `sensor_update` payload (`SPEC.md` §7) with a `growth` block
   carrying the cached values.

7. **CSV logging — a parallel file, decided (§0).** `vialNN_OD.csv` keeps its current
   header untouched, because `data_export.py` has **no version or schema marker at all**
   and any positional parser — including the lab's own analysis scripts — would break
   silently on inserted columns. Add instead:

   ```
   experiments/{name}/vialNN_growth.csv
   timestamp,elapsed_hours,regime,growth_rate_per_hour,doubling_time_min,
   r_squared,windows_searched,fit_span_s,fit_od_start,fit_od_end,flags
   ```

   Written at the growth recompute interval (default 60 s), not the 10 s sensor cadence —
   the estimate does not change meaningfully in 10 s, and this keeps the file ~1/6 the
   size of the OD file. Add it to `data_export.py`'s bundle and to the `.gitignore`d
   experiment tree exactly as the existing per-vial files are handled.

8. **`GET /api/growth_rate`** — already specified in `SPEC.md` §6. Implement to the shape
   there, adjusted for the single-estimator decision (§11.1).

9. **Downstream consumers** — Session R (`μ × V` forecasting), Session T (μ trace with R²
   shading, doubling-time trace), Session V (stall rule `μ < 0.05/h for > 2 h`). Session V's
   *estimator divergence* rule now depends on `dilution_check.enabled`, so it must be
   written to stay silent while the diagnostic is gated off.

**Not in scope:** `control_modes/morbidostat.py` (out per §0 — note its
`estimate_growth_rate` is currently the tree's only growth-rate code, so if the mode is
removed, `growth_rate.py` is the sole implementation, and `test_morbidostat.py` goes with
it); `control_modes/growth_rate.py`, which `SPEC.md` §4's file tree lists but which does
not exist and which Session N does not create.

---

## 8. Edge cases — all of these must be explicit

| Case | Behaviour |
|---|---|
| OD below the per-vial floor | `None`, flag `low_od`. Never clamp to a floor value (§3.2). |
| **No `od_blank.json` for the run** | The per-vial floor has no measured basis. Fall back to a global 0.05, flag every estimate `uncalibrated_floor`, and surface that in the UI. This is the live case today — no blank has ever been committed. If §19.6's "missing blank hard-blocks the run" is enforced, this path becomes unreachable, which is the better outcome. |
| Span < `MIN_FIT_SPAN_SECONDS` or fewer than `MIN_SAMPLES` | `None`, flag `short_span`. |
| Turbidostat warmup (first 8 cycles, `DEFAULT_MIN_SAMPLES_BEFORE_ACTION`) | Dormant, flag `warmup`. |
| NaN / dropped OD reads | Drop the sample; do not interpolate. If > 30 % of a window is missing, reject the window. The engine's `nan_streak` and `od_range_streak` already track this. |
| Slope ≤ 0 | Report μ (it is real information — the culture is shrinking) but set `doubling_time_min = None` and flag `negative_slope`. **Do not** return `60/slope`; the notebook does, and papers over it downstream with `replace([inf, -inf], nan)` in a plotting cell. |
| Reversed or empty range (regime A) | `None`, flag `insufficient_range`. This is measurement #7 — the notebook raises `TypeError` here. |
| Culture never spans the regime-A range | Fit what exists, flag `band_not_spanned`. This is measurement #8 — currently a silent wrong answer *carrying a high R²*. |
| R² below `MIN_R2_TRUST` | Return μ **with** the flag. A number without its R² is worse than nothing; a number *with* a low R² is still useful to a human. |
| Selected-window R² | Always report `windows_searched` beside it. Measurement #4: max-R² selection inflates R² by up to +0.0022, so this R² is an optimistic bound, not an unbiased fit statistic. Session V's rules must not treat it as one. |
| Chemostat mode | Regime B **does not apply** — boli every `bolus_interval_seconds` leave segments a few samples long. Use the §4.6 mass balance, flagged `assumes_commanded_D`. The dilution diagnostic is near-tautological there — mark it `tautological` and suppress the divergence signal. |
| Vial 1 | The committed OD envelope's own `qc.warnings` says its lower asymptote sits inside the working signal range and the curve diverges above OD ≈ 1: *"exclude from quantitative use"*. Flag every vial-1 estimate `calibration_suspect` until Session AA. |
| `pump_wait_minutes` configured below `MIN_FIT_SPAN_SECONDS/60` | Segmentation starves. Warn at experiment creation; do not refuse the run. §4.5. |
| Fewer than 2 usable segments in the window | `None`, flag `insufficient_segments`. Do not fall back to a cross-dilution fit — that is the −94 % failure mode of §3.3. |

---

## 9. Remaining open items

One, and it does not block writing the code.

1. **Not a code question:** whether the doubling times already reported from this script
   were intended as μ_max. Measurement #2 says they are growth rates at the operating
   density, ~15 % below μ_max for a K ≈ 1.5 culture in the 0.1–0.5 band. Worth checking
   against what has been published.

Resolved: the CSV format (§0, §7.7 — a parallel file);
the band and window constants (§1.1 — not ported);
the estimator-authority question, `SPEC.md` §14 Q9 (§0 — the Isaacs Lab algorithm, singly);
the low-OD floor source (§5 — O2 is built, with the §8 fallback until a blank is taken);
and the refactor blast radius (§7 — no control mode is touched).

---

## 10. Verification

Extends Session N's checklist rather than replacing it.

- [ ] Synthetic pure exponential at known μ recovered within 5 % *(Session N; measured at
      ±0.4 % bias, sd 2.2–3.7 % — expected to pass comfortably)*
- [ ] Dilution events do not depress the estimate *(Session N)* — assert the naive
      cross-dilution fit reproduces the −80 %/−94 %/−99 % errors of §3.3, so the test
      documents what the segmenting is protecting against
- [ ] Low OD and short history return `None`, not a number *(Session N)*
- [ ] R² reported; a deliberately non-exponential series produces a low R² *(Session N)*
- [ ] μ appears in `status()`, the WebSocket payload, and the per-vial CSV *(Session N —
      note the CSV column must be **added**, §7.7)*
- [ ] A segment with a 120 s injected mixing transient is estimated within 5 % (the
      window search doing its job; whole-segment OLS gives +7.1 % here)
- [ ] A flat OD series at 0.30 returns a valid estimate, not `TypeError` (measurement #7)
- [ ] A culture stalling at OD 0.26 is flagged `band_not_spanned` rather than returning
      DT = 131 min at R² = 0.99 (measurement #8)
- [ ] `windows_searched` present in every returned estimate
- [ ] Slope ≤ 0 returns `doubling_time_min = None`, never a negative or infinite number
- [ ] With `current.json` `"pump": null`, `dilution_check.enabled` is `False`, no
      divergence warning is raised, and `reason_unavailable` is populated
- [ ] With pump rates present, the diagnostic agrees with the reported μ within 10 % on a
      simulated steady-state turbidostat **using `v/V`** — and demonstrably fails to when
      `ln(1+v/V)` is substituted (the regression test for §4.4)
- [ ] A run with no `od_blank.json` produces estimates flagged `uncalibrated_floor`
- [ ] A dilution spanning three sensor cycles produces exactly one boundary, and no
      mid-dilution sample lands inside either adjoining segment
- [ ] A pump suppressed by the §15 consumables gate creates **no** segment boundary
- [ ] A manual efflux-only sampling event creates a boundary and contributes 0 mL to
      the diagnostic
- [ ] Simulated chemostat at known D: §4.6 mass balance recovers μ within 10 % and is
      flagged `assumes_commanded_D`; regime B is not attempted
- [ ] Reported μ is bit-identical when `delivered_ml` on every event is scaled by 2×
      (proves volume never reaches the reported path — §4.5)
- [ ] Round-trip against the notebook: run `estimate_batch` on one plate-reader well's
      data, **using the notebook's own constants (band 0.1–0.5, 8-sample window)**, and
      reproduce its doubling time to within 1 %. **This is the acceptance test that matters
      to the lab** — it is what makes eVOLVER numbers and plate-reader numbers the same
      quantity.

### 10.1 Validation data

There is **no real multi-hour eVOLVER run in `experiments/`** — the longest
`vial00_OD.csv` is 15 rows. Verification rests on synthetic series, `MockSerialManager`,
and the plate-reader round-trip. The round-trip needs the notebook's companion files
(`2026_08_16 DEO.074-4.xlsx`, `strain_layout.csv`, `strain_info.csv`, `growth_media.csv`),
none of which are in the repository. Add the first real turbidostat run to the regression
corpus when one exists; that is also the corpus §22.2 needs.

---

## 11. Corrections required to `SPEC.md` §17 and `ROADMAP.md` Session N

### 11.1 "Two estimators, both reported" no longer holds

Both documents are built around reporting segment regression and a dilution-rate estimator
side by side, with their divergence as a §22 detection rule. Per §0 there is now **one**
reported growth rate, and the dilution calculation is a gated diagnostic. Rewrite:

- §17's "Two estimators, both reported" → one estimator, plus a diagnostic with its gating
  condition stated.
- Session N's "Report both, and treat their disagreement as a diagnostic" → keep the
  disagreement idea, drop the second reported number.
- `SPEC.md` §22.1's *estimator divergence* row and Session V's equivalent must note that
  the rule is dormant until pump calibration exists.
- `SPEC.md` §6's `GET /api/growth_rate` example response shows `mu_dilution` beside
  `mu_per_hour` as co-equal; restructure per §6 above.
- `SPEC.md` §14 open question 9 ("what growth-rate estimate does the lab consider
  authoritative?") is now **answered** — close it.

### 11.2 The `growth_rate_per_hour` column does not exist where both docs say it does

`growth_rate_per_hour` appears exactly once in `data_logger.py`, at line 62, inside
`ESCALATION_HEADER` — i.e. in `escalation_log.csv`, written only in morbidostat mode. The
per-vial `OD_HEADER` is `(timestamp, elapsed_hours, raw_adc, calibrated_od, n_valid, flag,
dark)`. Both §17 and Session N describe this as existing work; it is new work on an
established file format (§7.7). If morbidostat is removed, that column disappears entirely.

### 11.3 The low-OD floor is specified twice, differently, and both are wrong

§17's table says "OD < 0.1 → return `None`"; Session N says "below ~0.05". Neither is
right, by Session N's own argument two sentences later: optical sensitivity varies
four-fold across sleeves, so the floor must be **per-vial**, derived from that run's blank
SD. Now implementable — O2 is built — with the §8 fallback until a blank is actually taken.

### 11.4 The dilution formula assumes the wrong mixing model

`ln(1 + vᵢ/V)` is bolus-add-then-overflow; this machine fires influx and efflux
simultaneously, which is perfusion → `v/V` (§4.4). Change it in both documents and state
the mixing assumption explicitly rather than leaving it implied by the algebra.

### 11.5 Two staleness items worth folding in while editing

- §17's edge-case table cites "turbidostat warmup (first 8 cycles, §9)", and §9 still
  documents a sub-second deficit accumulator for the turbidostat that
  `CONTROL_MODE_AUDIT.md` T-1/T-3 removed — `turbidostat.py`'s docstring now explicitly
  disclaims it ("There is deliberately no deficit accumulator here"). The audit is newer
  and matches the code.
- Session N is marked "Depends on: nothing." True for the estimator, now that the
  dilution calculation is gated rather than reported — but worth stating that the
  diagnostic half is dark until O3's bench work is done.
