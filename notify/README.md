# notify/ — the volunteer review service

The audit (`run.py`) ends with a ranked queue of listings that need a human:
ones where the agent found the stored data contradicted by the organisation's
live website, and ones where it **abstained** because the evidence was too weak
to call. Both need a person.

ShelterTech's reviewers are volunteers, not staff at desks. So the queue is
delivered as a **message**, not a dashboard.

| File | What it is |
|---|---|
| `spectrum.ts` | The real service. A Spectrum (Photon) client, a real message loop, real reply handling. **This is what runs.** |
| `dry_run.py` | A zero-dependency terminal mock-up of the same conversation. Kept as a no-Node, no-network fallback demo. |

---

## Run it locally — no credentials, no accounts

```bash
npm install
npm start
```

That's the whole setup. It opens a real terminal chat client and hands you the
review queue one item at a time.

It works because `terminal` is a first-class Spectrum provider, and
`Spectrum({ providers })` is a supported, typed call shape with no `projectId`
or `projectSecret`. So this is **not** a simulation with a real mode bolted on
— it's the real client and the real message loop, rendered in a terminal
instead of on a phone.

On first run the terminal provider downloads a small `tuichat` binary
(~4.8 MB, SHA256-verified) from the `photon-hq/tuichat` GitHub release and
caches it in `~/Library/Caches/tuichat/`. That one download needs network; the
app itself makes no other network calls.

### What you can reply

| Reply | Meaning |
|---|---|
| `Y` | Confirm — the agent is right. Files a change request. |
| `N` | Reject — the agent is wrong. Logged, so the error is visible. |
| `S` | Skip — not me, not now. Stays open for another reviewer. |
| `?` | Show the evidence: the quote, the pages fetched, the blast radius. |
| `queue` | How many reviews are still open. |
| `help` | The command list. |
| `stop` | End the session with a summary. |

`yes` / `no` / `skip` and the slash forms (`/queue`, `/help`, `/stop`) work too.

### Driving it non-interactively

The terminal client detects a non-TTY stdin and drops into plain readline mode,
so a whole session can be scripted — useful for a demo or a smoke test:

```bash
printf 'help\n?\nY\nN\nS\nstop\n' | TUICHAT_QUIET=1 npm start
```

---

## Switch it to iMessage

Same application code. Only the providers array changes.

```bash
export PHOTON_PROJECT_ID=...        # or SPECTRUM_PROJECT_ID
export PHOTON_PROJECT_SECRET=...    # or SPECTRUM_PROJECT_SECRET
export REVIEWER_PHONE=+14155550123  # the volunteer's number

npm start
```

All three must be set. With any of them missing the app stays on `terminal` —
it never half-starts, and it never silently fails to reach anyone.

### WhatsApp Business as well (optional)

Add these on top of the three above and `whatsapp-business` joins the providers
array. The inbound loop already serves every configured provider, so replies
from either platform land in the same handler.

```bash
export WHATSAPP_ACCESS_TOKEN=...
export WHATSAPP_PHONE_NUMBER_ID=...
export WHATSAPP_APP_SECRET=...      # optional
```

### Full environment variable reference

| Variable | Required | Effect |
|---|---|---|
| `PHOTON_PROJECT_ID` / `SPECTRUM_PROJECT_ID` | for iMessage | Spectrum Cloud project id |
| `PHOTON_PROJECT_SECRET` / `SPECTRUM_PROJECT_SECRET` | for iMessage | Spectrum Cloud project secret |
| `REVIEWER_PHONE` | for iMessage | Volunteer's number, E.164 |
| `WHATSAPP_ACCESS_TOKEN` | no | Enables WhatsApp Business |
| `WHATSAPP_PHONE_NUMBER_ID` | no | Enables WhatsApp Business |
| `WHATSAPP_APP_SECRET` | no | WhatsApp webhook signature secret |
| `TUICHAT_QUIET` | no | Silences the terminal client's non-TTY notice |

---

## Read-only against production

**This service never POSTs to askdarcel.org.** There is no HTTP client in
`spectrum.ts` at all.

A confirmed review is appended to `out/approved.json` as a `change_request`
payload, shaped for the endpoint ShelterTech's volunteers already use, and
stamped `"submitted_to_askdarcel": false`. A rejected one goes to
`out/rejected.json` — the agent being wrong is worth recording too.

The agent proposes, a volunteer judges, and a human with an account submits.
Three steps; this repo owns the first two and deliberately stops before the
third.

Decisions persist: confirmed and rejected listings are not offered again on a
later run. Skips are not persisted, because "skip" means *not me*, not *never*.

---

## Output shape

`out/approved.json` and `out/rejected.json` are JSON arrays. One entry:

```json
{
  "change_request": {
    "resource_id": 2258,
    "field": "phone",
    "current": "5106544000105",
    "proposed": "510.777.9560",
    "source_url": "https://www.feedingseniors.org",
    "source_quote": "…1721 Broadway #201, Oakland, CA, 94612 510.777.9560 (text or voice)…",
    "submitted_by": "darcel-watch (agent, human review required)"
  },
  "decision": "confirmed",
  "resource_id": 2258,
  "name": "Meals on Wheels of Alameda County",
  "agent_verdict": "discrepancy",
  "agent_confidence": 0.95,
  "blast_radius_size": 5,
  "decided_by": "terminal-user",
  "decided_via": "terminal",
  "decided_at": "2026-09-19T23:27:17.470Z",
  "submitted_to_askdarcel": false,
  "note": "Recorded locally only. Darcel Watch never POSTs to askdarcel.org — a human must submit this change request."
}
```

---

## Failure modes, all handled

| Situation | Behaviour |
|---|---|
| `out/results.json` missing | Message telling you to run `run.py`. Exit 0. |
| `out/results.json` not valid JSON | Parse error reported. Exit 0. |
| Queue empty | Says so. Exit 0. |
| Everything already reviewed | Says so, points at the decision files. Exit 0. |
| Malformed queue entries (nulls, wrong types, missing fields) | Skipped and counted, never rendered. No crash. |
| `out/approved.json` corrupt | Moved aside to `approved.json.corrupt-<ts>`, never overwritten. |
| Unrecognised reply | Nudge, and the review stays open. |

## Typecheck

```bash
npm run typecheck
```

Node 24 runs `spectrum.ts` directly by stripping types, so there is no build
step. `tsconfig.json` sets `erasableSyntaxOnly` to guarantee the file stays
within what Node can strip.
