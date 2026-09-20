"""Check one organisation's listing against the organisation's own website.

The loop, end to end:

    record  ->  claims       deterministic templates over structured fields
            ->  inventory    discover.py: robots.txt -> sitemap -> lastmod
            ->  shortlist    rank pages per field, no model involved
            ->  fetch        top-k only, budgeted
            ->  judge        jev.py: support AND contradict, per claim per page
            ->  reconcile    best evidence across pages wins
            ->  decide       supported / contradicted / absent / uncertain

Three decisions in here are load-bearing, and each one exists because an
earlier version got it wrong.

1. CLAIMS ARE TEMPLATED, NOT GENERATED. Phones, addresses and schedules are
   already structured. Turning `{"number": "510-808-7410"}` into "the
   organisation can be reached on 510-808-7410" needs a format string, not a
   language model. Generating them would add cost, latency, and a fresh way to
   be wrong about data we can already read exactly. Only free prose
   (eligibility, application process) needs a model, and that is a separate,
   cached step.

2. ABSENCE IS NOT CONTRADICTION. Every claim gets two independent questions:
   does the page SUPPORT this, and does the page CONTRADICT this. A page that
   never mentions a phone number scores low on both - that is ABSENT and it is
   not a finding. Only an active contradiction can reach a human. This is the
   single rule that would have prevented most of what we have retracted.

3. NOTHING HERE PUBLISHES. Output is a candidate for review. change_request
   payloads are written to disk and never POSTed. Every request this module
   makes to a third party is a GET; the only POST is to the judgment model.

Run it:  .venv/bin/python audit.py 2399
"""

from __future__ import annotations

import concurrent.futures as cf
import dataclasses
import json

import discover
import fetcher
import jev

UA = discover.UA
MAX_PAGES_PER_ORG = 4          # fetch budget, per organisation
PAGE_CHARS = 60_000            # per page, well inside Jev's context window


# --------------------------------------------------------------------------
# 1. claims
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Claim:
    key: str
    field: str        # drives page ranking: phone | address | schedule | ...
    text: str         # the sentence Jev is asked about
    stored: str       # the raw value, for the review queue


def _phone_claims(record) -> list[Claim]:
    out = []
    for i, p in enumerate(record.get("phones") or []):
        num = (p.get("number") or "").strip()
        if not num:
            continue
        kind = (p.get("service_type") or "").strip()
        label = f" ({kind})" if kind and kind.lower() != "voice" else ""
        out.append(Claim(
            key=f"phone{i}", field="phone", stored=num,
            text=f"The organisation can be reached on the telephone number {num}{label}.",
        ))
    return out


def _address_claims(record) -> list[Claim]:
    out = []
    for i, a in enumerate(record.get("addresses") or []):
        parts = [a.get("address_1"), a.get("city"), a.get("state_province"), a.get("postal_code")]
        stored = ", ".join(str(p).strip() for p in parts if p and str(p).strip())
        if not stored:
            continue
        out.append(Claim(
            key=f"addr{i}", field="address", stored=stored,
            text=f"The organisation has a location at {stored}.",
        ))
    return out


def _status_claim(record) -> list[Claim]:
    return [Claim(
        key="status", field="status", stored="approved/open",
        text="The organisation is currently operating and has not closed or permanently shut down.",
    )]


def extract_claims(record, include_prose: bool = False) -> list[Claim]:
    """Testable statements from one listing. Deterministic - no model call.

    `include_prose` is a placeholder for the eligibility/application-process
    path, which DOES need generation and is therefore cached separately. It is
    off until that cache exists; shipping it half-built would mean paying model
    cost on every run to produce claims we cannot yet reuse.
    """
    claims = _phone_claims(record) + _address_claims(record) + _status_claim(record)
    return claims


# --------------------------------------------------------------------------
# 2. fetch
# --------------------------------------------------------------------------

def page_text(url: str) -> tuple[str, str | None]:
    """Page text plus HOW we got it: "direct", "reader", or None on failure.

    Delegates to fetcher.fetch, which tries a plain GET first and falls back to
    a rendering reader when what comes back is a JavaScript shell. A first
    version of this module re-implemented the plain GET and nothing else, and
    it went blind on 4 of 12 sampled organisations - a third of the sample
    silently unauditable because their sites render client-side.

    The method is carried through to the output rather than dropped, because a
    verdict that rests on a third party's rendering of a page should say so.
    """
    text, method = fetcher.fetch(url)
    if not text or method is None:
        return "", None
    return text[:PAGE_CHARS], method


def select_pages(inv: discover.Inventory, claims: list[Claim],
                 budget: int = MAX_PAGES_PER_ORG) -> list[tuple[str, str | None]]:
    """Which pages to spend the fetch budget on.

    Union of the top-ranked pages for each field present in the claim set, so a
    listing with phones and an address looks at contact-shaped AND
    location-shaped pages rather than four near-identical ones. The homepage is
    always included: on small nonprofit sites it is often the only real page.
    """
    fields = list(dict.fromkeys(c.field for c in claims))
    picked: dict[str, str | None] = {}
    if inv.origin:
        picked[inv.origin.rstrip("/") + "/"] = None
    for f in fields:
        for url, lm in inv.top(f, limit=3):
            picked.setdefault(url, lm)
            if len(picked) >= budget:
                break
        if len(picked) >= budget:
            break
    return list(picked.items())[:budget]


# --------------------------------------------------------------------------
# 3. reconcile
# --------------------------------------------------------------------------

def reconcile(per_page: list[dict[str, jev.Verdict]]) -> dict[str, jev.Verdict]:
    """Best evidence across pages wins, with support beating contradiction.

    The asymmetry is deliberate and it is the lesson of Building Futures: its
    stored number is absent from the homepage and from /services/domestic-
    violence/, and present on /get-help/. Two pages were silent, one agreed.
    The number is correct. A rule that let two silences outvote one agreement
    would have filed a false finding - so ANY page supporting a claim settles
    it, and a contradiction only stands when nothing else supports it.
    """
    best: dict[str, jev.Verdict] = {}
    for page in per_page:
        for key, v in page.items():
            cur = best.get(key)
            if cur is None:
                best[key] = v
                continue
            rank = {"supported": 3, "contradicted": 2, "uncertain": 1, "absent": 0}
            if rank[v.label] > rank[cur.label]:
                best[key] = v
            elif v.label == cur.label == "supported" and v.support > cur.support:
                best[key] = v
    return best


# --------------------------------------------------------------------------
# 4. the audit
# --------------------------------------------------------------------------

@dataclasses.dataclass
class OrgAudit:
    resource_id: int
    name: str
    website: str
    inventory: dict
    pages_fetched: list[dict]
    verdicts: dict
    usage: jev.Usage
    note: str = ""

    @property
    def findings(self) -> list[dict]:
        """Only active contradictions. Silence is never a finding."""
        return [v for v in self.verdicts.values() if v["label"] == "contradicted"]

    @property
    def confirmations(self) -> list[dict]:
        return [v for v in self.verdicts.values() if v["label"] == "supported"]

    def as_dict(self) -> dict:
        return {
            "resource_id": self.resource_id,
            "name": self.name,
            "website": self.website,
            "listing_url": f"https://www.sfserviceguide.org/organizations/{self.resource_id}",
            "inventory": self.inventory,
            "pages_fetched": self.pages_fetched,
            "verdicts": self.verdicts,
            "findings": self.findings,
            "confirmations": len(self.confirmations),
            "note": self.note,
            "cost_usd": round(self.usage.usd, 6),
            "ms": self.usage.ms,
        }


def audit_org(record) -> OrgAudit:
    """One listing, end to end. Never raises; abstains instead."""
    rid = record.get("id")
    name = (record.get("name") or "").strip()
    website = (record.get("website") or "").strip()
    usage = jev.Usage()

    def empty(note):
        return OrgAudit(rid, name, website, {}, [], {}, usage, note)

    if not website:
        return empty("no website on the listing - nothing to check against")
    if not jev.available():
        return empty("JEV_API_KEY not set - abstaining rather than guessing")

    claims = extract_claims(record)
    if not claims:
        return empty("no checkable structured claims on this listing")

    inv = discover.discover(website)
    if inv.method == "robots-denied":
        return empty("robots.txt disallows crawling this site - honoured, abstaining")
    if not len(inv):
        return empty("could not discover any pages on the organisation's site")

    targets = select_pages(inv, claims)
    claim_map = {c.key: c.text for c in claims}

    per_page: list[dict[str, jev.Verdict]] = []
    fetched: list[dict] = []
    for url, lastmod in targets:
        text, method = page_text(url)
        if len(text) < 200:          # a shell, an error page, or a dead host
            continue
        fetched.append({"url": url, "via": method, "lastmod": lastmod})
        try:
            verdicts, u = jev.judge_page(text, claim_map, url=url, lastmod=lastmod)
        except jev.JevUnavailable as e:
            return empty(f"judgment model unreachable ({e}) - abstaining")
        except jev.JevError as e:
            return empty(f"judgment model error ({e}) - abstaining")
        usage = usage.add(u)
        per_page.append(verdicts)

    if not per_page:
        return empty("no readable pages - site may be JavaScript-rendered")

    best = reconcile(per_page)
    by_key = {c.key: c for c in claims}
    out = {}
    for key, v in best.items():
        c = by_key[key]
        out[key] = {
            "field": c.field,
            "stored": c.stored,
            "claim": c.text,
            "label": v.label,
            "support": round(v.support, 3),
            "contradict": round(v.contradict, 3),
            "evidence_url": v.url,
            "source_lastmod": v.lastmod,
        }
    return OrgAudit(rid, name, website, inv.as_dict(), fetched, out, usage)


def audit_many(records, workers: int = 4) -> list[OrgAudit]:
    """Concurrency is across ORGANISATIONS, never within one - a single
    nonprofit's web server should never see parallel requests from us."""
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(audit_org, records))


def _load(resource_id=None):
    import glob
    out = []
    for f in glob.glob("data_v2/*.json"):
        d = json.load(open(f))
        for r in (d if isinstance(d, list) else [d]):
            if not isinstance(r, dict) or "id" not in r:
                continue
            if resource_id is None or r["id"] == resource_id:
                out.append(r)
    return out


if __name__ == "__main__":
    import sys

    import envfile
    envfile.load()

    rid = int(sys.argv[1]) if len(sys.argv) > 1 else 2399
    recs = _load(rid)
    if not recs:
        print(f"no cached record {rid} - run harvest.py first")
        raise SystemExit(1)

    a = audit_org(recs[0])
    print(f"{a.name}  ({a.website})")
    print(f"  discovery : {a.inventory.get('method')} "
          f"{a.inventory.get('pages')} pages, "
          f"newest lastmod {a.inventory.get('newest_lastmod')}")
    print(f"  fetched   : {len(a.pages_fetched)}")
    for p in a.pages_fetched:
        print(f"              [{p['via']:6}] {p['lastmod'] or '    -     '}  {p['url']}")
    if a.note:
        print(f"  note      : {a.note}")
    print()
    for k, v in sorted(a.verdicts.items(), key=lambda kv: kv[1]["label"]):
        print(f"  {v['label']:13} sup={v['support']:.2f} con={v['contradict']:.2f}  "
              f"{v['field']:8} {v['stored'][:44]}")
        if v["label"] in ("supported", "contradicted"):
            print(f"                 via {v['evidence_url']}")
    print(f"\n  ${a.usage.usd:.6f}, {a.usage.ms}ms, {a.usage.calls} judgment calls")
