"""The same directory graph, in FalkorDB.

Drop-in for `graph.py`: identical public interface (`build`, `blast_radius`,
`contradictions`, `propagate_staleness`) and an identical node/edge model, so
`run.py` can swap backends with no other change.

The point of this file is that the three queries are *Cypher*, not Python loops
over a fetched result set:

  blast_radius       - two MATCH traversals UNIONed, one round trip
  contradictions     - a pattern match + aggregation, one round trip
  propagate_staleness - a variable-length path match, one round trip

Normalisation is imported from `graph.py` rather than reimplemented, so the two
backends are comparable by construction.

Model
  (:Node:Org      {id:"org:123",  kind:"org",      name, website, status, ...})
  (:Node:Service  {id:"svc:45",   kind:"service",  name, org})
  (:Node:Category {id:"cat:7",    kind:"category", name})
  (:Node:Phone    {id:"tel:415...", kind:"phone",   number, value})
  (:Node:Address  {id:"addr:...", kind:"address",  text, value})

  (Org)-[:OFFERS]->(Service)
  (Org|Service)-[:IN_CATEGORY]->(Category)
  (Org)-[:LOCATED_AT]->(Address)
  (Org)-[:REACHABLE_AT]->(Phone)

Every node carries the shared `:Node` label so untyped traversals
(`propagate_staleness`) can still hit the `:Node(id)` index.
"""

import os

from graph import norm_addr, norm_phone  # single source of truth for keys

HOST = os.environ.get("FALKORDB_HOST", "localhost")
PORT = int(os.environ.get("FALKORDB_PORT", 6379))
GRAPH_NAME = os.environ.get("FALKORDB_GRAPH", "shelflife")

# kind -> extra label. Order fixed so generated Cypher is stable/cacheable.
LABELS = {
    "org": "Org",
    "service": "Service",
    "category": "Category",
    "phone": "Phone",
    "address": "Address",
}

# Node kinds doubt is allowed to travel through. Mirrors graph.py, which only
# ever puts org/service nodes on the propagation frontier.
SPREADS = ["org", "service"]


class FalkorGraph:
    """Handle returned by build(). Quacks like graph.Graph where run.py looks."""

    def __init__(self, g, ids):
        self.g = g
        self.name = GRAPH_NAME
        self.backend = "falkordb"
        # run.py does `len(g.nodes)` and `node in g.nodes`; these ids come back
        # out of the database after the load, so the count is the real one.
        self.nodes = {i: True for i in ids}

    def query(self, q, params=None):
        return self.g.query(q, params=params or {})


def connect(host=HOST, port=PORT, timeout=3):
    """Raises if FalkorDB is not reachable - run.py catches and falls back."""
    from falkordb import FalkorDB

    db = FalkorDB(
        host=host, port=port, socket_timeout=timeout, socket_connect_timeout=timeout
    )
    db.connection.ping()
    return db


# ---------------------------------------------------------------- build


def _flatten(records):
    """records -> (nodes by kind, edges by type). Mirrors graph.py's build()."""
    nodes = {k: {} for k in LABELS}
    edges = {"OFFERS": set(), "IN_CATEGORY": set(), "LOCATED_AT": set(), "REACHABLE_AT": set()}

    def add(kind, nid, **attrs):
        nodes[kind].setdefault(nid, {"id": nid, "kind": kind, **attrs})
        return nid

    for r in records:
        org = add(
            "org",
            f"org:{r['id']}",
            name=r.get("name"),
            website=r.get("website"),
            status=r.get("status"),
            verified_at=r.get("verified_at"),
            certified_at=r.get("certified_at"),
            updated_at=r.get("updated_at"),
            raw_id=str(r["id"]),
        )

        for s in r.get("services") or []:
            sid = add("service", f"svc:{s['id']}", name=s.get("name"), org=org)
            edges["OFFERS"].add((org, sid))
            for c in s.get("categories") or []:
                cid = add("category", f"cat:{c['id']}", name=c.get("name"))
                edges["IN_CATEGORY"].add((sid, cid))

        for c in r.get("categories") or []:
            cid = add("category", f"cat:{c['id']}", name=c.get("name"))
            edges["IN_CATEGORY"].add((org, cid))

        for p in r.get("phones") or []:
            n = norm_phone(p.get("number"))
            if n:
                add("phone", f"tel:{n}", number=n, value=n)
                edges["REACHABLE_AT"].add((org, f"tel:{n}"))

        for a in r.get("addresses") or []:
            k = norm_addr(a)
            if k:
                add("address", f"addr:{k}", text=k, value=k)
                edges["LOCATED_AT"].add((org, f"addr:{k}"))

    return nodes, edges


def build(records, db=None, reset=True, batch=500):
    """Load the directory into FalkorDB. Indexes first, then batched UNWINDs."""
    db = db or connect()
    g = db.select_graph(GRAPH_NAME)

    if reset:
        try:
            g.delete()
        except Exception:
            pass  # graph did not exist yet

    # Indexes before the load: every edge insert resolves two ids by lookup.
    for label in ("Node", *LABELS.values()):
        try:
            g.query(f"CREATE INDEX FOR (n:{label}) ON (n.id)")
        except Exception:
            pass  # already exists

    nodes, edges = _flatten(records)

    for kind, label in LABELS.items():
        rows = list(nodes[kind].values())
        for i in range(0, len(rows), batch):
            g.query(
                f"UNWIND $rows AS r CREATE (n:Node:{label}) SET n = r",
                params={"rows": rows[i : i + batch]},
            )

    for rel, pairs in edges.items():
        rows = [{"a": a, "b": b} for a, b in sorted(pairs)]
        for i in range(0, len(rows), batch):
            g.query(
                "UNWIND $rows AS r "
                "MATCH (a:Node {id: r.a}) MATCH (b:Node {id: r.b}) "
                f"CREATE (a)-[:{rel}]->(b)",
                params={"rows": rows[i : i + batch]},
            )

    ids = [row[0] for row in g.query("MATCH (n:Node) RETURN n.id").result_set]
    return FalkorGraph(g, ids)


# ---------------------------------------------------------------- queries

BLAST_RADIUS = """
MATCH (o:Org {id: $org})-[:OFFERS]->(s:Service)
RETURN 'service' AS kind, s.id AS id, s.name AS name
UNION
MATCH (o:Org {id: $org})-[r1:LOCATED_AT|REACHABLE_AT]->(shared)
      <-[r2:LOCATED_AT|REACHABLE_AT]-(p:Org)
WHERE p.id <> $org AND type(r1) = type(r2)
RETURN 'org' AS kind, p.id AS id, p.name AS name
"""

CONTRADICTIONS = """
MATCH (n:Node)<-[:LOCATED_AT|REACHABLE_AT]-(o:Org)
WHERE n.kind IN ['phone', 'address']
WITH n, collect(DISTINCT o) AS orgs, count(DISTINCT o.name) AS distinct_names
WHERE size(orgs) > 1 AND distinct_names > 1
RETURN n.value AS shared,
       n.kind  AS kind,
       [x IN orgs | {id: x.id, name: x.name}] AS orgs
ORDER BY size(orgs) DESC, shared
"""

# Variable-length traversal from the seeds. Every node *after* the seed must be
# an org or a service - doubt travels org->service->org, it does not leak
# sideways through a shared category. min(length) per node, because the score is
# decay ** distance and the shortest path wins the max.
STALENESS = """
UNWIND $seeds AS sid
MATCH p = (s:Node {id: sid})-[*1..%d]-(n:Node)
WHERE all(x IN tail(nodes(p)) WHERE x.kind IN $spreads)
RETURN n.id AS id, min(length(p)) AS dist
"""

# graph.py walks, Cypher trails: its BFS may cross the same edge twice, so a
# seed lands back on itself at distance 2 (org -> service -> org). Cypher's
# relationship-uniqueness rule forbids reusing an edge inside one path, so the
# clause above cannot express that. This is the only case where the two differ
# (a shortest *walk* is otherwise always a path), so it gets its own clause
# rather than a silent behaviour change.
STALENESS_SELF = """
UNWIND $seeds AS sid
MATCH (s:Node {id: sid})-[]-(m:Node)
WHERE s.kind IN $spreads AND m.kind IN $spreads
RETURN s.id AS id, 2 AS dist
"""


def blast_radius(g, org_id):
    """If this org is wrong, what else is now suspect? One round trip."""
    rows = g.query(BLAST_RADIUS, {"org": org_id}).result_set
    services = [{"id": r[1], "name": r[2]} for r in rows if r[0] == "service"]
    co = [{"id": r[1], "name": r[2]} for r in rows if r[0] == "org"]
    return {"services": services, "co_located_orgs": co, "size": len(services) + len(co)}


def contradictions(g):
    """Distinct orgs sharing a phone or address - merge candidates or stale data."""
    rows = g.query(CONTRADICTIONS).result_set
    # ORDER BY in the query fixes the row order; orgs within a row come back in
    # collect() order, so they are sorted by id here to match graph.py exactly.
    return [
        {
            "shared": r[0],
            "kind": r[1],
            "orgs": sorted(
                ({"id": o["id"], "name": o["name"]} for o in r[2]),
                key=lambda o: o["id"],
            ),
        }
        for r in rows
    ]


# --- subgraph export, for ui/graph.html -------------------------------------

SUBGRAPH_NODES = """
UNWIND $focus AS fid
MATCH p = (s:Node {id: fid})-[*0..%d]-(n:Node)
RETURN n.id AS id, min(length(p)) AS dist
"""

# Labels render inside a node in ui/graph.html, so they are cut in the query.
LABEL_CHARS = 40

SUBGRAPH_ATTRS = """
MATCH (n:Node) WHERE n.id IN $ids
WITH n, coalesce(n.name, n.value, n.id) AS full
RETURN n.id AS id, n.kind AS kind,
       CASE WHEN size(full) > $chars
            THEN left(full, $chars - 1) + '…' ELSE full END AS label
"""

SUBGRAPH_EDGES = """
MATCH (a:Node)-[r]->(b:Node)
WHERE a.id IN $ids AND b.id IN $ids
RETURN a.id AS source, type(r) AS rel, b.id AS target
"""


def subgraph(g, focus, hops=2, cap=300, pinned=()):
    """The focus orgs' n-hop neighbourhood, capped, for the visualiser.

    `pinned` ids survive the cap (contradiction orgs, so the shared-switchboard
    picture never gets truncated away). Nearest nodes win the remaining budget.
    """
    dist = {}
    if focus:
        for nid, d in g.query(SUBGRAPH_NODES % int(hops), {"focus": list(focus)}).result_set:
            d = int(d)
            if dist.get(nid, d + 1) > d:
                dist[nid] = d

    order = list(dict.fromkeys(list(pinned) + sorted(dist, key=lambda n: (dist[n], n))))
    keep = order[:cap]
    truncated = len(order) > cap

    attrs = g.query(SUBGRAPH_ATTRS, {"ids": keep, "chars": LABEL_CHARS}).result_set
    nodes = [{"id": r[0], "kind": r[1], "label": r[2]} for r in attrs]
    edges = [
        {"source": r[0], "target": r[2], "rel": r[1].lower()}
        for r in g.query(SUBGRAPH_EDGES, {"ids": keep}).result_set
    ]
    return {"nodes": nodes, "edges": edges, "truncated": truncated}


def propagate_staleness(g, seeds, decay=0.5, hops=2):
    """Doubt spreads, weakening with distance. Ranks the review queue."""
    seeds = list(seeds)
    hops = int(hops)
    if not seeds or hops < 1:
        return {}
    params = {"seeds": seeds, "spreads": SPREADS}
    rows = list(g.query(STALENESS % hops, params).result_set)
    if hops >= 2:
        rows += g.query(STALENESS_SELF, params).result_set

    # Nearest wins: score is decay ** distance, and max score == min distance.
    best = {}
    for nid, dist in rows:
        d = int(dist)
        if best.get(nid, d + 1) > d:
            best[nid] = d
    return {nid: decay**d for nid, d in best.items()}
