"""Harvest listings from the live SF Service Guide v2 API.

Read-only. Public, unauthenticated endpoints. We never write to production.

Why v2: https://www.sfserviceguide.org/api/v2 is the sheltertech-go API that the
live website itself reads. The older Rails API at https://askdarcel.org/api serves
the same resource ids through a phone formatter that mangles US numbers - it reads
area codes as international dialling codes (510 -> +51 Peru, 209 -> +20 Egypt),
eats the area code and stores the remainder. Verified live on resource 2258:

    phone 4636   v1 "07779560" (PE)        v2 "(510) 777-9560" (US)
    phone 4639   "5106544000105" (None)    "(510) 654-4000 ext. 105" (US)

Auditing v1 means auditing a formatting bug, not the data users see. So: v2 only.

The work list comes from ShelterTech's own curation dataset rather than random id
sampling - it is the canonical list of listings they want looked at.
"""

import concurrent.futures as cf
import csv
import io
import json
import os
import pathlib
import urllib.error
import urllib.request

API = os.environ.get("SFSG_API", "https://www.sfserviceguide.org/api/v2")
# Separate from the old data/ directory on purpose: the v1 cache holds corrupted
# phone numbers and must never be able to contaminate a v2 run (and keeping both
# lets us diff old against new).
DATA = pathlib.Path(__file__).parent / "data_v2"
CURATION_CSV = DATA / "_content_curation_dataset.csv"
UA = {"User-Agent": "sfsg-watch/0.2 (Hack for Humanity SF; read-only)"}
WORKERS = 6  # be polite: their API serves the live site


def _get_bytes(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get(path, timeout=30):
    """GET a v2 JSON endpoint. Returns None on an empty body.

    v2 answers an unknown resource id with HTTP 200 and a zero-length body rather
    than a 404, so "empty" has to be treated as "not found" explicitly.
    """
    raw = _get_bytes(f"{API}{path}", timeout)
    if not raw.strip():
        return None
    return json.loads(raw)


def corpus_size():
    """The headline numbers. Verified live, not quoted from a blog post.

    /resources/count is served under v2; /services/count is not migrated to Go and
    400s there, so it is read from the same host's unversioned path. Both are plain
    integers, not JSON objects.
    """
    host = API.rsplit("/api", 1)[0]

    def count(url):
        try:
            return int(_get_bytes(url).strip())
        except (urllib.error.URLError, ValueError, TimeoutError, OSError):
            return None

    return {
        "resources": count(f"{API}/resources/count"),
        "services": count(f"{host}/api/services/count"),
    }


def curation_rows(refresh=False):
    """ShelterTech's content curation dataset, as a list of dicts.

    CSV, not JSON. Columns: service_id, service_name, resource_id, resource_name,
    resource_website, service_edit_url, service_updated_at. Cached to disk so a
    repeat run costs them nothing.
    """
    DATA.mkdir(exist_ok=True)
    if refresh or not CURATION_CSV.exists():
        raw = _get_bytes(f"{API}/datathon/content_curation_dataset", timeout=60)
        CURATION_CSV.write_bytes(raw)
    text = CURATION_CSV.read_text(encoding="utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def curation_index(rows=None):
    """resource_id -> {service_edit_url, services:[...]} for the work list.

    Note: despite the column name, service_edit_url is the *resource* edit page
    (/organizations/<resource_id>/edit) on every row in the dataset - it is the
    page a volunteer actually fixes the listing on.
    """
    idx = {}
    for row in rows if rows is not None else curation_rows():
        rid = (row.get("resource_id") or "").strip()
        if not rid.isdigit():
            continue
        e = idx.setdefault(int(rid), {"service_edit_url": None, "curated_services": []})
        if not e["service_edit_url"]:
            e["service_edit_url"] = (row.get("service_edit_url") or "").strip() or None
        name = (row.get("service_name") or "").strip()
        if name:
            e["curated_services"].append(name)
    return idx


def fetch_resource(rid):
    try:
        d = get(f"/resources/{rid}")
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        return None
    if not isinstance(d, dict):
        return None
    r = d.get("resource", d)
    return r if isinstance(r, dict) and "name" in r else None


def harvest(n=0, workers=WORKERS):
    """Fetch the curation dataset's resources. n<=0 means all of them.

    Cached to data_v2/ so repeat runs don't hammer them. Ids are taken in ascending
    order so a capped run is reproducible.
    """
    DATA.mkdir(exist_ok=True)
    index = curation_index()
    ids = sorted(index)
    if n and n > 0:
        ids = ids[:n]

    missing = [i for i in ids if not (DATA / f"{i}.json").exists()]
    with cf.ThreadPoolExecutor(workers) as ex:
        for rid, rec in zip(missing, ex.map(fetch_resource, missing)):
            if rec:
                (DATA / f"{rid}.json").write_text(json.dumps(rec))

    out = []
    for rid in ids:
        f = DATA / f"{rid}.json"
        if not f.exists():
            continue
        try:
            r = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        r = r.get("resource", r)
        if not (isinstance(r, dict) and "name" in r):
            continue
        # Carried alongside the API record, not merged into it: the edit page a
        # volunteer lands on, so an emitted change request can point straight at it.
        meta = index.get(rid) or {}
        if meta.get("service_edit_url"):
            r["service_edit_url"] = meta["service_edit_url"]
        if meta.get("curated_services"):
            r["curated_services"] = meta["curated_services"]
        out.append(r)
    return out


if __name__ == "__main__":
    print(json.dumps(corpus_size()))
    rows = curation_rows()
    print(f"curation dataset: {len(rows)} rows, "
          f"{len(curation_index(rows))} distinct resources")
    recs = harvest(int(os.environ.get("N", 0)))
    print(f"harvested {len(recs)} records into {DATA}")
