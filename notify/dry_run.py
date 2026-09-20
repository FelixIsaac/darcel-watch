#!/usr/bin/env python3
"""shelflife -- Spectrum dry run.

Renders, in the terminal, exactly what a volunteer reviewer would see on
their phone if we had live Photon/Spectrum credentials: an iMessage/WhatsApp
thread with the top-N review-queue items, a "Y" reply, and the resulting
confirmation. This is the demo fallback when there's no API key at the venue
-- no network calls, no deps, stdlib only.

Run from anywhere:
    python3 notify/dry_run.py
    python3 dry_run.py          (from inside notify/)
"""

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
RESULTS_PATH = HERE.parent / "out" / "results.json"

TOP_N = 5

# ANSI colors. Kept minimal so it still reads fine on a dumb terminal.
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
GRAY = "\033[90m"

# Sample data used only if out/results.json hasn't been generated yet
# (e.g. demoing before `python3 run.py` has run). Shape matches the real
# queue items produced by verify.py / run.py.
SAMPLE_QUEUE = [
    {
        "resource_id": 4821,
        "name": "Glide Memorial Food Pantry",
        "priority": 5.2,
        "change_request": {
            "field": "operating_status",
            "current": "open",
            "proposed": "temporarily closed",
            "source_url": "https://glide.org/programs/daily-free-meals/",
        },
    },
    {
        "resource_id": 1190,
        "name": "Tenderloin Health Services Clinic",
        "priority": 4.7,
        "change_request": {
            "field": "phone",
            "current": "(415) 555-0132",
            "proposed": "(415) 555-0199",
            "source_url": "https://tenderloinhealth.org/contact",
        },
    },
    {
        "resource_id": 2733,
        "name": "St. Anthony's Free Dining Room",
        "priority": 3.9,
        "change_request": {
            "field": "hours",
            "current": "Mon-Fri 11am-1pm",
            "proposed": "Mon-Sat 11:30am-1:30pm",
            "source_url": "https://stanthonysf.org/dining-room/",
        },
    },
]


def load_queue():
    if RESULTS_PATH.exists():
        try:
            payload = json.loads(RESULTS_PATH.read_text())
            queue = payload.get("queue", [])
            if queue:
                return sorted(queue, key=lambda v: -v.get("priority", 0))[:TOP_N]
        except (json.JSONDecodeError, OSError):
            pass
    print(f"{DIM}(no out/results.json found -- using sample data){RESET}\n")
    return SAMPLE_QUEUE[:TOP_N]


def build_message(item):
    """Same compact format spectrum.ts sends -- kept in sync by hand."""
    cr = item.get("change_request") or {}
    field = cr.get("field") or (item.get("fields") or [{}])[0].get("field", "?")
    stored = cr.get("current") or (item.get("fields") or [{}])[0].get("stored", "?")
    live = cr.get("proposed") or (item.get("fields") or [{}])[0].get("live", "?")
    url = cr.get("source_url") or (item.get("fields") or [{}])[0].get("evidence_url", "?")
    rid = item.get("resource_id")
    name = item.get("name", "Unknown org")
    text = (
        f'shelflife — review #{rid}\n'
        f'{name}\n'
        f'{field}: stored "{stored}" → live "{live}"\n'
        f"Source: {url}\n"
        f"Reply Y confirm · N reject · S skip"
    )
    return text


def wrap(text, width):
    """Wrap on existing newlines first, then word-wrap each line."""
    out = []
    for line in text.split("\n"):
        if len(line) <= width:
            out.append(line)
            continue
        words, cur = line.split(" "), ""
        for w in words:
            if len(cur) + len(w) + 1 > width:
                out.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        if cur:
            out.append(cur)
    return out


def bubble(text, align="left", color=CYAN, width=52):
    """Render one iMessage-style bubble as box-drawing lines."""
    lines = wrap(text, width - 4)
    inner = max(len(l) for l in lines) if lines else 0
    top = "╭" + "─" * (inner + 2) + "╮"
    bot = "╰" + "─" * (inner + 2) + "╯"
    body = [f"│ {l.ljust(inner)} │" for l in lines]
    block = [top] + body + [bot]
    if align == "right":
        pad = 76 - (inner + 4)
        return [(" " * max(pad, 0)) + f"{color}{l}{RESET}" for l in block]
    return [f"{color}{l}{RESET}" for l in block]


def main():
    queue = load_queue()
    if not queue:
        print("Review queue is empty -- nothing to send.")
        return

    print(f"{BOLD}{BLUE}shelflife → Spectrum (dry run){RESET}")
    print(f"{DIM}No PHOTON_API_KEY set -- this is what would be sent.{RESET}")
    print(f"{DIM}{'─' * 78}{RESET}\n")

    for i, item in enumerate(queue, 1):
        who = f"{GRAY}Volunteer #{(item.get('resource_id', 0) % 4) + 1} · iMessage{RESET}"
        print(who)
        msg = build_message(item)
        for line in bubble(msg, align="left", color=CYAN):
            print(line)
        print()

        reply = "Y"
        print(f"{GRAY}Reviewer replies:{RESET}")
        for line in bubble(reply, align="right", color=GREEN, width=12):
            print(line)
        print()

        field = (item.get("change_request") or {}).get("field", "?")
        confirm = (
            f"✓ Confirmed. Filed to out/approved.json for #{item.get('resource_id')} "
            f"({field})."
        )
        for line in bubble(confirm, align="left", color=YELLOW):
            print(line)
        print(f"\n{DIM}{'─' * 78}{RESET}\n")

    print(
        f"{BOLD}{len(queue)} review(s) sent · "
        f"{GREEN}{len(queue)} confirmed{RESET}{BOLD} in this simulated run.{RESET}"
    )


if __name__ == "__main__":
    sys.exit(main() or 0)
