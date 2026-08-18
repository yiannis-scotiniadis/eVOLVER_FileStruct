# PI_BRIEFING.md — Talking points for the conversation with Farren

**Situation:** he is vaguely aware you've been working on the eVOLVER software but not of the
scope. This is the conversation that makes it real.

**The one thing to get right:** this must land as *"the lab now has a working instrument and
there may be a paper in it,"* not as *"look what I built."* The failure mode you are guarding
against is the thought — reasonable, and one any PI will have — that a graduate student got
absorbed in infrastructure instead of doing biology. Everything below is arranged to close
that off early.

---

## 1. Open with the instrument, not the software (~45 seconds)

Something close to:

> "The eVOLVER hasn't really been usable as a shared instrument — the original stack is a
> Mac-side Python script plus a desktop app, and it doesn't run on our machine's firmware.
> I've rebuilt the control software so it runs on the Pi inside the box and you drive it from
> a browser. It's deployed and it's currently running the Pichia campaign and the *C. necator*
> carboxysome work. I wanted to talk about where it fits and whether there's a paper in it."

Three things this does: leads with the lab's capability, establishes it is *already working*
rather than proposed, and names the two campaigns before he has to ask what it is for.

**Do not** open with the competitive landscape, GitHub archaeology, or the Khalil lab. That
comes later and briefly.

---

## 2. Connect it to the lab's own methodology (~1 minute)

This is the strongest card and it uses his own papers. Do not skip it.

> "The reason this matters beyond convenience is that recoding creates fitness defects and
> recovering them is adaptive evolution — that's Wannier's ALE of C321, and it's the whole
> premise of Colin's optimised-phenotype work. The throughput limit on that is how many
> independent lineages we can hold for weeks without losing one. That's the bottleneck the
> software addresses."

Have ready if he probes:

- Wannier et al., *PNAS* 2018 — first ALE of a sub-64-codon organism; RF2 mutations recover
  much of the fitness loss.
- Hemez et al. 2024 — 17% and 42% doubling-time reductions vs ancestral C321, with improved
  ncAA incorporation. His own lab, and explicitly framed as a strategy for phenotypic
  optimisation of GROs.
- Grome *Nature* 2025 — each further codon removed raises the burden, so the selection
  problem gets harder, not easier.

The point to land: **this is infrastructure for the lab's central methodology, not a tool for
one project.**

---

## 3. The landscape, in three sentences (~30 seconds)

Resist the urge to present the competitive analysis. He does not care about GitHub stars.

> "There isn't a maintained modern alternative. The official stack hasn't shipped a release
> since 2022 and doesn't speak our firmware. The Khalil lab did fund a professional rewrite
> through Schmidt Futures with a software centre at Hopkins — it's well designed, and it was
> archived in May with no successor."

If he asks why it was archived: **say you don't know.** That is the honest answer and you
have an email out to find out (or should). Guessing here would be the weakest moment in the
conversation.

---

## 4. What's actually differentiated (~45 seconds)

Two things, one line each. Everything else is table stakes.

> "The piece nobody has is an automatic consumables interlock — the software stops pumping
> before a carboy runs dry or waste overflows, without anyone present. The Khalil version's
> answer to that is a human pressing an abort button; the official documentation calls vial
> overflow the most common problem on the platform and its answer is a hardware efflux line
> and instructions for washing media off the motherboard. The second is a full audit trail —
> every pump event, every suppressed pump, every manual intervention, with the calibration
> version recorded per experiment."

If he wants a third: it speaks our 2016 firmware, which nothing current does.

---

## 5. Anticipated pushback — prepare these

**"Is this a distraction from your biology?"** — the most likely question, possibly unspoken.
> Both campaigns are running on it now, and neither could run reliably before. The remaining
> work is about three weeks of the plan, and it's the part that keeps overnight runs alive.

Have the number ready. `ROADMAP.md` weeks 1–3 (interlock, volume-based pumping, calibration,
logging, growth rate) are the ones that matter; weeks 7+ are cuttable.

**"Why not just use the official software?"**
> Two reasons. Our firmware is from 2016 and speaks a different serial dialect than the
> current stack — using it would mean reflashing all four microcontrollers. And the Mac-side
> stack is effectively unmaintained; the forum's software section is mostly people who can't
> get it installed.

Flag honestly: the incompatibility is an inference from two documented protocols
(`REFERENCES_VERIFICATION.md` §2.3). If he pushes, say it's documented but untested, and that
it's worth confirming on the bench before it goes in a paper.

**"Who maintains this when you leave?"** — legitimate, and he should ask it.
> It's a real risk and worth naming. The mitigations are documentation, a test suite that
> runs without hardware, and — if we go the JOSS route — external reviewers who have to
> install it from scratch. What would help most is a second person in the lab reading the
> code before I go.

Do not be defensive here. Conceding this cleanly buys credibility for everything else.

**"Are the numbers trustworthy?"** — raise this yourself before he does.
> Not yet, and that's my main outstanding worry. Every OD and every millilitre currently
> rests on calibration files inherited from a previous user that have never been checked.
> That's about forty minutes of bench work for the pumps and it needs doing before anything
> quantitative goes in a manuscript.

Naming your own weakness first is the single highest-return move in this conversation.

**"Is this actually publishable?"**
> Modestly. ACS Synth Biol is the realistic target — the lab published Merlin there, and the
> C. necator work. It's not a Nature Methods paper and I wouldn't try.

---

## 6. The asks — keep them small and specific

1. **Bench time for gravimetric pump calibration**, and agreement on who does it (~40 min for
   32 pumps, plus per-run OD blanks). This gates every quantitative claim.
2. **A view on authorship for the platform paper**, before the campaigns produce results. Say
   plainly that you'd expect to lead the platform and protocol papers and that you're happy
   to be a methods contributor on the biology ones — that framing is generous and makes the
   conversation easy.
3. **Permission to contact the Khalil lab** about the archived project. Low cost, and every
   published eVOLVER derivative has carried Khalil-lab involvement.
4. *Optional, if the moment is right:* load cells or float switches under the media bottles.
   Small spend; converts the interlock from an inference into a measurement.

---

## 7. Raising Nature Protocols — timing and framing

**Raise it last, as the end of a sequence, not as an aspiration.** Raised early it reads as
counting chickens; raised as the endpoint of a plan it reads as someone who has thought about
the whole arc.

Suggested phrasing:

> "Longer term — the lab has two Nature Protocols papers, so the format's familiar here. Once
> the Pichia or *C. necator* work is out, a protocol paper on running automated evolution
> campaigns across bacteria and yeast would be a natural follow-on. NP is mostly
> presubmission-driven, so it'd be an enquiry once we have a primary paper to point at,
> rather than a blind submission."

**Why this framing works:** it demonstrates you know NP is largely commissioned or pitched via
presubmission enquiry, which is the credibility test on that journal. Getting that wrong is
the tell of someone who hasn't looked into it.

**What NP wants**, if he asks: an established method, a primary paper behind it, and a
protocol other groups will actually run. Which means it needs the installability work — the
adapter, docs, a second installation somewhere — that you want to do anyway.

**Mention the cheaper alternative if he seems lukewarm:** *Methods in Molecular Biology* just
published a chapter on Pichia ALE *by serial batch passaging*. A companion chapter on
automated turbidostat ALE would sit directly beside the method it supersedes, and MiMB
chapters are solicited far more readily than NP articles. It's a lower-prestige but much
higher-probability route to being read by the people doing this by hand.

---

## 8. If it goes well, the follow-up

Send a one-page summary the same day — the publication sequence from
`PUBLICATION_STRATEGY.md` §3, the calibration ask, and the authorship note in writing. A
verbal agreement about authorship that isn't written down is not an agreement, and this is
exactly the kind of project where the builder ends up in three acknowledgements and first
author on none.

## 9. If it goes badly

The likeliest bad outcome is not rejection but drift — "sounds good, let's talk later." If
that happens, the fallback ask is the smallest one: **the forty minutes of pump calibration
and a named second reader for the code.** Both are cheap, both are things he can say yes to
immediately, and both make the larger conversation easier next time.
