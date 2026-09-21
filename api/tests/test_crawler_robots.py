"""robots.txt handling."""

from __future__ import annotations

from api.crawler.robots import (
    DEFAULT_DELAY_SECONDS,
    MAX_HONOURED_DELAY_SECONDS,
    blocked_ai_crawlers,
    parse_robots,
)
from api.crawler.sitemaps import decode, extract_locations, is_index

ORIGIN = "https://example.com"


def test_disallow_rules_are_obeyed():
    policy = parse_robots(
        "User-agent: *\nDisallow: /admin\nDisallow: /cart\n", 200, ORIGIN
    )
    assert policy.allows("https://example.com/about")
    assert not policy.allows("https://example.com/admin/users")
    assert not policy.allows("https://example.com/cart")


def test_a_rule_aimed_at_us_specifically_is_obeyed():
    policy = parse_robots(
        "User-agent: VisibilityBot\nDisallow: /private\n\n"
        "User-agent: *\nDisallow:\n",
        200,
        ORIGIN,
    )
    assert not policy.allows("https://example.com/private/x")


def test_a_site_blocking_everything_is_detected_as_a_finding_not_a_failure():
    policy = parse_robots("User-agent: *\nDisallow: /\n", 200, ORIGIN)
    assert policy.blocks_everything is True


def test_crawl_delay_is_honoured_but_capped():
    """Obeying a 3600-second delay would take days over one site. Beyond the
    cap we stop obeying and tell the customer the crawl will be slow."""
    fast = parse_robots("User-agent: *\nCrawl-delay: 5\n", 200, ORIGIN)
    assert fast.delay_seconds == 5.0
    assert not fast.capped_delay

    absurd = parse_robots("User-agent: *\nCrawl-delay: 3600\n", 200, ORIGIN)
    assert absurd.delay_seconds == MAX_HONOURED_DELAY_SECONDS
    assert absurd.capped_delay


def test_a_delay_below_our_own_floor_does_not_speed_us_up():
    policy = parse_robots("User-agent: *\nCrawl-delay: 0\n", 200, ORIGIN)
    assert policy.delay_seconds == DEFAULT_DELAY_SECONDS


def test_sitemaps_are_collected_from_robots():
    policy = parse_robots(
        "Sitemap: https://example.com/sitemap.xml\n"
        "User-agent: *\nDisallow:\n"
        "SITEMAP: https://example.com/news-sitemap.xml\n",
        200,
        ORIGIN,
    )
    assert policy.sitemaps == [
        "https://example.com/sitemap.xml",
        "https://example.com/news-sitemap.xml",
    ]


def test_a_missing_robots_means_allow_but_records_that_we_never_saw_one():
    policy = parse_robots("", 404, ORIGIN)
    assert policy.allows("https://example.com/anything")
    assert policy.fetched is False


def test_ai_crawler_blocks_are_reported():
    """A finding, not a politeness concern: a site blocking these is invisible
    to AI answer engines, which is one of the things this product measures."""
    blocked = blocked_ai_crawlers(
        "User-agent: GPTBot\nDisallow: /\n\n"
        "User-agent: Google-Extended\nDisallow: /\n\n"
        "User-agent: *\nDisallow:\n"
    )
    assert set(blocked) == {"GPTBot", "Google-Extended"}
    assert blocked_ai_crawlers("User-agent: *\nDisallow:\n") == []


def test_sitemap_locations_are_extracted_including_cdata():
    xml = """<?xml version="1.0"?><urlset>
      <url><loc>https://example.com/a</loc></url>
      <url><loc><![CDATA[https://example.com/b]]></loc></url>
    </urlset>"""
    assert extract_locations(xml) == ["https://example.com/a", "https://example.com/b"]


def test_a_sitemap_index_is_recognised():
    assert is_index('<?xml version="1.0"?><sitemapindex><sitemap>')
    assert not is_index('<?xml version="1.0"?><urlset><url>')


def test_a_gzipped_sitemap_is_decoded_even_without_the_extension():
    import gzip

    body = gzip.compress(b"<urlset><url><loc>https://example.com/a</loc></url></urlset>")
    assert "https://example.com/a" in decode(body, "https://example.com/sitemap")
