# SCOOP_WATCH.md — Competitive monitoring log

Weekly automated check for competing eVOLVER control-software efforts. Exists because the
Khalil Lab demonstrably wanted a system like this one (see `ADOPTION_ANALYSIS.md` §1) and
their funded attempt is archived rather than dead — the archive is reversible, three
unenumerated forks exist, and a private continuation cannot be excluded.

**Scheduled task:** `evolver-scoop-watch`, Mondays 08:04 local.
**Canonical task file:** `C:\Users\hourt\Claude\Scheduled\evolver-scoop-watch\SKILL.md`
(the scheduler reads from there — do not move it into this repo; edit it via the Scheduled
panel or ask Claude to update it).
**This file** is the version-controlled record: the baseline below, plus one appended entry
per run.

---

## Why the baseline lives here

The baseline is the perishable part. If the scheduled task is deleted or the machine is
rebuilt, the ability to detect *change* is lost with it — a monitor without a baseline just
re-reports the same facts forever. Keeping it under version control here means the watch can
be reconstructed from the repo alone.

---

## What counts as a scoop

Three claims in this project are genuinely novel and therefore scoopable. Ranked by how
exposed they are:

1. **Automatic, pre-emptive consumables interlocking.** Nobody in this ecosystem has it.
   `evolver-ng`'s answer to a dry carboy was a human pressing `/abort`; the official stack's
   answer is a hardware emergency-efflux line and a runbook for washing media off the
   motherboard. Most exposed because it is the easiest to add to an existing system.
2. **The audited event log + calibration provenance + failure taxonomy combination.**
   `evolver-ng` had a history server, so the log alone is not novel; the novelty is the
   operational-reliability story built on it. Anyone with runtime data could publish a
   failure taxonomy — this is the "anyone could do this" item in the portfolio.
3. **Device-hosted single-process control speaking the legacy serial protocol.** Least
   exposed: nobody else has a commercial or academic reason to write legacy framing.

**Not threats**, and the watch should not raise an alarm for them: hardware/PCB/CAD repos,
analysis or plotting libraries, min-eVOLVER variants, Pioreactor or Chi.Bio feature releases
on their own hardware, or eVOLVER papers where the platform is used rather than modified.

---

## Baseline — 3 August 2026

Verified by direct fetch on this date.

| Source | State |
|---|---|
| `ssec-jhu` org | 22 repos. `evolver-ng` (2★, 3 forks) and `evolver-ui` (1★, 0 forks) both **public archive, updated 11 May 2026**. No other eVOLVER repo |
| `FYNCH-BIO` org | Exactly 7 repos: `hardware` (25 Jun 2026), `evolver-arduino` (2 Oct 2025), `dpu` (19 Aug 2025), `evolver` (28 May 2024), `virtual_evolver` (12 Dec 2023), `evolver-electron` (6 Mar 2023, 31 open issues), `eVOLVER-Wiki` (20 Apr 2022) |
| `khalillab` org | 22 repos. `evolver-docs` updated 18 May 2026. `evolver-electron` there is a stale 2019 fork. No control-software successor |
| evolver.bio forum | Last software release announcement: **v2.0.0, September 2022**. `evolver-ng` **never mentioned**. Traffic ~a handful of threads/month, mostly hardware and install problems |
| eVOLVER wiki | Still documents DPU + Electron GUI as the official software. **No `evolver-ng` page** |
| SSEC project page | "Continuous Microbial Culture Instrumentation Control" still listed under Research Projects; page modified 27 Feb 2024 |
| `evolver-ng` readthedocs | Live. Version string **`0.1.1.dev50+g57ba110b5`** |
| Khalil Lab publications | **No eVOLVER software paper in 2024, 2025 or 2026.** Nearest adjacent: AneVO (*Trends in Biotechnology*, Dec 2025), García-Ruano (*Open Biology*, 2023) |

**Most sensitive tripwire:** the readthedocs version string. Repos can be unarchived quietly
and org listings can lag, but if `0.1.1.dev50+g57ba110b5` moves, someone is committing again.

**Standing response if a competitor appears:** get a timestamped bioRxiv preprint of the
platform paper up quickly. Per `PUBLICATION_STRATEGY.md` §3, Paper A is the only item in the
portfolio vulnerable to being scooped; Papers B and C are immune.

---

## Run log

<!-- Newest entries appended below. Format:
## YYYY-MM-DD
**Status:** NO CHANGE | CHANGE DETECTED | PARTIAL (n sources unverifiable)
- source — result
-->

## 2026-08-16
**Status:** NO CHANGE
- `ssec-jhu` org — unchanged (22 repos; `evolver-ng` 2★/3 forks and `evolver-ui` 1★/0 forks both still public archive, updated 11 May 2026; no new eVOLVER repo)
- `FYNCH-BIO` org — unchanged (exactly 7 repos, every push date and issue count matches baseline exactly, incl. `evolver-electron` at 31 open issues)
- `khalillab` org — unchanged (22 repos; `evolver-docs` updated 18 May 2026; `evolver-electron` still the stale 2019 fork; no control-software successor)
- evolver.bio `/latest` — unchanged (newest threads are hardware/install problems, e.g. Smart Sleeve plate files Jun 25 2026; no software announcement)
- evolver.bio `/c/software/8` — unchanged (last release-tagged post is still "eVOLVER Wiki + Software Release 2.0.0," Sep 2022; newest activity is a non-release "playing around with code" thread, Feb 2026)
- eVOLVER gitbook wiki sitemap — unchanged (DPU + Electron GUI still documented as official software; no `evolver-ng`, next-gen, or web-UI page)
- SSEC project page — description, PI, and body text unchanged; no resumption/handoff language. **Note:** page metadata `modified_time` now reads 2026-08-04 (baseline recorded 27 Feb 2024). Visible content is identical, so most likely a template/CMS re-save rather than a content edit — flagging for continued tracking, not treated as a substantive change this run.
- `evolver-ng` readthedocs (tripwire) — version string unchanged: `0.1.1.dev50+g57ba110b5`
- BU Khalil Lab publications — unchanged, no new eVOLVER software/instrumentation paper. Newest 2026 entries (T-DNA vectors, synthetic cell-cell adhesion, PI3K optogenetics) are unrelated; AneVO (Dec 2025) still nearest adjacent
- WebSearch sweep (general eVOLVER software/GUI, `"evolver-ng"`, failure-taxonomy, consumables-interlock, bioRxiv) — no hits on any threat-model item; interlock/failure-taxonomy results returned only generic pre-existing bioreactor patents, not new announcements
- Pioreactor changelog (optional) — direct fetch exceeded size limit; verified instead by keyword search (`evolver`, `interlock`, `consumable`, `failure taxonomy`) over the full saved output — no matches

## 2026-08-17
**Status:** NO CHANGE
- `ssec-jhu` org — unchanged (22 repos; `evolver-ng` 2★/3 forks and `evolver-ui` 1★/0 forks both still public archive, updated 11 May 2026; no new eVOLVER repo)
- `FYNCH-BIO` org — unchanged (exactly 7 repos, every push date and issue count matches baseline exactly, incl. `evolver-electron` at 31 open issues)
- `khalillab` org — unchanged (22 repos; `evolver-docs` updated 18 May 2026; `evolver-electron` still the stale 2019 fork; no control-software successor)
- evolver.bio `/latest` and `/c/software/8` — unchanged (newest threads are hardware/install problems; last release-tagged post is still "eVOLVER Wiki + Software Release 2.0.0," Sep 2022; newest software-category activity still "Playing around with code to learn," Feb 2026)
- eVOLVER gitbook wiki sitemap — unchanged (DPU + Electron GUI still documented as official software; no `evolver-ng`, next-gen, or web-UI page)
- SSEC project page — body text unchanged, no resumption/handoff language; `modified_time` still reads 2026-08-04 (same value as last run, not a new edit this week)
- `evolver-ng` readthedocs (tripwire) — version string unchanged: `0.1.1.dev50+g57ba110b5`
- BU Khalil Lab publications — unchanged, no new eVOLVER software/instrumentation paper; newest entries (T-DNA vectors, synthetic cell-cell adhesion, PI3K optogenetics, all 2026) are unrelated
- WebSearch sweep (general eVOLVER software/GUI, `"evolver-ng"`, failure-taxonomy, consumables-interlock, bioRxiv, FynchBio announcements) — no hits on any threat-model item. Two new bioRxiv preprints surfaced and were read in full to check: "OpenEvo" (Binomica-Labs, browser-based turbidostat on its own Arduino Mega hardware — automated dilution and CSV logging only, no consumables interlock, no failure taxonomy/calibration provenance — falls in the explicit min-eVOLVER-variant non-threat category) and "TurboPRANCE" (liquid-handler-based PACE evolution platform; mentions eVOLVER only as prior art for ePACE, not competing control software). Neither touches threat-model items 1 or 2
- Pioreactor changelog (optional) — fetched successfully this run (via GitHub blob URL); keyword search (`evolver`, `interlock`, `consumable`, `failure taxonomy`) over full text — no matches
