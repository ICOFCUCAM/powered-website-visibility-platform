"""Whether a website may be crawled (locked decision 20, M1).

    crawl_allowed = ownership_verified
                    AND ownership coverage matches the crawl target
                    AND website status is active

Deliberately NOT a database trigger: a trigger cannot see plan state, robots
rules or an operator suspension. It is evaluated here, at the service boundary
when a crawl is requested, and AGAIN by the crawler before it starts fetching —
because a queue entry must never outlive the permission that created it.

`websites.crawl_allowed` is a derived column kept for observability. It is
never client-writable (migration 0012) and never the authority. This is.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.domain.models import Website, WebsiteStatus


@dataclass(frozen=True, slots=True)
class CrawlDecision:
    allowed: bool
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.allowed


#: Statuses from which a crawl may start. ERROR is included: a website whose
#: last crawl failed must still be re-crawlable, or a transient failure becomes
#: permanent.
_CRAWLABLE = frozenset(
    {
        WebsiteStatus.PENDING,
        WebsiteStatus.CONNECTING,
        WebsiteStatus.READY,
        WebsiteStatus.ERROR,
    }
)


def evaluate_crawl_allowed(
    website: Website,
    *,
    ownership_covers_target: bool,
    plan_permits: bool = True,
    paused: bool = False,
    robots_blocks: bool = False,
) -> CrawlDecision:
    """Evaluate the prerequisites. Order is by user-actionability: tell them
    the thing they can fix first."""
    if not website.ownership_verified:
        return CrawlDecision(False, "ownership_not_verified")
    if not ownership_covers_target:
        # Holding a Search Console property is not ownership of every URL: a
        # URL-prefix property proves nothing about a subdomain or another
        # scheme. See app.property_covers_url() in migration 0010.
        return CrawlDecision(False, "ownership_does_not_cover_target")
    if website.status not in _CRAWLABLE:
        return CrawlDecision(False, f"website_status_{website.status.value.lower()}")
    if paused:
        return CrawlDecision(False, "paused_by_user")
    if not plan_permits:
        return CrawlDecision(False, "plan_limit_exceeded")
    if robots_blocks:
        return CrawlDecision(False, "robots_disallows_crawl")
    return CrawlDecision(True)
