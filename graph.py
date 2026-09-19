"""The directory as a graph.

A flat table of listings cannot answer "this org just closed - what else is now
wrong?". A graph can, in one traversal. Three things depend on the graph and
stop existing without it:

  1. blast_radius  - a closure propagates to every service beneath the org
  2. contradictions - the same phone/address under different orgs
  3. propagate_staleness - doubt decays outward along edges

Zero dependencies: a directory of 1,759 orgs is small enough that an adjacency
dict IS the right data structure. FalkorDB/Cypher is the drop-in for real scale
(see README) - same model, same queries.
"""

import re
from collections import defaultdict

NORM_PHONE = re.compile(r"\D+")


def norm_phone(p):
    d = NORM_PHONE.sub("", p or "")
    return d[-10:] if len(d) >= 10 else None


def norm_addr(a):
    if not a:
        return None
    s = " ".join(str(a.get(k) or "") for k in ("address_1", "city", "postal_code"))
    s = re.sub(r"[^a-z0-9 ]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip() or None


class Graph:
    """Nodes: org, service, address, phone, category. Edges: typed, undirected."""

    def __init__(self):
        self.nodes = {}
        self.adj = defaultdict(set)

    def add(self, nid, kind, **attrs):
        self.nodes.setdefault(nid, {"id": nid, "kind": kind, **attrs})
        return nid

    def link(self, a, b, rel):
        self.adj[a].add((b, rel))
        self.adj[b].add((a, rel))

    def neighbors(self, nid, kind=None, rel=None):
        return [
            n
            for n, r in self.adj[nid]
            if (rel is None or r == rel) and (kind is None or self.nodes[n]["kind"] == kind)
        ]


def build(records):
    g = Graph()
    for r in records:
        org = g.add(
            f"org:{r['id']}",
            "org",
            name=r.get("name"),
            website=r.get("website"),
            status=r.get("status"),
            verified_at=r.get("verified_at"),
            certified_at=r.get("certified_at"),
            updated_at=r.get("updated_at"),
            schedule=r.get("schedule"),
            raw_id=r["id"],
        )

        for s in r.get("services") or []:
            sid = g.add(f"svc:{s['id']}", "service", name=s.get("name"), org=org)
            g.link(org, sid, "offers")
            for c in s.get("categories") or []:
                cid = g.add(f"cat:{c['id']}", "category", name=c.get("name"))
                g.link(sid, cid, "in_category")

        for c in r.get("categories") or []:
            cid = g.add(f"cat:{c['id']}", "category", name=c.get("name"))
            g.link(org, cid, "in_category")

        for p in r.get("phones") or []:
            n = norm_phone(p.get("number"))
            if n:
                g.link(org, g.add(f"tel:{n}", "phone", number=n), "reachable_at")

        for a in r.get("addresses") or []:
            k = norm_addr(a)
            if k:
                g.link(org, g.add(f"addr:{k}", "address", text=k), "located_at")
    return g


def blast_radius(g, org_id):
    """If this org is wrong, what else is now suspect?

    Services beneath it, plus sibling orgs sharing its address or phone - a
    relocation or shutdown almost never affects exactly one record.
    """
    services = g.neighbors(org_id, kind="service", rel="offers")
    siblings = set()
    for kind, rel in (("address", "located_at"), ("phone", "reachable_at")):
        for node in g.neighbors(org_id, kind=kind, rel=rel):
            siblings.update(o for o in g.neighbors(node, kind="org", rel=rel) if o != org_id)
    return {
        "services": [{"id": s, "name": g.nodes[s]["name"]} for s in services],
        "co_located_orgs": [{"id": o, "name": g.nodes[o]["name"]} for o in siblings],
        "size": len(services) + len(siblings),
    }


def contradictions(g):
    """Distinct orgs sharing a phone or address - merge candidates or stale data."""
    out = []
    for nid, node in g.nodes.items():
        if node["kind"] not in ("phone", "address"):
            continue
        orgs = g.neighbors(nid)
        if len(orgs) > 1:
            names = {g.nodes[o]["name"] for o in orgs}
            if len(names) > 1:
                out.append(
                    {
                        "shared": node.get("number") or node.get("text"),
                        "kind": node["kind"],
                        "orgs": [{"id": o, "name": g.nodes[o]["name"]} for o in orgs],
                    }
                )
    return sorted(out, key=lambda c: -len(c["orgs"]))


def propagate_staleness(g, seeds, decay=0.5, hops=2):
    """Doubt spreads. A confirmed-wrong org casts suspicion on its neighbourhood,
    weakening with distance. This is what ranks the review queue."""
    score = defaultdict(float)
    frontier = {s: 1.0 for s in seeds}
    for _ in range(hops):
        nxt = defaultdict(float)
        for nid, w in frontier.items():
            for nbr in g.neighbors(nid):
                if g.nodes[nbr]["kind"] in ("org", "service"):
                    nxt[nbr] = max(nxt[nbr], w * decay)
        for k, v in nxt.items():
            score[k] = max(score[k], v)
        frontier = nxt
    return dict(score)
