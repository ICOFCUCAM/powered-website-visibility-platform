"""The public scan: one page, for somebody with no account.

The thing being protected here is not the feature, it is everything around
it. No account means no ownership gate, so every test about what it refuses
matters more than the tests about what it finds.
"""

from __future__ import annotations

import httpx
import pytest

from api.analysis.rules.base import all_rules
from api.crawler.safety import UnsafeUrl
from api.crawler.safety import check_url as real_check_url
from api.peek import service
from api.peek.service import PeekFailed

HTML = """
<!doctype html><html><head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A perfectly reasonable title for a homepage</title>
<meta name="description" content="A description of roughly the right sort of
length for a search result, saying what the page is actually about.">
</head><body><h1>Hello</h1>
<p>{words}</p>
</body></html>
"""


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )


# -- the allowlist must name rules that exist -------------------------------
def test_every_named_rule_is_a_real_rule():
    """The allowlist was once written from memory and named fourteen rules
    that did not exist, so every peek found nothing and looked like it
    worked. A name that does not resolve is a silent no-op."""
    keys = {key for key, _ in all_rules()}
    assert keys >= service.SINGLE_PAGE_RULES, (
        f"not real rules: {sorted(service.SINGLE_PAGE_RULES - keys)}"
    )
    assert len(service.SINGLE_PAGE_RULES) >= 10


def test_no_rule_that_needs_google_data_is_run():
    """A rule with nothing to look at should be absent, not silent — a rule
    that needs Search Console cannot say anything true about an anonymous
    fetch, and running it invites it to guess."""
    for key in ("ctr_below_position_baseline", "striking_distance_keyword",
                "declining_page", "cannibalisation", "noindex_on_valuable_page",
                "duplicate_title", "orphan_page", "no_sitemap"):
        assert key not in service.SINGLE_PAGE_RULES


# -- what somebody types ----------------------------------------------------
def test_a_bare_domain_becomes_a_url():
    assert service.normalise("example.com") == "https://example.com/"
    assert service.normalise("  example.com  ") == "https://example.com/"


def test_a_pasted_deep_link_is_kept_but_its_query_is_not():
    # Tracking parameters and session ids have no business in our logs.
    assert service.normalise("https://a.com/b/c?utm_source=x") == "https://a.com/b/c"


def test_an_empty_or_absurd_address_is_refused_in_words():
    with pytest.raises(PeekFailed):
        service.normalise("")
    with pytest.raises(PeekFailed):
        service.normalise("x" * 400)


# -- what it refuses to fetch -----------------------------------------------
async def test_it_will_not_fetch_a_private_address():
    """The whole reason the guard exists."""
    with pytest.raises(UnsafeUrl):
        await service.run("http://169.254.169.254/latest/meta-data/")


async def test_a_redirect_into_private_space_is_caught(monkeypatch):
    """A guard that checks only the first URL is not a guard: the attacker
    controls a public host and answers 302 to somewhere internal."""
    monkeypatch.setattr(service, "check_peer", lambda addr: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "evil.example.com":
            return httpx.Response(302, headers={"location": "http://10.0.0.1/"})
        return httpx.Response(200, text="<html></html>")

    monkeypatch.setattr(service, "check_url", real_check_url)
    with pytest.raises(UnsafeUrl):
        async with _client(handler) as client:
            await service.fetch_once("https://evil.example.com/", client=client)


async def test_a_page_that_is_not_html_is_refused(monkeypatch):
    monkeypatch.setattr(service, "check_url", lambda u: u)
    monkeypatch.setattr(service, "check_peer", lambda addr: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}
        )

    async with _client(handler) as client:
        with pytest.raises(PeekFailed, match="web page"):
            await service.run("example.com", client=client)


async def test_an_endless_redirect_loop_stops(monkeypatch):
    monkeypatch.setattr(service, "check_url", lambda u: u)
    monkeypatch.setattr(service, "check_peer", lambda addr: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://a.example.com/next"})

    async with _client(handler) as client:
        with pytest.raises(PeekFailed, match="redirects"):
            await service.fetch_once("https://a.example.com/", client=client)


# -- what it finds ----------------------------------------------------------
async def test_a_healthy_page_produces_few_findings(monkeypatch):
    monkeypatch.setattr(service, "check_url", lambda u: u)
    monkeypatch.setattr(service, "check_peer", lambda addr: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=HTML.format(words=" ".join(["word"] * 500)),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    async with _client(handler) as client:
        peek = await service.run("example.com", client=client)

    assert peek.status_code == 200
    assert peek.title == "A perfectly reasonable title for a homepage"
    types = {f["type"] for f in peek.findings}
    # The page has a title, a description, one h1, a viewport and real text.
    for key in ("missing_title", "missing_meta_description", "missing_h1",
                "missing_viewport", "thin_content"):
        assert key not in types


async def test_a_page_missing_the_basics_says_so(monkeypatch):
    """The findings come from the real rules, so a peek can never disagree
    with what the full audit would say about the same page."""
    monkeypatch.setattr(service, "check_url", lambda u: u)
    monkeypatch.setattr(service, "check_peer", lambda addr: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="<html><head></head><body><p>hi</p></body></html>",
            headers={"content-type": "text/html"},
        )

    async with _client(handler) as client:
        peek = await service.run("example.com", client=client)

    types = {f["type"] for f in peek.findings}
    assert "missing_title" in types
    assert "missing_meta_description" in types
    assert "missing_h1" in types
    assert "missing_viewport" in types


async def test_nothing_is_written_anywhere(monkeypatch, service_conn):
    """A stranger's scan must not create an organisation, a website, a page
    or a finding. The result lives for the length of the response."""
    monkeypatch.setattr(service, "check_url", lambda u: u)
    monkeypatch.setattr(service, "check_peer", lambda addr: None)

    async def count(table: str) -> int:
        row = await (
            await service_conn.execute(f"select count(*) as n from {table}")
        ).fetchone()
        return row["n"]

    before = {t: await count(t) for t in
              ("organizations", "websites", "pages", "issues", "crawls")}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=HTML.format(words="word " * 300),
            headers={"content-type": "text/html"},
        )

    async with _client(handler) as client:
        await service.run("example.com", client=client)

    for table, n in before.items():
        assert await count(table) == n, f"the peek wrote to {table}"


# -- the endpoint ------------------------------------------------------------
class FakeRedis:
    """Counts like Redis does, without one."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> None:
        return None


async def test_a_scan_returns_what_the_rules_found(client, monkeypatch):
    from api.peek import limits, routes

    monkeypatch.setattr(routes, "_client", lambda: FakeRedis())
    monkeypatch.setattr(
        service, "run",
        lambda url: _peek_result(url),
    )

    response = await client.post("/api/v1/peek", json={"url": "example.com"})
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Example"
    assert body["findings"][0]["type"] == "missing_h1"
    assert body["checked"] >= 10
    assert limits.PER_IP >= 1


async def _peek_result(url: str):
    return service.Peek(
        url=url, final_url=url, status_code=200, title="Example",
        findings=[{"type": "missing_h1", "severity": "medium", "evidence": {}}],
        checked=len(service.SINGLE_PAGE_RULES),
    )


async def test_a_visitor_who_will_not_stop_is_told_to_wait(client, monkeypatch):
    from api.peek import limits, routes

    shared = FakeRedis()
    monkeypatch.setattr(routes, "_client", lambda: shared)
    monkeypatch.setattr(service, "run", lambda url: _peek_result(url))

    for _ in range(limits.PER_IP):
        assert (
            await client.post("/api/v1/peek", json={"url": "a.com"})
        ).status_code == 200

    refused = await client.post("/api/v1/peek", json={"url": "a.com"})
    assert refused.status_code == 429
    assert "scans" in refused.json()["error"]["message"].lower()


async def test_with_no_counter_it_refuses_rather_than_running_unlimited(
    client, monkeypatch
):
    """An endpoint that fetches whatever it is told to fetch does not get to
    keep serving when the thing counting it has gone away."""
    from api.peek import routes

    monkeypatch.setattr(routes, "_client", lambda: None)
    response = await client.post("/api/v1/peek", json={"url": "a.com"})
    assert response.status_code == 429


async def test_a_refused_address_does_not_say_why(client, monkeypatch):
    """"That is a private address" and "that does not resolve" are different
    answers, and telling them apart turns this into a port scanner."""
    from api.peek import routes

    monkeypatch.setattr(routes, "_client", lambda: FakeRedis())

    response = await client.post(
        "/api/v1/peek", json={"url": "http://169.254.169.254/"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "We can't scan that address."
    assert response.json()["error"]["code"] == "peek_refused"


async def test_the_endpoint_runs_the_whole_pipeline(client, monkeypatch):
    """Everything real except the socket: the guard, the fetch loop, the
    extractor and the rules all run. Only DNS and the network are faked,
    because the sandbox has no egress — and because a test that mocks
    `service.run` proves only that the router calls a function."""
    from api.peek import routes

    monkeypatch.setattr(routes, "_client", lambda: FakeRedis())
    monkeypatch.setattr(service, "check_url", lambda u: u)
    monkeypatch.setattr(service, "check_peer", lambda addr: None)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "Visibility" in request.headers["user-agent"]
        return httpx.Response(
            200,
            text="<html><head></head><body><p>hi</p></body></html>",
            headers={"content-type": "text/html"},
        )

    real_run = service.run

    async def run_with_fake_socket(url: str, **_: object):
        async with _client(handler) as mocked:
            return await real_run(url, client=mocked)

    monkeypatch.setattr(service, "run", run_with_fake_socket)

    response = await client.post("/api/v1/peek", json={"url": "example.com"})
    assert response.status_code == 200
    body = response.json()

    assert body["final_url"] == "https://example.com/"
    assert body["status_code"] == 200
    types = {f["type"] for f in body["findings"]}
    assert {"missing_title", "missing_h1", "missing_viewport"} <= types
    # The count is the number of checks run, not the number that fired.
    assert body["checked"] == len(service.SINGLE_PAGE_RULES)
