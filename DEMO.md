# Demo + screenshot guide

Everything you need to run this in front of a judge, and to capture submission assets.

---

## Before you start

```bash
cd ~/Projects/darcel-watch
export OPENROUTER_API_KEY=sk-or-...        # Gemini 2.5 Flash via OpenRouter
```

Regenerate fresh results (optional — `out/results.json` is already populated):

```bash
BUDGET=25 .venv/bin/python run.py
```

Takes ~60–90s. Prints the run summary. **Do this before the demo, not during it.**

---

## Open the demo surfaces

**Terminal 1 — the web UI**

```bash
docker start falkordb
npm run web
```

Then open <http://127.0.0.1:8787/> — Dashboard, Review and Graph are all there.

**The export is not optional.** Start the server without `OPENROUTER_API_KEY` and
the Run button silently degrades to evidence-only mode — no error, just no Gemini.

**Terminal 3 — the fallback simulation** (always works, zero dependencies)

```bash
python3 notify/dry_run.py
```

---

## Screenshots for submission

Capture at a **narrow browser window** (~900px wide) — the layout is mobile-first
and looks denser and more intentional than at full width. Use a dark desktop
background so the dark UI doesn't look like a floating rectangle.

**macOS:**
- `Cmd+Shift+4` then drag — region capture
- `Cmd+Shift+4` then `Space` then click the window — clean window capture with shadow
- `Cmd+Shift+5` — capture a video of the demo flow

Saves to Desktop by default. Put the ones you want to keep in `out/screenshots/`.

### The five shots worth having

| # | What | Where |
|---|---|---|
| 1 | **Stat tiles** — `523 / 813` no verification signal, `808 / 813` updated in 90 days, `53 days` median | Dashboard, `/` |
| 2 | **A discrepancy card** — Building Futures, the `tel:null` Call button, source link | Dashboard, Review queue |
| 3 | **The abstained section** — the agent declining to judge | Dashboard, scroll further |
| 4 | **The graph** — blast radius highlighted after a click | `/graph` |
| 5 | **The review thread** — a review being answered | `/review` |
| 6 | **The live run** — streaming log mid-audit | Dashboard, hit Run |

Shot 1 is the money shot. Shot 3 wins the responsible-AI argument. Shot 4 is for
the FalkorDB judge.

---

## The 90-second demo

**0:00 — the turn.** "We came to build an AI resource finder for people in SF who
need food, shelter, a clinic. We searched first. ShelterTech already built it —
1,759 orgs, 7,577 services, open source, plus a chatbot and a phone line, and
they report 16,000+ users a month. So we asked what's actually broken instead."

**0:20 — the number.** *(Shot 1)* "813 organisations are live to users, and 808 of
them were updated in the last 90 days — this directory is actively maintained.
But **523 of them, 64.3%, carry no verification signal at all**: no verified
date, no certified date, not even a certified flag. `updated_at` tells you
something changed. It doesn't tell you anyone checked it. Nobody — including
ShelterTech — can tell a fresh listing from a stale one."

**0:40 — the find.** *(Shot 2)* "Building Futures. Domestic violence services.
Their number is printed on the listing — `510-808-7410` — but it's in the label
column, and the number field is empty. So the Call button links to `tel:null`.
Tap it on a phone and nothing happens. Same for the LGBT Center and a youth
clinic. Check it right now."

**0:55 — the graph.** "The directory is a graph: org → service → address → phone.
One org closing invalidates everything beneath it — we measured a blast radius of
36. And three separate listings share one switchboard number, which a flat table
hides and one traversal surfaces."

**1:10 — the honest part.** *(Shot 3)* "Across 80 hand-audited listings the model
asserted **nothing** — zero discrepancies. Every finding came from the free
structural tier. We tightened that prompt twice to make it more conservative and
it is. Caveat we say out loud: the guard was built on the same 80 listings it
was measured on, so that's fitted, not a precision claim. A wrong shelter address
at 9pm is worse than no answer."

**1:15 — freshness.** "Broken and stale are different problems. We score every
listing on a decay model — hours expire in six months, phone numbers in three
years, and those shelf lives are our judgement, not measurements. The directory
sits at 36 out of 100, because almost nothing carries evidence stronger than
'well-formed' — not because nobody's working on it. We estimate about 204
volunteer-hours to move it to 70; that's an estimate built on our own constants,
but it's the first number of any kind anyone has had for this."

**1:25 — the four bugs.** "We shipped four of our own bugs and caught all four by
reading our own output — including auditing the wrong API for most of the build.
Two were caught by a human opening the actual listing. Which is exactly what the
human is there for."

**1:20 — the close.** *(Shot 4)* "Every finding goes to a volunteer as a message —
they're not staff at desks. We never write to ShelterTech's system. We're not
building another directory, and this isn't a complaint about theirs — they
maintain it actively. We're adding the instrument it doesn't have: a record of
what's actually been checked."

---

## Judge questions

**"Isn't this just scraping?"**
Scraping gathers; the agent decides. It chooses which sub-pages to fetch when the
first is insufficient, and it abstains — on the current run, 4 abstentions, and
every one of the 8 confirmed findings came from the free structural tier rather
than the model.

**"Did you use a graph database?"**
Yes — FalkorDB, running in Docker, queried with real Cypher. Blast radius is a
traversal, contradictions are a pattern match, staleness is a variable-length path.
We also kept an in-memory implementation and wrote `crosscheck.py` to prove they
agree: the whole corpus — 816 orgs, all 230 contradictions in published order,
staleness from every org, the subgraph export — asserted, exits non-zero on any
mismatch. That cross-check caught a real divergence (our BFS can reuse an edge;
Cypher forbids it inside one path). We kept the in-memory semantics and made the
Cypher reproduce them with an explicit second query, so the parity claim says
which backend defines the answer rather than hiding it.

**"Why a graph database at 7,405 nodes? A dict would do."**
At this size, yes — and we say so. The in-memory version is the fallback and it's
genuinely fine. The argument is the queries, not the scale: they're traversals, and
they stay traversals at 1,759 orgs or ten times that.

**"Are you calling the Gemini API directly?"**
Gemini 2.5 Flash via OpenRouter. The direct-Google path is in `verify.py` too and
selects on key shape.

**"Why not use ShelterTech's existing chatbot?"**
Their chatbot answers from this data. It has no way to know which listings
anyone has confirmed, because the data doesn't record that. A better chatbot
over unprovenanced data returns a confident wrong address.

**"How do you know your agent isn't wrong?"**
We don't, fully — which is why nothing is auto-applied. Every output is a
change-request for a human volunteer, every claim carries a source URL and a
verbatim quote, and we report the abstention rate as a headline number. We caught
our own false positive by reading the output.

---

## If something breaks mid-demo

- **UI blank** → it falls back to embedded demo data automatically; keep going.
- **No results** → `python3 notify/dry_run.py` works standalone with zero deps.
- **Network dead** → everything above runs off cached `data/` and `out/results.json`.
  Only a fresh `run.py` needs the internet.
