"""Proves graph_falkor.py (Cypher) and graph.py (adjacency dict) agree exactly.

Run it with `.venv/bin/python crosscheck.py`, FalkorDB up. It builds both
backends from the whole harvested corpus - every record harvest.harvest()
returns, not a sample - and compares every query: node ids, blast_radius for
every org, contradictions in full (including the published order, not just the
group sizes), propagate_staleness from every org individually and from 25 seeds
at 1-3 hops, and the out/graph.json subgraph export.

Every comparison is an assertion. A mismatch anywhere fails the run with a
non-zero exit code; there is no "N mismatches" counter that prints and passes.
Exit 0 means the FalkorDB port changed no results - which is what makes the
fallback honest.
"""

import sys

import harvest
import graph as G
import graph_falkor as F


class Mismatch(AssertionError):
    pass


def check(ok, label, detail=""):
    """One comparison. Prints it either way; raises when it fails."""
    if ok:
        print(f"  ok   {label}")
        return
    raise Mismatch(f"{label}{': ' + detail if detail else ''}")


def norm_br(r):
    return (
        frozenset((s["id"], s["name"]) for s in r["services"]),
        frozenset((o["id"], o["name"]) for o in r["co_located_orgs"]),
        r["size"],
    )


def norm_contra(rows):
    return frozenset(
        (c["shared"], c["kind"], frozenset((o["id"], o["name"]) for o in c["orgs"]))
        for c in rows
    )


def ordered_contra(rows):
    """The exact published shape: row order and org order both significant.

    run.py serialises contradictions[:15] into out/results.json, and many rows
    tie on group size right at that cut - so comparing only the size sequence
    would pass while the two backends emitted different files.
    """
    return [
        (c["shared"], c["kind"], tuple((o["id"], o["name"]) for o in c["orgs"]))
        for c in rows
    ]


def main():
    recs = harvest.harvest()  # whole corpus
    mem = G.build(recs)
    fal = F.build(recs)
    print(f"records {len(recs)} | in-memory {len(mem.nodes)} nodes | "
          f"falkordb {len(fal.nodes)} nodes")

    check(set(mem.nodes) == set(fal.nodes), "node ids identical",
          f"{len(set(mem.nodes) ^ set(fal.nodes))} ids differ")

    # --- blast_radius over every org
    orgs = sorted(n for n, d in mem.nodes.items() if d["kind"] == "org")
    bad = [
        o for o in orgs
        if norm_br(G.blast_radius(mem, o)) != norm_br(F.blast_radius(fal, o))
    ]
    check(not bad, f"blast_radius identical for all {len(orgs)} orgs",
          f"first mismatches {bad[:5]}")

    # --- contradictions: contents, then the exact published order
    ca, cb = G.contradictions(mem), F.contradictions(fal)
    check(len(ca) == len(cb), "contradictions count equal", f"{len(ca)} vs {len(cb)}")
    check(norm_contra(ca) == norm_contra(cb), "contradiction contents identical")
    oa, ob = ordered_contra(ca), ordered_contra(cb)
    first = next((i for i, (x, y) in enumerate(zip(oa, ob)) if x != y), None)
    check(oa == ob, f"contradiction order identical ({len(ca)} rows)",
          f"first difference at row {first}: {oa[first:first+1]} vs {ob[first:first+1]}"
          if first is not None else "")
    for c in cb[:3]:
        print(f"       {c['kind']:7} {c['shared'][:40]:42} {[o['name'] for o in c['orgs']]}")

    # --- propagate_staleness, from every org individually
    bad = []
    for o in orgs:
        a, b = G.propagate_staleness(mem, [o]), F.propagate_staleness(fal, [o])
        if a != b:
            ka, kb = set(a), set(b)
            bad.append((o, sorted(ka - kb)[:3], sorted(kb - ka)[:3],
                        [k for k in ka & kb if a[k] != b[k]][:3]))
    check(not bad, f"propagate_staleness identical for all {len(orgs)} single seeds",
          f"first mismatches {bad[:3]}")

    # --- subgraph export (out/graph.json), both backends
    import run

    pinned = [o["id"] for c in cb for o in c["orgs"]]
    focus_sets = [orgs[i:i + 3] for i in range(0, len(orgs), 20)]
    bad = []
    for focus in focus_sets:
        x = run.memory_subgraph(mem, focus, pinned=pinned)
        y = F.subgraph(fal, focus, pinned=pinned)
        same = (
            sorted((n["id"], n["kind"], n["label"]) for n in x["nodes"])
            == sorted((n["id"], n["kind"], n["label"]) for n in y["nodes"])
            and sorted((e["source"], e["target"], e["rel"]) for e in x["edges"])
            == sorted((e["source"], e["target"], e["rel"]) for e in y["edges"])
            and x["truncated"] == y["truncated"]
        )
        if not same:
            bad.append((focus, len(x["nodes"]), len(y["nodes"]),
                        len(x["edges"]), len(y["edges"])))
    check(not bad, f"subgraph identical for all {len(focus_sets)} focus sets",
          f"first mismatches {bad[:3]}")

    # --- propagate_staleness with many seeds at once, at every hop depth.
    # Depth is where the two implementations are most likely to part: a walk and
    # a path agree at 1 hop and diverge as the radius grows.
    allseeds = orgs[:25]
    # 1-3 only. At 4 the Cypher variable-length match exceeds FalkorDB's default
    # query timeout even from a single seed, so a 4-hop parity claim is not one
    # we can make. The pipeline only ever runs at the default 2.
    for hops in (1, 2, 3):
        a = G.propagate_staleness(mem, allseeds, hops=hops)
        b = F.propagate_staleness(fal, allseeds, hops=hops)
        check(a == b,
              f"propagate_staleness identical for 25 seeds at {hops} hop(s) "
              f"({len(a)} nodes)",
              f"{len(set(a) ^ set(b))} node ids differ")


if __name__ == "__main__":
    try:
        main()
    except Mismatch as e:
        print(f"\nFAIL: {e}")
        sys.exit(1)
    print("\nPASS: both backends agree on every query.")
