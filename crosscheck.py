"""Cross-check: FalkorDB backend vs the in-memory graph, same records.

    .venv/bin/python crosscheck.py

Compares all three queries over every org in the corpus. Any difference is a
real bug in one of the two implementations.
"""

import harvest
import graph as G
import graph_falkor as F


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


def main():
    recs = harvest.harvest(200)
    mem = G.build(recs)
    fal = F.build(recs)
    print(f"records {len(recs)} | in-memory {len(mem.nodes)} nodes | "
          f"falkordb {len(fal.nodes)} nodes")

    assert set(mem.nodes) == set(fal.nodes), "node id sets differ"
    print("node ids: identical")

    # --- blast_radius over every org
    orgs = [n for n, d in mem.nodes.items() if d["kind"] == "org"]
    bad = []
    sizes = []
    for o in orgs:
        a, b = G.blast_radius(mem, o), F.blast_radius(fal, o)
        sizes.append(a["size"])
        if norm_br(a) != norm_br(b):
            bad.append((o, a, b))
    print(f"blast_radius: {len(orgs)} orgs compared, {len(bad)} mismatches, "
          f"max size {max(sizes)}")
    for o, a, b in bad[:5]:
        print("  MISMATCH", o, a["size"], b["size"])

    # --- contradictions
    ca, cb = G.contradictions(mem), F.contradictions(fal)
    print(f"contradictions: in-memory {len(ca)}, falkordb {len(cb)}, "
          f"identical={norm_contra(ca) == norm_contra(cb)}")
    print(f"  order-by-size equal: {[len(c['orgs']) for c in ca] == [len(c['orgs']) for c in cb]}")
    for c in cb[:3]:
        print(f"  {c['kind']:7} {c['shared'][:40]:42} {[o['name'] for o in c['orgs']]}")

    # --- propagate_staleness, from every org individually and all at once
    worst = 0
    mism = 0
    for o in orgs:
        a = G.propagate_staleness(mem, [o])
        b = F.propagate_staleness(fal, [o])
        if a != b:
            mism += 1
            if mism <= 3:
                ka, kb = set(a), set(b)
                print(f"  MISMATCH {o}: only-mem={sorted(ka-kb)[:3]} "
                      f"only-falkor={sorted(kb-ka)[:3]} "
                      f"diff-scores={[k for k in ka & kb if a[k] != b[k]][:3]}")
        worst = max(worst, len(a))
    print(f"propagate_staleness: {len(orgs)} single-seed runs, {mism} mismatches, "
          f"max reached {worst}")

    # --- subgraph export (out/graph.json), both backends
    import run

    bad = 0
    for i in range(0, len(orgs), 20):
        focus = orgs[i : i + 3]
        pinned = [o["id"] for c in cb for o in c["orgs"]]
        x = run.memory_subgraph(mem, focus, pinned=pinned)
        y = F.subgraph(fal, focus, pinned=pinned)
        same = (
            sorted((n["id"], n["kind"], n["label"]) for n in x["nodes"])
            == sorted((n["id"], n["kind"], n["label"]) for n in y["nodes"])
            and sorted((e["source"], e["target"], e["rel"]) for e in x["edges"])
            == sorted((e["source"], e["target"], e["rel"]) for e in y["edges"])
            and x["truncated"] == y["truncated"]
        )
        bad += not same
        if not same:
            print(f"  MISMATCH subgraph {focus}: "
                  f"{len(x['nodes'])}/{len(y['nodes'])} nodes, "
                  f"{len(x['edges'])}/{len(y['edges'])} edges")
    print(f"subgraph: {len(range(0, len(orgs), 20))} focus sets compared, {bad} mismatches")

    allseeds = orgs[:25]
    a = G.propagate_staleness(mem, allseeds)
    b = F.propagate_staleness(fal, allseeds)
    print(f"propagate_staleness (25 seeds): in-memory {len(a)} nodes, "
          f"falkordb {len(b)} nodes, identical={a == b}")


if __name__ == "__main__":
    main()
