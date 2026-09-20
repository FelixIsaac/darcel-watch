"""Re-verify SF Service Guide listings against the org's live website.

Reads the v2 API's records (see harvest.py for why v2 and not askdarcel.org/api).

Read-only, agentic. Deterministic evidence gathering + an explicit "where do I
look next" decision loop, with an optional Gemini adjudication layer. We never
write to the Service Guide - change_request payloads are emitted, never POSTed.
"""

import concurrent.futures as cf
import html
import json
import os
import pathlib
import re
import urllib.error
import urllib.request

import discover
import envfile

envfile.load()

DATA = pathlib.Path(__file__).parent / "data_v2"
UA = {"User-Agent": "shelflife/0.4 (Hack for Humanity SF; read-only)"}
API = os.environ.get("SFSG_API", "https://www.sfserviceguide.org/api/v2")
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
OPEN_TAG_RE = re.compile(r"<(script|style)[^>]*>.*$", re.S | re.I)
# Serialized config that survived tag-stripping: quoted keys, braces, dotted
# identifiers. Prose about a closure does not look like this.
CODE_RE = re.compile(r'":\s*"|\{"|":\[|\w+\.\w+\.\w+|=>|function\s*\(')
# Phrases that can only be about the subject of the page.
STRONG_CLOSURE_RE = re.compile(
    r"permanently closed|out of business|no longer offering|temporarily closed", re.I
)
# Who the sentence is about. "has closed" and "relocated" are only evidence when
# the organisation is the subject.
SUBJECT_RE = re.compile(
    r"\b(we|our|us)\b|\bthis (location|office|site|program|programme|center|centre|"
    r"clinic|pantry|shelter|branch|facility)\b",
    re.I,
)
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
                    "HTTP-Referer": "https://github.com/FelixIsaac/shelflife",
                    "X-Title": "SF Service Guide Watch",
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


# The public listing a volunteer would actually look at and edit. Route shapes
# confirmed against ShelterTechSF/askdarcel-web: path="/organizations/:id" and
# path="/organizations/:id/edit".
SERVICE_GUIDE = "https://www.sfserviceguide.org"


def listing_url(rid):
    return f"{SERVICE_GUIDE}/organizations/{rid}" if rid else None


def listing_edit_url(rid):
    return f"{SERVICE_GUIDE}/organizations/{rid}/edit" if rid else None


def edit_url(record):
    """Prefer the curation dataset's own edit link when harvest attached one.

    (In practice it is always /organizations/<resource_id>/edit, i.e. identical to
    what we construct - but taking theirs means we follow if they ever move it.)
    """
    return record.get("service_edit_url") or listing_edit_url(record.get("id"))


# Phone/address normalisation lives in normalize.py because the graph backends
# key their phone and address nodes on exactly these functions - a second copy
# drifts silently. Re-exported here for callers that import from verify.
from normalize import norm_phone, phone_digits  # noqa: E402,F401


def text_from_html(raw):
    """Strip scripts/styles/tags to plain text. Crude but stdlib-only."""
    no_script = TAG_RE.sub(" ", raw)
    # fetch() caps the body at 500KB, which on a big Wix/SPA page lands in the
    # middle of a <script>. TAG_RE only matches balanced pairs, so that final
    # unclosed block survives and its JS config dumps into the "text" - which is
    # how we once reported Temple United Methodist Church as relocated on the
    # strength of "specs.events.ui.RelocatedPagesModal":"true". Drop any opener
    # with no closer.
    no_script = OPEN_TAG_RE.sub(" ", no_script)
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


def candidate_pages(website):
    """Which pages to look at, asked of the SITE rather than guessed.

    discover.py reads robots.txt, follows the sitemaps the site declares, and
    falls back to a link-graph crawl when there are none. 81% of organisations
    in this corpus publish a sitemap.

    SUBPAGES remains only as the last resort, for the site that has no sitemap,
    no robots.txt and no crawlable links. Until this function existed it was
    the ONLY strategy: five guesses, against every organisation, while the
    docstring here claimed the agent chose where to look. Auditing Building
    Futures - a domestic violence service - those guesses returned one page and
    two 404s, none carrying the stored number, and this function's caller came
    one step from reporting a correct phone number as wrong. The number is on
    /get-help/, which the sitemap lists and no guess would ever have found.
    """
    base = website.rstrip("/")
    try:
        inv = discover.discover(website)
    except Exception:            # discovery must never break verification
        inv = None
    if inv and len(inv):
        ranked = [u for u, _ in inv.top("phone", limit=MAX_FETCHES + 2)]
        # Homepage first: on small nonprofit sites it is often the only page.
        return list(dict.fromkeys([base + "/"] + ranked))
    return [base] + [base + p for p in SUBPAGES]


def gather_evidence(website):
    """Fetch the pages most likely to carry evidence, stopping early once we
    have enough to adjudicate. Returns (texts, urls).

    The page list comes from candidate_pages() - the site's own sitemap, not a
    hardcoded list of slugs.
    """
    texts = {}
    fetched = []
    queue = candidate_pages(website)

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
    """Compare the live site against EVERY stored number, not just the first.

    A listing routinely carries ten numbers, one per programme or region. An
    earlier version compared only the first and "found" that the main line was
    missing - while it sat in the list two rows down. A number is only absent
    if it matches none of them.
    """
    stored_all = []
    for p in record.get("phones") or []:
        raw = p.get("number")
        n = norm_phone(raw)
        if n:
            stored_all.append((raw, n, p.get("service_type") or "", p.get("id")))
    if not stored_all:
        return None

    stored_norms = {n for _, n, _, _ in stored_all}

    for url, t in texts.items():
        for m in PHONE_RE.finditer(t):
            live_norm = norm_phone(m.group())
            if live_norm and live_norm in stored_norms:
                raw, _, _, pid = next(s for s in stored_all if s[1] == live_norm)
                return {"field": "phone", "stored": raw, "live": m.group(),
                        "evidence_url": url, "evidence_quote": m.group(),
                        "match": True, "phone_id": pid}

    # Nothing on the site matched ANY stored number.
    #
    # With one stored number that is a straightforward "this looks wrong". With
    # ten - one per programme or region - it is NOT evidence that any particular
    # one is wrong, and proposing to replace an arbitrary one is how we produced
    # a false positive on Meals on Wheels. The honest finding is that the number
    # on the site is missing from the listing: an addition, not a replacement.
    for url, t in texts.items():
        m = PHONE_RE.search(t)
        if not m:
            continue
        if len(stored_all) > 1:
            return {"field": "phone_missing",
                    "stored": f"not among the {len(stored_all)} numbers on the listing",
                    "live": m.group(), "evidence_url": url,
                    "evidence_quote": snippet(t, m), "match": False,
                    "stored_count": len(stored_all), "addition": True}
        return {"field": "phone", "stored": stored_all[0][0], "live": m.group(),
                "evidence_url": url, "evidence_quote": snippet(t, m),
                "match": False, "stored_count": 1, "phone_id": stored_all[0][3]}
    return None


def check_phone_format(record):
    """Stored numbers that cannot be dialled as written.

    Deliberately narrow. An earlier, looser version of this check flagged
    anything whose digits didn't come to 10 or 11, and produced a wave of
    findings that were all artifacts: we were reading the v1 API, whose phone
    formatter mangles US numbers, and on top of that the rule fired on every
    legitimate short code in the corpus. Re-run against v2, "not 10 or 11
    digits" catches 14 numbers in 816 listings and 12 of them are fine -
    311 (SF city services), 711 (TTY relay), 838255 and 9881 (crisis-line text
    and dial-then-option codes), and three inline "ext. NNN" suffixes.

    So the rule is now only the three shapes that are unambiguously broken and
    that a volunteer can actually fix:

      1. too many digits - two numbers, or a number and a ZIP, typed into one
         field (phone 4777: "41574423832383");
      2. an empty number field, sometimes with the real number sitting in the
         service_type label instead (phone 4392: number None, label
         "(415) 333-3017") - the listing shows a contact row with no contact;
      3. a non-US country_code on a San Francisco listing, which means the
         number was parsed as a foreign one and is rendered - and dialled -
         wrong (phone 2149: SF311's TTY line stored as Swiss "057 012 31 17"
         when the same record carries the correct (415) 701-2311).

    Short numbers are no longer flagged at all: below 10 digits we cannot tell
    a truncated number from a real short code, and guessing wrong wastes the
    volunteer we are trying to help.
    """
    bad = []
    for p in record.get("phones") or []:
        raw = (p.get("number") or "").strip()
        label = (p.get("service_type") or "").strip()
        cc = p.get("country_code")

        if not raw:
            recovered = PHONE_RE.search(label)
            why = "no number stored"
            if recovered:
                why += f" - but the label reads {recovered.group()!r}, typed into the wrong field"
            bad.append({"phone_id": p.get("id"), "raw": raw or "(empty)",
                        "label": label or "unlabelled", "why": why,
                        "proposed": recovered.group() if recovered else None})
            continue

        digits = phone_digits(raw)
        if len(digits) > 11:
            bad.append({"phone_id": p.get("id"), "raw": raw,
                        "label": label or "unlabelled",
                        "why": f"{len(digits)} digits - two values run into one field",
                        "proposed": None})
            continue

        if cc and cc != "US":
            bad.append({"phone_id": p.get("id"), "raw": raw,
                        "label": label or "unlabelled",
                        "why": f"stored with country_code {cc} on a Bay Area listing - "
                               "parsed as a foreign number, so it renders and dials wrong",
                        "proposed": None})

    if not bad:
        return None
    summary = "; ".join(f"{b['raw']} ({b['label']}): {b['why']}" for b in bad)
    return {
        "field": "phone_format",
        "stored": summary,
        "live": "not dialable as stored",
        # The evidence is the stored value itself, so point at the listing.
        # Every finding carries a source; this one's source is the record.
        "evidence_url": listing_url(record.get("id")),
        "evidence_quote": summary,
        "match": False,
        "structural": True,
        "phone_id": bad[0]["phone_id"],
        "phone_issues": bad,
    }


# Identity anchors are used one way only: to confirm a fetched page IS this
# listing's, before any discrepancy found on it is believed.
#
# There used to be a check that reported the inverse - "none of this listing's
# anchors appear on its website, so the website is probably not theirs" - as a
# finding. It is not shipped, deliberately: the count fell 53 -> 32 -> 18 as the
# crawler got better at rendering JS shells, which means it was measuring our
# crawl depth, not the directory's errors. An absence is only evidence once you
# can prove you looked properly, and we cannot.


def identity_anchors(record):
    """The facts that would prove a page belongs to this listing.

    Address and phone only - deliberately NOT the name. Plenty of legitimate
    organisations run a domain with no relation to what they are called (Meals
    on Wheels at feedingseniors.org), and name-to-domain similarity would flag
    every one of them. A street, a ZIP or a working number is a fact about the
    body; a domain name is branding.
    """
    phones, places = [], []
    for p in record.get("phones") or []:
        n = norm_phone(p.get("number"))
        if n:
            phones.append(n)
    for a in record.get("addresses") or []:
        zipcode = (a.get("postal_code") or "").strip()
        if zipcode:
            places.append(("postal_code", zipcode))
        street = (a.get("address_1") or "").strip()
        # House number + first word of the street name: distinctive enough to
        # mean something, short enough to survive the site writing "Ave" for
        # "Avenue" or dropping a "Suite 300".
        head = " ".join(street.split()[:2])
        if len(head) >= 6:
            places.append(("street", head))
        city = (a.get("city") or "").strip()
        if len(city) >= 4:
            places.append(("city", city))
    return phones, places


def find_identity_anchor(record, texts):
    """First anchor that appears in any fetched page, or None. (kind, value, url)."""
    phones, places = identity_anchors(record)
    for url, t in texts.items():
        low = t.lower()
        for n in phones:
            if any(norm_phone(m.group()) == n for m in PHONE_RE.finditer(t)):
                return "phone", n, url
        for kind, value in places:
            if value.lower() in low:
                return kind, value, url
    return None


def check_closure(texts):
    """A closure claim has to come from prose a person could read on the page.

    Defence in depth behind text_from_html: any match whose surroundings look
    like serialized config rather than a sentence is discarded, not reported.
    Telling a volunteer a church has relocated on the evidence of a feature-flag
    name is worse than finding nothing.
    """
    for url, t in texts.items():
        for m in CLOSURE_RE.finditer(t):
            quote = snippet(t, m)
            if CODE_RE.search(quote):
                continue
            # "has closed" and "relocated" are ordinary English that appears in
            # staff bios ("he relocated to New Orleans") and in news about other
            # bodies ("San Francisco has closed the public spaces around..."),
            # both of which we reported as closures before this guard. Weak
            # phrases now need the organisation to be the subject; only the
            # unambiguous banner phrases stand on their own.
            if not STRONG_CLOSURE_RE.search(m.group()) and not SUBJECT_RE.search(quote):
                continue
            return {"field": "operating_status", "stored": "open (approved)", "live": m.group(),
                    "evidence_url": url, "evidence_quote": quote, "match": False}
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


def actionable(findings):
    """Findings a volunteer could actually rule on.

    A field whose stored and live values are identical is not a discrepancy, no
    matter what the adjudicator concluded - the model sometimes reasons about a
    field (say, the phone) while the only gathered evidence is another (the
    address), which used to produce 'change this address to itself'. Requiring a
    real difference makes that impossible to emit.
    """
    out = []
    for f in findings or []:
        stored = str(f.get("stored") or "").strip()
        live = str(f.get("live") or "").strip()
        if not stored or not live or stored == live:
            continue
        if f.get("field") == "phone" and norm_phone(stored) == norm_phone(live):
            continue  # same number, different formatting
        out.append(f)
    return out


PHONE_FIELDS = ("phone", "phone_format", "phone_missing")


def change_request_endpoint(record, finding):
    """The v2 route this payload WOULD go to, if a human chose to submit it.

    Phone edits have their own resource in the Go API; everything else is a field
    edit on the resource. Both confirmed migrated to Go in sheltertech-go's
    docs/askdarcel-web-endpoint-migration-plan.md (rows 5 and 22).
    """
    if finding.get("field") in PHONE_FIELDS and finding.get("phone_id"):
        return f"{API}/phones/{finding['phone_id']}/change_requests"
    return f"{API}/resources/{record.get('id')}/change_requests"


def build_change_request(record, findings, verdict):
    if verdict != "discrepancy":
        return None
    usable = actionable(findings)
    bad = next((f for f in usable if f.get("match") is False), usable[0] if usable else None)
    if not bad:
        return None
    # A phone_format finding may already know the corrected value (the number was
    # typed into the label field); prefer that over the placeholder "live" text.
    proposed = bad["live"]
    for issue in bad.get("phone_issues") or []:
        if issue.get("proposed"):
            proposed = issue["proposed"]
            break
    return {
        "resource_id": record.get("id"),
        "phone_id": bad.get("phone_id"),
        "field": bad["field"],
        "listing_url": listing_url(record.get("id")),
        # The curation dataset hands us the exact page a volunteer fixes this on.
        "listing_edit_url": edit_url(record),
        "endpoint": change_request_endpoint(record, bad),
        "method": "POST",
        "current": bad["stored"],
        "proposed": proposed,
        "source_url": bad["evidence_url"],
        "source_quote": bad["evidence_quote"],
        "submitted_by": "shelflife (agent, human review required)",
        "posted": False,
    }
    # NOTE: EMITTED ONLY. We never POST to /api/v2/resources/:id/change_requests
    # or /api/v2/phones/:id/change_requests. "endpoint" records where a payload
    # would go so a human can check our work; this process is read-only and
    # issues no write request of any kind. A human reviews and submits it.


def verify(record, api_key=None):
    """Re-verify one listing against its live website. Never raises.

    An opt-in tool-calling agent used to sit in front of this (AGENT=1), 899
    lines shipped off by default because it was, in its own docstring, "newer
    and less proven". audit.py supersedes it with a path that is measured and
    calibrated, so it has been removed rather than left as dead code in a
    public repo. It is in git history if it is ever wanted back.
    """
    rid = record.get("id")
    name = record.get("name", "")
    website = record.get("website")

    # TIER 1 FIRST. This reads the stored value and nothing else - no network,
    # no model, and it cannot false-positive. Running it before the fetch is
    # not an optimisation, it is the difference between finding these defects
    # and not: an earlier version ran it last, so any listing whose website was
    # unreachable or JS-only had its structural check skipped entirely. That was
    # 15 of 23 abstentions - every one of them a listing we could have judged
    # for free, silently dropped because an unrelated, expensive step failed.
    structural = check_phone_format(record)

    def early(reason, confidence, fetched=()):
        """Abstain on the live-source question, but keep any Tier 1 finding."""
        if structural:
            return {
                "resource_id": rid, "name": name, "verdict": "discrepancy",
                "reason": structural["stored"], "confidence": 0.95,
                "fetched": list(fetched), "fields": [structural],
                "listing_url": listing_url(rid), "listing_edit_url": edit_url(record),
                "org_website": website,
                "change_request": build_change_request(record, [structural], "discrepancy"),
            }
        return {
            "resource_id": rid, "name": name, "verdict": "abstain",
            "reason": reason, "confidence": confidence,
            "fetched": list(fetched), "fields": [], "change_request": None,
        }

    if not website:
        return early("no website on file", 1.0)

    try:
        texts, fetched = gather_evidence(website)
    except Exception as e:
        return early(f"fetch failed: {e}", 0.0)

    if not texts:
        return early(
            "site unreachable or JS-only shell with no readable text", 0.2, fetched
        )

    findings = [f for f in (
        check_phone(record, texts), check_closure(texts), check_address(record, texts),
        structural,
    ) if f]

    result = None
    if api_key:
        result = adjudicate_gemini(record, findings, texts, api_key)
    if result is None:
        result = adjudicate_deterministic(record, findings, texts, fetched)
    verdict, reason, confidence = result

    # A "discrepancy" a volunteer cannot act on is not a discrepancy. If the
    # adjudicator called one but no field actually differs, we abstain rather
    # than send someone a review with nothing to decide.
    usable = actionable(findings)
    if verdict == "discrepancy" and not usable:
        verdict = "abstain"
        reason = ("Adjudicator flagged a problem but no gathered field actually "
                  f"differs, so there is nothing to act on. Original note: {reason}")
        confidence = min(confidence, 0.3)

    # Never show a reviewer a stored->live pair that is identical, whatever the
    # verdict. "stored X / live X - please confirm" asks someone to decide
    # nothing, and it reads as a bug because it is one. Abstentions keep only
    # genuinely differing fields; if none differ, the reason text stands alone.
    shown = usable
    return {
        "resource_id": rid, "name": name, "verdict": verdict, "reason": reason,
        # Both sides of the comparison, so a reviewer can see what they are
        # about to change as well as the evidence for changing it.
        "listing_url": listing_url(rid),
        "listing_edit_url": edit_url(record),
        "org_website": website,
        "confidence": confidence, "fetched": fetched,
        "fields": [{k: v for k, v in f.items() if k != "match"} for f in shown],
        "change_request": build_change_request(record, findings, verdict),
    }


def verify_many(records, api_key=None, workers=6):
    """Fan out verify() over ThreadPoolExecutor - network-bound, so threads are fine.

    verify() promises never to raise, but a malformed record can still trip a
    check that sits outside its try (or build_change_request). Re-raising here
    would throw away every other record's network work for one bad row, so a
    failure is caught per future and returned as that record's abstention: one
    bad record costs one record.
    """
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = [(r, ex.submit(verify, r, api_key)) for r in records]
        out = []
        for record, f in futs:
            try:
                out.append(f.result())
            except Exception as e:
                out.append({
                    "resource_id": record.get("id"),
                    "name": record.get("name", ""),
                    "verdict": "abstain",
                    "reason": f"verifier crashed on this record: {type(e).__name__}: {e}",
                    "confidence": 0.0,
                    "fetched": [],
                    "fields": [],
                    "change_request": None,
                })
        return out


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
