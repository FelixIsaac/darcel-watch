"""Find out what pages a site actually has, instead of guessing.

The version this replaces had a module-level constant:

    SUBPAGES = ["/contact", "/about", "/hours", "/visit", "/locations"]

Five guesses, tried against every organisation in the directory. It is hard to
overstate how bad that is. It 404s on most sites, it never finds the page that
matters, and - the part that actually caused damage - when the guesses miss, the
absence of evidence looks exactly like evidence of absence.

That is not hypothetical. Auditing Building Futures (a domestic violence
service), the guesses found /contact/ and two 404s. The stored number
510-808-7410 was on none of them, and the pipeline was one step from reporting
it as wrong. The site's sitemap lists /get-help/ - modified two days earlier -
where that number is published. The number was fine. The crawler was broken.

So: ask the site what it has.

    1. robots.txt        - sites DECLARE their sitemaps here. Also tells us
                           what not to touch, which we honour.
    2. sitemap(s)        - the site's own index of itself, usually with
                           <lastmod>. Recurses through sitemapindex, handles
                           gzip.
    3. link-graph BFS    - for the ~19% with no sitemap. Breadth-first from the
                           homepage, same registered domain, bounded depth.

Measured on 60 random organisation websites from the live corpus:

    sitemap found            49/60  (81%)   - 32 declared in robots.txt
    carrying <lastmod>       39/60  (65%)
    no sitemap               11/60  (19%)   - these fall through to BFS

<lastmod> is worth more than a cheaper route to the same pages. It is the
site's own statement about when a page last changed, which is a source-side
freshness signal. Everything else in this project infers freshness from
ShelterTech's `updated_at` - when the DIRECTORY changed. This is when the
SOURCE changed. See freshness.py for how the two differ.

Read-only throughout: GET requests, no forms, no auth, no cookies.
"""

from __future__ import annotations

import concurrent.futures as cf
import dataclasses
import gzip
import io
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

UA = {
    "User-Agent": (
        "sfsg-watch/0.3 (+https://github.com/FelixIsaac/darcel-watch) "
        "civic-directory-freshness-audit; read-only"
    )
}

# Budgets. A civic directory audit has no business hammering a nonprofit's
# website, and an unbounded crawler on 739 distinct domains is a denial of
# service with extra steps.
FETCH_TIMEOUT = 15
MAX_SITEMAPS = 25          # sitemapindex fan-out
MAX_URLS = 3000            # per site
MAX_BFS_PAGES = 12         # fallback crawl, when there is no sitemap
MAX_BFS_DEPTH = 2
MAX_BYTES = 4_000_000      # per response, before decompression

SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/sitemap.xml.gz")

_CDATA_RE = re.compile(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", re.S)
_LOC_RE = re.compile(r"<loc>(.*?)</loc>", re.S | re.I)
_URL_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.S | re.I)
_SITEMAP_BLOCK_RE = re.compile(r"<sitemap>(.*?)</sitemap>", re.S | re.I)
_LASTMOD_RE = re.compile(r"<lastmod>(.*?)</lastmod>", re.S | re.I)
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def registered_domain(url_or_host: str) -> str:
    """Last two labels of the hostname, lowercased.

    Deliberately the same crude rule agent.py uses, and deliberately NOT a
    public-suffix lookup: this is an allowlist check, and the failure mode of
    being too strict (refusing bfwc.org -> cdn.bfwc.org) is a missed fetch,
    while the failure mode of being too loose is fetching somebody else's site.
    Prefer the miss.
    """
    s = (url_or_host or "").strip()
    if not s:
        return ""
    if "//" not in s:
        s = "//" + s
    host = (urllib.parse.urlsplit(s).hostname or "").lower().strip(".")
    if not host:
        return ""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def same_site(url: str, website: str) -> bool:
    allowed = registered_domain(website)
    return bool(allowed) and registered_domain(url) == allowed


def _cdata(s: str) -> str:
    m = _CDATA_RE.match(s or "")
    return (m.group(1) if m else (s or "")).strip()


def _norm_date(s: str | None) -> str | None:
    """Sitemaps carry W3C datetimes, ISO dates, and occasionally junk. We only
    ever compare these by day, so keep YYYY-MM-DD and discard the rest."""
    if not s:
        return None
    m = _DATE_RE.search(s)
    return m.group(1) if m else None


def _get(url: str, timeout: int = FETCH_TIMEOUT) -> bytes | None:
    """One GET. Returns body bytes, transparently gunzipped, or None."""
    try:
        req = urllib.request.Request(url, headers=UA, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(MAX_BYTES)
            enc = (r.headers.get("Content-Encoding") or "").lower()
        if url.endswith(".gz") or enc == "gzip" or raw[:2] == b"\x1f\x8b":
            try:
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read(MAX_BYTES)
            except OSError:
                pass  # mislabelled; treat as plain
        return raw
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None


def _text(raw: bytes | None) -> str:
    return raw.decode("utf-8", "ignore") if raw else ""


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Robots:
    """What the site asked crawlers to do.

    `disallow` holds only the `User-agent: *` group. We do not look for a group
    naming us, because we do not want site owners to have to know our name to
    be respected.
    """
    sitemaps: list[str] = dataclasses.field(default_factory=list)
    disallow: list[str] = dataclasses.field(default_factory=list)
    fetched: bool = False

    def allows(self, url: str) -> bool:
        path = urllib.parse.urlsplit(url).path or "/"
        # Longest-match wins is the real rule; for Disallow-only groups a
        # simple prefix test is equivalent and much easier to audit.
        return not any(path.startswith(rule) for rule in self.disallow)


def read_robots(origin: str) -> Robots:
    body = _text(_get(urllib.parse.urljoin(origin, "/robots.txt"), timeout=10))
    if not body:
        return Robots()
    out = Robots(fetched=True)
    applies = False
    for line in body.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            applies = value == "*"
        elif field == "sitemap" and value:
            out.sitemaps.append(value)
        elif field == "disallow" and applies and value and value != "/":
            # A bare "Disallow: /" locks out the whole site. We honour it by
            # returning no pages rather than by adding a rule that silently
            # filters everything - see discover(), which checks for it.
            out.disallow.append(value)
        elif field == "disallow" and applies and value == "/":
            out.disallow.append("/")
    return out


# --------------------------------------------------------------------------
# sitemaps
# --------------------------------------------------------------------------

def _parse_sitemap(body: str) -> tuple[list[str], list[tuple[str, str | None]]]:
    """Return (child sitemap URLs, [(page url, lastmod)]).

    A <sitemapindex> nests sitemaps; a <urlset> holds pages. Some sites emit
    both in one document, so we look for each independently rather than
    branching on the root tag.
    """
    children = [
        _cdata(m.group(1))
        for block in _SITEMAP_BLOCK_RE.findall(body)
        for m in _LOC_RE.finditer(block)
    ]
    pages: list[tuple[str, str | None]] = []
    for block in _URL_BLOCK_RE.findall(body):
        loc = _LOC_RE.search(block)
        if not loc:
            continue
        lm = _LASTMOD_RE.search(block)
        pages.append((_cdata(loc.group(1)), _norm_date(_cdata(lm.group(1)) if lm else None)))

    # Fallback for malformed documents with <loc> but no <url>/<sitemap>
    # wrappers - rare, but they exist and they are still useful.
    if not children and not pages:
        pages = [(_cdata(m.group(1)), None) for m in _LOC_RE.finditer(body)]
    return children, pages


def crawl_sitemaps(origin: str, declared: list[str], website: str) -> list[tuple[str, str | None]]:
    """Walk declared sitemaps, then standard paths, recursing through indexes."""
    queue = deque(declared or [])
    for p in SITEMAP_PATHS:
        queue.append(urllib.parse.urljoin(origin, p))

    seen_sitemaps: set[str] = set()
    pages: dict[str, str | None] = {}

    while queue and len(seen_sitemaps) < MAX_SITEMAPS and len(pages) < MAX_URLS:
        sm = queue.popleft()
        if sm in seen_sitemaps or not same_site(sm, website):
            continue
        seen_sitemaps.add(sm)
        body = _text(_get(sm))
        if "<loc" not in body.lower():
            continue
        children, found = _parse_sitemap(body)
        for c in children:
            if c not in seen_sitemaps:
                queue.append(c)
        for url, lm in found:
            if not same_site(url, website):
                continue
            # Keep the newest lastmod if a URL appears in several sitemaps.
            prev = pages.get(url)
            if url not in pages or (lm and (not prev or lm > prev)):
                pages[url] = lm
            if len(pages) >= MAX_URLS:
                break
    return sorted(pages.items())


# --------------------------------------------------------------------------
# link-graph fallback
# --------------------------------------------------------------------------

def crawl_links(origin: str, website: str, robots: Robots) -> list[tuple[str, str | None]]:
    """Breadth-first from the homepage. For the ~19% with no sitemap.

    No lastmod is available this way - that information simply does not exist
    outside a sitemap or a Last-Modified header, and most CMSes do not send a
    meaningful one for generated pages. Callers must treat a None lastmod as
    "unknown", never as "old".
    """
    seen = {origin}
    out: list[tuple[str, str | None]] = [(origin, None)]
    frontier = [(origin, 0)]

    while frontier and len(out) < MAX_BFS_PAGES:
        url, depth = frontier.pop(0)
        if depth >= MAX_BFS_DEPTH:
            continue
        body = _text(_get(url))
        if not body:
            continue
        for href in _HREF_RE.findall(body):
            if href.lower().startswith(("mailto:", "tel:", "javascript:", "data:", "#")):
                continue
            nxt = urllib.parse.urldefrag(urllib.parse.urljoin(url, href)).url
            if nxt in seen or not nxt.startswith(("http://", "https://")):
                continue
            if not same_site(nxt, website) or not robots.allows(nxt):
                continue
            seen.add(nxt)
            out.append((nxt, None))
            frontier.append((nxt, depth + 1))
            if len(out) >= MAX_BFS_PAGES:
                break
    return out


# --------------------------------------------------------------------------
# ranking
# --------------------------------------------------------------------------

# Which URL slugs plausibly answer which kind of question, MOST SPECIFIC FIRST -
# position in the tuple is meaningful, earlier hints score higher. Deterministic
# and free, so the shortlist is built before any model call is made.
#
# These are keyword priors, not truth. They shortlist; they never decide.
SLUG_HINTS = {
    "phone": ("hotline", "crisis", "get-help", "gethelp", "contact", "reach",
              "connect", "call", "help", "about"),
    "address": ("location", "locations", "visit", "directions", "find-us",
                "contact", "where", "office"),
    "schedule": ("hours", "schedule", "when", "contact", "visit", "location"),
    "services": ("services", "service", "programs", "program", "what-we-do",
                 "our-work", "resources", "help"),
    "eligibility": ("eligibility", "who-we-serve", "qualify", "requirements",
                    "apply", "application", "services", "service", "programs",
                    "program"),
    "status": (),  # homepage first; closure notices live on the front page
}

# Slugs that are almost never the authoritative statement about an organisation:
# news posts, event pages, fundraisers, staff bios. They are often the FRESHEST
# pages on a nonprofit site, which is exactly why they need an explicit penalty -
# otherwise recency alone floats them to the top.
_NOISE_RE = re.compile(
    r"(19|20)\d{2}|category|tag|author|page/\d+|news|blog|event|gala|drive|"
    r"benefit|donate|volunteer|jazz|holiday|newsletter|press|story|stories|"
    r"testimonial|gallery|job|career|employment|financial|annual-report"
)

_SEG_SPLIT_RE = re.compile(r"[/\-_.]+")


def _segments(path: str) -> list[str]:
    """Path split into slug words, so a hint matches a WORD not a substring.

    Without this, "program" matches inside "jazzprogrambooks" and a fundraiser
    page outranks /services/ for an eligibility question. That was a real bug,
    caught on bfwc.org.
    """
    return [s for s in _SEG_SPLIT_RE.split(path.lower()) if s]


def rank_pages(inventory: list[tuple[str, str | None]], field: str, origin: str,
               limit: int = 8) -> list[tuple[str, str | None]]:
    """Order the inventory by how likely a page is to answer about `field`.

    Scoring, highest first:
      +12..+6  a slug WORD matches a hint, weighted by hint specificity
      + 5      homepage - small sites put everything on one page
      + 0..3   recently modified, when the sitemap says so (tiebreak only)
      - 7      news/event/archive shaped URLs
      - 1.5    per level of depth beyond two

    Freshness is deliberately a weak term. A stale /contact/ is a better
    witness to a phone number than a fresh /gala-night/, and letting recency
    dominate is how a crawler ends up reading a fundraiser page.
    """
    hints = SLUG_HINTS.get(field, ())
    scored = []
    for url, lastmod in inventory:
        path = (urllib.parse.urlsplit(url).path or "/").lower()
        segs = set(_segments(path))
        score = 0.0

        for i, hint in enumerate(hints):
            # Hints may themselves be multi-word ("get-help", "who-we-serve").
            parts = set(_segments(hint))
            if parts and parts <= segs:
                score += 12 - min(i, 6)
                break

        depth = len([p for p in path.split("/") if p])
        if depth == 0:
            score += 5
        elif depth == 1:
            score += 1
        score -= 1.5 * max(0, depth - 2)

        if _NOISE_RE.search(path):
            score -= 7

        if lastmod:
            try:
                age = (time.time() - time.mktime(time.strptime(lastmod, "%Y-%m-%d"))) / 86400
                score += 3 if age < 180 else (1.5 if age < 730 else 0)
            except ValueError:
                pass

        scored.append((score, url, lastmod))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [(u, lm) for s, u, lm in scored[:limit]]


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Inventory:
    website: str
    origin: str
    pages: list[tuple[str, str | None]]
    method: str                 # "sitemap" | "links" | "none" | "robots-denied"
    robots: Robots
    newest_lastmod: str | None  # source-side freshness for the whole site

    def __len__(self) -> int:
        return len(self.pages)

    def top(self, field: str, limit: int = 8) -> list[tuple[str, str | None]]:
        return rank_pages(self.pages, field, self.origin, limit)

    def as_dict(self) -> dict:
        return {
            "website": self.website,
            "method": self.method,
            "pages": len(self.pages),
            "with_lastmod": sum(1 for _, lm in self.pages if lm),
            "newest_lastmod": self.newest_lastmod,
            "robots_txt": self.robots.fetched,
            "robots_disallow": len(self.robots.disallow),
        }


def discover(website: str) -> Inventory:
    """What pages does this site have, and when did each last change?

    Never raises: a dead site returns an empty Inventory. Callers are expected
    to abstain on an empty inventory, not to conclude anything from it.
    """
    if not website or not website.startswith(("http://", "https://")):
        return Inventory(website or "", "", [], "none", Robots(), None)

    sp = urllib.parse.urlsplit(website)
    origin = f"{sp.scheme}://{sp.netloc}"
    robots = read_robots(origin)

    if "/" in robots.disallow:
        # The site asked everyone to stay out. Honour it and report honestly.
        return Inventory(website, origin, [], "robots-denied", robots, None)

    pages = crawl_sitemaps(origin, robots.sitemaps, website)
    method = "sitemap"
    if not pages:
        pages = crawl_links(origin, website, robots)
        method = "links" if len(pages) > 1 else "none"

    pages = [(u, lm) for u, lm in pages if robots.allows(u)]
    newest = max((lm for _, lm in pages if lm), default=None)
    return Inventory(website, origin, pages, method, robots, newest)


def discover_many(websites, workers: int = 8) -> dict[str, Inventory]:
    """Concurrent discovery. One worker pool across DIFFERENT domains - we
    never issue parallel requests to the same host."""
    uniq = [w for w in dict.fromkeys(websites) if w]
    out: dict[str, Inventory] = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for site, inv in zip(uniq, ex.map(discover, uniq)):
            out[site] = inv
    return out


if __name__ == "__main__":
    import json
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "https://bfwc.org/"
    inv = discover(target)
    print(json.dumps(inv.as_dict(), indent=2))
    for field in ("phone", "eligibility", "schedule"):
        print(f"\n  best pages for {field!r}:")
        for url, lm in inv.top(field, 5):
            print(f"    {lm or '    -     '}  {url}")
