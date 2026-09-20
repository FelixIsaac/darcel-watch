"""SF Service Guide Watch - end to end.

  harvest -> triage -> verify -> adjudicate -> emit

Writes out/results.json (ui/index.html) and out/graph.json (ui/graph.html).
Read-only against the SF Service Guide: every request to their API is a GET.

    python3 run.py                  # evidence-only, in-memory graph
    GEMINI_API_KEY=... python3 run.py   # + Gemini adjudication

The graph layer runs on FalkorDB when it is reachable AND the `falkordb`
client is importable - which means the venv interpreter, not system python3:

    .venv/bin/python run.py         # FalkorDB backend
"""

import datetime as dt
import json
import os
import pathlib
import statistics

import graph as G
import harvest

import envfile

envfile.load()

OUT = pathlib.Path(__file__).parent / "out"
NOW = dt.datetime.now(dt.timezone.utc)

# Categories where being wrong hurts most. A stale tutoring listing is an
# annoyance; a stale shelter or clinic listing strands someone at night.
CRITICAL = (
    "shelter", "housing", "food", "meal", "pantry", "health", "medical",
    "clinic", "mental", "crisis", "legal", "hygiene", "shower", "detox",
    "addiction", "recovery", "emergency", "domestic",
)


# Node labels render inside a node in ui/graph.html. Must match
# graph_falkor.LABEL_CHARS so both backends emit the same file.
LABEL_CHARS = 40


def graph_falkor_endpoint():
    host = os.environ.get("FALKORDB_HOST", "localhost")
    return f"{host}:{os.environ.get('FALKORDB_PORT', '6379')}"


def memory_subgraph(g, focus, hops=2, cap=300, pinned=()):
    """In-memory twin of graph_falkor.subgraph(), same contract.

    Lives here rather than in graph.py so the zero-dependency fallback module
    stays exactly as shipped.
    """
    dist, frontier = {f: 0 for f in focus if f in g.nodes}, list(focus)
    for d in range(1, hops + 1):
        nxt = []
        for nid in frontier:
            for nbr in g.neighbors(nid):
                if nbr not in dist:
                    dist[nbr] = d
                    nxt.append(nbr)
        frontier = nxt

    order = list(dict.fromkeys(list(pinned) + sorted(dist, key=lambda n: (dist[n], n))))
    keep = [n for n in order[:cap] if n in g.nodes]
    truncated = len(order) > cap

    # graph.py stores every edge both ways; re-orient to the FalkorDB model by
    # keeping only the arc that points at the relationship's head kind.
    HEAD = {"offers": "service", "in_category": "category",
            "located_at": "address", "reachable_at": "phone"}
    kept = set(keep)
    edges = {
        (a, b, rel)
        for a in keep
        for b, rel in g.adj[a]
        if b in kept and g.nodes[b]["kind"] == HEAD[rel]
    }

    def label(n):
        full = (g.nodes[n].get("name") or g.nodes[n].get("number")
                or g.nodes[n].get("text") or n)
        return full if len(full) <= LABEL_CHARS else full[: LABEL_CHARS - 1] + "…"

    return {
        "nodes": [
            {"id": n, "kind": g.nodes[n]["kind"], "label": label(n)} for n in keep
        ],
        "edges": [{"source": a, "target": b, "rel": rel} for a, b, rel in sorted(edges)],
        "truncated": truncated,
    }


def age_days(ts):
    if not ts:
        return None
    try:
        return (NOW - dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))).days
    except ValueError:
        return None


def baseline_stats(records):
    """Provenance coverage, measured. NOT a measure of neglect.

    The directory is actively maintained - nearly every approved listing has
    been touched in the last 90 days, in daily batches that look like datathon
    sessions. What is missing is provenance: `updated_at` records THAT something
    changed, not that anyone confirmed it against reality.

    So the number to report is `no_verification_signal` - listings carrying no
    verification evidence at all: no `verified_at`, no `certified_at`, and no
    `certified` flag. An earlier version of this function counted missing
    `verified_at` alone and called the result "never verified". That was wrong
    twice over: `verified_at` was abandoned around 2022 (newest value in the
    corpus is 2022-10-12), so it measures a dead field rather than neglect, and
    it ignored the listings that carry a `certified` flag with no date.
    See FACTS.md sections E and F.
    """
    approved = [r for r in records if r.get("status") == "approved"]
    updated = sorted(
        a for a in (age_days(r.get("updated_at")) for r in approved)
        if a is not None
    )
    return {
        "sampled": len(records),
        "approved": len(approved),
        # No date and no flag - nothing at all saying anyone checked this.
        "no_verification_signal": sum(
            1 for r in approved
            if not r.get("verified_at")
            and not r.get("certified_at")
            and not r.get("certified")
        ),
        # Evidence of active maintenance, reported alongside so the first
        # number is never read as "nobody looks at this directory".
        "median_updated_age_days": int(statistics.median(updated)) if updated else None,
        "updated_last_90d": sum(1 for a in updated if a <= 90),
        "with_website": sum(1 for r in approved if r.get("website")),
    }


def triage_score(record):
    """Cheap, deterministic, no model calls. Decides where to spend tokens.

    No `verified_at` + critical category + a checkable website ranks highest.

    Caveat worth stating: `verified_at` stopped being written around 2022, so
    the age term is saturated for most of the corpus and contributes little
    ordering. Ranking on freshness score instead would be better; left as-is
    rather than changed silently.
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
    # N=0 means the whole curation dataset (816 resources). The old default of 200
    # existed because v1 had no work list and we sampled random ids; v2 hands us
    # ShelterTech's own list, so there is nothing to sample.
    records = harvest.harvest(int(os.environ.get("N", 0)))
    try:
        corpus = harvest.corpus_size()
    except Exception:
        corpus = {"resources": None, "services": None}

    stats = baseline_stats(records)
    print(f"  {len(records)} records | no verification signal: "
          f"{stats['no_verification_signal']}/{stats['approved']} approved "
          f"| {stats['updated_last_90d']} updated in the last 90 days")

    # FalkorDB when it is reachable, the in-memory adjacency graph when it is
    # not. Same model, same three queries, cross-checked identical - so the
    # pipeline never hard-fails just because Docker is down.
    graph_falkor, why = None, None
    try:
        import graph_falkor

        g = graph_falkor.build(records)
        GQ, backend, label = graph_falkor, "falkordb", "FalkorDB"
    except ImportError as e:
        # A missing dependency is not Docker being down. Saying so costs ten
        # seconds of demo debugging; conflating them costs the demo.
        graph_falkor = None
        why = (f"falkordb client not installed ({e}) - run with "
               ".venv/bin/python, or pip install -r requirements.txt")
    except Exception as e:
        graph_falkor = None
        why = (f"FalkorDB unreachable at {graph_falkor_endpoint()} "
               f"({type(e).__name__}: {e}) - is the container running?")

    if graph_falkor is None:
        g = G.build(records)
        GQ, backend, label = G, "in-memory", "in-memory fallback"
        print(f"  {why}")
    print(f"  graph: {len(g.nodes)} nodes ({label})")

    ranked = sorted(records, key=triage_score, reverse=True)
    candidates = [r for r in ranked if triage_score(r) > 0][:budget]
    print(f"verifying {len(candidates)} "
          f"({'Gemini adjudication' if api_key else 'evidence-only'})...")

    import verify  # imported late so the rest runs even if this file is mid-edit

    verdicts = verify.verify_many(candidates, api_key=api_key)

    # Structural checks are free: no fetch, no model, no judgement. They read
    # the stored value and nothing else, so there is no reason to ration them
    # behind the budget that exists to limit network and token spend. Run them
    # over the WHOLE corpus, and merge into the verdicts we already have.
    #
    # This also fixes a real regression: when the corpus grew from a 156-record
    # sample to all 816, triage stopped selecting the records carrying the
    # structural defects, and the most certain findings we have vanished from
    # the queue entirely.
    checked = {v["resource_id"] for v in verdicts}
    structural = 0
    for r in records:
        if r.get("status") != "approved":
            continue
        f = verify.check_phone_format(r)
        if not f:
            continue
        structural += 1
        if r.get("id") in checked:
            # Already verified against its source - attach, preferring the
            # structural row since it cannot be a scraping false positive.
            for v in verdicts:
                if v["resource_id"] == r.get("id"):
                    v["fields"] = [f] + [x for x in v.get("fields") or []]
                    v["verdict"] = "discrepancy"
                    v["reason"] = f["stored"]
                    v["confidence"] = max(v.get("confidence", 0), 0.95)
                    v["change_request"] = verify.build_change_request(r, [f], "discrepancy")
                    break
            continue
        verdicts.append({
            "resource_id": r.get("id"), "name": r.get("name", ""),
            "verdict": "discrepancy", "reason": f["stored"], "confidence": 0.95,
            "fetched": [], "fields": [f],
            "listing_url": verify.listing_url(r.get("id")),
            "listing_edit_url": verify.edit_url(r),
            "org_website": r.get("website"),
            "change_request": verify.build_change_request(r, [f], "discrepancy"),
        })
    print(f"  structural: {structural} listing(s) with a defect provable from "
          f"the stored value alone (whole corpus, no model)")

    queue, abstained = [], []
    for v in verdicts:
        node = f"org:{v['resource_id']}"
        if v["verdict"] == "discrepancy":
            radius = GQ.blast_radius(g, node) if node in g.nodes else {"size": 0}
            v["blast_radius"] = radius
            # Confidence alone is a bad rank. A wrong record that invalidates
            # twelve downstream services deserves a volunteer's attention first.
            v["priority"] = round(v.get("confidence", 0.5) * (1 + radius["size"] * 0.15), 3)
            queue.append(v)
        elif v["verdict"] == "abstain":
            abstained.append(v)

    seeds = [f"org:{v['resource_id']}" for v in queue if f"org:{v['resource_id']}" in g.nodes]
    suspicion = GQ.propagate_staleness(g, seeds)

    queue.sort(key=lambda v: -v.get("priority", 0))
    stats.update(
        checked=len(verdicts),
        discrepancies=len(queue),
        abstained=len(abstained),
        matched=sum(1 for v in verdicts if v["verdict"] == "match"),
        gemini=bool(api_key),
        downstream_suspect=len(suspicion),
        graph_backend=backend,
    )

    OUT.mkdir(exist_ok=True)
    payload = {
        "generated_at": NOW.isoformat(),
        "corpus": corpus,
        "stats": stats,
        "queue": queue,
        "abstained": abstained,
        # A "match" is not a null result. The organisation's own site agreed
        # with the stored value, which is a real confirmation and the single
        # most common outcome of a run. Discarding it was throwing away most
        # of the work: these feed freshness.py and are what makes the index
        # move when the agent runs.
        "confirmed_matches": [
            {
                "resource_id": v["resource_id"],
                "name": v.get("name"),
                "fields": [f["field"] for f in (v.get("fields") or [])],
                "evidence_url": next(
                    (f.get("evidence_url") for f in (v.get("fields") or [])
                     if f.get("evidence_url")), None
                ),
            }
            for v in verdicts if v["verdict"] == "match"
        ],
        "contradictions": GQ.contradictions(g)[:15],
        "source": "https://www.sfserviceguide.org/api/v2 (public, read-only)",
    }
    (OUT / "results.json").write_text(json.dumps(payload, indent=1))

    # --- out/graph.json, for ui/graph.html --------------------------------
    # Focus: the top queue orgs, topped up from triage rank if the queue is
    # short, so the visualiser always has something to draw.
    focus = [f"org:{v['resource_id']}" for v in queue][:3]
    for r in ranked:
        if len(focus) >= 3:
            break
        nid = f"org:{r['id']}"
        if nid not in focus and nid in g.nodes:
            focus.append(nid)

    contras = payload["contradictions"]
    # Contradiction orgs are pinned past the cap: three listings sharing one
    # switchboard is the picture worth keeping.
    pinned = [o["id"] for c in contras for o in c["orgs"] if o["id"] in g.nodes]

    sub = (graph_falkor.subgraph if backend == "falkordb" else memory_subgraph)(
        g, focus, hops=2, cap=300, pinned=pinned
    )

    blast = {f: GQ.blast_radius(g, f) for f in focus if f in g.nodes}
    suspect = set(suspicion) | {
        n["id"] for b in blast.values() for n in b["services"] + b["co_located_orgs"]
    }
    for n in sub["nodes"]:
        n["suspect"] = n["id"] in suspect

    (OUT / "graph.json").write_text(json.dumps({
        "backend": backend,
        "generated_at": NOW.isoformat(),
        "focus": focus,
        "nodes": sub["nodes"],
        "edges": sub["edges"],
        "truncated": sub["truncated"],
        "blast": {
            f: [n["id"] for n in b["services"] + b["co_located_orgs"]]
            for f, b in blast.items()
        },
        "contradictions": [
            {"shared": c["shared"], "kind": c["kind"], "orgs": [o["id"] for o in c["orgs"]]}
            for c in contras
        ],
    }, indent=1))

    print(f"\n  discrepancies {len(queue)} | abstained {len(abstained)} "
          f"| matched {stats['matched']}")
    print(f"  {len(payload['contradictions'])} shared phone/address contradictions")
    print(f"  -> {OUT/'results.json'}")
    print(f"  -> {OUT/'graph.json'} ({len(sub['nodes'])} nodes, "
          f"{len(sub['edges'])} edges{', truncated' if sub['truncated'] else ''}, {backend})")
    print("\n  python3 -m http.server 8000  then open ui/index.html")


if __name__ == "__main__":
    main()
