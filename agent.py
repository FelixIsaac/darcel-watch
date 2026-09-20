"""A tool-calling agent that investigates one listing against the org's own site.

The difference from verify.py's path is where the decisions live. There, regexes
gather candidate evidence and the model is handed a finished list to rule on -
it adjudicates, it does not investigate. Here the model drives: it chooses which
page to fetch, what to search for in what it got back, which link to follow when
the homepage doesn't answer the question, and when it has enough to conclude. We
supply the tools and the budget; it supplies the plan.

That is strictly more capable and strictly more dangerous, so the limits are in
code, never in the prompt. A prompt is a request; a volunteer acting on a bad
finding is a real cost. Enforced here, not asked for:

  - fetch_page only reaches the organisation's OWN registered domain. Any other
    host is refused and the refusal is handed back to the model as a tool result,
    so it can adapt instead of crashing.
  - at most MAX_FETCHES page fetches and max_steps tool calls per listing.
  - a wall-clock deadline per listing; past it, the run abstains.
  - GET only. No POST, no auth, no cookies, no forms, no JS beyond whatever the
    reader service already ran to produce its markdown.

The verdict dict matches what verify.verify() returns, field for field, so run.py
and the UI cannot tell the two apart - plus `steps` (the tool-call trace) and
`fetch_methods`. The trace is the point: it is what lets a reviewer see the
investigation rather than take the conclusion on faith.
"""

import json
import os
import re
import time
import urllib.parse

import envfile
import fetcher
import verify

envfile.load()

MODEL = os.environ.get("DW_AGENT_MODEL", os.environ.get("DW_MODEL", "google/gemini-2.5-flash"))
MAX_FETCHES = 4
DEADLINE_SECONDS = float(os.environ.get("DW_AGENT_DEADLINE", "60"))
PAGE_CHARS = 6000  # what we show the model per fetch; it can find_in_page for more
SNIPPET_PAD = 80
MAX_MATCHES = 8
MAX_LINKS = 25

VERDICTS = ("discrepancy", "match", "abstain")

# [label](url) -> label, for checking a quote against the reader tier's markdown.
MD_LINK_TEXT_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _norm_quote(s):
    """Whitespace-insensitive, case-insensitive form used to check quotes."""
    return verify.WS_RE.sub(" ", s or "").strip().lower()


def _readings(text):
    """The page as fetched, and with markdown links flattened to their labels.

    The reader tier returns markdown, so a model quoting what a human would SEE
    writes "Phone:(415) 487-3300" where the source holds
    "Phone:[(415) 487-3300](tel:415 487 3300)". That is a faithful quote of the
    rendered page. This check exists to catch invention, not formatting.
    """
    return (_norm_quote(text), _norm_quote(MD_LINK_TEXT_RE.sub(r"\1", text)))


def _locate_quote(quote, pages):
    """Find the page a quote came from. Returns (url, composed) or (None, False).

    Contiguous quotes are checked first. Failing that, we accept a quote whose
    every line appears on one page: asked for evidence spanning a contact block,
    the model reliably stitches "Phone: ...", "Address ..." from separate parts
    of the page into one string. Each line is genuine, the concatenation is not,
    and rejecting the whole thing threw away correct findings on ECS and City
    Youth Now. `composed` is returned so the reviewer is told which they are
    looking at rather than being quietly handed a quote that is not on the page
    in that form.

    Lines shorter than 8 characters are ignored - "Phone:" or "CA" matches
    everything and would let a stitched quote through on nothing.
    """
    needle = _norm_quote(quote)
    if not needle:
        return None, False
    # A quote the model deliberately cut short - "...215 Salvio St..." - is not
    # an invented one. Drop the marker and check what it did commit to. What
    # precedes the ellipsis must still match exactly, so this forgives honest
    # truncation without forgiving a wrong word before it.
    needle = re.sub(r"(?:\.\.\.|…)\s*$", "", needle).strip() or needle
    for url, text in pages.items():
        if any(needle in r for r in _readings(text)):
            return url, False

    lines = [_norm_quote(ln) for ln in re.split(r"[\r\n]+", quote)]
    lines = [ln for ln in lines if len(ln) >= 8]
    if not lines:
        return None, False
    for url, text in pages.items():
        readings = _readings(text)
        if all(any(ln in r for r in readings) for ln in lines):
            return url, True
    return None, False


def registered_domain(url_or_host):
    """Last two labels of the hostname, lowercased.

    Deliberately crude - a real public-suffix list is a dependency we don't want
    and the corpus is US nonprofits, so the two-label rule holds. It is also the
    conservative direction of error for our purpose: on a multi-part suffix like
    .co.uk it would collapse to "co.uk" and over-permit, which is why the caller
    ALSO requires a non-empty match against the stored website's own domain and
    we keep the corpus in mind. Subdomains and www pass, unrelated hosts do not.
    """
    s = (url_or_host or "").strip()
    if "://" not in s:
        s = "http://" + s
    host = (urllib.parse.urlsplit(s).hostname or "").lower().strip(".")
    if not host:
        return None
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def same_org(url, website):
    allowed = registered_domain(website)
    return bool(allowed) and registered_domain(url) == allowed


def _stored_summary(record):
    phones = [
        f"{p.get('number')!r} (label: {p.get('service_type') or 'unlabelled'})"
        for p in record.get("phones") or []
    ]
    addrs = []
    for a in record.get("addresses") or []:
        parts = [a.get("address_1"), a.get("city"), a.get("state_province"), a.get("postal_code")]
        label = a.get("name")
        line = " ".join(str(p).strip() for p in parts if p)
        addrs.append(f"{line}" + (f" (label: {label})" if label else ""))
    return phones, addrs


SYSTEM = """You are auditing one listing in a San Francisco social-services \
directory against the organisation's own live website. A human volunteer acts on \
your answer.

Your job: decide whether the STORED values below are still true today, using the \
tools. You choose what to fetch and what to look for. Start with the \
organisation's website, then follow links (contact, about, locations, hours) if \
the first page does not settle it.

ABSTAIN IS A FIRST-CLASS ANSWER AND OFTEN THE RIGHT ONE. A wrong "discrepancy" \
sends a volunteer to replace a working phone number with a useless one; "I could \
not tell" costs them nothing. When in doubt, abstain.

Answer "discrepancy" ONLY when the page plainly contradicts a stored value for \
the same purpose - i.e. the live value is presented as this organisation's main \
public contact and the stored value is absent or superseded. A listing routinely \
holds many numbers, one per programme; a page listing a number you don't \
recognise is NOT a discrepancy.

ABSTAIN when the number or address you found serves a different purpose than the \
stored one: a careers or hiring line, a fax, donations, press, a specific \
department or clinic, a partner organisation, or a second location. Also abstain \
on vanity numbers that spell words, partial matches, extensions, navigation \
boilerplate, cookie banners, and anything ambiguous.

NOT FINDING THE STORED VALUE IS NOT A DISCREPANCY. The text you get back is not \
the whole page: contact details are often rendered by JavaScript, sit inside an \
image or a map widget, or live on a page you did not fetch. "I searched and the \
number was not there" means ABSTAIN, every time. A discrepancy needs a POSITIVE \
contradiction - a different value, visible in text you quote, presented for the \
same purpose as the stored one. Absence is never evidence.

Answer "match" when the page corroborates the stored values.

Method: fetch the website, then run find_in_page for EACH stored value before \
you conclude. fetch_page shows you only the first part of a long page, so a \
value you cannot see in it may still be there - searching is how you find out. \
Do not conclude on the strength of the visible excerpt alone.

Every non-abstain verdict must quote text you actually saw through a tool, \
verbatim, in evidence_quote, with the URL you saw it on. Quote ONE contiguous \
passage as it appears on the page - do not stitch together lines from different \
parts of it, and do not tidy it up. Do not paraphrase and do not invent a quote; \
a quote that isn't on the page is the worst outcome here.

Calibrate confidence honestly. Above 0.9 is only for a page that explicitly \
supersedes the stored value. Call conclude() exactly once when you are done."""


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": (
                "GET one page on the organisation's own domain and return its visible "
                "text. Off-domain URLs are refused. Truncated to "
                f"{PAGE_CHARS} characters; use find_in_page to search the full text. "
                f"Limit: {MAX_FETCHES} fetches."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Absolute http(s) URL on the org's own domain."}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_in_page",
            "description": (
                "Regex search across the FULL text of every page fetched so far, "
                "including the parts truncated out of fetch_page. Returns matches with "
                "surrounding context. Use this to look for a specific phone number, "
                "street, ZIP, or phrase like 'permanently closed'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regex, case-insensitive."}
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_links",
            "description": (
                "List candidate links on the org's own domain found on the pages "
                "fetched so far, so you can choose where to look next."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "conclude",
            "description": "Give the final verdict and stop. Call exactly once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "reason": {"type": "string", "description": "One or two sentences a volunteer can act on."},
                    "confidence": {"type": "number", "description": "0.0-1.0, honestly calibrated."},
                    "evidence_quote": {
                        "type": "string",
                        "description": "Verbatim text you saw via a tool. Empty only when abstaining.",
                    },
                    "evidence_url": {"type": "string", "description": "The page the quote came from."},
                    "field": {
                        "type": "string",
                        "description": "Which stored field this is about: phone, address, operating_status, hours.",
                    },
                    "live_value": {
                        "type": "string",
                        "description": "The value the site shows, when it differs from the stored one.",
                    },
                    "stored_value": {
                        "type": "string",
                        "description": "The stored value this verdict concerns.",
                    },
                },
                "required": ["verdict", "reason", "confidence"],
            },
        },
    },
]


class Session:
    """Tool implementations + the budget they spend. One per listing."""

    def __init__(self, website, deadline):
        self.website = website
        self.deadline = deadline
        self.pages = {}         # url -> full text
        self.methods = {}       # url -> "direct" | "reader"
        self.links = []         # same-domain candidates, deduped, order preserved
        self.fetches = 0
        self.rejected = []      # off-domain attempts, kept for the trace

    def out_of_time(self):
        return time.time() > self.deadline

    def fetch_page(self, url=None, **_):
        url = (url or "").strip()
        if not url:
            return "ERROR: no url given."
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url.lstrip("/")

        # Guardrail, in code. The model is told about it, but being told is not
        # what stops it - this branch is.
        if not same_org(url, self.website):
            self.rejected.append(url)
            return (
                f"REFUSED: {url} is not on {registered_domain(self.website)}, the "
                "organisation's own domain. This tool only reaches the organisation's "
                "own site. Third-party pages (Yelp, Google, Facebook, news, other "
                "nonprofits) are not acceptable evidence here. Fetch a page on "
                f"{registered_domain(self.website)} instead, or conclude."
            )
        if self.fetches >= MAX_FETCHES:
            return (
                f"ERROR: fetch budget exhausted ({MAX_FETCHES} pages). Use "
                "find_in_page on what you already have, or conclude - abstain is fine."
            )
        if self.out_of_time():
            return "ERROR: time budget exhausted. Call conclude now."
        if url in self.pages:
            body = self.pages[url]
        else:
            self.fetches += 1
            text, method = fetcher.fetch(url)
            if not text:
                return f"FETCH FAILED for {url}: {method}. Try another page on the same domain, or conclude."
            self.pages[url] = text
            self.methods[url] = method
            for link in fetcher.links_for(url, method):
                if same_org(link, self.website) and link not in self.links:
                    self.links.append(link)
            body = text

        head = body[:PAGE_CHARS]
        note = f"[fetched via {self.methods.get(url, 'cache')}]"
        if len(body) > PAGE_CHARS:
            note += (
                f" [TRUNCATED: showing {PAGE_CHARS} of {len(body)} characters. "
                "The rest is still searchable with find_in_page.]"
            )
        return f"{note}\n{head}"

    def find_in_page(self, pattern=None, **_):
        if not pattern:
            return "ERROR: no pattern given."
        if not self.pages:
            return "ERROR: nothing fetched yet. Call fetch_page first."
        try:
            rx = re.compile(pattern, re.I)
        except re.error as e:
            return f"ERROR: bad regex {pattern!r}: {e}"

        hits = []
        for url, text in self.pages.items():
            for m in rx.finditer(text):
                i, j = m.span()
                context = text[max(0, i - SNIPPET_PAD): j + SNIPPET_PAD].strip()
                hits.append(f"- {url}: ...{context}...")
                if len(hits) >= MAX_MATCHES:
                    break
            if len(hits) >= MAX_MATCHES:
                break
        if not hits:
            # The model sometimes wraps its pattern in a stray quote character -
            # searching for '"\(510\) 267-7800' finds nothing, and it read that
            # empty result as proof the number was gone. Retry without the
            # quotes and say which pattern actually ran, so a null result means
            # "not on the page" rather than "you mistyped the search".
            stripped = pattern.strip("\"'")
            if stripped and stripped != pattern:
                retry = self.find_in_page(stripped)
                return f"(no match for {pattern!r}; retried without quote characters) {retry}"
            return (
                f"No match for {pattern!r} on any page fetched so far ({len(self.pages)} page(s)). "
                "Remember: not finding a value is NOT evidence that it is wrong - the page may "
                "render it with JavaScript, in an image, or on a page you have not fetched."
            )
        return f"{len(hits)} match(es) for {pattern!r}:\n" + "\n".join(hits)

    def list_links(self, **_):
        if not self.pages:
            return "ERROR: nothing fetched yet. Call fetch_page first."
        if not self.links:
            return "No same-domain links found on the pages fetched so far."
        return "Same-domain links available:\n" + "\n".join(f"- {u}" for u in self.links[:MAX_LINKS])


# "Dial extension 259 for the youth clinic", "ext. 105", "press 2 for intake".
EXTENSION_RE = re.compile(r"\b(?:dial|press)?\s*(?:ext(?:ension|\.)?|x)\s*\.?\s*\d+", re.I)


def _extension_qualifier(live, pages, window=240):
    """The site's own words about reaching a specific service on a shared line.

    Looks only just AFTER the number we are proposing, so an extension note
    belonging to some other department further down the page is not attached to
    this one. Returns the line verbatim - a paraphrase of a dialling
    instruction is worse than none.
    """
    n = verify.norm_phone(live)
    if not n:
        return None
    for text in pages.values():
        for m in verify.PHONE_RE.finditer(text):
            if verify.norm_phone(m.group()) != n:
                continue
            tail = text[m.end(): m.end() + window]
            for line in re.split(r"[\r\n]+|(?<=\.)\s", tail):
                line = verify.WS_RE.sub(" ", line).strip(" -|*#[]")
                if line and EXTENSION_RE.search(line) and len(line) <= 160:
                    return line
    return None


def _identity_anchor(record, session):
    """Do the fetched pages corroborate that they are about THIS listing?

    Thin wrapper over verify.find_identity_anchor so the agent and the
    deterministic path share one definition of "this page is theirs". It is a
    guard only - the agent uses it to refuse a discrepancy found on a page it
    cannot tie to this listing. The inverse ("no anchors, so the website is
    wrong") is deliberately not reported as a finding; see the note above
    verify.identity_anchors.
    """
    return verify.find_identity_anchor(record, session.pages) is not None


def _abstain(record, reason, confidence, steps, session, extra=None):
    rid = record.get("id")
    out = {
        "resource_id": rid,
        "name": record.get("name", ""),
        "verdict": "abstain",
        "reason": reason,
        "listing_url": verify.listing_url(rid),
        "listing_edit_url": verify.edit_url(record),
        "org_website": record.get("website"),
        "confidence": confidence,
        "fetched": list(session.pages) if session else [],
        "fields": [],
        "change_request": None,
        "steps": steps or [],
        "fetch_methods": dict(session.methods) if session else {},
        "agent": True,
    }
    if session and session.rejected:
        out["rejected_urls"] = session.rejected
    if extra:
        out.update(extra)
    return out


def _finalise(record, session, args, steps):
    """Turn the model's conclude() arguments into the pipeline's verdict dict.

    Everything the model says is treated as a claim to be checked, not a result
    to be copied. Two checks matter:

      - the quote has to actually appear on a page we fetched. Verbatim, modulo
        whitespace. A model that paraphrases its evidence has, from a reviewer's
        point of view, invented it, and this project has already retracted
        findings for less.
      - a discrepancy has to name a stored value and a differing live value,
        because a change request is a diff. verify.actionable() enforces the same
        rule on the deterministic path; the agent gets no exemption.

    Either check failing downgrades the verdict to abstain rather than dropping
    the answer, so the trace still shows what happened.
    """
    rid = record.get("id")
    verdict = args.get("verdict")
    if verdict not in VERDICTS:
        return _abstain(record, f"agent returned an unusable verdict {verdict!r}", 0.2, steps, session)

    # `reason` is declared required, but Gemini omits it on roughly one call in
    # five when the other arguments already say everything. Rendering "no reason
    # given" to a reviewer wastes their time when we hold the quote and both
    # values; compose the sentence ourselves instead.
    reason = str(args.get("reason") or "").strip()
    try:
        confidence = max(0.0, min(1.0, float(args.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5

    quote = str(args.get("evidence_quote") or "").strip()
    evidence_url = str(args.get("evidence_url") or "").strip()
    field = str(args.get("field") or "").strip() or "unspecified"
    live = str(args.get("live_value") or "").strip()
    stored = str(args.get("stored_value") or "").strip()

    if not reason:
        if verdict == "match" and stored:
            reason = f"Live site shows {field} {live or stored}, matching the listing."
        elif verdict == "discrepancy" and stored and live:
            reason = f"Live site shows {field} {live}; the listing stores {stored}."
        else:
            reason = "Agent gave no reason; treat this verdict as unexplained."
            confidence = min(confidence, 0.3)

    if verdict != "abstain":
        if not quote:
            return _abstain(
                record,
                f"Agent concluded '{verdict}' without quoting evidence, so it cannot be "
                f"reviewed. Original note: {reason}",
                min(confidence, 0.3), steps, session,
            )
        found_on, composed = _locate_quote(quote, session.pages)
        if not found_on:
            return _abstain(
                record,
                f"Agent quoted \"{quote[:120]}\" as evidence for '{verdict}', but that "
                "text does not appear on any page it fetched, so the finding is not "
                f"verifiable. Original note: {reason}",
                min(confidence, 0.2), steps, session,
                {"unverified_quote": quote},
            )
        if not evidence_url or evidence_url not in session.pages:
            evidence_url = found_on  # it read the right page, misattributed the URL
        if composed:
            reason += (
                " (Evidence quote was assembled from several places on the page; "
                "each line appears there, the block as written does not.)"
            )

    fields = []
    if verdict == "discrepancy" and stored and live and stored != live:
        if not (field == "phone" and verify.norm_phone(stored) == verify.norm_phone(live)):
            fields.append({
                "field": field, "stored": stored, "live": live,
                "evidence_url": evidence_url, "evidence_quote": quote,
            })
    elif verdict == "match" and quote:
        fields.append({
            "field": field, "stored": stored or "(as listed)", "live": live or stored or "(as listed)",
            "evidence_url": evidence_url, "evidence_quote": quote,
        })

    # A listing's stored `website` is not always the listing's organisation.
    # "Goodwill Industries of the Greater East Bay" carries sfgoodwill.org (a
    # different Goodwill entity); "Getting Out & Staying Out", a San Francisco
    # listing, carries gosonyc.org (a New York charity). The domain guardrail
    # anchors on that website, so the agent dutifully audits the wrong
    # organisation and proposes moving an SF listing's address to Manhattan.
    # Nothing in the trace looks wrong - the quote is real and the values do
    # differ - so this has to be caught on identity, not on evidence. Before
    # accepting a discrepancy, require the page to corroborate that it is about
    # THIS listing: one of its stored phones, street, postal code or city.
    if verdict == "discrepancy" and not _identity_anchor(record, session):
        return _abstain(
            record,
            "Agent found a difference, but nothing on the fetched pages ties them to "
            f"this listing - none of its stored phone numbers, street, postal code or "
            "city appear there. The website on file may belong to a different "
            f"organisation, so the difference is not evidence about this one. "
            f"Original note: {reason}",
            0.2, steps, session, {"identity_unconfirmed": True},
        )

    # The model can produce a discrepancy that our own evidence contradicts.
    # Both of these were caught auditing a 20-listing run, and neither is a
    # prompt problem - the model SAID the stored number was on the page and
    # flagged it anyway. They are invariants, so they live here.
    if verdict == "discrepancy" and fields and field == "phone":
        want = verify.norm_phone(stored)
        if want:
            on_page = any(
                verify.norm_phone(m.group()) == want
                for text in session.pages.values()
                for m in verify.PHONE_RE.finditer(text)
            )
            if on_page:
                # Upwardly Global: the agent proposed replacing (212) 219-9218
                # with the donor-support line, quoting a passage that contained
                # BOTH numbers. A stored number still printed on the org's own
                # page is not out of date, whatever else the page also lists.
                return _abstain(
                    record,
                    f"Agent called a discrepancy on {stored}, but that number is still "
                    "printed on the organisation's own page, so it is not out of date. "
                    f"The live value it found serves a different purpose. Original note: {reason}",
                    0.25, steps, session,
                )

        # Internet For All Now publishes "415-744-CETF (2383)". The finding is
        # right - the stored value is a 14-digit mash - but proposing the vanity
        # string verbatim swaps one undialable value for another. Keep the
        # finding, hand the reviewer the digits, and say which is which.
        if re.search(r"[A-Za-z]", live):
            dialable = verify.norm_phone(re.sub(r"[A-Za-z]", "", live))
            reason += (
                f" Note: the site writes this as a vanity number ({live}); the dialable "
                f"form is {dialable or 'not recoverable from the page'} and that, not the "
                "letters, is what belongs in the phone field."
            )
            confidence = min(confidence, 0.5)

        # Bay Area Cancer Connections: the site now shows one number for both
        # its helpline and its business line. The finding is real, but applying
        # it verbatim leaves the listing holding the same number twice, and the
        # right edit is probably to drop the row. Say so rather than hiding it.
        live_norm = verify.norm_phone(live)
        if live_norm:
            dupe = next(
                (p for p in record.get("phones") or []
                 if verify.norm_phone(p.get("number")) == live_norm), None,
            )
            if dupe:
                reason += (
                    f" Note: {live} is ALREADY on this listing as "
                    f"\"{dupe.get('service_type') or 'unlabelled'}\", so applying this as "
                    "written would store it twice - the row may need removing instead."
                )
                confidence = min(confidence, 0.5)

    if verdict == "discrepancy" and not fields:
        return _abstain(
            record,
            "Agent flagged a discrepancy but did not name a stored value and a "
            f"differing live value, so there is nothing to act on. Original note: {reason}",
            min(confidence, 0.3), steps, session,
        )

    change_request = None
    if verdict == "discrepancy":
        bad = dict(fields[0])
        bad["match"] = False
        # Bind the change request to a specific phone row, but only for phone
        # findings and only on a real number-to-number match. norm_phone returns
        # None for anything without ten digits, so an unguarded equality test
        # makes None == None true and staples the first unnumbered phone row
        # onto whatever we found - which is how an ADDRESS change request came
        # out carrying phone_id 1752. Everything else edits the resource.
        if field == "phone":
            want = verify.norm_phone(stored)
            for p in record.get("phones") or []:
                got = verify.norm_phone(p.get("number"))
                if want and got and want == got:
                    bad["phone_id"] = p.get("id")
                    break
            else:
                # The stored side is empty - the listing has a contact row with
                # no number in it. The fix belongs on that row, so find the one
                # the agent is talking about rather than editing the resource.
                if not want:
                    empty = [p for p in record.get("phones") or [] if not verify.norm_phone(p.get("number"))]
                    if len(empty) == 1:
                        bad["phone_id"] = empty[0].get("id")
        change_request = verify.build_change_request(record, [bad], verdict)
        if change_request:
            change_request["submitted_by"] = "darcel-watch (tool-calling agent, human review required)"
            # A number can be right and still be the wrong thing to store bare.
            # sf.gov gives the Larkin Street Youth Clinic's number as
            # 415-673-0911 followed by "Dial extension 259 for the youth
            # clinic" - that is the Youth Services main line, and a volunteer
            # told only "set the clinic's phone to 415-673-0911" would drop the
            # half that reaches the clinic. Carry the site's own wording.
            qualifier = _extension_qualifier(live, session.pages)
            if qualifier:
                change_request["qualifier"] = qualifier
                change_request["proposed_note"] = (
                    f"{change_request['proposed']} - but the site adds: \"{qualifier}\". "
                    "Store the extension too; the bare number is not the direct line."
                )
                existing = change_request.get("source_quote", "")
                if _norm_quote(qualifier) not in _norm_quote(existing):
                    change_request["source_quote"] = f"{existing} {qualifier}".strip()
                reason += f" The site adds: \"{qualifier}\"."

    out = {
        "resource_id": rid,
        "name": record.get("name", ""),
        "verdict": verdict,
        "reason": reason,
        "listing_url": verify.listing_url(rid),
        "listing_edit_url": verify.edit_url(record),
        "org_website": record.get("website"),
        "confidence": confidence,
        "fetched": list(session.pages),
        "fields": fields,
        "change_request": change_request,
        "steps": steps,
        "fetch_methods": dict(session.methods),
        "agent": True,
    }
    if session.rejected:
        out["rejected_urls"] = session.rejected
    return out


def investigate(record, api_key, max_steps=8):
    """Run the tool-use loop on one record. Returns a verdict dict. Never raises."""
    started = time.time()
    website = record.get("website")
    session = Session(website, started + DEADLINE_SECONDS)
    steps = []

    if not website:
        return _abstain(record, "no website on file", 1.0, steps, session)
    if not registered_domain(website):
        return _abstain(record, f"website on file is not a usable URL: {website!r}", 1.0, steps, session)
    if not api_key:
        return _abstain(record, "no API key, so the agent could not run", 0.0, steps, session,
                        {"agent_unavailable": "no api key"})
    if verify._transport(api_key) != "openrouter":
        # Tool calling here is the OpenAI wire format. A Google key reaches the
        # same model through a different shape, so the agent cannot run on it.
        return _abstain(record, "agent needs an OpenRouter key; this key is not one", 0.0,
                        steps, session, {"agent_unavailable": "wrong transport"})

    phones, addrs = _stored_summary(record)
    user = (
        f"Organisation: {record.get('name', '').strip()}\n"
        f"Official website (the only domain you may fetch): {website}\n"
        f"Stored phone numbers: {phones or 'none on file'}\n"
        f"Stored addresses: {addrs or 'none on file'}\n"
        f"Stored status: {record.get('status')}\n"
        f"Last verified: {record.get('verified_at') or 'never'}\n\n"
        "Investigate whether these stored values are still true on the "
        "organisation's own website, then call conclude()."
    )
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    usage = {"prompt_tokens": 0, "completion_tokens": 0}

    for step in range(max_steps):
        if session.out_of_time():
            return _abstain(
                record,
                f"Wall-clock budget of {DEADLINE_SECONDS:.0f}s exhausted after "
                f"{len(steps)} tool call(s) without a conclusion.",
                0.2, steps, session, {"elapsed_s": round(time.time() - started, 2)},
            )

        resp = _chat(messages, api_key, session.deadline - time.time())
        if resp is None:
            extra = {"elapsed_s": round(time.time() - started, 2)}
            # A first-call failure means we never reached the model at all - a
            # dead key, a quota wall, no network. That is not an abstention, it
            # is the agent being unavailable, and the caller should run the
            # deterministic path instead of recording "no verdict" on every
            # listing in the run. A failure mid-investigation is different: we
            # have partial evidence and a trace, so we abstain and keep it.
            if step == 0:
                extra["agent_unavailable"] = "model call failed"
            return _abstain(
                record, f"Model call failed at step {step + 1}; no verdict reached.",
                0.0, steps, session, extra,
            )
        for k in usage:
            usage[k] += (resp.get("usage") or {}).get(k, 0) or 0

        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = msg.get("tool_calls") or []

        if not calls:
            # It answered in prose instead of calling conclude(). Nudge once;
            # persistent prose runs out the step budget and abstains, which is
            # the correct outcome for a model that won't use its own tools.
            messages.append({"role": "assistant", "content": msg.get("content") or ""})
            messages.append({
                "role": "user",
                "content": "Use the tools. When you are ready, call conclude(). Do not answer in prose.",
            })
            steps.append({"step": len(steps) + 1, "tool": None,
                          "note": "model replied in prose; re-prompted to use tools"})
            continue

        messages.append({
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": calls,
        })

        for call in calls:
            fn = (call.get("function") or {})
            name = fn.get("name")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}

            if name == "conclude":
                steps.append({"step": len(steps) + 1, "tool": "conclude", "args": args})
                result = _finalise(record, session, args, steps)
                result["elapsed_s"] = round(time.time() - started, 2)
                result["usage"] = usage
                return result

            handler = {"fetch_page": session.fetch_page,
                       "find_in_page": session.find_in_page,
                       "list_links": session.list_links}.get(name)
            if handler is None:
                observation = f"ERROR: no such tool {name!r}."
            else:
                try:
                    observation = handler(**args)
                except Exception as e:  # a tool bug must not end the run
                    observation = f"ERROR: tool {name} failed: {type(e).__name__}: {e}"

            steps.append({
                "step": len(steps) + 1, "tool": name, "args": args,
                "result_preview": observation[:200],
                "result_chars": len(observation),
            })
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "name": name,
                "content": observation,
            })

        if len(steps) >= max_steps:
            break

    # Budget gone with no conclude(). Ask once for a verdict on what it has;
    # forcing the tool means the answer comes back in the same shape as any
    # other conclusion rather than as prose we would have to parse.
    messages.append({
        "role": "user",
        "content": ("Your tool budget is spent. Call conclude() now with what you have. "
                    "If the evidence is thin, abstain - that is the right answer here."),
    })
    resp = _chat(messages, api_key, session.deadline - time.time(),
                 tool_choice={"type": "function", "function": {"name": "conclude"}})
    if resp:
        for k in usage:
            usage[k] += (resp.get("usage") or {}).get(k, 0) or 0
        calls = ((resp.get("choices") or [{}])[0].get("message") or {}).get("tool_calls") or []
        for call in calls:
            if (call.get("function") or {}).get("name") == "conclude":
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                steps.append({"step": len(steps) + 1, "tool": "conclude", "args": args,
                              "note": "forced at budget exhaustion"})
                result = _finalise(record, session, args if isinstance(args, dict) else {}, steps)
                result["elapsed_s"] = round(time.time() - started, 2)
                result["usage"] = usage
                return result

    out = _abstain(
        record,
        f"Agent used its budget of {max_steps} steps without reaching a conclusion.",
        0.2, steps, session,
    )
    out["elapsed_s"] = round(time.time() - started, 2)
    out["usage"] = usage
    return out


def _chat(messages, api_key, timeout, tool_choice=None):
    """One OpenRouter chat completion with tools. Returns the parsed body or None.

    OpenRouter only - the tool-calling wire format is OpenAI-shaped and the
    Google-direct transport in verify.call_model uses a different one. A Google
    key therefore gets no agent, and verify.py's AGENT=1 wiring falls back to the
    deterministic path rather than pretending otherwise.
    """
    if verify._transport(api_key) != "openrouter":
        return None
    body = {
        "model": MODEL,
        "messages": messages,
        "tools": TOOLS,
        "temperature": 0,
    }
    if tool_choice:
        body["tool_choice"] = tool_choice
    try:
        return verify._post_json(
            verify.OPENROUTER_URL,
            body,
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/FelixIsaac/darcel-watch",
                "X-Title": "SF Service Guide Watch",
            },
            timeout=max(5, min(60, timeout)),
        )
    except Exception:
        return None


if __name__ == "__main__":
    import sys

    key = os.environ.get("OPENROUTER_API_KEY")
    for rid in sys.argv[1:] or ["30"]:
        path = verify.DATA / f"{rid}.json"
        data = json.loads(path.read_text())
        rec = data.get("resource", data)
        print(json.dumps(investigate(rec, key), indent=2))
