# REFERENCES_VERIFICATION.md — Check my work

Every substantive claim made in `COMPETITIVE_ANALYSIS.md`, `ADOPTION_ANALYSIS.md` and
`PUBLICATION_STRATEGY.md`, with the source and what you should see. Verified 3 August 2026;
re-verified by the scheduled watch on 16 August 2026.

**Confidence key:**
- **[D] Direct** — I fetched the page and read it. Should reproduce exactly.
- **[I] Inference** — reasoning from two or more direct observations. Reproducible logic, but I did not test it.
- **[E] Estimate** — triangulation with real uncertainty. Treat as an order-of-magnitude claim.

Read the **[I]** and **[E]** items first; those are where I could be wrong.

---

## 1. The official eVOLVER stack — architecture and health

| # | Claim | Source | What you should see | Conf. |
|---|---|---|---|---|
| 1.1 | Three-part architecture: RPi server + DPU on a separate lab computer + Electron GUI | https://khalil-lab.gitbook.io/evolver/software/overview-of-software-architecture | "Lab Computer — This is where the logic for experiments happens" | **[D]** |
| 1.2 | Experiment logic lives in `custom_script.py`, called once per broadcast (default 20 s) | https://khalil-lab.gitbook.io/evolver/software/dpu/custom_script.py | "The functions are not called continuously in a loop - they are only called when new data is received" | **[D]** |
| 1.3 | Repo push dates showing stagnation | https://github.com/orgs/FYNCH-BIO/repositories?sort=updated | 7 repos. `hardware` Jun 2026; `evolver-arduino` Oct 2025; `dpu` Aug 2025; `evolver` **May 2024**; `evolver-electron` **Mar 2023** | **[D]** |
| 1.4 | Last GUI release is `v2.0.1-beta`; 31 open issues | https://github.com/FYNCH-BIO/evolver-electron/releases | Release list ends at 2.0.1 BETA | **[D]** |
| 1.5 | Forum's software category is dominated by installation failures | https://www.evolver.bio/c/software/8 | "DPU installation issues", "Issues compiling evolver-electron on Ubuntu 22.04" (0 replies), "Stuck with DPU installation" | **[D]** |
| 1.6 | Last software release announcement was Sept 2022 | Same page, scroll to bottom | "eVOLVER Wiki + Software Release 2.0.0", Sept 2022 | **[D]** |
| 1.7 | min-eVOLVER ships without GUI integration | https://khalil-lab.gitbook.io/evolver/extensions/min-evolver/software-installation-and-startup | "Note: as of now, there is no GUI integration. Command line only." | **[D]** |

### The single most important page

**1.8 [D] — The wiki's own Known Issues list matches your roadmap.**
https://khalil-lab.gitbook.io/evolver/software/known-issues

Look for: "Need a way to re-blank OD during the middle of an experiment"; "Remote access /
backup of experiment — Also alerts for"; "Pumps run for a second when server is restarted —
Can cause unintended media spillage"; "Calibrations require command line"; "Addition of
experiment parameters (pH, light, etc) requires many manual changes to code"; and the request
for long-format single-CSV output.

**1.9 [D] — Vial overflow is documented as the most common problem.**
https://khalil-lab.gitbook.io/evolver/troubleshooting/experiment-troubleshooting/vial-overflow-pump-failure-and-spills

"Vial overflow is one of the most common problems to plague eVOLVER experiments. Overflow
events can cause damage to internal components such as the PCB motherboard…" Note that every
mitigation offered is hardware or cleanup — there is no software interlock.

---

## 2. The protocol incompatibility — read this one critically

| # | Claim | Source | Conf. |
|---|---|---|---|
| 2.1 | Modern firmware uses `<ADDRESS><TYPE>,<VALUES>,_!` with a three-way handshake (`r`/`i`/`e`/`b`/`a`) | https://khalil-lab.gitbook.io/evolver/software/server-raspberry-pi → "Serial Message Structure" | **[D]** |
| 2.2 | Your machine uses `{prefix}{csv} !` outbound, `{prefix}{csv}end` inbound, no handshake | `server/serial_manager.py` lines 7–10, 46, 50, 219–243; `CLAUDE.md` protocol table | **[D]** |
| 2.3 | **Therefore the current official DPU/GUI cannot drive your machine without reflashing the SAMD21s** | — | **[I]** |

**2.3 is inference, and it is load-bearing for Position A.** The two protocols are documented
and they differ; I did not connect the modern stack to your hardware to confirm it fails. If
you want certainty, the cheap test is to check whether your Arduinos respond to a
handshake-form message. Worth doing before it goes in a manuscript.

---

## 3. `evolver-ng` — the Khalil/SSEC effort

| # | Claim | Source | What you should see | Conf. |
|---|---|---|---|---|
| 3.1 | Khalil Lab funded via Schmidt Futures VISS to work with SSEC-JHU on revamping the codebase | https://ai.jhu.edu/ssec/research/continuous-microbial-culture-instrumentation-control/ | "the Khalil Lab is working with the Scientific Software Engineering Center at JHU to revamp eVOLVER's codebase"; and the diagnosis "typically involves forking and directly modifying core eVOLVER code" | **[D]** |
| 3.2 | Both repos archived 11 May 2026, BSD-3 | https://github.com/ssec-jhu/evolver-ng and https://github.com/ssec-jhu/evolver-ui | Banner: "This repository was archived by the owner on May 11, 2026" | **[D]** |
| 3.3 | 2★/3 forks and 1★/0 forks respectively | Same pages, sidebar | **[D]** |
| 3.4 | They planned a user-scriptable controller | `evolver-ng` README, "Experiment controller extensions" | "a generic 'development' controller that can take a blob of python code to execute… a user can write code in the webUI" | **[D]** |
| 3.5 | VISS itself is still operating (so this was a project decision, not funder collapse) | https://www.schmidtsciences.org/viss/ | **[D]** |
| 3.6 | SSEC archives on engagement completion as routine | https://github.com/orgs/ssec-jhu/repositories | `flfm` and `flfm-ij-plugin` both archived 23 Apr 2026 | **[D]** |
| 3.7 | **Why it was archived is unknown** | — | **[Unknown]** |

### 3.8 [D] — It got much further than the README implies. Check this yourself.

I initially under-read this project and had to correct it. The README's "🚧 early
development / design goals" banners are stale. The documentation is not:

- https://evolver-ng.readthedocs.io/en/latest/ — version string `0.1.1.dev50+g57ba110b5`
- https://evolver-ng.readthedocs.io/en/latest/installation.html — installing on the Pi
  *inside* the eVOLVER, `enable_uart=1`, `dtoverlay=pi3-disable-bt`, systemd units for server
  and UI, real driver `classinfo` with `addr: "od90"`
- https://evolver-ng.readthedocs.io/en/latest/usage.html — calibration procedures, named
  experiments, per-vial-subset controllers, a **history server** recording sensor/log/event
  per vial, `/abort`
- https://evolver-ng.readthedocs.io/en/latest/concepts.html — read→control→commit loop with a
  correct account of the all-vials-per-serial-call constraint

**Whether it ever ran a real culture is unknown.** There is no published data, no forum
report and no paper. Absence of evidence only.

### 3.9 [D] — What it did *not* have. This is your differentiation; verify it directly.

https://evolver-ng.readthedocs.io/en/latest/usage.html#aborting — read the whole section.
They identify the exact failure mode ("feedback about certain conditions in the environment
(for example, the liquid volume in a vial)") and answer it with a **human-pressed button**.

https://evolver-ng.readthedocs.io/en/latest/development/controllers.html — "At present, the
logger is configured only with basic handling, and will print to stdout"; and the testing
framework is an open issue (#156), unbuilt.

No media/waste model, no watchdog, no maintenance mode, no growth-rate service, no anomaly
detection, no hygiene records anywhere in the docs.

---

## 4. Scoop status — the seven checks

All **[D]**, all re-run 16 Aug 2026 with no change. See `SCOOP_WATCH.md` for the baseline
table and the run log.

`ssec-jhu` / `FYNCH-BIO` / `khalillab` org listings · https://www.evolver.bio/latest and
`/c/software/8` · https://khalil-lab.gitbook.io/evolver/sitemap.md ·
https://ai.jhu.edu/ssec/research/... · https://evolver-ng.readthedocs.io/en/latest/ ·
https://www.bu.edu/khalillab/papers.html

**Residual risk is [Unknown], not [D]:** private repos, the three unenumerated `evolver-ng`
forks (GitHub's fork-listing pages would not render for me), a closed commercial FynchBio
product, or a parallel effort elsewhere. One email to `ssec@jhu.edu` closes most of it.

---

## 5. Alternative platforms

| # | Claim | Source | Conf. |
|---|---|---|---|
| 5.1 | Pioreactor ships monthly (26.5.x → 26.7.0 through mid-2026), SQLite, YAML experiment profiles, plugins, MQTT, MCP endpoint | https://github.com/Pioreactor/pioreactor/blob/master/CHANGELOG.md | **[D]** |
| 5.2 | Pioreactor uses a Kalman filter that survives dilution events | https://docs.pioreactor.com/user-guide/growth-rate-modelling | **[D]** |
| 5.3 | Chi.Bio serves a web UI from the device at `192.168.7.2:5000`, up to 8 reactors per control computer | Steel et al., *PLoS Biology* 2020, https://journals.plos.org/plosbiology/article?id=10.1371/journal.pbio.3000794 | **[D]** |
| 5.4 | A third party integrated a pH module into Chi.Bio's UI and published it | *Biochemistry* 2024, https://pubs.acs.org/doi/10.1021/acs.biochem.4c00149 | **[D]** |
| 5.5 | Neither has the shared-carboy consumables problem, because both are one-reactor-per-controller | — | **[I]** |

---

## 6. Citation and adoption data

All counts from **Semantic Scholar**, August 2026. Google Scholar will read higher — these
are a consistent lower bound, not the headline number.

| Paper | Cites | "Influential" | Check |
|---|---|---|---|
| Wong et al., eVOLVER, *Nat Biotechnol* 2018 | 210 | 6 | `api.semanticscholar.org/graph/v1/paper/DOI:10.1038/nbt.4151?fields=citationCount,influentialCitationCount` |
| Steel et al., Chi.Bio, *PLoS Biol* 2020 | 83 | 5 | DOI `10.1371/journal.pbio.3000794` |
| García-Ruano et al., eVOLVER upgrade, *Open Biol* 2023 | **7** | **0** | DOI `10.1098/rsob.230118` |
| EVE, *eLife* 2022 | 6 | 0 | DOI `10.7554/eLife.83067` |

**6.5 [E] — Installed base: ~40–100 labs own an eVOLVER; ~25–60 actively running.**
This is the softest number in any of these documents. It is triangulated from fork counts
(dpu 21, evolver 19, evolver-electron 17), forum activity, the citation profile above, and
derivative-platform counts. **No published figure exists.** Do not put a number in a
manuscript without saying how it was derived.

---

## 7. Isaacs Lab precedents (all [D], one page)

https://isaacslab.yale.edu/publications

- **Quintin, Ma, … Isaacs & Densmore, "Merlin", *ACS Synth Biol* 2016** — the lab has
  published a pure software tool, and it went to ACS Synth Biol. *I initially claimed the lab
  had never published software; you caught this and you were right.*
- **Nakamura, Fulk, Johnson & Isaacs, *ACS Synth Biol* 2025** — inorganic carbon uptake in
  *C. necator* H16. Your carboxysome antecedent, same organism, same venue.
- **Gallagher et al. and Ma et al., *Nature Protocols* 9:2301 and 9:2285 (2014)** — two NP
  papers; the format is familiar to the lab.
- **eMAGE lineage:** Barbieri *Cell* 2017 → Liang bioRxiv 2020 → Ciaccia *Nat Commun* 2024.
- **Recoding/ALE thread:** Grome *Nature* 2025 (one stop codon); Hemez et al. 2024 (17% and
  42% doubling-time reductions in a recoded strain); Wannier *PNAS* 2018 (first ALE of a
  sub-64-codon organism), https://pubmed.ncbi.nlm.nih.gov/29440500/

**7.6 [D] — Pichia ALE state of the art is serial batch passaging.**
"Adaptive Laboratory Evolution of *Pichia pastoris* by Serial Batch Cultivation", *Methods in
Molecular Biology* 2026, https://link.springer.com/protocol/10.1007/978-1-0716-4779-0_26

**7.7 [D] — Autotrophy claims are made in chemostats.** The 2025 reductive-glycine-pathway
paper claims 17% higher biomass yield in chemostat: https://www.nature.com/articles/s41564-025-01941-9

---

## 8. Where I was wrong, corrected

Kept visible so you can calibrate how much to trust the rest.

1. **"`evolver-ng` never drove a vial."** Overclaim from a stale README. The docs describe
   hardware installation, drivers and calibration procedures. Corrected in
   `ADOPTION_ANALYSIS.md` §1.
2. **"The Isaacs lab has never published a software paper."** Wrong — Merlin, 2016.
3. **"Position A = serve the legacy cohort."** Too narrow. Firmware is flashable, so vintage
   is a transitional segment, not a durable one. Corrected in `ADOPTION_ANALYSIS.md` §3.
