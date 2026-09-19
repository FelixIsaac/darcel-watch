"""Darcel Watch - end to end.

  harvest -> triage -> verify -> adjudicate -> emit

Writes out/results.json, which ui/index.html renders.
Read-only against the SF Service Guide. We never POST to production.

    python3 run.py                  # evidence-only mode
    GEMINI_API_KEY=... python3 run.py   # + Gemini adjudication
"""

import datetime as dt
import json
import os
import pathlib
import statistics

import graph as G
import harvest

OUT = pathlib.Path(__file__).parent / "out"
NOW = dt.datetime.now(dt.timezone.utc)

# Categories where being wrong hurts most. A stale tutoring listing is an
# annoyance; a stale shelter or clinic listing strands someone at night.
CRITICAL = (
    "shelter", "housing", "food", "meal", "pantry", "health", "medical",
    "clinic", "mental", "crisis", "legal", "hygiene", "shower", "detox",
    "addiction", "recovery", "emergency", "domestic",
)


def age_days(ts):
    if not ts:
        return None
    try:
        return (NOW - dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))).days
    except ValueError:
        return None


def baseline_stats(records):
    """The finding that justifies the project. Measured, not quoted."""
    approved = [r for r in records if r.get("status") == "approved"]
    ages = [age_days(r.get("verified_at")) for r in approved]
    known = sorted(a for a in ages if a is not None)
    return {
        "sampled": len(records),
        "approved": len(approved),
        "never_verified": sum(1 for a in ages if a is None),
        "median_verified_age_days": int(statistics.median(known)) if known else None,
        "verified_last_year": sum(1 for a in known if a < 365),
        "with_website": sum(1 for r in approved if r.get("website")),
    }


def triage_score(record):
    """Cheap, deterministic, no model calls. Decides where to spend tokens.

    Never-verified + critical category + has a checkable website ranks highest.
    """
    if record.get("status") != "approved":
        return 0.0
    if not record.get("website"):
        return 0.0

    score = 1.0
    va = age_days(record.get("verified_at"))
    score += 3.0 if va is None else min(va / 365.0, 3.0)

    names = " ".join(
        (c.get("name") or "") for c in (record.get("categories") or [])
    ).lower()
    blob = f"{record.get('name','')} {names}".lower()
    if any(k in blob for k in CRITICAL):
        score += 4.0

    if not (record.get("schedule") or {}).get("hours_known"):
        score += 1.0
    return score


def main():
    budget = int(os.environ.get("BUDGET", 25))
    # Gemini 2.5 Flash either way - OpenRouter is just the transport.
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("GEMINI_API_KEY")

    print("harvesting (read-only, cached)...")
    records = harvest.harvest(int(os.environ.get("N", 200)))
    try:
        corpus = harvest.corpus_size()
    except Exception:
        corpus = {"resources": None, "services": None}

    stats = baseline_stats(records)
    print(f"  {len(records)} records | never verified: {stats['never_verified']}"
          f"/{stats['approved']} approved")

    g = G.build(records)
    print(f"  graph: {len(g.nodes)} nodes")

    ranked = sorted(records, key=triage_score, reverse=True)
    candidates = [r for r in ranked if triage_score(r) > 0][:budget]
    print(f"verifying {len(candidates)} "
          f"({'Gemini adjudication' if api_key else 'evidence-only'})...")

    import verify  # imported late so the rest runs even if this file is mid-edit

    verdicts = verify.verify_many(candidates, api_key=api_key)

    queue, abstained = [], []
    for v in verdicts:
        node = f"org:{v['resource_id']}"
        if v["verdict"] == "discrepancy":
            radius = G.blast_radius(g, node) if node in g.nodes else {"size": 0}
            v["blast_radius"] = radius
            # Confidence alone is a bad rank. A wrong record that invalidates
            # twelve downstream services deserves a volunteer's attention first.
            v["priority"] = round(v.get("confidence", 0.5) * (1 + radius["size"] * 0.15), 3)
            queue.append(v)
        elif v["verdict"] == "abstain":
            abstained.append(v)

    seeds = [f"org:{v['resource_id']}" for v in queue if f"org:{v['resource_id']}" in g.nodes]
    suspicion = G.propagate_staleness(g, seeds)

    queue.sort(key=lambda v: -v.get("priority", 0))
    stats.update(
        checked=len(verdicts),
        discrepancies=len(queue),
        abstained=len(abstained),
        matched=sum(1 for v in verdicts if v["verdict"] == "match"),
        gemini=bool(api_key),
        downstream_suspect=len(suspicion),
    )

    OUT.mkdir(exist_ok=True)
    payload = {
        "generated_at": NOW.isoformat(),
        "corpus": corpus,
        "stats": stats,
        "queue": queue,
        "abstained": abstained,
        "contradictions": G.contradictions(g)[:15],
        "source": "https://askdarcel.org/api (public, read-only)",
    }
    (OUT / "results.json").write_text(json.dumps(payload, indent=1))

    print(f"\n  discrepancies {len(queue)} | abstained {len(abstained)} "
          f"| matched {stats['matched']}")
    print(f"  {len(payload['contradictions'])} shared phone/address contradictions")
    print(f"  -> {OUT/'results.json'}")
    print("\n  python3 -m http.server 8000  then open ui/index.html")


if __name__ == "__main__":
    main()
