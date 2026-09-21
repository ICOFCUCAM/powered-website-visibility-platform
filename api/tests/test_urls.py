"""URL normalisation (V1 spec s13, step 1).

Pure domain logic: no database, no network. The point of these cases is that a
non-technical user should never have to know what any of them mean.
"""

from __future__ import annotations

import pytest

from api.domain.errors import InvalidWebsite
from api.domain.urls import normalise_website_input


@pytest.mark.parametrize(
    ("raw", "domain", "canonical_url"),
    [
        ("example.com", "example.com", "https://example.com"),
        ("  example.com  ", "example.com", "https://example.com"),
        ("EXAMPLE.COM", "example.com", "https://example.com"),
        ("www.example.com", "example.com", "https://www.example.com"),
        ("https://www.example.com/", "example.com", "https://www.example.com"),
        ("http://example.com", "example.com", "http://example.com"),
        ("https://example.com/about?utm=x#top", "example.com", "https://example.com"),
        ("https://example.com:443/", "example.com", "https://example.com"),
        ("example.com.", "example.com", "https://example.com"),
        ("blog.example.co.uk", "blog.example.co.uk", "https://blog.example.co.uk"),
    ],
)
def test_normalises_what_people_actually_type(raw, domain, canonical_url):
    result = normalise_website_input(raw)
    assert result.domain == domain
    assert result.canonical_url == canonical_url


def test_www_is_kept_in_the_origin_but_stripped_from_the_domain():
    """`www.example.com` and `example.com` can serve different sites, so the
    origin we fetch keeps what the user typed. Dedupe uses the bare domain."""
    with_www = normalise_website_input("https://www.example.com")
    without = normalise_website_input("https://example.com")

    assert with_www.domain == without.domain == "example.com"
    assert with_www.canonical_url == "https://www.example.com"
    assert without.canonical_url == "https://example.com"
    assert with_www.had_www and not without.had_www


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "example",                       # no dot: not a hostname
        "ftp://example.com",             # unsupported scheme
        "http://127.0.0.1",              # IP literal
        "https://192.168.0.1/admin",
        "http://localhost",
        "https://user:pw@example.com",   # credentials
        "https://example.com:8443",      # non-default port
        "https://-example.com",          # leading hyphen label
        "https://exa mple.com",          # whitespace
        "https://example..com",          # empty label
    ],
)
def test_rejects_what_is_not_a_public_website(raw):
    with pytest.raises(InvalidWebsite):
        normalise_website_input(raw)


def test_internationalised_domains_are_punycoded():
    result = normalise_website_input("https://köln.example")
    assert result.domain == "xn--kln-sna.example"
