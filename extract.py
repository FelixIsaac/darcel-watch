"""Turn a whole listing into atomic, checkable claims - not just its phone rows.

WHY THIS EXISTS. audit.extract_claims templates three structured fields: phone,
address, status. That covers about a tenth of what a listing actually asserts.
The corpus is mostly prose:

    3,340  long_description      3,308  eligibilities
    2,913  application_process   1,483  fee
      855  required_documents      379  wait_time

None of it is reachable by a format string, because none of it is structured.
"Open to adults 18+ with proof of SF residency" cannot be pattern-matched into
a claim; it has to be read. So a model reads it, once, and the result is cached
against the record's own `updated_at`.

WHY CACHING BY updated_at IS THE WHOLE TRICK. Extraction is the one step here
that genuinely needs a generative model, and paying for it on every run would
make cost proportional to runs rather than to changes. Keyed on
(id, updated_at), a listing is extracted once and re-extracted only when
ShelterTech actually touch it. Across 813 listings that is roughly a dollar,
once, and near-zero thereafter.

WHAT IT MUST NOT DO. Extraction decides WHAT IS WORTH CHECKING. It never
decides whether anything is true - that is jev.py's job, against the
organisation's own website, with calibrated thresholds. Keeping those two apart
is what stops a model that is confidently wrong about the world from also being
the thing that grades itself.

A claim earns its place only if a page on the organisation's own website could
plausibly confirm or contradict it. "The service is compassionate" is not a
claim; "the service is open to adults 18 and over" is.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import re
import urllib.error
import urllib.request

CACHE = pathlib.Path(__file__).parent / "cache" / "claims"
MODEL = "google/gemini-2.5-flash"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

# Fields worth reading. `internal_note` is deliberately absent: it is
# ShelterTech's own working notes, not a public assertion, and nothing on an
# organisation's website could confirm or contradict it.
PROSE_FIELDS = ("long_description", "short_description", "eligibilities",
                "application_process", "required_documents", "fee",
                "wait_time", "interpretation_services", "notes")

# Which discovery hint a claim's field maps onto, for page ranking.
FIELD_GROUP = {
    "phone": "phone", "address": "address", "schedule": "schedule",
    "status": "status", "fee": "eligibility", "eligibility": "eligibility",
    "application": "eligibility", "documents": "eligibility",
    "service": "services", "language": "services", "wait": "services",
}

PROMPT = """You are preparing a civic services directory listing for fact-checking \
against the organisation's OWN website.

Extract every factual claim the listing makes that a page on that organisation's \
website could plausibly CONFIRM or CONTRADICT.

Rules:
- One fact per claim. Split compound statements.
- Write each claim as a complete, standalone sentence that names the subject. \
Write "The organisation ..." or "The service ...", never "it" or "they".
- Include the specific value: a number, an age, a document name, a language.
- SKIP anything unverifiable from a website: internal notes, subjective quality \
("compassionate", "welcoming"), and anything already obvious from the org's name.
- SKIP claims that no outside page could settle, such as how long a waiting list \
is on a given day.
- If the listing says nothing checkable, return an empty list.

Return ONLY JSON, no prose, in this exact shape:
{"claims": [{"field": "<one of: phone|address|schedule|status|fee|eligibility|\
application|documents|service|language|wait>", "text": "<the claim sentence>", \
"stored": "<the exact value or phrase from the listing this came from>"}]}"""


@dataclasses.dataclass
class Claim:
    key: str
    field: str        # discovery hint group: phone | eligibility | services | ...
    text: str         # the sentence the judge is asked about
    stored: str       # the raw value, shown to a human in the review queue
    source: str       # which listing field it came from, for provenance


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------

def _cache_key(record) -> str:
    """Identity of a listing VERSION. Re-extract only when the record changes.

    `updated_at` is ShelterTech's own statement that something about this
    record moved, which is exactly the trigger we want. The name hash is a
    guard for the rare record with no timestamp.
    """
    stamp = record.get("updated_at") or ""
    raw = f"{record.get('id')}:{stamp}:{record.get('name') or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _cached(record):
    p = CACHE / f"{_cache_key(record)}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _store(record, payload):
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / f"{_cache_key(record)}.json").write_text(json.dumps(payload, indent=1))


# --------------------------------------------------------------------------
# the listing, as the model sees it
# --------------------------------------------------------------------------

def _listing_text(record) -> str:
    """The listing rendered for reading. Structured fields are included too, so
    the model can phrase a claim about a phone number in the same voice as one
    about eligibility - the judge downstream sees only sentences."""
    parts = [f"ORGANISATION: {(record.get('name') or '').strip()}"]
    if record.get("website"):
        parts.append(f"WEBSITE: {record['website']}")

    for p in record.get("phones") or []:
        num = (p.get("number") or "").strip()
        if num:
            parts.append(f"PHONE ({p.get('service_type') or 'Voice'}): {num}")
    for a in record.get("addresses") or []:
        bits = [a.get("address_1"), a.get("city"), a.get("state_province"),
                a.get("postal_code")]
        line = ", ".join(str(b).strip() for b in bits if b and str(b).strip())
        if line:
            parts.append(f"ADDRESS: {line}")

    for f in PROSE_FIELDS:
        v = record.get(f)
        if isinstance(v, str) and v.strip():
            parts.append(f"{f.upper()}: {v.strip()[:1500]}")

    for s in (record.get("services") or [])[:6]:
        name = (s.get("name") or "").strip()
        parts.append(f"\nSERVICE: {name}")
        elig = s.get("eligibilities")
        if isinstance(elig, list) and elig:
            names = [e.get("name") for e in elig if isinstance(e, dict) and e.get("name")]
            if names:
                parts.append(f"  ELIGIBILITY: {', '.join(names)}")
        for f in PROSE_FIELDS:
            v = s.get(f)
            if isinstance(v, str) and v.strip():
                parts.append(f"  {f.upper()}: {v.strip()[:1200]}")
    return "\n".join(parts)[:20000]


# --------------------------------------------------------------------------
# the model call
# --------------------------------------------------------------------------

def _call(listing: str, api_key: str, timeout: int = 60) -> dict | None:
    body = {
        "model": MODEL,
        "temperature": 0,
        "messages": [{"role": "system", "content": PROMPT},
                     {"role": "user", "content": listing}],
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None
    try:
        content = out["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return None


def _sanitise(payload, record) -> list[dict]:
    """Defend against a model that returns the wrong shape.

    An extraction step feeds every later stage, so a malformed claim here is a
    malformed accusation later. Anything that is not a well-formed claim about
    a plausible field is dropped rather than repaired.
    """
    out, seen = [], set()
    for c in (payload or {}).get("claims") or []:
        if not isinstance(c, dict):
            continue
        text = (c.get("text") or "").strip()
        field = (c.get("field") or "").strip().lower()
        stored = (c.get("stored") or "").strip()
        if len(text) < 12 or field not in FIELD_GROUP:
            continue
        # A claim that does not name its subject cannot be judged on a page
        # that discusses several programmes.
        if not re.match(r"^(the|this)\b", text, re.I):
            continue
        k = text.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append({"field": field, "text": text[:400], "stored": stored[:200]})
    return out[:40]


def extract(record, api_key: str | None = None, use_cache: bool = True) -> list[Claim]:
    """Every checkable claim in one listing. Cached against its `updated_at`.

    Returns [] rather than raising when no key is set or the model is
    unreachable - callers fall back to the deterministic templates in audit.py,
    which still cover phone, address and status.
    """
    if use_cache:
        hit = _cached(record)
        if hit is not None:
            return _to_claims(hit.get("claims") or [], record)

    api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return []

    payload = _call(_listing_text(record), api_key)
    claims = _sanitise(payload, record)
    if payload is not None:
        _store(record, {"id": record.get("id"),
                        "updated_at": record.get("updated_at"),
                        "claims": claims})
    return _to_claims(claims, record)


def _to_claims(raw, record) -> list[Claim]:
    return [
        Claim(key=f"c{i}", field=FIELD_GROUP.get(c["field"], "services"),
              text=c["text"], stored=c.get("stored") or "", source=c["field"])
        for i, c in enumerate(raw)
    ]


def cache_stats() -> dict:
    files = list(CACHE.glob("*.json")) if CACHE.exists() else []
    return {"cached_listings": len(files)}


if __name__ == "__main__":
    import sys

    import audit
    import envfile
    envfile.load()

    rid = int(sys.argv[1]) if len(sys.argv) > 1 else 2399
    recs = audit._load(rid)
    if not recs:
        print(f"no cached record {rid}")
        raise SystemExit(1)
    rec = recs[0]
    claims = extract(rec)
    print(f"{(rec.get('name') or '').strip()}  ->  {len(claims)} claims\n")
    for c in claims:
        print(f"  [{c.source:12}] {c.text}")
        if c.stored:
            print(f"   {'':14} from: {c.stored[:70]}")
    print(f"\n  templates would have produced: "
          f"{len(audit.extract_claims(rec))}")
