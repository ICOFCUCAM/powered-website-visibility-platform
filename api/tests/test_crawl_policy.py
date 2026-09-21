"""Decision 20: crawl_allowed is derived, never user-controlled."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from api.domain.crawl_policy import evaluate_crawl_allowed
from api.domain.models import OwnershipMethod, Website, WebsiteStatus


def _website(**overrides) -> Website:
    base = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "domain": "example.com",
        "canonical_url": "https://example.com",
        "name": None,
        "status": WebsiteStatus.READY,
        "timezone": "UTC",
        "created_at": datetime.now(UTC),
        "ownership_verified_at": datetime.now(UTC),
        "ownership_method": OwnershipMethod.SEARCH_CONSOLE,
    }
    return Website(**(base | overrides))


def test_allows_a_verified_active_website():
    decision = evaluate_crawl_allowed(_website(), ownership_covers_target=True)
    assert decision.allowed
    assert bool(decision) is True


def test_refuses_an_unverified_website():
    decision = evaluate_crawl_allowed(
        _website(ownership_verified_at=None), ownership_covers_target=True
    )
    assert not decision
    assert decision.reason == "ownership_not_verified"


def test_refuses_when_the_property_does_not_cover_the_target():
    """Holding a Search Console property is not ownership of every URL: a
    URL-prefix property proves nothing about a subdomain or another scheme."""
    decision = evaluate_crawl_allowed(_website(), ownership_covers_target=False)
    assert not decision
    assert decision.reason == "ownership_does_not_cover_target"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"paused": True}, "paused_by_user"),
        ({"plan_permits": False}, "plan_limit_exceeded"),
        ({"robots_blocks": True}, "robots_disallows_crawl"),
    ],
)
def test_refuses_for_each_prerequisite(kwargs, reason):
    decision = evaluate_crawl_allowed(
        _website(), ownership_covers_target=True, **kwargs
    )
    assert decision.reason == reason


def test_a_website_whose_last_crawl_failed_can_be_recrawled():
    """Otherwise a transient failure becomes permanent."""
    decision = evaluate_crawl_allowed(
        _website(status=WebsiteStatus.ERROR), ownership_covers_target=True
    )
    assert decision.allowed


def test_verification_is_reported_before_pausing():
    """Order is by user-actionability: tell them the thing they can fix."""
    decision = evaluate_crawl_allowed(
        _website(ownership_verified_at=None),
        ownership_covers_target=False,
        paused=True,
    )
    assert decision.reason == "ownership_not_verified"
