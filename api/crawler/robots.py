"""robots.txt.

Respected always, with no override flag — because an override flag eventually
gets used. The crawler identifies itself and points at a page explaining how
to block it; a site owner who wants us gone must be able to make that stick.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

USER_AGENT = "VisibilityBot"
USER_AGENT_STRING = "VisibilityBot/1.0 (+https://visibilityhub.example/bot)"

DEFAULT_DELAY_SECONDS = 1.0
#: Beyond this we stop obeying and tell the customer the crawl will be slow,
#: rather than silently taking days over one site.
MAX_HONOURED_DELAY_SECONDS = 30.0

_SITEMAP_LINE = re.compile(r"^\s*sitemap:\s*(\S+)", re.IGNORECASE | re.MULTILINE)


@dataclass(slots=True)
class RobotsPolicy:
    """What robots.txt allows us to do here."""

    fetched: bool = False
    status_code: int | None = None
    delay_seconds: float = DEFAULT_DELAY_SECONDS
    sitemaps: list[str] = field(default_factory=list)
    blocks_everything: bool = False
    raw: str = ""
    _parser: RobotFileParser | None = None

    def allows(self, url: str) -> bool:
        """Unreachable robots.txt means allow, which is the conventional
        reading — but `fetched` records that we never saw one."""
        if self._parser is None:
            return True
        return self._parser.can_fetch(USER_AGENT, url)

    @property
    def capped_delay(self) -> bool:
        return self.delay_seconds >= MAX_HONOURED_DELAY_SECONDS


def parse_robots(body: str, status_code: int, origin: str) -> RobotsPolicy:
    # 4xx means "no rules", which is allow-all. 5xx is ambiguous, and the
    # conservative reading — treat an erroring server as allow-all — is also
    # the conventional one; the crawl's own rate limiting still applies.
    if status_code >= 400 or not body.strip():
        return RobotsPolicy(fetched=status_code < 400, status_code=status_code)

    parser = RobotFileParser()
    parser.parse(body.splitlines())

    delay = parser.crawl_delay(USER_AGENT) or parser.crawl_delay("*")
    delay_seconds = float(delay) if delay else DEFAULT_DELAY_SECONDS
    delay_seconds = min(max(delay_seconds, DEFAULT_DELAY_SECONDS),
                        MAX_HONOURED_DELAY_SECONDS)

    sitemaps = []
    for match in _SITEMAP_LINE.finditer(body):
        url = match.group(1).strip()
        if url.startswith("http"):
            sitemaps.append(url)

    root = urlsplit(origin)._replace(path="/", query="", fragment="").geturl()
    blocks_everything = not parser.can_fetch(USER_AGENT, root)

    return RobotsPolicy(
        fetched=True,
        status_code=status_code,
        delay_seconds=delay_seconds,
        sitemaps=sitemaps,
        blocks_everything=blocks_everything,
        raw=body,
        _parser=parser,
    )


#: robots.txt directives aimed at AI crawlers. Not a politeness concern — a
#: finding. A site blocking these is invisible to AI answer engines, which is
#: one of the things this product is for.
AI_CRAWLERS = (
    "GPTBot",
    "ClaudeBot",
    "Claude-Web",
    "PerplexityBot",
    "Google-Extended",
    "CCBot",
    "anthropic-ai",
)


def blocked_ai_crawlers(body: str) -> list[str]:
    if not body.strip():
        return []
    blocked: list[str] = []
    for agent in AI_CRAWLERS:
        parser = RobotFileParser()
        parser.parse(body.splitlines())
        if not parser.can_fetch(agent, "/"):
            blocked.append(agent)
    return blocked
