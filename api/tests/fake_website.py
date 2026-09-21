"""A fake website, served through httpx's MockTransport.

Drives the REAL crawler — real robots parsing, real sitemap reading, real
extraction, real frontier — against a site whose shape each test chooses. Only
the network is replaced.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

ORIGIN = "https://example.com"


def page(
    title: str = "A page",
    *,
    body: str = "<p>Some ordinary body copy that is long enough to be real.</p>",
    links: tuple[str, ...] = (),
    description: str | None = "A description",
    extra_head: str = "",
) -> str:
    anchors = "".join(f'<a href="{href}">link</a>' for href in links)
    meta = f'<meta name="description" content="{description}">' if description else ""
    return (
        f"<!doctype html><html lang='en'><head><title>{title}</title>{meta}"
        f"<meta name='viewport' content='width=device-width'>{extra_head}</head>"
        f"<body><h1>{title}</h1>{body}{anchors}</body></html>"
    )


@dataclass
class FakeWebsite:
    pages: dict[str, str] = field(default_factory=dict)
    robots: str | None = "User-agent: *\nAllow: /\n"
    sitemap: str | None = None
    statuses: dict[str, int] = field(default_factory=dict)
    slow_paths: set[str] = field(default_factory=set)
    requests: list[str] = field(default_factory=list)
    user_agents: list[str] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self.transport(),
            follow_redirects=True,
            headers={"User-Agent": "VisibilityBot/1.0 (+https://visibilityhub.example/bot)"},
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        self.user_agents.append(request.headers.get("user-agent", ""))
        path = request.url.path

        if path == "/robots.txt":
            if self.robots is None:
                return httpx.Response(404, text="")
            return httpx.Response(200, text=self.robots,
                                  headers={"content-type": "text/plain"})

        if path.endswith("sitemap.xml") or path.endswith("sitemap_index.xml"):
            if self.sitemap is None:
                return httpx.Response(404, text="")
            return httpx.Response(200, text=self.sitemap,
                                  headers={"content-type": "application/xml"})

        if path in self.statuses:
            return httpx.Response(self.statuses[path], text="",
                                  headers={"content-type": "text/html"})

        if path in self.pages:
            return httpx.Response(200, text=self.pages[path],
                                  headers={"content-type": "text/html; charset=utf-8"})

        return httpx.Response(404, text="<html><body>Not found</body></html>",
                              headers={"content-type": "text/html"})


def sitemap_for(paths: list[str], origin: str = ORIGIN) -> str:
    entries = "".join(f"<url><loc>{origin}{p}</loc></url>" for p in paths)
    return f'<?xml version="1.0"?><urlset>{entries}</urlset>'
