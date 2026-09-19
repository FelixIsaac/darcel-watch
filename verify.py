"""Re-verify SF Service Guide (AskDarcel) listings against the org's live website.

Read-only, agentic. Deterministic evidence gathering + an explicit "where do I
look next" decision loop, with an optional Gemini adjudication layer. We never
write to AskDarcel - change_request payloads are emitted, never POSTed.
"""

import concurrent.futures as cf
import html
import json
import os
import pathlib
import re
import urllib.error
import urllib.request

DATA = pathlib.Path(__file__).parent / "data"
UA = {"User-Agent": "darcel-watch/0.1 (Hack for Humanity SF; read-only)"}
FETCH_TIMEOUT = 12
MAX_FETCHES = 3
# Sub-pages the agent tries, in order, when the homepage doesn't settle a question.
SUBPAGES = ["/contact", "/about", "/hours", "/visit", "/locations"]

CLOSURE_RE = re.compile(
    r"permanently closed|we('| ha)?ve moved|no longer offering|temporarily closed|"
    r"has closed|out of business|relocated",
    re.I,
)
PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
STRIP_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

# Adjudication runs on Gemini 2.5 Flash. Two transports, same model:
#   - OpenRouter (default when the key looks like sk-or-...), OpenAI-shaped API
#   - Google AI Studio direct, when given a Google key
# Set DW_MODEL to override the model slug on either transport.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = os.environ.get("DW_MODEL", "google/gemini-2.5-flash")

GOOGLE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)
GOOGLE_MODEL = os.environ.get("DW_MODEL", "gemini-2.5-flash")


def _transport(api_key):
    """OpenRouter keys are sk-or-...; anything else is treated as a Google key."""
    return "openrouter" if (api_key or "").startswith("sk-or-") else "google"


def _post_json(url, body, headers, timeout=20):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def call_model(prompt, api_key):
    """Returns the model's raw text, or None on any failure.

    Never raises. A dead key, a quota wall or a flaky venue hotspot must
    degrade to the deterministic path, not take down the run.
    """
    try:
        if _transport(api_key) == "openrouter":
            resp = _post_json(
                OPENROUTER_URL,
                {
                    "model": OPENROUTER_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                },
                {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/FelixIsaac/darcel-watch",
                    "X-Title": "Darcel Watch",
                },
            )
            return resp["choices"][0]["message"]["content"]

        resp = _post_json(
            GOOGLE_URL.format(model=GOOGLE_MODEL, key=api_key),
            {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0,
                },
            },
            {"Content-Type": "application/json"},
        )
        return resp["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return None


def norm_phone(p):
    """Last 10 digits - area code + number, ignores formatting/country code."""
    d = re.sub(r"\D+", "", p or "")
    return d[-10:] if len(d) >= 10 else None


def text_from_html(raw):
    """Strip scripts/styles/tags to plain text. Crude but stdlib-only."""
    no_script = TAG_RE.sub(" ", raw)
    no_tags = STRIP_RE.sub(" ", no_script)
    return WS_RE.sub(" ", html.unescape(no_tags)).strip()


def fetch(url):
    """One GET. Tolerates non-200 (some sites 403 bots but still serve a body)."""
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            body = r.read(500_000)  # cap - we're reading for evidence, not archiving
            return body.decode(r.headers.get_content_charset() or "utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def gather_evidence(website):
    """The agentic loop: fetch, check if evidence is enough, else pick the next
    likely sub-page and retry - up to MAX_FETCHES total. Returns (texts, urls).

    This is the decision step the demo hinges on: a plain scraper would just
    fetch the homepage once. Here the agent inspects what it got back and
    *chooses* where to look next based on what's still missing.
    """
    base = website.rstrip("/")
    texts = {}
    fetched = []
    queue = [base] + [base + p for p in SUBPAGES]

    for url in queue:
        if len(fetched) >= MAX_FETCHES:
            break
        raw = fetch(url)
        fetched.append(url)
        if raw is None:
            continue
        t = text_from_html(raw)
        if len(t) < 50:
            continue  # likely a JS-only shell - not usable evidence
        texts[url] = t

        # Decide: do we already have enough to adjudicate, or keep looking?
        have_phone = any(PHONE_RE.search(v) for v in texts.values())
        have_closure = any(CLOSURE_RE.search(v) for v in texts.values())
        have_hours = any(re.search(r"\b(hours|mon|monday|open)\b", v, re.I) for v in texts.values())
        if have_closure or (have_phone and have_hours):
            break  # enough signal - stop spending fetches

    return texts, fetched


def check_phone(record, texts):
    stored = None
    for p in record.get("phones") or []:
        n = norm_phone(p.get("number"))
        if n:
            stored = p.get("number")
            stored_norm = n
            break
    else:
        return None

    for url, t in texts.items():
        for m in PHONE_RE.finditer(t):
            live_norm = norm_phone(m.group())
            if live_norm and live_norm == stored_norm:
                return {"field": "phone", "stored": stored, "live": m.group(),
                         "evidence_url": url, "evidence_quote": m.group(), "match": True}
    # No match found anywhere we looked - report the first live phone we DID see, if any
    for url, t in texts.items():
        m = PHONE_RE.search(t)
        if m:
            return {"field": "phone", "stored": stored, "live": m.group(),
                     "evidence_url": url, "evidence_quote": snippet(t, m), "match": False}
    return None


def check_closure(texts):
    for url, t in texts.items():
        m = CLOSURE_RE.search(t)
        if m:
            return {"field": "operating_status", "stored": "open (approved)", "live": m.group(),
                     "evidence_url": url, "evidence_quote": snippet(t, m), "match": False}
    return None


def check_address(record, texts):
    addr = (record.get("addresses") or [None])[0]
    if not addr:
        return None
    stored = " ".join(str(addr.get(k) or "") for k in ("address_1", "city", "postal_code")).strip()
    if not stored:
        return None
    zipcode = addr.get("postal_code")
    for url, t in texts.items():
        if zipcode and zipcode in t:
            return {"field": "address", "stored": stored, "live": stored,
                     "evidence_url": url, "evidence_quote": snippet(t, re.search(re.escape(zipcode), t)),
                     "match": True}
    return None


def snippet(text, match, pad=60):
    if not match:
        return ""
    i, j = match.span()
    return text[max(0, i - pad):j + pad].strip()


def adjudicate_deterministic(record, findings, texts, fetched):
    """No-Gemini fallback. Conservative: only claim discrepancy on a positive
    contradiction; anything thin abstains rather than guessing."""
    closure = next((f for f in findings if f["field"] == "operating_status"), None)
    if closure:
        return "discrepancy", f"Live site language suggests closure/relocation: \"{closure['evidence_quote']}\"", 0.7

    phone = next((f for f in findings if f["field"] == "phone"), None)
    mismatches = [f for f in findings if f.get("match") is False]
    if phone and phone["match"] is False:
        return ("discrepancy",
                f"Stored phone {phone['stored']} not found on site; live number {phone['live']} shown instead.",
                0.55)

    if any(f.get("match") for f in findings):
        return "match", "Live site corroborates stored details.", 0.6

    return "abstain", "No clear evidence either way on the pages fetched.", 0.3


def adjudicate_gemini(record, findings, texts, api_key):
    """Deterministic regex gathers candidates; Gemini judges whether a quote
    actually contradicts the stored value, and writes the human-readable reason.
    Falls back to deterministic on any failure - never crashes, never hangs."""
    if not findings:
        return None
    evidence_lines = "\n".join(
        f"- field={f['field']} stored={f['stored']!r} live_candidate={f['live']!r} "
        f"quote={f['evidence_quote']!r} url={f['evidence_url']}"
        for f in findings
    )
    prompt = (
        "You are auditing a nonprofit service directory listing against its live "
        "website. A volunteer will act on your answer, and a wrong 'discrepancy' "
        "wastes their time or, worse, replaces a working phone number with a "
        "useless one. Be conservative.\n\n"
        "The evidence below was gathered by a regex, which is dumb: it reports ANY "
        "phone number it finds anywhere on the page. Your job is to judge whether "
        "the stored value is actually WRONG.\n\n"
        "Answer 'discrepancy' ONLY if the live value plainly replaces the stored "
        "one for the same purpose - i.e. it is presented as this organisation's "
        "main public contact, and the stored value is absent or contradicted.\n"
        "Answer 'abstain' if the quote's context suggests the live number serves a "
        "DIFFERENT purpose than the stored one - a careers or hiring line, fax, "
        "donations, press, a specific department or clinic, a partner organisation, "
        "or a second location. A page can legitimately list many numbers; that alone "
        "is NOT a discrepancy.\n"
        "Also abstain on: vanity numbers (e.g. '555-CARE' spelling out digits), "
        "partial matches, extensions, JS-shell text, or anything ambiguous.\n"
        "Answer 'match' if the evidence confirms the stored value.\n\n"
        "Calibrate confidence honestly: reserve values above 0.9 for cases where "
        "the page explicitly supersedes the stored value. Unsure means abstain.\n\n"
        'Answer STRICT JSON only: {"verdict": "discrepancy"|"match"|"abstain", '
        '"reason": "<one short sentence>", "confidence": <0.0-1.0>}\n\n'
        f"Org name: {record.get('name')}\n"
        f"Stored phone(s): {[p.get('number') for p in record.get('phones') or []]}\n"
        f"Stored address: {record.get('addresses')}\n\n"
        f"Evidence found on live site:\n{evidence_lines}"
    )
    text = call_model(prompt, api_key)
    if not text:
        return None
    try:
        parsed = json.loads(text)
        v = parsed.get("verdict")
        if v not in ("discrepancy", "match", "abstain"):
            return None
        return v, str(parsed.get("reason", "")), float(parsed.get("confidence", 0.5))
    except Exception:
        return None  # malformed JSON -> caller falls back to deterministic


def build_change_request(record, findings, verdict):
    if verdict != "discrepancy":
        return None
    bad = next((f for f in findings if f.get("match") is False), findings[0] if findings else None)
    if not bad:
        return None
    return {
        "resource_id": record.get("id"),
        "field": bad["field"],
        "current": bad["stored"],
        "proposed": bad["live"],
        "source_url": bad["evidence_url"],
        "source_quote": bad["evidence_quote"],
        "submitted_by": "darcel-watch (agent, human review required)",
    }
    # NOTE: this is emitted only. We never POST to /resources/:id/change_requests -
    # a human reviews and submits it themselves.


def verify(record, api_key=None):
    """Re-verify one AskDarcel record against its live website. Never raises."""
    rid = record.get("id")
    name = record.get("name", "")
    website = record.get("website")

    if not website:
        return {
            "resource_id": rid, "name": name, "verdict": "abstain",
            "reason": "no website on file", "confidence": 1.0,
            "fetched": [], "fields": [], "change_request": None,
        }

    try:
        texts, fetched = gather_evidence(website)
    except Exception as e:
        return {
            "resource_id": rid, "name": name, "verdict": "abstain",
            "reason": f"fetch failed: {e}", "confidence": 0.0,
            "fetched": [], "fields": [], "change_request": None,
        }

    if not texts:
        return {
            "resource_id": rid, "name": name, "verdict": "abstain",
            "reason": "site unreachable or JS-only shell with no readable text",
            "confidence": 0.2, "fetched": fetched, "fields": [], "change_request": None,
        }

    findings = [f for f in (
        check_phone(record, texts), check_closure(texts), check_address(record, texts),
    ) if f]

    result = None
    if api_key:
        result = adjudicate_gemini(record, findings, texts, api_key)
    if result is None:
        result = adjudicate_deterministic(record, findings, texts, fetched)
    verdict, reason, confidence = result

    return {
        "resource_id": rid, "name": name, "verdict": verdict, "reason": reason,
        "confidence": confidence, "fetched": fetched,
        "fields": [{k: v for k, v in f.items() if k != "match"} for f in findings],
        "change_request": build_change_request(record, findings, verdict),
    }


def verify_many(records, api_key=None, workers=6):
    """Fan out verify() over ThreadPoolExecutor - network-bound, so threads are fine."""
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(verify, r, api_key) for r in records]
        return [f.result() for f in futs]


if __name__ == "__main__":
    files = sorted(DATA.glob("*.json"))[:3]
    records = []
    for f in files:
        try:
            d = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        r = d.get("resource", d)
        if isinstance(r, dict) and "id" in r:
            records.append(r)

    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("GEMINI_API_KEY")
    for result in verify_many(records, api_key=key):
        print(json.dumps(result, indent=2)[:2000])
