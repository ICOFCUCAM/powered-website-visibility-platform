"""Who may call this API from a browser.

The front end and the API are two origins in any real deployment — the app on
Vercel, the API on a container host — so this is the one piece of
configuration that is guaranteed wrong if it is left at its development
value, and wrong in a way that only shows up in a customer's browser.
"""

from __future__ import annotations

import pytest

from api.config import ConfigError, _cors_origins


def test_development_still_works_with_nothing_set(monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert _cors_origins("development") == ("http://localhost:3000",)


def test_production_refuses_to_start_without_one(monkeypatch):
    """A localhost default in production is not a degraded mode. It is a
    deployment where every request is blocked, and the error appears in the
    customer's browser console rather than in our logs."""
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    with pytest.raises(ConfigError, match="required in production"):
        _cors_origins("production")


def test_production_refuses_a_wildcard(monkeypatch):
    """The setting somebody reaches for when CORS is failing and the deadline
    is close. It makes every site on the internet able to call this API with a
    token taken from a customer's browser."""
    monkeypatch.setenv("CORS_ORIGINS", "*")
    with pytest.raises(ConfigError, match="may not be"):
        _cors_origins("production")


def test_production_refuses_plain_http(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "http://app.example.com")
    with pytest.raises(ConfigError, match="must be https"):
        _cors_origins("production")


def test_a_wildcard_hidden_among_real_origins_is_still_refused(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com, *")
    with pytest.raises(ConfigError, match="may not be"):
        _cors_origins("production")


def test_several_origins_and_a_trailing_slash(monkeypatch):
    """Vercel gives a preview deployment its own origin, so more than one is
    normal. A trailing slash is not part of an origin and never matches."""
    monkeypatch.setenv(
        "CORS_ORIGINS",
        "https://app.example.com/, https://visibility-hub.vercel.app",
    )
    assert _cors_origins("production") == (
        "https://app.example.com",
        "https://visibility-hub.vercel.app",
    )
