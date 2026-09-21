"""Auto-matching a Google property to a website.

Pure logic, so these are the cheap tests that carry most of the wizard's
promise: "we found yours" instead of a list of 47 properties.
"""

from __future__ import annotations

import pytest

from api.hub.services.matching import (
    MatchQuality,
    matched_hosts_for_search_console,
    rank_search_console_matches,
    score_analytics_property,
    score_search_console_property,
)


@pytest.mark.parametrize(
    ("property_uri", "expected"),
    [
        ("sc-domain:example.com", ["example.com"]),
        ("https://www.example.com/", ["www.example.com"]),
        ("http://example.com/", ["example.com"]),
        ("https://example.com:443/", ["example.com"]),
    ],
)
def test_normalises_a_property_to_the_hosts_it_concerns(property_uri, expected):
    assert matched_hosts_for_search_console(property_uri) == expected


@pytest.mark.parametrize(
    ("website", "property_uri", "quality"),
    [
        # A domain property covers everything, which is why it outranks.
        ("example.com", "sc-domain:example.com", MatchQuality.DOMAIN_PROPERTY),
        ("www.example.com", "sc-domain:example.com", MatchQuality.DOMAIN_PROPERTY),
        ("blog.example.com", "sc-domain:example.com", MatchQuality.DOMAIN_PROPERTY),
        # A URL-prefix property covers one host and one scheme.
        ("example.com", "https://www.example.com/", MatchQuality.URL_PREFIX_HOST),
        ("example.com", "http://example.com/", MatchQuality.URL_PREFIX_HOST),
        # Related but not the same website.
        ("example.com", "https://blog.example.com/", MatchQuality.SUBDOMAIN),
        # Not ours at all — including the classic suffix spoof.
        ("example.com", "sc-domain:notexample.com", MatchQuality.NONE),
        ("example.com", "sc-domain:example.com.evil.net", MatchQuality.NONE),
        ("example.com", "https://example.com.evil.net/", MatchQuality.NONE),
    ],
)
def test_scores_each_property_shape(website, property_uri, quality):
    assert score_search_console_property(website, property_uri).quality == quality


def test_a_domain_property_is_preferred_over_a_url_prefix_one():
    """Both match. The domain property keeps working when the customer moves
    from www to apex or adds HTTPS, so it is the one to pre-tick."""
    ranked = rank_search_console_matches(
        "example.com",
        [
            {"siteUrl": "https://www.example.com/", "permissionLevel": "siteOwner"},
            {"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"},
        ],
    )
    assert [m.property_uri for m in ranked] == [
        "sc-domain:example.com",
        "https://www.example.com/",
    ]


def test_a_property_the_user_cannot_read_is_not_offered():
    """siteUnverifiedUser sees the property listed and nothing else. Offering
    it produces a connection that returns no data and no explanation."""
    ranked = rank_search_console_matches(
        "example.com",
        [
            {"siteUrl": "sc-domain:example.com", "permissionLevel": "siteUnverifiedUser"},
            {"siteUrl": "https://example.com/", "permissionLevel": "siteFullUser"},
        ],
    )
    assert [m.property_uri for m in ranked] == ["https://example.com/"]


def test_matching_is_not_ownership():
    """A URL-prefix property matches the website by host while proving nothing
    about another scheme. Ownership is decided separately, in SQL."""
    match = score_search_console_property(
        "example.com", "https://www.example.com/", "siteRestrictedUser"
    )
    assert match.quality == MatchQuality.URL_PREFIX_HOST
    assert match.is_usable          # we can read its data
    assert not match.proves_ownership  # but it is not proof of ownership


def test_analytics_matches_through_its_web_streams():
    """A GA4 property has no hostname of its own; the stream URL is the only
    link back to a website."""
    match = score_analytics_property(
        "example.com",
        "properties/123",
        ["https://www.example.com", "https://shop.example.com"],
        "Example — GA4",
    )
    assert match.quality == MatchQuality.URL_PREFIX_HOST


def test_analytics_without_streams_never_auto_matches():
    match = score_analytics_property("example.com", "properties/999", [], "GA4")
    assert match.quality == MatchQuality.NONE
