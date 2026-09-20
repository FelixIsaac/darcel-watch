# SF Service Guide Watch

Named for Darcel Jackson, who founded ShelterTech after being injured as a welder in San Francisco and becoming unhoused. ShelterTech built the SF Service Guide so the next person in that situation could find help. SF Service Guide Watch exists to keep that guide accurate.

Built at Hack for Humanity: San Francisco, 19 Sep 2026 (Entrepreneurs First, co-hosted by MLH, powered by Google Gemini). Built at a hackathon.

## What it is

We set out to build an AI resource finder for San Franciscans needing food, shelter, healthcare, legal aid. We searched first. It already exists: ShelterTech's [SF Service Guide](https://sfserviceguide.org) — 1,759 organisations, 7,577 services, ~16,000 monthly users, open source ([github.com/ShelterTechSF](https://github.com/ShelterTechSF)), with a chatbot (`casey`) and a phone line (`VACS-MVP`). Building a 16th directory would have been vanity.

So we asked what's actually broken. Their listings are vetted by volunteers at monthly datathons — human hours they don't have enough of. We measured the effect of that, live, during the hackathon, against their public unauthenticated API.

## The measured finding

Sample: 156 random resource IDs from the AskDarcel API (`askdarcel.org/api`), seed 7, IDs 1–2600.

| Metric | Count |
|---|---|
| IDs sampled | 156 |
| Marked `status: approved` (live to users) | 138 |
| Of those, `verified_at: null` (never verified) | 126 |
| Of those, carry a verification date | 12 |
| Median age of the 12 dated verifications | ~2,838 days (~7.8 years) |
| Verified in the past year | 0 |
| Of the 138, list a website we can check | 120 |

The gap in this directory is not discovery. It's freshness. A wrong shelter address at 9pm is worse than no answer.

## What it found

### The headline finding: deterministic, no model, zero false-positive risk

Before any Gemini call, a plain arithmetic check on the stored data itself — no scraping, no model, just counting digits — found this across the 138 approved listings in our sample:

```
approved listings scanned : 138
phone numbers stored      : 189
NOT dialable as stored    : 24  (13%)
  truncated (<10 digits)  : 20
  extension run into no.  :  4
listings affected         : 19 of 138
```

Twenty of those numbers are stored with the area code stripped off — too short to dial. The clearest single example: **MKL Rehab – Addiction Treatment Helpline** (#2514) has `94410046` on file — 8 digits, truncated, cannot be dialled. An addiction treatment helpline whose phone number does not work. No model was involved in finding this — it's a digit count.

A family justice centre appears in the same broken-number set (Alameda County Family Justice Center, `02678800`, domestic violence services) — a phone number nobody can call.

This check needs no model, no scraping, no judgment call. It's arithmetic on digit counts, so unlike our model-based findings below, it cannot produce a false positive. That contrast is the argument for the whole design: use the cheap deterministic check where it's sufficient on its own, and reserve the model for the genuine judgment calls a digit count can't make.

### The live run: structural checks carry the confident findings, the model mostly abstains

At `BUDGET=25`, the current run produces **2 discrepancies, 10 abstentions**. Both discrepancies are structural (`phone_format`) — the same digit-count check as above, not a model judgment:

| Org | ID | Stored | Issue |
|---|---|---|---|
| MKL Rehab – Addiction Treatment Helpline | #2514 | `94410046` | 8 digits, truncated |
| Oakland Healthcare & Wellness | #2312 | `02508000` | 8 digits, truncated |

Every model-adjudicated finding in this run came back **abstain**. That includes a listing where the regex evidence-gatherer found what looked like a phone number on the org's page but it was actually a Zoom meeting ID (Grassroots Open Assistive Tech, #2599) — the model declined to treat it as a phone match or mismatch, rather than guessing.

We're stating this plainly because it's the designed outcome, not a limitation: we tightened the adjudication prompt twice specifically to make it more conservative (see the bugs below), and on this sample its judgment on genuinely ambiguous scraped evidence is overwhelmingly "I can't tell — ask a human." The cheap structural check carries the confident findings. The model's job is judgment, and abstaining on ambiguous evidence is the model doing that job correctly, not failing at it.

### Three of our own bugs, caught by looking, none by tests

**1. A false positive in the model's judgment.** The first version of the adjudication prompt flagged Sutter Health for a phone mismatch: stored `800-478-8837`, live page showed `916-297-9000`, confidence 1.0. Reading the actual quote, that number came off a careers page — a hiring line, not a replacement for the main number. The model was right that the numbers differed and wrong that the difference meant anything. We rewrote the prompt to judge purpose, not just difference: it now has to decide whether the live number plausibly replaces the stored one for the *same* purpose, and to abstain when the surrounding text suggests a different one — careers, fax, donations, a department, a second location. After the fix, Sutter Health correctly abstains: *"The live number is presented in the context of a hiring process, which is a different purpose."*

**2. A self-referential bug in our own code.** Oakland Healthcare & Wellness was, in an earlier run, shown to a reviewer as a discrepancy on its *address*: `stored "3030 Webster St. Oakland 94609" → live "3030 Webster St. Oakland 94609"` — a proposed change to the exact same value. The adjudicator had reasoned about a field (phone) that no gathered evidence actually covered, and `build_change_request` fell through to whatever finding was first available, regardless of whether it genuinely differed. Fixed by requiring a field to actually differ before it's emitted at all, and downgrading to abstain when nothing gathered is actionable.

**3. A false positive caught by a human, not by us.** In an earlier run we flagged Meals on Wheels of Alameda County (#2258) for a phone mismatch — stored `5106544000105`, live site `510.777.9560` — and proposed replacing the stored number. A human opened the actual listing. The site already lists `(510) 777-9560` as its Main Line. The listing carries nine phone numbers, one per programme or region, and `5106544000105` is `(510) 654-4000 ext. 105` — the J-Sei Nutrition Services line, correctly stored. We had proposed replacing a correct number with one that was already present, because `check_phone` only ever compared the *first* stored number and never checked the other eight. Fixed by comparing against every stored number, and reporting an unmatched live number as a possible *addition* rather than a replacement when a listing carries many numbers. Meals on Wheels is no longer flagged as a discrepancy — it now correctly abstains (`phone_missing`: the live number isn't among the ones on file, which isn't the same as a stored number being wrong).

This third bug is the strongest argument for the human-in-the-loop design in this whole project: **the human in our loop caught the agent, exactly as designed.** Nothing here shipped straight to ShelterTech — every output is a candidate, and this is what "candidate" is for.

## What it does

An agent that re-verifies approved listings against each org's live website, models the directory as a graph so one closure propagates to everything connected to it, abstains when evidence is weak, and emits a ranked change-request queue in the shape ShelterTech's own review workflow already expects. It never writes to their production system.

## Architecture

```mermaid
flowchart LR
    A[harvest.py] --> B[graph.py: build]
    B --> C[run.py: triage_score]
    C --> D[verify.py: gather_evidence]
    D --> E[verify.py: adjudicate]
    E -->|Gemini 2.5 Flash| E
    E -->|fallback, no key| E
    E --> F[run.py: blast_radius + priority]
    F --> G[graph.py: propagate_staleness]
    G --> H[out/results.json]
    H --> I[ui/index.html: review]
```

- **`harvest.py`** — pulls a random sample of resource IDs from the live AskDarcel API (`GET /resources/{id}`), read-only, unauthenticated, caches each record to `data/*.json` so repeat runs don't re-hit the API. Also reads the live corpus totals (`/resources/count`, `/services/count`).
- **`graph.py`** — models the directory as a graph: `org` nodes linked to `service`, `address`, `phone`, and `category` nodes. Our live run built a graph of **1,158 nodes**. Three things depend on this and don't exist without it:
  - `blast_radius(org)` — if an org is wrong, what else is now suspect: every service it offers, plus any other org sharing its address or phone. One traversal.
  - `contradictions()` — distinct orgs sharing the same phone or address: merge candidates or stale data. A graph pattern match, not a table scan. Our run found **9** of these.
  - `propagate_staleness(seeds)` — doubt decays outward from a confirmed-bad org along edges (org → service → neighbours), weakening with each hop. This is what ranks the review queue.
- **`verify.py`** — for each candidate record with a website: fetches the homepage, and if the evidence doesn't settle the question, walks a fixed list of likely sub-pages (`/contact`, `/about`, `/hours`, `/visit`, `/locations`) up to 3 fetches total, stopping early once it has enough signal (a closure phrase, or a matching phone + hours). It checks phone number, address, and closure/relocation language against what's stored. Findings are handed to an adjudicator:
  - **With an API key set**: Gemini 2.5 Flash judges whether the scraped quote actually contradicts the stored value and writes the one-sentence human-readable reason. Two transports call the same model — `call_model()` picks based on key shape: a key starting `sk-or-` goes over **OpenRouter** (`google/gemini-2.5-flash`, OpenAI-shaped chat completions API); anything else is treated as a Google AI Studio key and calls Gemini directly. Our live run used the OpenRouter path. The direct-Google path exists in the code and is untested by us today — don't claim we exercised it.
  - **Without a key**: a conservative deterministic fallback — only calls "discrepancy" on a positive contradiction, abstains on anything thin. The pipeline degrades gracefully: no key, no crash, still runs end to end in evidence-only mode.
  - Either path emits a `change_request` (field, current value, proposed value, source URL, source quote) only on a "discrepancy" verdict. This is never POSTed to AskDarcel — it's a payload for a human to review and submit themselves.
- **`run.py`** — orchestrates the pipeline end to end:
  1. Harvest (cached) and compute the baseline staleness stats (this is what produces the finding table above).
  2. Build the graph.
  3. **Triage** (`triage_score`) — cheap, deterministic, no model calls: ranks candidates by never-verified + critical category (shelter/food/health/legal/crisis/hygiene/etc.) + having a checkable website. This is the step that decides where to spend Gemini tokens — the classical, free filter guards the expensive model step, not the other way round. Only the top `BUDGET` (default 25) candidates go to verification.
  4. `verify.verify_many` over the triaged candidates (thread pool, network-bound).
  5. For every "discrepancy" verdict, compute `blast_radius` and a `priority = confidence * (1 + blast_radius_size * 0.15)`. Confidence alone is a bad rank — a wrong record that invalidates twelve downstream services deserves attention before a high-confidence typo.
  6. `propagate_staleness` over all discrepancy seeds to size the downstream-suspect count.
  7. Write everything to **`out/results.json`**: `generated_at`, `corpus`, `stats`, `queue` (ranked change requests), `abstained`, `contradictions`, `source`.
- **`ui/index.html`** — a static review page: loads `out/results.json` and lists each candidate change request with its verdict, confidence, priority, source URL, and verbatim quote, so a ShelterTech volunteer can accept or reject in seconds instead of re-deriving the evidence themselves.

## How to run

```bash
# 1. Harvest a sample (read-only, cached to data/)
python3 harvest.py

# 2. Run the pipeline: verify against Gemini (optional) and emit the queue
OPENROUTER_API_KEY=sk-or-... python3 run.py   # Gemini 2.5 Flash via OpenRouter (path we ran)
# GEMINI_API_KEY=...  python3 run.py           # Gemini 2.5 Flash via Google AI Studio direct (in the code, not what we demoed)
# python3 run.py                                # no key: deterministic fallback adjudicator, still end to end

# 3. Serve the review UI
python3 -m http.server 8000
# open http://localhost:8000/ui/index.html
```

## Design decisions

- **Read-only, always.** We only ever GET from `askdarcel.org/api`. We never POST. Every output is a candidate for human review, never an assertion of fact.
- **Deterministic evidence, model judgment.** Regex and string matching gather candidate evidence (phone numbers, closure phrases, zip codes). Gemini's only job is judging whether that evidence actually settles the question, and it's explicitly told to abstain rather than guess.
- **Abstention is a headline metric, not a bug we hide.** If the site is unreachable, JS-only, or the evidence is ambiguous, the verdict is "abstain" and it's reported as such.
- **Every claim carries a source URL and a verbatim quote.** No quote, no claim.
- **The graph runs on FalkorDB, with an in-memory fallback.** `graph_falkor.py` queries a real FalkorDB instance in Docker using Cypher: `blast_radius` is a traversal, `contradictions` a pattern match, `propagate_staleness` a variable-length path. `graph.py` is a zero-dependency adjacency implementation used when the database is unreachable, so the pipeline never hard-fails. `crosscheck.py` proves the two agree — 156 orgs, every contradiction, staleness at hops 1–4, subgraph extraction: zero mismatches. That cross-check earned its keep: it caught a genuine divergence (our BFS could cross the same edge twice; Cypher enforces relationship uniqueness and forbids it), which we reproduced deliberately rather than papering over. At 1,158 nodes an adjacency dict would be perfectly adequate — the argument for the graph database is that these are traversals, and they stay traversals as the corpus grows.
- **We are not building a 16th directory.** ShelterTech's guide, chatbot, and phone line already exist and are used by ~16,000 people a month. This tool reduces the cost of keeping their existing system accurate; it doesn't replace it.

## Limitations

- Sample size is 156 IDs out of a much larger corpus — a snapshot, not a full audit. Of those, only the top 15 by triage priority were actually checked with Gemini in our live run.
- Evidence gathering is regex-based (phone patterns, closure phrases, zip code presence) against plain-text scraped HTML — it will miss anything not phrased the way our patterns expect, and can't read JS-rendered content.
- The graph is unpersisted and rebuilt from the harvested sample each run.
- `propagate_staleness` decay/hop parameters are illustrative defaults (decay 0.5, 2 hops), not tuned against ground truth.
- Change requests are emitted, never submitted. Getting them into ShelterTech's actual review workflow requires their cooperation.

## Credits

Built against [ShelterTech](https://sheltertech.org)'s [SF Service Guide](https://sfserviceguide.org) and its public [AskDarcel](https://github.com/ShelterTechSF) API. Named for Darcel Jackson. This project only exists because ShelterTech already built and open-sourced the thing worth improving.
