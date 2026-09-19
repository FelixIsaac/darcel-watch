# Darcel Watch

Hack for Humanity: San Francisco — 19 Sep 2026, Entrepreneurs First, co-hosted by MLH, powered by Google Gemini.
Built at a hackathon.

## Inspiration

We wanted to build an AI resource finder for San Franciscans needing food, shelter, healthcare, legal aid. Before writing code, we searched for prior art, the way we always should. It already exists: ShelterTech's SF Service Guide (sfserviceguide.org) — 1,759 organisations, 7,577 services, ~16,000 monthly users, open source, with a chatbot (`casey`) and a phone line (`VACS-MVP`). Building a 16th directory would have been vanity work at a hackathon that's supposed to help people.

So we asked a different question: what's actually broken about the one that exists? Their listings are vetted by volunteers at monthly datathons — human hours they don't have enough of. That's a resourcing problem, not a discovery problem, and it's the kind of problem an agent can help with.

It's named for Darcel Jackson, who founded ShelterTech after being injured as a welder and becoming unhoused himself.

## What it does

Darcel Watch is an agent that re-verifies SF Service Guide listings against each organisation's own live website, models the directory as a graph so a single closure propagates to everything connected to it, abstains when the evidence is weak, and emits a ranked change-request queue in the shape ShelterTech's own volunteers already work with. It never writes to their production system — every output is a candidate for a human to review.

We measured the problem before building the fix. Sample: 160 random resource IDs from the live, public, unauthenticated AskDarcel API. 122 resolved to real records. 108 of those are marked `approved` — live to users right now. Of those 108: **98 have never been verified once** (`verified_at: null`). The 10 that do carry a verification date were verified a median of **~2,837 days ago (~7.8 years)**. **Zero** were verified in the past year. 93 of the 108 list a website we can check against.

The gap isn't discovery. It's freshness. A wrong shelter address at 9pm is worse than no answer at all.

## How we built it

- **Harvest** (`harvest.py`): read-only sampling of the public AskDarcel API, cached locally so we don't hammer it on repeat runs.
- **Graph** (`graph.py`): the directory modelled as Org→Service→Category, Org→Address, Org→Phone. From the 122 sampled records this builds 949 nodes (122 org, 265 service, 246 category, 161 address, 155 phone). This is load-bearing for three features that don't exist without it: blast radius (one org closing invalidates every service beneath it and any co-located org, one traversal — our run found a max blast radius of 36), contradiction detection (same phone/address under different org names, a graph pattern match — our run found 7, the clearest being phone 415-831-2700 shared by three unrelated listings: Miraloma Playground Clubhouse, Civic Center Plaza, and Laurel Hill Playground and Clubhouse), and staleness propagation (doubt decays outward along edges to rank the queue).
- **Triage** (`run.py`): a cheap, deterministic, model-free scoring pass before any Gemini call — ranks candidates by never-verified + critical category (shelter/food/health/legal/crisis/hygiene) + having a checkable website. This decides where to spend the model budget; the classical step guards the expensive step, not the other way round.
- **Verify** (`verify.py`): an agentic evidence loop — fetch the homepage, and if that doesn't settle the question, walk likely sub-pages (`/contact`, `/about`, `/hours`, `/visit`, `/locations`) up to 3 fetches, stopping early once there's enough signal. Checks phone, address, and closure/relocation language against the stored record.
- **Adjudicate**: deterministic code gathers the evidence; Gemini 2.5 Flash judges it. Given the stored value, the live candidate, and the exact quote it came from, Gemini decides discrepancy / match / abstain and writes the one-sentence human-readable reason — instructed explicitly to abstain when the quote doesn't clearly settle the question. A conservative deterministic fallback runs when no API key is set, and the whole pipeline still runs end to end without one.
- **Rank and emit**: on a genuine discrepancy, `run.py` computes blast radius and a priority score (`confidence * (1 + blast_radius_size * 0.15)`) — confidence alone is a bad rank, a wrong record that invalidates twelve services deserves attention before a high-confidence typo. The change-request payload (field, current value, proposed value, source URL, verbatim quote) is written to `out/results.json` — never POSTed. `run.py` chains all of this: harvest → build graph → triage → verify → rank by blast radius/priority → propagate staleness → write output.
- **Review UI** (`ui/index.html`): a static page a ShelterTech volunteer could actually use, loading `out/results.json` — each candidate shown with its verdict, confidence, priority, source link, and quote, so a decision takes seconds instead of re-deriving the evidence from scratch.

## Challenges we ran into

- Distinguishing an actually-stale listing from a slow/JS-only/bot-blocking website. We cap fetches, check for real readable text, and abstain rather than guess when a site doesn't give us enough.
- Keeping Gemini's role honest: it only judges evidence a deterministic step already gathered, and it's told to abstain rather than fabricate certainty — the same instinct that made us search before building.
- Building a graph model in the time available without pulling in a database — an adjacency dict is the right scale-appropriate answer for 1,759 nodes, but we had to be disciplined about which queries actually need graph structure (three of them do) versus which are just filters.

## Accomplishments we're proud of

- We searched first and found the real gap instead of shipping a duplicate directory.
- We measured the gap live during the hackathon against production data, not a claim from a blog post.
- Every output in the review queue carries a source URL and a verbatim quote — no quote, no claim.
- We're honest about what we didn't use: no FalkorDB, no production writes. We say so plainly instead of overclaiming.

## What we learned

Prior-art search is itself the highest-leverage hour of a hackathon. The organisation-with-best-intentions problem in civic tech usually isn't "no one built a directory" — it's that the directory exists and nobody has the hours to keep it honest. That reframed the whole build from "AI finds services" to "AI keeps the humans who already do this job from drowning."

## What's next

- Offer this to ShelterTech directly — the point is to reduce their volunteer cost, not compete with them.
- Swap the in-memory graph for FalkorDB + Cypher at full corpus scale (1,759 orgs, 7,577 services) — same three queries, same model.
- Widen evidence sources beyond the org's own website (e.g. Google Business Profile status) with the same abstain-by-default discipline.
- Run staleness propagation against the full corpus, not a 160-ID sample, and validate the decay/hop parameters against real ground truth.

## 90-second demo script

| Time | Beat |
|---|---|
| 0:00–0:15 | "We set out to build an AI resource finder for SF. Then we searched — it already exists." Show sfserviceguide.org, mention 1,759 orgs / 16,000 monthly users. |
| 0:15–0:35 | "So we measured what's actually broken." Show the table: 160 sampled → 108 approved → 98 never verified, 10 verified a median of 7.8 years ago, zero in the past year. |
| 0:35–0:55 | Show the Mermaid pipeline diagram, walk it left to right: harvest → graph (949 nodes) → triage (cheap, model-free) → verify (agentic evidence loop) → Gemini adjudicates → ranked by blast radius, emits a change request, never a POST. Mention the 7 contradictions found, incl. one phone number shared by three unrelated listings, and the max blast radius of 36. |
| 0:55–1:15 | Live: open the review UI, point at one real flagged listing — verdict, confidence, source URL, verbatim quote. Show an abstain case too, so it's clear the model says "I don't know" on purpose. |
| 1:15–1:30 | Close: "This isn't a 16th directory. It's fewer volunteer-hours to keep the one that exists honest. Next step: give it to ShelterTech." |

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
