"""A fake Google, served through httpx's MockTransport.

This drives the REAL client code — the same request building, the same JSON
parsing, the same error mapping — against realistic payloads. Only the network
is replaced, so a change that breaks the client breaks these tests.

Payload shapes follow the published API responses: `siteEntry` with
`permissionLevel` for Search Console, nested `accountSummaries` /
`propertySummaries` for Analytics Admin, `webStreamData.defaultUri` for
data streams.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from api.hub.providers.google.client import (
    ANALYTICS_ACCOUNT_SUMMARIES,
    SEARCH_ANALYTICS_ROW_LIMIT,
    SEARCH_CONSOLE_SITES,
    TOKEN_ENDPOINT,
)


def unsigned_id_token(sub: str, email: str) -> str:
    """An id_token as the token endpoint returns it.

    The client deliberately does not verify the signature — per OIDC Core
    3.1.3.7 a token received directly from the token endpoint over TLS is
    authenticated by that channel — so an unsigned one is a faithful stand-in.
    """

    def seg(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    header, body = seg({"alg": "none"}), seg({"sub": sub, "email": email})
    return f"{header}.{body}."


@dataclass
class FakeGoogle:
    """Configurable behaviour, so each test states the situation it is about."""

    subject: str = "google-sub-123"
    email: str = "owner@example.com"
    scopes: list[str] = field(
        default_factory=lambda: [
            "openid",
            "email",
            "profile",
            "https://www.googleapis.com/auth/webmasters.readonly",
        ]
    )
    refresh_token: str | None = "refresh-token-1"
    sites: list[dict[str, str]] = field(default_factory=list)
    analytics_accounts: list[dict[str, Any]] = field(default_factory=list)
    stream_urls: dict[str, list[str]] = field(default_factory=dict)

    # Search Analytics. Keyed by the dimension tuple, e.g. ("date","query").
    # Values are (keys, clicks, impressions, position) tuples.
    search_analytics: dict[tuple[str, ...], list[tuple]] = field(default_factory=dict)
    search_analytics_calls: list[dict[str, Any]] = field(default_factory=list)
    quota_exceeded_after: int | None = None

    # Failure switches
    refresh_invalid_grant: bool = False
    exchange_fails: bool = False
    search_console_status: int = 200
    rotate_refresh_token_to: str | None = None

    # Observed calls, so tests can assert what was sent.
    token_requests: list[dict[str, str]] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)
    stream_lookups: list[str] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport())

    # -- handler -----------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]

        if url == TOKEN_ENDPOINT:
            return self._token(request)
        if url.endswith("/revoke"):
            body = dict(_form(request))
            self.revoked.append(body.get("token", ""))
            return httpx.Response(200, json={})
        if url == SEARCH_CONSOLE_SITES:
            if self.search_console_status != 200:
                return httpx.Response(self.search_console_status, json={})
            return httpx.Response(200, json={"siteEntry": self.sites})
        if url == ANALYTICS_ACCOUNT_SUMMARIES:
            return httpx.Response(
                200, json={"accountSummaries": self.analytics_accounts}
            )
        if "/searchAnalytics/query" in url:
            return self._search_analytics(request)
        if "/dataStreams" in url:
            prop = url.split("/v1beta/")[1].rsplit("/dataStreams", 1)[0]
            self.stream_lookups.append(prop)
            return httpx.Response(
                200,
                json={
                    "dataStreams": [
                        {"webStreamData": {"defaultUri": uri}}
                        for uri in self.stream_urls.get(prop, [])
                    ]
                },
            )
        return httpx.Response(404, json={"error": "unexpected endpoint"})

    def _search_analytics(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.search_analytics_calls.append(body)

        if (
            self.quota_exceeded_after is not None
            and len(self.search_analytics_calls) > self.quota_exceeded_after
        ):
            return httpx.Response(429, json={"error": {"code": 429}})

        dims = tuple(body["dimensions"])
        start, end = body["startDate"], body["endDate"]
        rows = [
            {
                "keys": list(keys),
                "clicks": clicks,
                "impressions": impressions,
                "ctr": (clicks / impressions) if impressions else 0.0,
                "position": position,
            }
            for keys, clicks, impressions, position in self.search_analytics.get(dims, [])
            if start <= keys[0] <= end
        ]
        # Honour pagination exactly as the API does, so the client's loop is
        # genuinely exercised rather than assumed.
        offset = body.get("startRow", 0)
        limit = body.get("rowLimit", SEARCH_ANALYTICS_ROW_LIMIT)
        return httpx.Response(200, json={"rows": rows[offset : offset + limit]})

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = dict(_form(request))
        self.token_requests.append(form)

        if form.get("grant_type") == "refresh_token":
            if self.refresh_invalid_grant:
                return httpx.Response(400, json={"error": "invalid_grant"})
            payload: dict[str, Any] = {
                "access_token": "access-refreshed",
                "expires_in": 3599,
                "scope": " ".join(self.scopes),
            }
            if self.rotate_refresh_token_to:
                payload["refresh_token"] = self.rotate_refresh_token_to
            return httpx.Response(200, json=payload)

        if self.exchange_fails:
            return httpx.Response(400, json={"error": "invalid_grant"})

        payload = {
            "access_token": "access-1",
            "expires_in": 3599,
            "scope": " ".join(self.scopes),
            "id_token": unsigned_id_token(self.subject, self.email),
        }
        if self.refresh_token:
            payload["refresh_token"] = self.refresh_token
        return httpx.Response(200, json=payload)


def _form(request: httpx.Request) -> list[tuple[str, str]]:
    from urllib.parse import parse_qsl

    return parse_qsl(request.content.decode())


def site(url: str, permission: str = "siteOwner") -> dict[str, str]:
    return {"siteUrl": url, "permissionLevel": permission}


def ga4_account(name: str, properties: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "account": "accounts/1",
        "displayName": name,
        "propertySummaries": [
            {
                "property": prop,
                "displayName": label,
                "propertyType": "PROPERTY_TYPE_ORDINARY",
            }
            for prop, label in properties
        ],
    }


def analytics_row(
    keys: tuple[str, ...], clicks: int, impressions: int, position: float
) -> tuple:
    return (keys, clicks, impressions, position)
