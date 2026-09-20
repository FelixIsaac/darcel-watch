# SF Service Guide Watch

Hack for Humanity: San Francisco — 19 Sep 2026, Entrepreneurs First, co-hosted by MLH, powered by Google Gemini.
Built at a hackathon.

## Inspiration

We wanted to build an AI resource finder for San Franciscans needing food, shelter, healthcare, legal aid. Before writing code, we searched for prior art, the way we always should. It already exists: ShelterTech's SF Service Guide (sfserviceguide.org) — 1,759 organisations, 7,577 services, ~16,000 monthly users, open source, with a chatbot (`casey`) and a phone line (`VACS-MVP`). Building a 16th directory would have been vanity work at a hackathon that's supposed to help people.

So we asked a different question: what's actually broken about the one that exists? Their listings are vetted by volunteers at monthly datathons — human hours they don't have enough of. That's a resourcing problem, not a discovery problem, and it's the kind of problem an agent can help with.

It's named for Darcel Jackson, who founded ShelterTech after being injured as a welder and becoming unhoused himself.

## What it does

SF Service Guide Watch checks whether the directory's listings are still true, and how confident it should be that they are. It runs four tiers of checking, cheapest and most certain first: a structural pass over the stored data (free, cannot false-positive), a graph model of the directory (so one org's closure propagates to everything connected to it), and — only when the first tiers can't answer the question — a live fetch of the org's own website adjudicated by Gemini. Everything is a candidate for a human to review, never an assertion of fact, and we never write to ShelterTech's production system.

We measured the problem before building the fix, against the full directory, not a sample: **813 approved organisations. 598 (73.6%) have never been verified or certified by anyone.** The 215 that were, a median of ~7.7 years ago. Only 8 have been confirmed in the last three years.

The gap isn't discovery. It's freshness. A wrong shelter address at 9pm is worse than no answer at all.

## How we built it

We inverted the architecture partway through, after our most expensive tier produced two false positives. The pipeline now runs cheapest-and-most-certain first:

- **Structural** (`verify.check_phone_format`): reads a stored phone record and flags three unambiguous shapes — an empty number field with the real number typed into the label instead, a non-US country code on a Bay Area listing, or two values run into one field. No fetch, no model, runs over the whole corpus every time because it's free.
- **Graph** (`graph_falkor.py` on FalkorDB, `graph.py` as a zero-dependency fallback): the directory modelled as Org→Service→Category, Org→Address, Org→Phone. FalkorDB builds **7,405 nodes** on the full corpus and answers `blast_radius`, `contradictions`, and `propagate_staleness` as real Cypher queries — a traversal, a pattern match, a variable-length path. `crosscheck.py` proves the in-memory fallback agrees with FalkorDB exactly, and caught one real divergence: the fallback's BFS can cross an edge twice, which Cypher's relationship-uniqueness rule forbids. We made the Cypher reproduce the fallback's behaviour with an explicit extra query rather than silently changing either side.
- **Live source + model** (`verify.gather_evidence`, `verify.adjudicate_gemini`): the expensive tier. Fetches the org's homepage, walks likely sub-pages if that doesn't settle the question, and hands deterministic evidence to Gemini 2.5 Flash — via **OpenRouter** — to judge whether it contradicts the stored value. Instructed to abstain on anything ambiguous. Runs only against a `BUDGET`-sized subset per pass, because it's the tier that costs money and produces false positives if we're not careful.
- **`run.py`** chains all of it — harvest → structural (whole corpus) → triage → live-verify (budgeted) → blast radius / priority ranking → staleness propagation → `out/results.json` and `out/graph.json`.
- **`freshness.py`** scores every listing separately for currency — see below — into `out/freshness.json`.
- **`notify/web.ts`**: one Node server on :8787, no framework, serving four pages off those JSON files — Dashboard, Review (the queue, one item at a time), Graph, Freshness — plus a `/api/run` + SSE stream so the dashboard can trigger a real pipeline run and show it working live.

## What we found

### We audited the wrong API, and it produced a false headline finding

Our first phone-number audit read `askdarcel.org/api` (v1), an older Rails API. It reported 24 of 189 numbers as undialable, including a claim that an addiction-treatment helpline's number didn't work, and we published that as the project's headline finding.

**It was wrong, and we retracted it.** The live site actually reads `https://www.sfserviceguide.org/api/v2`, and v1's phone formatter mangles US numbers — it reads a US area code as an international dialling code (510 → Peru, 209 → Egypt) and strips the "foreign" digits on the way out. Re-run against v2, 21 of the original 23 flagged numbers are fine, including the addiction-treatment line, which dials correctly on the live site. We caught this because a reviewer, comparing the two runs, asked "wait, the source for both is the same?" It wasn't.

### The real finding: 8 of 813 approved listings, a phone defect provable from the stored value alone

No model, no scraping. Lead example: **Building Futures**, a domestic violence services organisation — its listing's Call button links to `tel:null` while a working number (`510-808-7410`) sits typed into the label field right beside it, unused. The same defect (number in the label, `number` field empty) affects the San Francisco LGBT Community Center, Larkin Street Youth Clinic, Calvary Street Ministries, and Pilipino Senior Resource Center. Two more listings (SF311's TTY line, Toolworks) carry a non-US country code on a Bay Area number, so it's parsed and dialled wrong. One (Internet For All Now) has two values run into a single phone field.

### The live run: structural checks carry the confident findings, the model mostly abstains

At `BUDGET=25` against the full corpus: 33 checked, **8 discrepancies (all structural — the phone defects above), 9 abstained, 16 matched.** None of the discrepancies in this run came from the model's judgment alone. One abstention: our regex evidence-gatherer flagged what looked like a phone number on an org's page, but it was a Zoom meeting ID — the model declined to call it a match or mismatch.

We tightened the adjudication prompt twice specifically to make it more conservative, and it got more conservative. That's the design working, not a limitation: the cheap structural tier now carries every confident finding, and the model's job — judgment on genuinely ambiguous scraped evidence — is done correctly by abstaining when it can't tell.

### Four of our own bugs, caught by looking, none by tests

**1. A false positive in the model's judgment.** The first prompt version flagged Sutter Health at confidence 1.0 for a phone "mismatch" — stored `800-478-8837`, live page showed `916-297-9000`. That live number was scraped off a careers page: a hiring line, not a replacement main number. We rewrote the prompt to judge purpose, not difference, and it now correctly abstains: *"The live number is presented in the context of a hiring process, which is a different purpose."*

**2. A self-referential bug in our own code.** Oakland Healthcare & Wellness was, in an earlier run, shown to a reviewer as a discrepancy on its *address* — proposing to change it to the exact same value — because the adjudicator reasoned about a field no gathered evidence covered, and our change-request builder fell through to whatever finding was first available. Fixed by requiring a field to actually differ before it's emitted.

**3. A false positive caught by a human, not by us.** We once flagged Meals on Wheels of Alameda County for a "wrong" phone number that was already correctly stored among nine others on the same listing — our own `check_phone` only ever compared the first one. A human opened the real listing and found it. It now correctly abstains.

**4. We were reading the wrong API entirely.** The retraction above. The biggest of the four, because it invalidated a headline claim rather than one listing — and, like bug #3, found by a human looking at the actual data, not by any check we'd written.

Two of these four were caught by a person opening the real listing, not by our code. That's the human-in-the-loop argument made by evidence: the human in our loop caught the agent, exactly as designed.

## The freshness index

This is the centre of the project, not a side metric. Structural checks answer "is this value wrong right now" for the handful of listings where it's provably true. They can't answer the far more common case: nobody has looked at this listing in years, it's probably fine, and "probably" isn't good enough for someone deciding where to sleep tonight.

`freshness.py` treats staleness as **expiry, not error**. Every field has a half-life — phone/address 3 years, website/email 2 years, schedule 6 months — and a score decays from whatever evidence last supported it. The idea that makes this work: **evidence has a ceiling, not just an age.** A structural pass proves a value is well-formed, capped at 40/100, no matter how recently it was touched — because `(415) 555-0123` is a perfectly well-formed number for an organisation that closed in 2019. Only the organisation's own current source, or a human, resets the clock to a higher ceiling.

Run against all 813 approved listings: **median freshness 36.2/100. 8 fresh (1.0%), 600 stale (73.8%), 205 expired (25.2%).** Every listing gets one named next action — the single cheapest thing that would raise its score most — so the output is a work plan, not a guilt trip. We estimate roughly **204 volunteer-hours** to move the median from 36 to 70. That's a number ShelterTech's current monthly-datathon process has never had, because nobody has scored the whole directory this way.

We tested whether the method generalises past phones by applying it once to opening hours: **32 schedule entries across 13 organisations close before they open**, 20 of them looking like a "9 to 5" typed as 09:00–05:00. Same method, different field, a correction a volunteer can compute rather than just a flag.

## Challenges we ran into

- Getting burned by our own most expensive tier, twice (bugs #1 and #4), and having to invert the architecture as a result: cheap-and-certain first, live-source-plus-model only when the first tiers can't answer.
- Distinguishing an actually-stale listing from a slow/JS-only/bot-blocking website in the live-fetch tier. We cap fetches, check for real readable text, and abstain rather than guess.
- Deciding what a structural check is even allowed to claim. The rule that survived: only the three phone shapes a volunteer can fix by looking at the stored value alone — nothing that requires guessing at intent.
- Getting the freshness ceiling right: it took us a wrong first instinct (score by recency alone) before landing on "well-formed is not current," which is the idea the whole index depends on.

## Accomplishments we're proud of

- We searched first and found the real gap instead of shipping a duplicate directory.
- We measured the gap against the full directory, not a sample — 813 approved orgs, 73.6% never confirmed by anyone.
- Our strongest per-listing finding needed no model: 8 structural phone defects, provable from the stored value, cannot false-positive. Lead example: a domestic-violence organisation's Call button dials nothing.
- We built a second product on top of the first: a freshness index that scores the whole directory for currency and gives every listing a named next action, with a cost estimate (~204 hours) ShelterTech doesn't have today.
- We caught four of our own bugs by reading the output, never by a test — and retracted a headline finding in public rather than let it stand. Two of the four were caught by a human opening the real listing, which is exactly what the human-in-the-loop design is for.
- We're precise about what we did and didn't use. FalkorDB is real — Docker, Cypher, 7,405 nodes, cross-checked against an in-memory twin for parity. Gemini 2.5 Flash runs over OpenRouter, not the direct Google API; the direct path exists in the code but we didn't exercise it. iMessage review is wired against the real Spectrum SDK but has never delivered a message — the shared-line pool still refuses our allowlisted recipient. No production writes, ever.

## What we learned

Prior-art search is the highest-leverage hour of a hackathon, and so is checking your own sources a second time. The organisation-with-best-intentions problem in civic tech usually isn't "no one built a directory" — it's that the directory exists and nobody has the hours to keep it honest. We learned that the same discipline applies to our own pipeline: our most expensive, most impressive-looking tier (a model reading a live website) was also the one that produced every false positive we found. The fix in both cases was the same instinct — check the cheap, certain thing first, and don't trust confidence you haven't verified.

## What's next

- Offer this to ShelterTech directly — the point is to reduce their volunteer cost, not compete with them.
- Turn the one-off schedule/hours check into a shipped, tested module alongside `check_phone_format`.
- Fix iMessage delivery (Photon Business plan, or another dedicated-line path) so the review queue is actually answerable from a phone.
- Validate the freshness half-life and evidence-ceiling constants against ShelterTech's own datathon outcomes, instead of our judgment calls.
- Widen live-source verification beyond the org's own website (e.g. Google Business Profile status) with the same abstain-by-default discipline.

## 90-second demo script

| Time | Beat |
|---|---|
| 0:00–0:15 | "We set out to build an AI resource finder for SF. Then we searched — it already exists." Show sfserviceguide.org, mention 1,759 orgs / 16,000 monthly users. |
| 0:15–0:30 | "So we measured what's actually broken." Full corpus, not a sample: 813 approved orgs, 73.6% never verified or certified by anyone, the rest a median of 7.7 years ago. |
| 0:30–0:45 | The real structural finding: 8 of 813 listings have a phone defect provable from the stored value alone. Lead with Building Futures — a domestic-violence organisation whose Call button dials `tel:null` while a working number sits unused in the label field beside it. No model, cannot false-positive. |
| 0:45–0:55 | Say the retraction out loud: our first version of this exact finding was wrong — we were reading a superseded API whose phone formatter mangled US numbers into fake international ones. We caught it, deleted the false claim, and rebuilt the check against the API the live site actually uses. |
| 0:55–1:10 | Open the Freshness page: median 36.2/100, 600 of 813 listings stale, 205 expired, each with one named next action. Explain the one idea that matters: a well-formed value that's never been confirmed caps at 40 — being well-formed isn't being current. |
| 1:10–1:20 | Show the live dashboard: trigger a run over SSE, watch the four-tier pipeline execute — structural, graph (FalkorDB, 7,405 nodes), live-source, Gemini adjudication — landing on 8 discrepancies, 9 abstentions, 16 matches. |
| 1:20–1:28 | The human-in-the-loop story: two of our four self-caught bugs, including the API mistake, were found by a person opening the real listing, not by our code. That's the argument for shipping a review queue instead of an auto-updater. |
| 1:28–1:30 | Close: "This isn't a 16th directory. It's fewer volunteer-hours to keep the one that exists honest. Next step: give it to ShelterTech." |

## Anticipated judge questions

**Isn't this just scraping?**
Scraping is only the last of four tiers, and it's the one we trust least. Structural checks read the stored value alone and cannot false-positive. The graph tier is pattern matching and traversal, not scraping at all. Only when those can't answer a question do we fetch a live page, and even then deterministic code gathers the evidence — Gemini's only job is judging whether it actually contradicts the stored value, with an explicit instruction to abstain when it's ambiguous.

**Why not just use their existing chatbot (`casey`)?**
`casey` answers a user's question from the data as stored. It doesn't check whether that stored data is still true. We're not building a better front end — we're checking the back end the front end (and the phone line) both depend on.

**Did you actually use a graph database?**
Yes — FalkorDB, over Cypher, in Docker, 7,405 nodes on the full corpus. `blast_radius` is a traversal, `contradictions` a pattern match, `propagate_staleness` a variable-length path. There's also a zero-dependency in-memory fallback for when FalkorDB isn't running, and `crosscheck.py` proves the two produce identical results across the whole corpus — it even caught a real divergence before we shipped: the fallback's BFS can cross the same edge twice, which Cypher forbids inside one path. We kept the fallback's semantics and added an explicit second Cypher query to match them, instead of quietly changing one side to make the check pass.

**Do you ever write to the SF Service Guide?**
No. Every request is a GET. Change requests are written to a local file for a human to review; nothing is POSTed to their production system.

**How do you know your verification is more accurate than what's already there?**
We don't claim it is across the board — we retracted our first headline finding when it turned out to be wrong. What we claim is narrower and checkable: 8 structural findings that are provably true from the stored value alone, and a freshness score that's honest about what it doesn't know. Every claim carries a source; abstention is reported as a metric, not hidden.

**Didn't you get a finding wrong (the phone numbers)?**
Yes — badly, and publicly. Our first pass read a superseded API whose phone formatter corrupted US numbers into fake international ones, and we published 24 "undialable" numbers as a headline finding before catching it. 21 of 23 were artifacts. We deleted the claim, rebuilt the check against the API the live site actually uses, and found a smaller, verified, structurally-provable version of the same idea (8 of 813). We're leading with this story, not hiding it, because catching your own headline finding being wrong and saying so is a stronger claim about everything else in this repo than getting it right the first time would have been.

**Are you calling Gemini directly through Google's API?**
No — our runs go through OpenRouter (`google/gemini-2.5-flash`). The code also has a direct-Google-AI-Studio path that activates on a Google-shaped key, but we didn't exercise it in the runs we're demoing. Same model and prompt either way; only the transport differs.

**Your model mostly abstains — isn't that a weak result?**
No, it's the point. In our live run, every confirmed discrepancy came from the deterministic structural tier, not the model — and every model-adjudicated case came back abstain or match, including a Zoom meeting ID our regex mistook for a phone number. We tightened the adjudication prompt twice specifically to make it more conservative, and it got more conservative. A model that abstains on genuinely ambiguous scraped evidence, letting a cheap structural check carry the confident findings, is doing its job. The failure mode we were guarding against was false confidence, not caution.

**Does the iMessage/review-by-text feature actually work?**
Not end to end. Authentication succeeds, the iMessage provider is enabled, and our reviewer number is registered as a project user — but Photon's shared-line pool (the plan we're on) still refuses delivery. A dedicated line on their Business plan isn't subject to that restriction; we haven't upgraded to confirm it fixes it. We're saying this plainly rather than demoing around it.
