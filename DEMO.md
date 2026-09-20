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
| 1 | **Stat tiles** — `126 / 138`, `7.8 yrs`, `0` | Dashboard, `/` |
| 2 | **A discrepancy card** — MKL Rehab, evidence, source link | Dashboard, Review queue |
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
1,759 orgs, 7,577 services, 16k users a month, open source, plus a chatbot and a
phone line. So we asked what's actually broken instead."

**0:20 — the number.** *(Shot 1)* "Thirteen percent of the phone numbers in this
directory cannot be dialled. 24 of 189 — twenty of them truncated below ten digits
— across 19 of 138 live listings. No model found that. It's arithmetic on digit
counts, which means it cannot be a false positive. Separately: 126 of those 138
listings have never been verified against their source. Not once."

**0:40 — the find.** *(Shot 2)* "MKL Rehab. An addiction treatment helpline. The
stored number is `94410046` — eight digits. You cannot call it. Oakland Healthcare
& Wellness, same fault: `02508000`. Check them on your phone right now."

**0:55 — the graph.** "The directory is a graph: org → service → address → phone.
One org closing invalidates everything beneath it — we measured a blast radius of
36. And three separate listings share one switchboard number, which a flat table
hides and one traversal surfaces."

**1:10 — the honest part.** *(Shot 3)* "Ten abstentions against two confirmed
findings. On this sample every model-judged case came back 'I can't tell' — including
a number that turned out to be a Zoom meeting ID. We tightened that prompt twice to
make it more conservative, and it got more conservative. That's the design working.
A wrong shelter address at 9pm is worse than no answer."

**1:15 — the three bugs.** "We shipped three of our own bugs and caught all three by
reading our own output. A careers line read as a main number. A change request that
proposed changing an address to itself. And comparing only the first of ten stored
numbers — that one a human caught, by opening the real listing. Which is exactly
what the human is there for."

**1:20 — the close.** *(Shot 4)* "Every finding goes to a volunteer as a message —
they're not staff at desks. We never write to ShelterTech's system. We're not
building a 16th directory. We're making the one that exists cheaper to keep true."

---

## Judge questions

**"Isn't this just scraping?"**
Scraping gathers; the agent decides. It chooses which sub-pages to fetch when the
first is insufficient, and it abstains — 10 abstentions against 2 confirmed
findings on the current run.

**"Did you use a graph database?"**
Yes — FalkorDB, running in Docker, queried with real Cypher. Blast radius is a
traversal, contradictions are a pattern match, staleness is a variable-length path.
We also kept an in-memory implementation and wrote `crosscheck.py` to prove they
agree: 156 orgs, every contradiction, staleness at hops 1–4 — zero mismatches. That
cross-check caught a real divergence (our BFS could reuse an edge; Cypher forbids
it), which is how we know the port is faithful rather than merely passing.

**"Why a graph database at 1,158 nodes? A dict would do."**
At this size, yes — and we say so. The in-memory version is the fallback and it's
genuinely fine. The argument is the queries, not the scale: they're traversals, and
they stay traversals at 1,759 orgs or ten times that.

**"Are you calling the Gemini API directly?"**
Gemini 2.5 Flash via OpenRouter. The direct-Google path is in `verify.py` too and
selects on key shape.

**"Why not use ShelterTech's existing chatbot?"**
Their chatbot answers from this data. If the data is 7.8 years stale, a better
chatbot returns a confident wrong address.

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
