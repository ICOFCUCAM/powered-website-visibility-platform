"""URL normalisation, scope and trap detection."""

from __future__ import annotations

import pytest

from api.crawler.urls import ScopeRule, looks_like_a_trap, normalise


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com/about", "https://example.com/about"),
        ("https://EXAMPLE.com/about", "https://example.com/about"),
        ("https://example.com:443/about", "https://example.com/about"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com//a///b", "https://example.com/a/b"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com/a?utm_source=x&id=2", "https://example.com/a?id=2"),
        ("https://example.com/a?b=2&a=1", "https://example.com/a?a=1&b=2"),
        ("https://example.com", "https://example.com/"),
    ],
)
def test_the_same_page_normalises_to_one_url(raw, expected):
    assert normalise(raw) == expected


def test_tracking_parameters_are_stripped_so_a_page_is_fetched_once():
    """A campaign link pointing at /about is still /about."""
    campaign = (
        "https://example.com/about?utm_source=news"
        "&utm_campaign=spring&fbclid=z"
    )
    assert normalise(campaign) == "https://example.com/about"


def test_a_trailing_slash_is_preserved():
    """On many sites /a and /a/ genuinely differ. The canonical tag resolves
    it, not a guess here."""
    assert normalise("https://example.com/a/") == "https://example.com/a/"
    assert normalise("https://example.com/a") == "https://example.com/a"


@pytest.mark.parametrize(
    "raw",
    [
        "javascript:void(0)", "mailto:a@b.com", "tel:+44",
        "#top", "ftp://example.com/x", "",
    ],
)
def test_things_that_are_not_fetchable_pages_are_dropped(raw):
    assert normalise(raw) is None


def test_relative_links_resolve_against_the_page_they_were_found_on():
    base = "https://example.com/shop/category/"
    assert normalise("../item", base=base) == "https://example.com/shop/item"
    assert normalise("/top", base=base) == "https://example.com/top"


def test_scope_covers_www_and_apex_but_not_subdomains_by_default():
    """A blog on its own subdomain is usually a different site with its own
    Search Console property; crawling it spends the customer's page budget on
    pages they did not ask about."""
    scope = ScopeRule(host="example.com")
    assert scope.covers("https://example.com/a")
    assert scope.covers("https://www.example.com/a")
    assert not scope.covers("https://blog.example.com/a")
    assert not scope.covers("https://example.com.evil.net/a")

    wide = ScopeRule(host="example.com", include_subdomains=True)
    assert wide.covers("https://blog.example.com/a")
    assert not wide.covers("https://example.com.evil.net/a")


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://example.com/photo.jpg", "non_html"),
        ("https://example.com/brochure.pdf", "non_html"),
        ("https://example.com/app.js", "non_html"),
        ("https://example.com/shop/shop/shop/item", "repeating_segments"),
        ("https://example.com/" + "a/" * 20, "path_too_deep"),
        ("https://example.com/events/calendar/2027/03", "infinite_space"),
        ("https://example.com/s?a=1&b=2&c=3&d=4&e=5&f=6&g=7", "too_many_parameters"),
    ],
)
def test_traps_are_refused_with_a_reason(url, reason):
    assert looks_like_a_trap(url) == reason


def test_an_ordinary_page_is_not_a_trap():
    assert looks_like_a_trap("https://example.com/services/counselling") is None
    assert looks_like_a_trap("https://example.com/blog?page=2") is None
