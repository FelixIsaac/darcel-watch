"""SF Service Guide Watch - end to end.

  harvest -> triage -> verify -> adjudicate -> emit

Writes out/results.json (ui/index.html) and out/graph.json (ui/graph.html).
Read-only against the SF Service Guide. We never POST to production.

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

OUT = pathlib.Path(__file__).parent / "out"
NOW = dt.datetime.now(dt.timezone.utc)

# Categories where being wrong hurts most. A stale tutoring listing is an
# annoyance; a stale shelter or clinic listing strands someone at night.
CRITICAL = (
    "shelter", "housing", "food", "meal", "pantry", "health", "medical",
    "clinic", "mental", "crisis", "legal", "hygiene", "shower", "detox",
    "addiction", "recovery", "emergency", "domestic",
)


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

    return {
        "nodes": [
            {
                "id": n,
                "kind": g.nodes[n]["kind"],
                "label": g.nodes[n].get("name") or g.nodes[n].get("number")
                or g.nodes[n].get("text") or n,
            }
            for n in keep
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
        "contradictions": GQ.contradictions(g)[:15],
        "source": "https://askdarcel.org/api (public, read-only)",
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
