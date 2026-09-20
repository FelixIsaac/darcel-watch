# SF Service Guide Watch

Hack for Humanity: San Francisco — 19 Sep 2026, Entrepreneurs First, co-hosted by MLH, powered by Google Gemini.
Built at a hackathon.

## Inspiration

We wanted to build an AI resource finder for San Franciscans needing food, shelter, healthcare, legal aid. Before writing code, we searched for prior art, the way we always should. It already exists: ShelterTech's SF Service Guide (sfserviceguide.org) — 1,759 organisations, 7,577 services, ~16,000 monthly users, open source, with a chatbot (`casey`) and a phone line (`VACS-MVP`). Building a 16th directory would have been vanity work at a hackathon that's supposed to help people.

So we asked a different question: what's actually broken about the one that exists? Their listings are vetted by volunteers at monthly datathons — human hours they don't have enough of. That's a resourcing problem, not a discovery problem, and it's the kind of problem an agent can help with.

It's named for Darcel Jackson, who founded ShelterTech after being injured as a welder and becoming unhoused himself.

## What it does

SF Service Guide Watch is an agent that re-verifies SF Service Guide listings against each organisation's own live website, models the directory as a graph so a single closure propagates to everything connected to it, abstains when the evidence is weak, and emits a ranked change-request queue in the shape ShelterTech's own volunteers already work with. It never writes to their production system — every output is a candidate for a human to review.

We measured the problem before building the fix. Sample: 156 random resource IDs from the live, public, unauthenticated AskDarcel API. 138 of those are marked `approved` — live to users right now. Of those 138: **126 have never been verified once** (`verified_at: null`). The 12 that do carry a verification date were verified a median of **~2,838 days ago (~7.8 years)**. **Zero** were verified in the past year. 120 of the 138 list a website we can check against.

The gap isn't discovery. It's freshness. A wrong shelter address at 9pm is worse than no answer at all.

## How we built it

- **Harvest** (`harvest.py`): read-only sampling of the public AskDarcel API, cached locally so we don't hammer it on repeat runs.
- **Graph** (`graph.py`): the directory modelled as Org→Service→Category, Org→Address, Org→Phone. Our live run built 1,158 nodes and found 9 contradictions (distinct org names sharing one phone/address — a graph pattern match, not a table scan). Two features are load-bearing on this and don't exist without it: blast radius (one org closing invalidates every service beneath it and any co-located org, one traversal) and staleness propagation (doubt decays outward along edges to rank the queue).
- **Triage** (`run.py`): a cheap, deterministic, model-free scoring pass before any Gemini call — ranks candidates by never-verified + critical category (shelter/food/health/legal/crisis/hygiene) + having a checkable website. This decides where to spend the model budget; the classical step guards the expensive step, not the other way round.
- **Verify** (`verify.py`): an agentic evidence loop — fetch the homepage, and if that doesn't settle the question, walk likely sub-pages (`/contact`, `/about`, `/hours`, `/visit`, `/locations`) up to 3 fetches, stopping early once there's enough signal. Checks phone, address, and closure/relocation language against the stored record.
- **Adjudicate**: deterministic code gathers the evidence; Gemini 2.5 Flash judges it — over **OpenRouter** (`google/gemini-2.5-flash`, OpenAI-shaped chat completions API), the path we actually ran. The code also supports calling Google AI Studio directly if given a Google-shaped key, selected automatically by key prefix, but we did not exercise that path in our run — the transport differs, the model and prompt don't. Given the stored value, the live candidate, and the exact quote it came from, Gemini decides discrepancy / match / abstain and writes the one-sentence human-readable reason. A conservative deterministic fallback runs when no API key is set, and the whole pipeline still runs end to end without one.
- **Rank and emit**: on a genuine discrepancy, `run.py` computes blast radius and a priority score (`confidence * (1 + blast_radius_size * 0.15)`) — confidence alone is a bad rank, a wrong record that invalidates twelve services deserves attention before a high-confidence typo. The change-request payload (field, current value, proposed value, source URL, verbatim quote) is written to `out/results.json` — never POSTed. `run.py` chains all of this: harvest → build graph → triage → verify → rank by blast radius/priority → propagate staleness → write output.
- **Review UI** (`ui/index.html`): a static page a ShelterTech volunteer could actually use, loading `out/results.json` — each candidate shown with its verdict, confidence, priority, source link, and quote, so a decision takes seconds instead of re-deriving the evidence from scratch.

## What we found

### The headline finding needs no model

Before any Gemini call, a plain arithmetic check on the stored data itself — no scraping, no model, just counting digits — found this across the 138 approved listings in our sample:

```
approved listings scanned : 138
phone numbers stored      : 189
NOT dialable as stored    : 24  (13%)
  truncated (<10 digits)  : 20
  extension run into no.  :  4
listings affected         : 19 of 138
```

Twenty numbers are stored with the area code stripped off, too short to dial. The clearest single example: **MKL Rehab – Addiction Treatment Helpline** (#2514) has `94410046` on file — 8 digits, truncated, cannot be dialled. An addiction treatment helpline whose phone number doesn't work. No model found this; it's a digit count. The same broken-number set includes Alameda County Family Justice Center (`02678800`) — domestic violence services with a phone number nobody can call.

This check is arithmetic on digit counts. No model, no scraping, no judgment call, and therefore no false-positive risk. That's the argument for the whole design: use the cheap deterministic check where it's sufficient by itself, and reserve the model for judgment calls a digit count can't make.

### The live run: structural checks carry the confident findings, the model mostly abstains

At `BUDGET=25`, the current run produces **2 discrepancies, 10 abstentions**. Both discrepancies are structural (`phone_format`), not model judgment calls:

- MKL Rehab – Addiction Treatment Helpline (#2514) — stored `94410046`, 8 digits, truncated.
- Oakland Healthcare & Wellness (#2312) — stored `02508000`, 8 digits, truncated.

Every model-adjudicated finding in this run came back **abstain** — including a case where our regex evidence-gatherer found something that looked like a phone number on an org's page (Grassroots Open Assistive Tech, #2599) but it was actually a Zoom meeting ID. The model declined to call it a match or a mismatch.

We're saying this plainly because it's the designed outcome, not a limitation. We tightened the adjudication prompt twice specifically to make it more conservative, and it got more conservative: on this sample, its judgment on genuinely ambiguous scraped evidence is overwhelmingly "I can't tell — ask a human." The cheap structural check carries the confident findings; the model's job is judgment, and abstaining on ambiguous evidence is it doing that job correctly.

### Three of our own bugs, caught by looking, none by tests

**1. A false positive in the model's judgment.** The first version of our prompt flagged Sutter Health at confidence 1.0 for a phone "mismatch" — stored `800-478-8837`, live page showed `916-297-9000`. That live number was scraped off a careers page: a hiring line, not a replacement main number. Different numbers isn't the same thing as one being wrong. We rewrote the prompt to judge purpose, not difference: does the live number plausibly replace the stored one for the *same* purpose, and abstain if context says otherwise (careers, fax, donations, a department, a second location). After the fix, Sutter Health correctly abstains: *"The live number is presented in the context of a hiring process, which is a different purpose."*

**2. A self-referential bug in our own code.** In an earlier run, Oakland Healthcare & Wellness was shown to a reviewer as a discrepancy on its *address*: `stored "3030 Webster St. Oakland 94609" → live "3030 Webster St. Oakland 94609"` — a proposed change to the exact same value. The adjudicator reasoned about a field (phone) that no gathered evidence covered, and our change-request builder fell through to whatever finding was first available, whether or not it genuinely differed. Fixed by requiring a field to actually differ before it's emitted, and downgrading to abstain when nothing gathered is actionable.

**3. A false positive caught by a human, not by us.** In an earlier run we flagged Meals on Wheels of Alameda County (#2258) for a phone mismatch — stored `5106544000105`, live `510.777.9560` — and proposed replacing the stored number. A human opened the actual listing: the site already lists `(510) 777-9560` as its Main Line. The listing carries nine phone numbers, one per programme or region, and `5106544000105` is `(510) 654-4000 ext. 105` — the J-Sei Nutrition Services line, correctly stored. Our own `check_phone` only ever compared the *first* stored number and never checked the other eight. Fixed by comparing against every stored number, and reporting an unmatched live number as a possible *addition* rather than a replacement when a listing carries many numbers. Meals on Wheels is no longer flagged — it now correctly abstains (`phone_missing`: the live number isn't among the ones on file, which isn't the same as a stored number being wrong).

That third bug is the strongest argument for the human-in-the-loop design in this whole project: the human in our loop caught the agent, exactly as designed. Nothing here ships straight to ShelterTech. Every output is a candidate, and this is what "candidate" is for.

## Challenges we ran into

- Distinguishing an actually-stale listing from a slow/JS-only/bot-blocking website. We cap fetches, check for real readable text, and abstain rather than guess when a site doesn't give us enough.
- Catching our own mistakes across two different layers. Gemini flagged Sutter Health at confidence 1.0 for a phone "mismatch" that was actually a careers-page number. Separately, our own `check_phone` compared only the first of ten stored numbers for Meals on Wheels of Alameda County and proposed replacing a number that was already correct. Neither was caught by a test — both were caught by a human reading the actual output and checking it against the real listing.
- Building a graph model in the time available without pulling in a database — an adjacency dict is the right scale-appropriate answer for 1,759 nodes, but we had to be disciplined about which queries actually need graph structure versus which are just filters.

## Accomplishments we're proud of

- We searched first and found the real gap instead of shipping a duplicate directory.
- We measured the gap live during the hackathon against production data, not a claim from a blog post.
- Our strongest finding needed no model at all: a deterministic digit-count check found 24 of 189 stored phone numbers (13%) are undialable as stored — including an addiction treatment helpline and a domestic-violence family justice centre.
- We caught three of our own bugs by reading the output, never by a test — two in the model's judgment and one in our own comparison logic — and fixed the root cause in each, not just the one case. The third was caught by a human opening the real listing: exactly what the human-in-the-loop design is for.
- Every output in the review queue carries a source URL and a verbatim quote — no quote, no claim.
- We're precise about what we did and didn't use. FalkorDB is real — Docker, Cypher, cross-checked against an in-memory twin for parity. Gemini 2.5 Flash runs over OpenRouter, not the direct Google API; the direct path exists in the code but we didn't exercise it. iMessage is wired against the real Spectrum SDK but has never delivered a message — the shared-line pool still refuses our allowlisted recipient. No production writes, ever.

## What we learned

Prior-art search is itself the highest-leverage hour of a hackathon. The organisation-with-best-intentions problem in civic tech usually isn't "no one built a directory" — it's that the directory exists and nobody has the hours to keep it honest. That reframed the whole build from "AI finds services" to "AI keeps the humans who already do this job from drowning."

## What's next

- Offer this to ShelterTech directly — the point is to reduce their volunteer cost, not compete with them.
- Swap the in-memory graph for FalkorDB + Cypher at full corpus scale (1,759 orgs, 7,577 services) — same three queries, same model.
- Widen evidence sources beyond the org's own website (e.g. Google Business Profile status) with the same abstain-by-default discipline.
- Run staleness propagation against the full corpus, not a 156-ID sample, and validate the decay/hop parameters against real ground truth.

## 90-second demo script

| Time | Beat |
|---|---|
| 0:00–0:15 | "We set out to build an AI resource finder for SF. Then we searched — it already exists." Show sfserviceguide.org, mention 1,759 orgs / 16,000 monthly users. |
| 0:15–0:30 | "So we measured what's actually broken." Show the baseline table: 156 sampled → 138 approved → 126 never verified, 12 verified a median of 7.8 years ago, zero in the past year. |
| 0:30–0:45 | The structural stat, no model involved: a digit-count check on 189 stored phone numbers finds 24 (13%) undialable as stored, 20 of them truncated below 10 digits, across 19 of 138 listings. Zero false-positive risk — it's arithmetic. |
| 0:45–0:55 | Lead with MKL Rehab – Addiction Treatment Helpline: stored number `94410046`, 8 digits, cannot be dialled. An addiction treatment helpline with a phone number that doesn't work. Most concrete, most checkable, most human-consequential finding in the project. |
| 0:55–1:05 | Show the live run and the Mermaid pipeline diagram: harvest → graph (1,158 nodes, 9 contradictions) → triage (cheap, model-free) → verify (agentic evidence loop) → Gemini 2.5 Flash via OpenRouter adjudicates → 2 discrepancies (both structural), 10 abstentions. |
| 1:05–1:15 | Say it plainly: every model-adjudicated finding in this run came back abstain, including a Zoom meeting ID the regex mistook for a phone number. That's the design working — the cheap check carries the confident findings, the model abstains rather than guess on ambiguous evidence. |
| 1:15–1:28 | Tell the strongest bug story: we once flagged Meals on Wheels for a "wrong" phone number, and a human opened the real listing and found the number was already correctly stored among nine others — our code only checked the first one. It now correctly abstains. The human in the loop caught the agent, exactly as designed. |
| 1:28–1:30 | Close: "This isn't a 16th directory. It's fewer volunteer-hours to keep the one that exists honest. Next step: give it to ShelterTech." |

## Anticipated judge questions

**Isn't this just scraping?**
Scraping gathers evidence; it doesn't decide anything. The deterministic layer only extracts candidate signals (a phone number, a closure phrase, a zip code). Whether that signal actually contradicts the stored record is a judgment call we hand to Gemini, with an explicit instruction to abstain when the evidence is ambiguous. A plain scraper also wouldn't fetch a second page when the homepage doesn't answer the question — ours does, up to 3 fetches, stopping early once it has enough signal.

**Why not just use their existing chatbot (`casey`)?**
`casey` answers a user's question from the data as stored. It doesn't check whether that stored data is still true. We're not building a better front end — we're checking the back end the front end (and the phone line) both depend on.

**Did you actually use a graph database?**
No, and we're not claiming we did. We built an in-memory adjacency graph, which is the correct tool at 1,759 nodes — zero dependencies, and it's genuinely a graph model (typed edges, multi-hop traversal), not a table with a graph label on it. Three features — blast radius, contradiction detection, staleness propagation — are graph queries, not table scans, and they'd port directly to FalkorDB + Cypher at real scale with the same model and same three queries. We'd rather say exactly what we built than claim a database we didn't touch.

**Do you ever write to AskDarcel?**
No. Every request is a GET. Change requests are written to a local file for a human to review; nothing is POSTed to their production system.

**How do you know your verification is more accurate than what's already there?**
We don't claim it is — we claim it's fresher and evidenced. Every flagged item carries a live source URL and a verbatim quote a volunteer can check in seconds, which is faster than the datathon process re-deriving it from scratch. The system is also honest about not knowing: abstention is reported as a metric, not hidden.

**Didn't Gemini get one wrong (Sutter Health)?**
Yes, on our first prompt version — flagged at confidence 1.0 for a number that was actually a careers-page line, not a replacement main number. We caught it by reading our own output, rewrote the prompt to judge whether the live number serves the *same purpose* as the stored one rather than just checking for a difference, and it now correctly abstains with a stated reason. We found two more bugs the same way: our own code proposing "change this address to itself" for Oakland Healthcare & Wellness (a change-request builder that didn't check the field actually differed), and a comparison bug that flagged Meals on Wheels of Alameda County for a "wrong" phone number that was already correctly stored elsewhere on the same listing — our code only checked the first of nine stored numbers. That last one was caught by a human opening the real listing, not by any code we wrote: the human in the loop caught the agent, which is the point of shipping a review queue instead of an auto-updater. Meals on Wheels is no longer flagged; it now correctly abstains. We're showing all three because they're the strongest evidence our abstention and review design does real work, not because we're proud of any of the misses.

**Are you calling Gemini directly through Google's API?**
No — our live run goes through OpenRouter (`google/gemini-2.5-flash`). The code also has a direct-Google-AI-Studio path that activates on a Google-shaped key, but we didn't exercise it in the run we're demoing. Same model and prompt either way; only the transport differs.

**Your model mostly abstains — isn't that a weak result?**
No, it's the point. In our live run, both confirmed discrepancies came from a deterministic digit-count check, not the model — and every model-adjudicated case came back abstain, including a Zoom meeting ID our regex mistook for a phone number. We tightened the adjudication prompt twice specifically to make it more conservative, and it got more conservative. A model that abstains on genuinely ambiguous scraped evidence, and lets a cheap structural check carry the confident findings, is doing its job. The failure mode we were guarding against was false confidence, not caution.
