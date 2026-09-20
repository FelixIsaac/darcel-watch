# shelflife

**Every fact in a civic directory has a shelf life.** A phone number stays true
for years; opening hours for months. A perfectly formatted phone number for an
organisation that closed in 2019 is still perfectly formatted — being well-formed
is not being true.

shelflife finds contact data that is provably broken, checks everything else
against each organisation's own website, scores how much evidence there is that
each fact is still current, and **abstains when it isn't sure.**

Built against [ShelterTech](https://sheltertech.org)'s
[SF Service Guide](https://sfserviceguide.org) — 813 approved organisations —
read-only, at Hack for Humanity: San Francisco, 19 Sep 2026.

```
support    ≥ 0.69   precision 1.000   recall 0.867     held out, 43 orgs
contradict ≥ 0.99   12 flagged, 0 of them actually on the page
```

> **[`FACTS.md`](FACTS.md) is the single source of truth for every number in this repo** — what was measured, how, and what we got wrong. Six claims have been retracted from this project so far. If a number appears here and not there, it is a bug in this README. See [What we got wrong](#what-we-got-wrong) below.

| | |
|---|---|
| [**PROBLEM.md**](PROBLEM.md) | What is actually broken, measured — and why it matters at 2am |
| [**VISION.md**](VISION.md) | What this becomes, and the commitments that constrain it |
| [**ARCHITECTURE.md**](ARCHITECTURE.md) | How it works, what each part may and may not do |
| [**JOURNAL.md**](JOURNAL.md) | How it was built, including five wrong turns and what they cost |
| [**FACTS.md**](FACTS.md) | Every number, with the command that establishes it |

## What it is

We set out to build an AI resource finder for San Franciscans needing food, shelter, healthcare, legal aid. We searched first. It already exists: ShelterTech's [SF Service Guide](https://sfserviceguide.org) — 1,759 organisations and 7,577 services counted from their live API, open source ([github.com/ShelterTechSF](https://github.com/ShelterTechSF)), with a chatbot (`casey`) and a phone line (`VACS-MVP`). ShelterTech report 16,000+ monthly users. Building another directory would have been vanity.

So we asked what's actually broken. **Every number below is in [`FACTS.md`](FACTS.md), with how it was established. If a claim isn't there, we don't make it.**

## The measured baseline

Full corpus, not a sample: **813 approved organisations** in the live SF Service Guide.

**The directory is actively maintained.** 808 of those 813 listings were updated within the last 90 days — July 2026: 568 listings, August: 204, September: 36 — and they land in bursts on particular days rather than a trickle, the shape you'd expect from datathon sessions. Whatever is wrong here, it is not that nobody is doing the work.

What is missing is **provenance**.

| Metric | Count |
|---|---|
| Approved organisations | 813 |
| Updated within 90 days | 808 (median 53 days) |
| **No verification signal at all** — no `verified_at`, no `certified_at`, no `certified` flag | **523 (64.3%)** |
| Listings with a `verified_at` date | 144, newest **2022-10-12** |
| Listings with a `certified_at` date | 124, newest 2026-09-15 (9 in 2026) |

`updated_at` records *that something changed*. It cannot distinguish a careful confirmation against the organisation's own website from a typo fix. The field that would carry that distinction — `verified_at` — stopped being written around 2022. `certified_at` is still written, but rarely.

So the honest problem is not "nobody checks." It's that **there is no machine-readable record of what was checked, so nobody — including ShelterTech — can tell a freshly confirmed listing from a stale one.** That is what the freshness index below is for, and why it scores the directory at 36.2: almost nothing carries evidence stronger than "well-formed".

The gap in this directory is not discovery. It's provenance. A wrong shelter address at 9pm is worse than no answer.

## What we got wrong

Six claims have been retracted from this project. The pattern was the same every time: a number that was real, attached to an interpretation that was not. Full detail and provenance in [`FACTS.md`](FACTS.md) section E.

| Retracted claim | Why it died |
|---|---|
| "24 of 189 phone numbers are undialable (13%)" | An artifact of reading the v1 API, which mangles US area codes into international dialling codes. 21 of 23 vanish on v2. |
| "An addiction treatment helpline can't be called" | The live page dials it correctly. |
| "`5106544000105` should be `510.777.9560`" (Meals on Wheels) | We compared only the first of ten stored numbers. The main line was two rows below. |
| "47 duplicate listings" | Same name + same address are usually distinct programme listings, not duplicates. |
| "Website-identity check finds N broken listings" | The count measured our crawl depth, not their data — 53 findings became 18 as the crawler improved. |
| **"73.6% have never been verified" + "volunteers at monthly datathons, more work than hours available"** | **Two errors in one sentence.** `verified_at` was abandoned around 2022, so measuring it and calling the result "never verified" conflates a dead field with neglect. And the directory is actively maintained — 808 of 813 listings updated within 90 days. The defensible number is 523 (64.3%) carrying no verification signal at all; 73.6% excluded 75 listings that carry a `certified` flag without a date. Datathons are biweekly, not monthly, per ShelterTech's help centre — though that article is dated 5 November 2019, so we don't assert today's cadence either. |

That last one was this project's founding premise. It was wrong, and correcting it made the claim smaller and the project more honest: the issue is missing provenance, not missing effort.

One further note, since it has already happened: a search engine now returns this repo as a "third-party source corroborating" ShelterTech's 16,000-monthly-users figure. It is not. We cited them. Nobody should cite us back for it.

## What it found

### We read the wrong API, and it produced a headline finding that was wrong

Our first pass audited phone numbers through `askdarcel.org/api` (v1), the older Rails API. It reported 24 of 189 stored phone numbers as undialable — truncated below 10 digits — and we wrote that up as the project's headline finding, including a claim that an addiction-treatment helpline's number couldn't be dialled.

**That finding was an artifact, and we retracted it.** The live site (sfserviceguide.org) reads a different, newer API — `https://www.sfserviceguide.org/api/v2` — and v1's phone formatter mangles US numbers: it reads an area code as an international dialling code, so `510` becomes Peru and `209` becomes Egypt, and strips the "foreign" prefix on the way out. Verified live on resource 2258:

```
             v1 (askdarcel.org)        v2 (sfserviceguide.org, what users see)
phone 4636   "07779560"  (PE)          "(510) 777-9560"  (US)
phone 4639   "5106544000105"  (None)   "(510) 654-4000 ext. 105"  (US)
```

We were auditing a formatting bug in a superseded API, not the data anyone actually sees. Re-run against v2, the same check finds 21 of the original 23 flagged numbers are fine — including the addiction-treatment helpline, which dials correctly on the live site. We deleted the v1-based findings rather than keep any of them.

This was caught by reviewing the two runs side by side and asking whether the source for both was the same. It wasn't. That question is now the reason this project only publishes findings sourced from v2.

### The real finding: 8 of 813 approved listings have a phone defect provable from the stored value alone

No model, no scraping — arithmetic and string checks on the stored record itself, so this class of finding cannot produce a false positive. Three shapes, each one a volunteer can fix by looking at the listing:

**The number is typed into the label, and the number field is empty.** The listing's Call button links to `tel:null` while a working number is printed right next to it. Affected: **Building Futures** (domestic violence services, `510-808-7410` in the label), **San Francisco LGBT Community Center** (`(415) 865-5521`), **Larkin Street Youth Clinic** (number field empty), **Calvary Street Ministries** (`(415) 333-3017`), **Pilipino Senior Resource Center** ("call for more information", no number anywhere).

**A non-US country code on a Bay Area listing**, which means the number is parsed and dialled as a foreign number. SF311's TTY relay line is stored with a Swiss country code and renders as `057 012 31 17`, even though the same record also carries the correct `(415) 701-2311`. Toolworks has the same defect on an ASL line.

**Two values run into one field.** Internet For All Now has a 14-digit string in its phone field — two numbers, or a number and something else, concatenated.

Lead example: **Building Futures**, a domestic violence services organisation, has a Call button that dials nothing while a real number sits unused in the label one field over. A judge can load the listing and check this in ten seconds.

### The live run: structural checks carry the confident findings, the model mostly abstains

At `BUDGET=25` against the full v2-sourced corpus, the current run produces 33 checked listings: **8 discrepancies (all structural, the phone defects above), 3 abstained, 22 matched.** The abstain/match split moves between runs — the model is not deterministic — so treat those two as a snapshot and re-run to check. Every model-adjudicated finding in this run came back **abstain or match** — none of the discrepancies in this run came from the model's judgment alone. One abstention: the regex evidence-gatherer found what looked like a phone number on an org's page (Grassroots Open Assistive Tech) but it was actually a Zoom meeting ID — the model declined to treat it as a phone match or mismatch.

We're stating this plainly because it's the designed outcome, not a limitation: we tightened the adjudication prompt twice specifically to make it more conservative, and it got more conservative. The cheap structural check now carries every confident finding in this run. The model's job is judgment on genuinely ambiguous scraped evidence, and abstaining there is it doing that job correctly.

### Four of our own bugs, caught by looking, none by tests

**1. A false positive in the model's judgment.** The first version of the adjudication prompt flagged Sutter Health for a phone mismatch: stored `800-478-8837`, live page showed `916-297-9000`, confidence 1.0. Reading the actual quote, that number came off a careers page — a hiring line, not a replacement for the main number. We rewrote the prompt to judge purpose, not just difference: it now has to decide whether the live number plausibly replaces the stored one for the *same* purpose, and to abstain when the surrounding text suggests a different one — careers, fax, donations, a department, a second location. After the fix, Sutter Health correctly abstains: *"The live number is presented in the context of a hiring process, which is a different purpose."*

**2. A self-referential bug in our own code.** In an earlier run, Oakland Healthcare & Wellness was shown to a reviewer as a discrepancy on its *address*: `stored "3030 Webster St. Oakland 94609" → live "3030 Webster St. Oakland 94609"` — a proposed change to the exact same value. The adjudicator had reasoned about a field (phone) that no gathered evidence actually covered, and `build_change_request` fell through to whatever finding was first available, regardless of whether it genuinely differed. Fixed by requiring a field to actually differ before it's emitted at all, and downgrading to abstain when nothing gathered is actionable.

**3. A false positive caught by a human, not by us.** In an earlier run we flagged Meals on Wheels of Alameda County for a phone mismatch — stored `5106544000105`, live site `510.777.9560` — and proposed replacing the stored number. A human opened the actual listing. The site already lists `(510) 777-9560` as its Main Line. The listing carries nine phone numbers, one per programme or region, and `5106544000105` is `(510) 654-4000 ext. 105` — the J-Sei Nutrition Services line, correctly stored. Our own `check_phone` only ever compared the *first* stored number and never checked the other eight. Fixed by comparing against every stored number, and reporting an unmatched live number as a possible *addition* rather than a replacement when a listing carries many numbers.

**4. We were reading the wrong API entirely.** Described above — this is the largest of the four, because it invalidated a headline claim rather than one listing. Same lesson as #3: found by a human looking at the actual data, not by any check we'd written.

Two of these four were caught by a human opening the real listing, not by our code. That is the argument for the human-in-the-loop design made by evidence rather than assertion: **the human in our loop caught the agent, exactly as designed.** Nothing here ships straight to ShelterTech — every output is a candidate, and this is what "candidate" is for.

## The freshness index

Structural checks answer "is this value wrong right now." They can't answer the more common question: nobody has checked this listing in years, and it's probably still fine, but "probably" is doing a lot of work for a family deciding where to sleep tonight.

`freshness.py` treats staleness as **expiry, not error**. Every field carries an implicit shelf life — a phone number stays believable for years, opening hours for months — and a score decays from whatever evidence last supported it:

- **Half-life per field**: schedule 180 days, phone/address 1,095 days (3 years), website/email 730 days (2 years). **These are our judgement, not measurements** — never fitted to data. The right method is to mine ShelterTech's change-request history for how often each field actually changes. The same caveat applies to the field weights and the evidence ceilings below.
- **Evidence has a ceiling, not just an age.** `human_verified` can reach 100. `source_agreement` (the org's own site currently says the same thing) caps at 90. **`structural_ok` — well-formed, never confirmed — caps at 40, no matter how recently the record was touched.** This is the load-bearing idea: `(415) 555-0123` is a perfectly well-formed number for an organisation that closed in 2019. Being well-formed is not being current. Only the organisation's own source, or a human, resets the clock.
- **Every listing gets one named next action** — the single cheapest thing that would raise its score the most. A list of 800 stale listings is a guilt trip. A list of 800 listings each with one action ("confirm phone against the org's own site") is a work plan.

Run against the full 813-listing corpus:

| | |
|---|---|
| Median freshness | 36.2 / 100 |
| Fresh (≥70) | 12 listings (1.5%) |
| Stale (15–39) | 596 listings (73.3%) |
| Expired (<15) | 205 listings (25.2%) |

The band counts shift slightly between runs, because a "match" verdict from the agent counts as a confirmation and moves listings up. The median has held at 36.2.

**A cost, not just a score to feel bad about:** the index estimates roughly 204 volunteer-hours of confirmation work to move the median from 36 to 70. **That is an estimate resting on an estimate** — it is arithmetic over the half-lives and weights above, which are themselves judgement calls. Treat it as sizing the problem, not scheduling it. Its value is that nobody has scored the whole directory this way before, so there was no figure of any kind to argue from.

### The method generalises beyond phones

Applying the same approach to opening hours (not a shipped module — a same-method check run over the corpus once, to test whether this scales) found **32 schedule entries across 13 organisations that close before they open** — 20 of them look like a PM-conversion error ("9 to 5" stored as 09:00–05:00 instead of 09:00–17:00). Different field, identical method: a structural rule that's cheap, certain, and produces a correction a volunteer can compute, not just a report that something's wrong.

## Architecture

We inverted the design after getting burned. Every one of our false positives (bugs #1 and #4 above) came from the most expensive tier — a model judging live-scraped evidence. Every finding we can stand behind fully (the 8 phone defects, the freshness score) came from the cheapest tier. So the pipeline now runs cheap-and-certain first, and only reaches for a fetch or a model call when the cheap tiers can't answer the question:

```mermaid
flowchart TD
    A[1. Structural<br/>stored value only, free, cannot false-positive] --> B[2. Cross-field<br/>compare fields within/across records]
    B --> C[3. Graph<br/>blast radius, contradictions, staleness propagation]
    C --> D[4. Live-source + model<br/>fetch the org's site, Gemini adjudicates<br/>expensive - and where every false positive came from]
```

```mermaid
flowchart LR
    A[harvest.py: v2 API] --> B[graph_falkor.py / graph.py: build]
    B --> C[run.py: triage_score]
    C --> D[verify.py: check_phone_format<br/>whole corpus, free]
    C --> E[verify.py: gather_evidence + adjudicate<br/>budgeted candidates only]
    D --> F[run.py: merge + blast_radius + priority]
    E --> F
    F --> G[graph: propagate_staleness]
    G --> H[out/results.json, out/graph.json]
    H --> I[freshness.py]
    I --> J[out/freshness.json]
    J --> K[notify/web.ts :8787<br/>Dashboard / Review / Graph / Freshness]
```

- **`harvest.py`** — pulls listings from `https://www.sfserviceguide.org/api/v2`, the API the live site itself reads, read-only and unauthenticated. The work list comes from ShelterTech's own content-curation dataset (the CSV their datathon volunteers use), not random sampling — it's the list they already want looked at. Caches to `data_v2/`, kept separate from the retired v1 cache on purpose so a corrupted phone number can never leak back in.
- **Tier 1 — structural (`verify.check_phone_format`)** — reads a stored phone record and flags only three unambiguous shapes: empty number with a real one stuck in the label, a non-US country code on a Bay Area listing, or too many digits run together. No fetch, no model. Runs over the whole corpus every time, because it's free — an earlier version rationed it behind the same budget as the expensive tier, and the most certain findings briefly vanished from the queue as a result.
- **Tier 2/3 — the graph (`graph_falkor.py`, `graph.py`)** — models the directory as `org` nodes linked to `service`, `address`, `phone`, `category`. Runs on **FalkorDB** (Cypher, Docker) when reachable — real graph queries: `blast_radius` a traversal, `contradictions` a pattern match, `propagate_staleness` a variable-length path, **7,405 nodes on the full corpus**. Falls back to a zero-dependency in-memory adjacency dict (`graph.py`) when FalkorDB isn't running, so the pipeline never hard-fails. `crosscheck.py` asserts the two agree exactly over the whole corpus — all 816 orgs, all 230 contradictions in the exact order `run.py` publishes them, staleness from every org and at 1–3 hops, and the subgraph export — and exits non-zero on any mismatch. It caught one genuine divergence, which we resolved explicitly rather than papered over: the in-memory BFS walks, so it can cross the same edge twice and land a seed back on itself at distance 2, while Cypher's relationship-uniqueness rule forbids reusing an edge inside one path. We did **not** change the in-memory behaviour — we made the Cypher reproduce it, with a second named query (`STALENESS_SELF`) and a comment saying exactly why it exists, so the parity claim is honest about which backend defines the semantics.
- **Discovery — `discover.py`** — asks each organisation's site what pages it has, instead of guessing. `robots.txt` → the sitemaps it declares → recursive `sitemapindex` walk → URLs with `<lastmod>`; a bounded link-graph crawl for sites with no sitemap. Measured on 60 random corpus sites: **81% publish a sitemap** (32 declared in `robots.txt`), 65% carry `lastmod`, 19% fall through to the crawl. `Disallow` rules are honoured. Until this existed the only strategy was five hardcoded slugs, which found nothing on a domestic violence listing whose number sits on `/get-help/` — see [JOURNAL.md](JOURNAL.md), wrong turn 5. `<lastmod>` is also a signal we didn't have before: *source-side* freshness, as opposed to when the directory last changed.
- **Tier 4 — live source + model (`verify.gather_evidence`, `verify.adjudicate_gemini`)** — the expensive tier, reserved for questions the first three can't answer. Page candidates now come from `discover.py`; `fetcher.py` tries a direct GET then a rendering reader for JavaScript-only sites. Deterministic code gathers evidence; **Gemini 2.5 Flash, via OpenRouter**, judges whether it contradicts the stored value, instructed to abstain on anything ambiguous. Falls back to a conservative deterministic adjudicator with no key.
- **`jev.py` + `audit.py` — typed, calibrated judgment** — the newer path, and where this is heading. [TypeSafe's Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev) emits no strings at all: it returns calibrated probability distributions over answers we define, so it **cannot fabricate a citation** — it selects among spans and URLs built from bytes we fetched. Each claim gets *two* questions, `support` and `contradict`, because they are not complements: a page that never mentions a number scores low on both, which is `absent` and is **not a finding**. Measured over 12 random organisations and 50 claims: 25 supported, 21 uncertain, 4 absent, **0 contradicted** — and ~**$0.26** to audit all 813 listings. Thresholds are provisional until calibrated against a hand-labelled holdout.
- **`run.py`** — orchestrates all four tiers, computes `blast_radius`/`priority` for confirmed discrepancies, runs `propagate_staleness`, and writes `out/results.json` and `out/graph.json`.
- **`freshness.py`** — the fifth stage, scoring every approved listing for currency (see above) into `out/freshness.json`.
- **`notify/web.ts`** — one Node HTTP server on **:8787**, no framework, serving four pages backed by the JSON files above: **Dashboard** (`/`), **Review** (`/review` — the queue, one item at a time, with accept/reject actions), **Graph** (`/graph` — the subgraph explorer), **Freshness** (`/freshness` — the score and next-action list). `POST /api/run` starts the pipeline from the dashboard; `GET /api/run/stream` streams its stdout live over Server-Sent Events, so a demo can trigger a real run and watch it work rather than show a static screenshot. Binds to localhost and only ever spawns the local pipeline — it never contacts sfserviceguide.org itself.
- **`notify/spectrum.ts`** — the same review queue, reachable over iMessage/WhatsApp via Photon (Spectrum), so a volunteer could answer from their phone. **Not working end to end yet**: authentication succeeds, the iMessage provider is enabled, and the reviewer's number is allowlisted as a project user, but Photon's shared-line pool (Free/Pro plan) still refuses delivery. The Business plan's dedicated line isn't subject to that restriction; we haven't upgraded to test it. Stated plainly rather than left ambiguous.

## How to run

```bash
# 1. Harvest the curation dataset (read-only, cached to data_v2/)
python3 harvest.py

# 2. Run the pipeline
OPENROUTER_API_KEY=sk-or-... .venv/bin/python run.py   # FalkorDB backend + Gemini adjudication
python3 run.py                                          # in-memory graph, still end to end
python3 freshness.py                                    # freshness scoring, reads out/results.json

# 3. Score currency, then serve the app
npm run web    # http://localhost:8787 - Dashboard / Review / Graph / Freshness
```

`.env` (gitignored, see `.env.example`) holds `OPENROUTER_API_KEY`, `FALKORDB_HOST`/`PORT`, and the optional Photon/Spectrum credentials for iMessage review.

## Design decisions

- **Read-only, always.** Every request to the SF Service Guide is a GET — `harvest.py`, `verify.py` and `fetcher.py` never set a method or a body. Nothing in this codebase can mutate their data; `change_request` payloads are written to a local file and never sent. (The only POSTs anywhere are to the LLM provider for inference, in `verify.py:76`.) Every output is a candidate for human review, never an assertion of fact.
- **Cheapest, most certain tier first.** Structural checks before cross-field checks before graph queries before a live fetch and a model call — because every false positive we produced came from the last tier, and every finding we fully stand behind came from the first.
- **Evidence has a ceiling, not just an age.** A well-formed value that's never been confirmed can't out-score a value someone actually checked, no matter how fresh-looking it is. That's the whole point of the freshness index.
- **Abstention is a headline metric, not a bug we hide.** In our live run, 9 of 33 checked listings abstained. That's reported, not smoothed over.
- **Every claim carries a source.** A structural finding points at the listing itself — the stored value is the evidence. A model finding carries a source URL and a verbatim quote. No quote, no claim.
- **We are not building another directory.** ShelterTech's guide, chatbot, and phone line already exist, are actively maintained, and — ShelterTech report — serve 16,000+ people a month. This tool adds a provenance signal their data doesn't currently carry; it doesn't replace anything and it isn't a criticism of their upkeep.

## Limitations

- We got the source API wrong once, on the most important finding in the project, and only caught it by a human asking a pointed question. We have no reason to believe there isn't a fifth mistake we haven't found yet — treat every number here as checkable, not as settled.
- Live-source verification (tier 4) only runs against a `BUDGET`-sized subset (25 by default) per run, not the whole corpus — it's the expensive tier on purpose.
- Evidence gathering there is regex-based against plain-text scraped HTML — it will miss anything not phrased the way our patterns expect, and can't read JS-rendered content.
- The schedule/PM-conversion finding was a one-off exploratory check, not a shipped, tested module like `check_phone_format`.
- The freshness half-life and evidence-ceiling numbers are our own judgment calls, not fitted to ShelterTech ground truth.
- iMessage/WhatsApp review does not deliver end to end yet (see `notify/spectrum.ts` above).
- Change requests are emitted, never submitted. Getting them into ShelterTech's actual review workflow requires their cooperation.

## Credits

Built against [ShelterTech](https://sheltertech.org)'s [SF Service Guide](https://sfserviceguide.org) and its public API, read-only.

ShelterTech was founded by **Darcel Jackson**, who became unhoused after being injured as a welder in San Francisco, and who built the directory so the next person in that situation could find help. This project only exists because ShelterTech already built and open-sourced the thing worth improving, and it is not a criticism of how they maintain it.

ShelterTech maintain the guide with volunteers and paid community representatives — including people with lived experience of homelessness — on a programme budget they put at roughly $200,000 a year. Their help centre describes datathons every two weeks with a final review by the Homeless Advocacy Project; **that article is dated 5 November 2019, so we can't assert what the cadence is today.** Nothing in this repo is a claim that they aren't doing the work. They are. It is a claim that the data doesn't record what was checked, which is a different and much smaller problem.
