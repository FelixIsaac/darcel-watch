"""Tests for site discovery. No network: every test drives pure functions.

The bugs these lock down are all ones we actually shipped, not hypotheticals.
Each test names the failure it prevents.
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import discover  # noqa: E402


# --------------------------------------------------------------------------
# registered_domain / same_site - the SSRF allowlist
# --------------------------------------------------------------------------

def test_registered_domain_basic():
    assert discover.registered_domain("https://bfwc.org/get-help/") == "bfwc.org"
    assert discover.registered_domain("http://www.example.com") == "example.com"
    assert discover.registered_domain("sub.deep.example.com") == "example.com"
    assert discover.registered_domain("") == ""
    assert discover.registered_domain(None) == ""


def test_same_site_rejects_other_domains():
    """The guard that stops the crawler wandering off a nonprofit's site."""
    assert discover.same_site("https://bfwc.org/contact/", "https://bfwc.org/")
    assert discover.same_site("https://www.bfwc.org/x", "https://bfwc.org/")
    assert not discover.same_site("https://evil.test/x", "https://bfwc.org/")
    # The classic prefix trick: bfwc.org.evil.test must NOT pass.
    assert not discover.same_site("https://bfwc.org.evil.test/x", "https://bfwc.org/")


def test_same_site_rejects_internal_targets():
    """SSRF: a discovered link must never let us fetch loopback or metadata."""
    for bad in ("http://127.0.0.1/", "http://localhost/admin",
                "http://169.254.169.254/latest/meta-data/"):
        assert not discover.same_site(bad, "https://bfwc.org/")


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------

def _robots(disallow=(), sitemaps=()):
    return discover.Robots(sitemaps=list(sitemaps), disallow=list(disallow), fetched=True)


def test_robots_allows_respects_disallow_prefix():
    r = _robots(disallow=["/wp-admin/", "/private"])
    assert r.allows("https://x.test/get-help/")
    assert not r.allows("https://x.test/wp-admin/edit.php")
    assert not r.allows("https://x.test/private/notes")


def test_robots_default_allows_everything():
    assert discover.Robots().allows("https://x.test/anything")


# --------------------------------------------------------------------------
# sitemap parsing
# --------------------------------------------------------------------------

SITEMAP_INDEX = """<?xml version="1.0"?>
<sitemapindex><sitemap><loc><![CDATA[https://x.test/page-sitemap.xml]]></loc></sitemap>
<sitemap><loc>https://x.test/post-sitemap.xml</loc></sitemap></sitemapindex>"""

URLSET = """<?xml version="1.0"?>
<urlset>
 <url><loc><![CDATA[https://x.test/get-help/]]></loc><lastmod>2026-09-18T06:59:00+00:00</lastmod></url>
 <url><loc>https://x.test/contact/</loc><lastmod>2026-08-14</lastmod></url>
 <url><loc>https://x.test/no-date/</loc></url>
</urlset>"""


def test_parse_sitemap_index_returns_children_not_pages():
    children, pages = discover._parse_sitemap(SITEMAP_INDEX)
    assert children == ["https://x.test/page-sitemap.xml", "https://x.test/post-sitemap.xml"]
    assert pages == []


def test_parse_sitemap_urlset_strips_cdata_and_normalises_dates():
    children, pages = discover._parse_sitemap(URLSET)
    assert children == []
    assert pages == [
        ("https://x.test/get-help/", "2026-09-18"),   # W3C datetime -> date
        ("https://x.test/contact/", "2026-08-14"),
        ("https://x.test/no-date/", None),            # missing lastmod stays None
    ]


def test_norm_date_discards_junk():
    assert discover._norm_date("2026-09-18T06:59:00+00:00") == "2026-09-18"
    assert discover._norm_date("not a date") is None
    assert discover._norm_date(None) is None


# --------------------------------------------------------------------------
# ranking - the substring bug
# --------------------------------------------------------------------------

def test_segments_splits_on_slug_punctuation():
    assert discover._segments("/services/domestic-violence/") == \
        ["services", "domestic", "violence"]


def test_hint_matches_word_not_substring():
    """REGRESSION: "program" matched inside "jazzprogrambooks", floating a
    fundraiser page above /services/ for an eligibility question."""
    inv = [
        ("https://x.test/jazzprogrambooks/", "2026-05-12"),
        ("https://x.test/services/", "2026-09-15"),
    ]
    ranked = discover.rank_pages(inv, "eligibility", "https://x.test")
    assert ranked[0][0] == "https://x.test/services/"


def test_phone_ranking_prefers_get_help_over_about():
    """REGRESSION: the stored number for Building Futures lives on /get-help/.
    Ranking that put /about-2/ first fetched the wrong page and the pipeline
    came within one step of reporting a correct phone number as wrong."""
    inv = [
        ("https://x.test/about-2/", "2026-06-04"),
        ("https://x.test/get-help/", "2026-09-18"),
        ("https://x.test/contact/", "2026-08-14"),
    ]
    assert discover.rank_pages(inv, "phone", "https://x.test")[0][0] == \
        "https://x.test/get-help/"


def test_news_and_archive_pages_are_penalised():
    """Nonprofit sites publish news constantly, so event pages are often the
    FRESHEST pages. Recency alone must not float them to the top."""
    inv = [
        ("https://x.test/2026/09/gala-night/", "2026-09-19"),
        ("https://x.test/contact/", "2024-01-01"),
    ]
    assert discover.rank_pages(inv, "phone", "https://x.test")[0][0] == \
        "https://x.test/contact/"


def test_ranking_is_deterministic():
    inv = [("https://x.test/a/", None), ("https://x.test/b/", None)]
    assert discover.rank_pages(inv, "phone", "https://x.test") == \
        discover.rank_pages(inv, "phone", "https://x.test")


def test_empty_inventory_ranks_empty():
    assert discover.rank_pages([], "phone", "https://x.test") == []
