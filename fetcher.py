"""Tiered page fetching: cheap direct GET first, reader service only when needed.

The direct path (urllib + tag-strip) is the same one verify.py has always used.
It is free, unlimited and works on most nonprofit sites. It also returns zero
usable text on anything rendered client-side, which is a large and growing
share of the corpus - sf.gov's location pages yield 0 characters, calyouth.org
fails the connection outright. Those are exactly the listings we most want to
check, and they are the ones we have been abstaining on.

So: if the direct path yields nothing worth reading, fall through to
r.jina.ai, which renders the page and returns markdown. Keyless, ~20 req/min,
no auth. Measured on the two sites above:

    https://www.sf.gov/location--omi-family-center   direct 0 chars    reader 21,710
    http://calyouth.org/                             direct FAILED     reader  5,224

Every result carries the method that produced it ("direct" or "reader") so a
reviewer can see whether a finding rests on the raw HTML or on a third party's
rendering of it. That distinction matters when the finding is wrong.

Cloudflare Browser Rendering's /markdown endpoint is the paid swap-in at scale;
it slots in as another branch of fetch() behind the same (text, method) contract.

Stdlib only. Never raises - callers get (None, reason).
"""

import hashlib
import json
import os
import pathlib
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from verify import text_from_html

HREF_RE = re.compile(r'<a\b[^>]*\bhref\s*=\s*["\']([^"\'#][^"\']*)["\']', re.I)
# Reader output is markdown, so its links look like [label](url) instead.
MD_LINK_RE = re.compile(r"\]\((https?://[^)\s]+)\)")

CACHE = pathlib.Path(__file__).parent / "cache"
UA = {"User-Agent": "sfsg-watch/0.3 (Hack for Humanity SF; read-only)"}
TIMEOUT = 12
READER_TIMEOUT = 45  # it renders the page for us, so it is slow by design
READER = "https://r.jina.ai/"

# Below this, the "text" is a nav bar and a cookie banner - a JS shell, not a page.
MIN_USEFUL_CHARS = 200
# Keyless r.jina.ai allows ~20 req/min. One per 3s process-wide keeps us under it
# even with verify_many's thread pool fanning out.
READER_MIN_INTERVAL = 3.0

_reader_lock = threading.Lock()
_reader_last = 0.0


def _cache_path(url, method):
    h = hashlib.sha256(f"{method}:{url}".encode()).hexdigest()[:24]
    return CACHE / f"{h}.json"


def _cache_read(url, method):
    p = _cache_path(url, method)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _cache_write(url, method, text, error=None, links=None):
    try:
        CACHE.mkdir(exist_ok=True)
        _cache_path(url, method).write_text(json.dumps(
            {"url": url, "method": method, "text": text, "error": error,
             "links": links or [], "at": time.time()}
        ))
    except OSError:
        pass  # a read-only disk must not break a run


def _throttle():
    """One reader request per READER_MIN_INTERVAL, across all threads."""
    global _reader_last
    with _reader_lock:
        wait = READER_MIN_INTERVAL - (time.time() - _reader_last)
        if wait > 0:
            time.sleep(wait)
        _reader_last = time.time()


def _links_from_html(raw, base):
    """Absolute hrefs, deduped, order preserved. Stripped tags lose these, and
    without them the agent's list_links tool would only ever see bare URLs
    printed in body copy."""
    out, seen = [], set()
    for href in HREF_RE.findall(raw):
        if href.lower().startswith(("mailto:", "tel:", "javascript:", "data:")):
            continue
        absolute = urllib.parse.urljoin(base, href)
        if absolute.startswith(("http://", "https://")) and absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out[:200]


def plain_fetch(url):
    """Direct GET + tag strip. Returns (text, None) or (None, reason).

    Tolerates non-200: some sites 403 a bot user-agent and still serve the body.
    """
    cached = _cache_read(url, "direct")
    if cached is not None:
        return cached["text"], cached["error"]
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read(500_000)  # reading for evidence, not archiving
            charset = r.headers.get_content_charset() or "utf-8"
            final_url = r.geturl()  # resolve relative hrefs against redirects
        raw = body.decode(charset, errors="replace")
        text = text_from_html(raw)
        _cache_write(url, "direct", text, None, _links_from_html(raw, final_url))
        return text, None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        reason = f"direct fetch failed: {type(e).__name__}: {e}"
        _cache_write(url, "direct", None, reason)
        return None, reason


def reader_fetch(url):
    """r.jina.ai renders the page and hands back markdown. (text, None) or (None, reason)."""
    cached = _cache_read(url, "reader")
    if cached is not None:
        return cached["text"], cached["error"]
    target = READER + url
    try:
        _throttle()
        req = urllib.request.Request(target, headers=UA)
        with urllib.request.urlopen(req, timeout=READER_TIMEOUT) as r:
            body = r.read(500_000)
            charset = r.headers.get_content_charset() or "utf-8"
        text = body.decode(charset, errors="replace").strip()
        links, seen = [], set()
        for u in MD_LINK_RE.findall(text):
            if u not in seen:
                seen.add(u)
                links.append(u)
        _cache_write(url, "reader", text, None, links[:200])
        return text, None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        reason = f"reader fetch failed: {type(e).__name__}: {e}"
        _cache_write(url, "reader", None, reason)
        return None, reason


def render_fetch(url):
    """Best available rendering of a page: reader if it returns more, else direct.

    fetch() only escalates when the direct tier returns almost nothing, which is
    the right trade for a per-listing budget but wrong when the question is "is
    this text REALLY absent from the site". A page that serves 500 characters of
    nav chrome and renders its address in JavaScript passes fetch()'s threshold
    and then appears to mention nothing - which is how a corpus scan concluded
    First Friendship Baptist's website never prints "501 Steiner", a line the
    agent had already quoted from that very page. Absence is only evidence once
    we have tried our best to render.
    """
    direct, _ = plain_fetch(url)
    rendered, _ = reader_fetch(url)
    if rendered and (not direct or len(rendered) > len(direct)):
        return rendered, "reader"
    if direct:
        return direct, "direct"
    return None, "no readable text from either tier"


def links_for(url, method):
    """Links harvested during the fetch that produced (url, method)."""
    cached = _cache_read(url, method)
    return (cached or {}).get("links") or []


def fetch(url):
    """Direct first, reader as fallback. Returns (text, method) - method is
    "direct", "reader", or, when both fail, None with the reason in text's slot.

    Return shape is deliberately (value, label) both ways round so a caller that
    ignores the second element still can't mistake a failure for a page.
    """
    text, err = plain_fetch(url)
    if text and len(text) >= MIN_USEFUL_CHARS:
        return text, "direct"

    if os.environ.get("DW_NO_READER") == "1":
        if text:
            return text, "direct"
        return None, err or "direct fetch returned too little text (reader disabled)"

    # Either the fetch failed or what came back was a shell. Both are the
    # reader's job. The escalation is recorded in the trace rather than left as
    # a silent implementation detail - a finding that rests on a third party's
    # rendering should say so.
    rtext, rerr = reader_fetch(url)
    if rtext and len(rtext) >= MIN_USEFUL_CHARS:
        return rtext, "reader"

    if text:  # short but non-empty direct text beats nothing at all
        return text, "direct"
    return None, rerr or err or "no readable text from either tier"
