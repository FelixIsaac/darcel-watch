"""Check one organisation's listing against the organisation's own website.

The loop, end to end:

    record  ->  claims       extract.py reads the WHOLE listing (cached), plus
                             deterministic templates as an unremovable floor
            ->  inventory    discover.py: robots.txt -> sitemap -> lastmod
            ->  shortlist    rank pages per field
            ->  fetch        top-k only, budgeted
            ->  judge        jev.py: one three-way Choice per claim per page
            ->  reconcile    best evidence across pages wins
            ->  decide       supported / contradicted / absent / uncertain

Three decisions in here are load-bearing, and each one exists because an
earlier version got it wrong.

1. CLAIMS ARE READ, NOT PATTERN-MATCHED. An earlier version templated three
   structured fields and stopped there, which reached about a tenth of what a
   listing asserts - the corpus is mostly prose. On Building Futures that
   produced 2 claims where reading the listing produces 19, and it missed the
   organisation's 24-hour crisis line entirely, because that number lives in a
   description paragraph and never reaches the structured phone data.

   Templates remain as a floor that cannot be removed: they are free,
   deterministic, and a model having a bad day must not be able to silently
   shrink our coverage.

2. ABSENCE IS NOT CONTRADICTION. Each claim gets ONE three-way Choice -
   supports / contradicts / says_nothing - so silence is an option the model
   SELECTS rather than a state inferred in code from two low probabilities.
   Only an active contradiction can reach a human. This is the single rule that
   would have prevented most of what this project has retracted, and the form
   was chosen by measurement: see experiment.py.

3. NOTHING HERE PUBLISHES. Output is a candidate for review. change_request
   payloads are written to disk and never POSTed. Every request this module
   makes to a third party is a GET; the only POSTs are to the two models.

Run it:  .venv/bin/python audit.py 2399
"""

from __future__ import annotations

import concurrent.futures as cf
import dataclasses
import json

import discover
import extract
import fetcher
import jev

UA = discover.UA
MAX_PAGES_PER_ORG = 5          # fetch budget, per organisation
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


def template_claims(record) -> list[Claim]:
    """Structured fields only, by format string. No model, never fails.

    The floor, not the ceiling: phone, address and status are already
    structured, so turning them into sentences needs a template, not a
    language model. This is what runs when extraction is unavailable.
    """
    return _phone_claims(record) + _address_claims(record) + _status_claim(record)


def extract_claims(record, use_model: bool = True) -> list[Claim]:
    """Everything in the listing worth checking, not just the structured rows.

    Templates reach three fields. The corpus is mostly prose - 3,340
    long_descriptions, 3,308 eligibilities, 2,913 application processes - and
    none of it is reachable by a format string. extract.py reads the whole
    listing with a model, once per record version, cached against the record's
    own `updated_at`.

    The difference is not marginal. On Building Futures, templates produce 2
    claims; extraction produces 19, including the organisation's 24-hour crisis
    line 1-866-292-9688 - which appears nowhere in the structured phone data
    and so was invisible to every check this project had before.

    Falls back to templates when no key is set or the model is unreachable, so
    the pipeline degrades rather than stopping. Templated claims are always
    included: they are free, deterministic, and a model that drops one should
    not be able to silently shrink our coverage.
    """
    base = template_claims(record)
    if not use_model:
        return base
    try:
        extracted = extract.extract(record)
    except Exception:
        return base
    if not extracted:
        return base

    seen = {c.text.lower() for c in base}
    for e in extracted:
        if e.text.lower() in seen:
            continue
        seen.add(e.text.lower())
        base.append(Claim(key=f"x{e.key}", field=e.field, text=e.text,
                          stored=e.stored or e.text))
    return base


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


SHORTLIST = 40          # candidates handed to the judge, per field group


def select_pages(inv: discover.Inventory, claims: list[Claim],
                 budget: int = MAX_PAGES_PER_ORG,
                 use_model: bool = True) -> list[tuple[str, str | None]]:
    """Which pages to spend the fetch budget on.

    Two stages, and the split matters:

      1. SHORTLIST, deterministic. Slug-keyword scoring in discover.rank_pages
         narrows a 265-page sitemap to ~40 candidates. This is a cheap
         prefilter, not a decision - the same role BM25 plays in TypeSafe's
         re-ranking cookbook, where a fast lexical pass cuts 3,565 passages to
         30 before the model ever looks.

      2. SELECT, by model. Jev picks from those slugs. Keyword scoring is a
         heuristic about what a URL probably means, and heuristics about
         meaning are exactly what a judgment model is for. It cannot invent a
         URL: every option is one discovery.py actually found.

    Leaving stage 2 as keyword matching would have repeated, one layer up, the
    mistake that SUBPAGES made - guessing at meaning instead of asking.

    The homepage is always included: on small nonprofit sites it is often the
    only real page, and it costs one slot to never be wrong about that.

    Falls back to pure stage 1 when no key is set, so the pipeline still runs.
    """
    fields = list(dict.fromkeys(c.field for c in claims))
    picked: dict[str, str | None] = {}
    if inv.origin:
        picked[inv.origin.rstrip("/") + "/"] = None

    lastmod = dict(inv.pages)

    if use_model and jev.available() and len(inv) > budget:
        shortlists = {f: inv.top(f, limit=SHORTLIST) for f in fields}
        qs = {}
        for f, cands in shortlists.items():
            if len(cands) < 2:
                continue
            example = next((c.text for c in claims if c.field == f), f)
            qs[f"pick__{f}"] = jev.best_page(example, [u for u, _ in cands])
        if qs:
            try:
                # One request, one question per field group - questions against
                # the same state run in parallel and cost only their own tokens.
                answers, _ = jev.ask(
                    json.dumps({"organisation": inv.website,
                                "note": "Choose which page is most likely to "
                                        "answer the claim."}), qs)
                for f, cands in shortlists.items():
                    pick, conf = jev.choice(answers, f"pick__{f}")
                    if not pick or pick == "none" or not pick.startswith("u"):
                        continue
                    try:
                        url = cands[int(pick[1:])][0]
                    except (ValueError, IndexError):
                        continue
                    picked.setdefault(url, lastmod.get(url))
            except jev.JevError:
                pass              # fall through to the deterministic ordering

    # Top up from the deterministic ranking, so the budget is always spent.
    for f in fields:
        for url, lm in inv.top(f, limit=3):
            if len(picked) >= budget:
                break
            picked.setdefault(url, lm)
        if len(picked) >= budget:
            break
    return list(picked.items())[:budget]


# --------------------------------------------------------------------------
# 3. identity - does this website even belong to this listing?
# --------------------------------------------------------------------------

_DIGITS_RE = __import__("re").compile(r"\D")


def _digits(s: str) -> str:
    return _DIGITS_RE.sub("", s or "")


def identity_anchors(record) -> list[tuple[str, str]]:
    """Facts that would prove a page belongs to this listing.

    Address and phone only - deliberately NOT the organisation's name. Plenty
    of legitimate nonprofits run a domain unrelated to what they are called,
    and name-to-domain similarity would flag every one of them. A street, a ZIP
    or a working number is a fact about the body; a domain name is branding.
    """
    out: list[tuple[str, str]] = []
    for p in record.get("phones") or []:
        d = _digits(p.get("number"))
        if len(d) >= 10:
            out.append(("phone", d[-10:]))
    for a in record.get("addresses") or []:
        z = (a.get("postal_code") or "").strip()
        if len(z) >= 5:
            out.append(("postal_code", z[:5]))
        head = " ".join((a.get("address_1") or "").strip().split()[:2])
        if len(head) >= 6:
            out.append(("street", head))
        city = (a.get("city") or "").strip()
        if len(city) >= 4:
            out.append(("city", city))
    return out


def page_belongs(record, pages: dict[str, str]) -> tuple[bool, str | None]:
    """Does ANY fetched page carry a fact from this listing? -> (ok, evidence)

    THE PRECONDITION THIS PROJECT LEARNED THE HARD WAY.

    Every claim-level verdict rests on an unexamined assumption: that the
    `website` field actually points at this organisation. When it does not,
    every claim is judged against a stranger's site, everything looks
    contradicted, and the tool produces a confident, specific, WRONG accusation.

    That is not hypothetical. Listing 2035, "Getting Out & Staying Out", stores
    San Francisco addresses (1485 Bayshore Blvd) and San Francisco phone
    numbers (415-489-7300), and a website of gosonyc.org - which is GOSO, an
    East Harlem organisation in New York with a similar name and no San
    Francisco presence at all. The pipeline dutifully reported the SF phone
    number as contradicted, because a New York page does indeed list different
    numbers. The phone is probably fine. The WEBSITE is wrong.

    So site identity is a precondition, not a claim. If no anchor appears
    anywhere, we say so about the website and abstain on everything else,
    because the only honest reading is "we cannot see this organisation from
    here".

    Note the asymmetry that keeps this safe: finding one anchor is enough to
    proceed, and finding none never asserts that a value is wrong - it only
    withdraws our standing to judge.
    """
    anchors = identity_anchors(record)
    if not anchors:
        return True, None          # nothing to check with; proceed as before

    # Strength order matters. A phone number or a street line is a fact about
    # THIS body. A city is not - every nonprofit page in this corpus says "San
    # Francisco", and accepting that as proof of identity would wave through
    # exactly the case this function exists to catch. City and postal code are
    # kept only as last-resort anchors, and the kind is reported so a reviewer
    # can see how thin the evidence was.
    strength = {"phone": 0, "street": 1, "postal_code": 2, "city": 3}
    best: tuple[int, str] | None = None
    for url, text in pages.items():
        low = text.lower()
        digits = _digits(text)
        for kind, value in sorted(anchors, key=lambda a: strength[a[0]]):
            hit = (value in digits) if kind == "phone" else (value.lower() in low)
            if not hit:
                continue
            rank = strength[kind]
            if best is None or rank < best[0]:
                best = (rank, f"{kind} {value!r} on {url}")
            if rank == 0:
                return True, best[1]      # a matching phone settles it
    if best is None:
        return False, None
    return True, best[1]


# --------------------------------------------------------------------------
# 4. reconcile
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
    website_suspect: bool = False

    @property
    def findings(self) -> list[dict]:
        """Only active contradictions, deduplicated. Silence is never a finding.

        Deduplication matters: extraction and the templates both produce a
        claim about the same stored phone number, so an undeduplicated count
        reported 6 findings where there were 3 distinct problems. A review
        queue that double-counts trains people to distrust it.
        """
        seen, out = set(), []
        for v in self.verdicts.values():
            if v["label"] != "contradicted":
                continue
            k = (v["field"], (v.get("stored") or "").strip().lower())
            if k in seen:
                continue
            seen.add(k)
            out.append(v)
        return out

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
            "website_suspect": self.website_suspect,
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
    texts: dict[str, str] = {}
    for url, lastmod in targets:
        text, method = page_text(url)
        if len(text) < 200:          # a shell, an error page, or a dead host
            continue
        texts[url] = text
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

    # PRECONDITION, checked before any verdict is allowed out. If nothing on
    # the site matches anything in the listing, the website field is the
    # suspect - not the phone number, not the address. Report that and stop.
    ok, anchor = page_belongs(record, texts)
    if not ok:
        a = OrgAudit(rid, name, website, inv.as_dict(), fetched, {}, usage,
                     "no fact from this listing appears anywhere on the linked "
                     "site - the WEBSITE is the likely error, so every other "
                     "claim is withheld")
        a.website_suspect = True
        return a

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
