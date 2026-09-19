"""Harvest listings from the live SF Service Guide (AskDarcel) API.

Read-only. Public, unauthenticated endpoints. We never write to production.
"""

import concurrent.futures as cf
import json
import os
import pathlib
import random
import urllib.error
import urllib.request

API = "https://askdarcel.org/api"
DATA = pathlib.Path(__file__).parent / "data"
UA = {"User-Agent": "darcel-watch/0.1 (Hack for Humanity SF; read-only)"}


def get(path, timeout=30):
    req = urllib.request.Request(f"{API}{path}", headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def corpus_size():
    """The headline numbers. Verified live, not quoted from a blog post."""
    return {
        "resources": get("/resources/count"),
        "services": get("/services/count"),
    }


def fetch_resource(rid):
    try:
        d = get(f"/resources/{rid}")
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        return None
    r = d.get("resource", d)
    return r if isinstance(r, dict) and "name" in r else None


def harvest(n=200, seed=7, workers=8):
    """Sample n resource ids. Cached to data/ so repeat runs don't hammer them."""
    DATA.mkdir(exist_ok=True)
    random.seed(seed)
    ids = random.sample(range(1, 2600), n)
    missing = [i for i in ids if not (DATA / f"{i}.json").exists()]

    with cf.ThreadPoolExecutor(workers) as ex:
        for rid, rec in zip(missing, ex.map(fetch_resource, missing)):
            if rec:
                (DATA / f"{rid}.json").write_text(json.dumps(rec))

    out = []
    for f in DATA.glob("*.json"):
        try:
            rec = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        r = rec.get("resource", rec)
        if isinstance(r, dict) and "name" in r:
            out.append(r)
    return out


if __name__ == "__main__":
    print(json.dumps(corpus_size()))
    recs = harvest(int(os.environ.get("N", 200)))
    print(f"harvested {len(recs)} records into {DATA}")
