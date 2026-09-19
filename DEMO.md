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
BUDGET=15 python3 run.py
```

Takes ~60–90s. Prints the run summary. **Do this before the demo, not during it.**

---

## Open the demo surfaces

**Terminal 1 — the web UI**

```bash
python3 -m http.server 8777
```

Then open <http://localhost:8777/ui/index.html>

**Terminal 2 — the messaging review app**

```bash
npm start          # terminal transport (no credentials needed)
npm run web        # web chat UI
```

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
| 1 | **Stat tiles** — `126 / 138`, `7.8 yrs`, `0` | top of `ui/index.html` |
| 2 | **A discrepancy card** — field diff, evidence quote, source link | scroll to Review queue |
| 3 | **The abstained section** — the Sutter Health reasoning | scroll further |
| 4 | **The messaging review** — chat thread with Y/N/S | `npm run web`, or `dry_run.py` in terminal |
| 5 | **Terminal run output** — `run.py` summary line | Terminal 1 |

Shot 1 is the money shot. Shot 3 is the one that wins the responsible-AI argument.

---

## The 90-second demo

**0:00 — the turn.** "We came to build an AI resource finder for people in SF who
need food, shelter, a clinic. We searched first. ShelterTech already built it —
1,759 orgs, 7,577 services, 16k users a month, open source, plus a chatbot and a
phone line. So we asked what's actually broken instead."

**0:20 — the number.** *(Shot 1)* "We pulled their live public API today. Of 138
listings marked approved — live to users right now — **126 have never been
verified once**. The twelve that carry a date: median **7.8 years**. Every single
verification in our sample happened between 2018 and 2020. Zero since."

**0:40 — the find.** *(Shot 2)* "Meals on Wheels of Alameda County. Stored phone
`5106544000105` — a number with the extension jammed onto the end. Their site says
`510.777.9560`. Here's the quote, here's the URL. Check it on your phone."

**0:55 — the graph.** "The directory is a graph: org → service → address → phone.
One org closing invalidates everything beneath it — we measured a blast radius of
36. And three separate listings share one switchboard number, which a flat table
hides and one traversal surfaces."

**1:10 — the honest part.** *(Shot 3)* "Eight of fifteen, the agent refused to
judge. Our first run had a false positive — it flagged Sutter Health over a
careers line. We rewrote the prompt to judge the *role* of the evidence, not just
the difference. Now it abstains and tells you why. A wrong shelter address at 9pm
is worse than no answer."

**1:20 — the close.** *(Shot 4)* "Abstentions need a human. ShelterTech's reviewers
are volunteers, not staff at desks — so the queue goes to their iMessage. Reply Y.
We're not building a 16th directory. We're making the one that exists cheaper to
keep true."

---

## Judge questions

**"Isn't this just scraping?"**
Scraping gathers; the agent decides. It chooses which sub-pages to fetch when the
first is insufficient, and it abstains — 8 of 15 today.

**"Did you use a graph database?"**
No, and we won't claim we did. In-memory adjacency graph, 1,158 nodes, correct at
this scale. FalkorDB + Cypher is the drop-in — same model, same three queries.

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
