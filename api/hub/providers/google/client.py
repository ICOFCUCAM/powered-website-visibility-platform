"""The Google API client.

THE ONLY MODULE IN THE APPLICATION THAT TALKS TO GOOGLE. The import contract in
pyproject.toml forbids a Google client anywhere outside `api.hub`, and a test
asserts no googleapis.com endpoint appears outside this package.

Plain httpx rather than the Google SDK: three endpoints and two OAuth calls do
not justify a dependency whose surface is mostly things this product must never
do (write scopes, service accounts, impersonation).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from api.hub.providers.google.errors import (
    GoogleAuthorizationFailed,
    GoogleError,
    GooglePermissionDenied,
    GoogleQuotaExceeded,
    GoogleRefreshRejected,
)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
SEARCH_CONSOLE_SITES = "https://searchconsole.googleapis.com/webmasters/v3/sites"
ANALYTICS_ACCOUNT_SUMMARIES = (
    "https://analyticsadmin.googleapis.com/v1beta/accountSummaries"
)
ANALYTICS_DATA_STREAMS = (
    "https://analyticsadmin.googleapis.com/v1beta/{property}/dataStreams"
)

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


@dataclass(frozen=True, slots=True)
class GoogleTokens:
    access_token: str
    expires_in: int
    scopes: list[str]
    refresh_token: str | None = None
    subject: str | None = None
    email: str | None = None

    def redacted(self) -> dict[str, Any]:
        """What may appear in a log line. Never the tokens themselves."""
        return {
            "expires_in": self.expires_in,
            "scopes": self.scopes,
            "has_refresh_token": self.refresh_token is not None,
            "subject": self.subject,
        }


def _raise_for(response: httpx.Response) -> None:
    if response.status_code == 429:
        raise GoogleQuotaExceeded()
    if response.status_code == 403:
        raise GooglePermissionDenied()
    if response.status_code >= 400:
        raise GoogleError()


class GoogleClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)

    # -- OAuth ------------------------------------------------------------

    async def exchange_code(
        self, code: str, code_verifier: str, redirect_uri: str
    ) -> GoogleTokens:
        response = await self._http.post(
            TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
        )
        if response.status_code >= 400:
            raise GoogleAuthorizationFailed()
        return self._tokens_from(response.json())

    async def refresh(self, refresh_token: str) -> GoogleTokens:
        response = await self._http.post(
            TOKEN_ENDPOINT,
            data={
                "refresh_token": refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
            },
        )
        if response.status_code >= 400:
            body = response.json() if response.content else {}
            if body.get("error") == "invalid_grant":
                raise GoogleRefreshRejected()
            raise GoogleError()
        return self._tokens_from(response.json())

    async def revoke(self, token: str) -> None:
        """Best effort. A 400 means Google already considers it dead, which is
        the outcome we wanted; the local secret is destroyed either way."""
        with contextlib.suppress(httpx.HTTPError):
            await self._http.post(REVOKE_ENDPOINT, data={"token": token})

    def _tokens_from(self, payload: dict[str, Any]) -> GoogleTokens:
        subject = email = None
        if id_token := payload.get("id_token"):
            # Signature verification is deliberately skipped: per OIDC Core
            # 3.1.3.7, an ID token received directly from the token endpoint
            # over TLS is already authenticated by that channel. We never
            # accept an id_token from anywhere else.
            claims = jwt.decode(id_token, options={"verify_signature": False})
            subject = claims.get("sub")
            email = claims.get("email")

        return GoogleTokens(
            access_token=payload["access_token"],
            expires_in=int(payload.get("expires_in", 3600)),
            scopes=str(payload.get("scope", "")).split(),
            refresh_token=payload.get("refresh_token"),
            subject=subject,
            email=email,
        )

    # -- Resource discovery ------------------------------------------------

    async def list_search_console_sites(
        self, access_token: str
    ) -> list[dict[str, Any]]:
        response = await self._http.get(
            SEARCH_CONSOLE_SITES,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        _raise_for(response)
        return response.json().get("siteEntry", [])

    async def list_analytics_properties(
        self, access_token: str
    ) -> list[dict[str, Any]]:
        """Flattens account summaries into one row per GA4 property.

        Paginated: an agency's Google account can hold hundreds, and taking
        only the first page would silently hide the property they wanted.
        """
        properties: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            params = {"pageSize": 200}
            if page_token:
                params["pageToken"] = page_token
            response = await self._http.get(
                ANALYTICS_ACCOUNT_SUMMARIES,
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
            )
            _raise_for(response)
            payload = response.json()

            for account in payload.get("accountSummaries", []):
                for prop in account.get("propertySummaries", []):
                    properties.append(
                        {
                            "property": prop.get("property"),
                            "displayName": prop.get("displayName"),
                            "propertyType": prop.get("propertyType"),
                            "account": account.get("account"),
                            "accountName": account.get("displayName"),
                        }
                    )

            page_token = payload.get("nextPageToken")
            if not page_token:
                return properties

    async def list_web_stream_urls(
        self, access_token: str, property_uri: str
    ) -> list[str]:
        """Default URIs of a GA4 property's web data streams.

        A GA4 property has no hostname of its own, so this is the only way to
        tell which website it measures. Without it the wizard can only offer an
        unsorted list, which for an agency account is hundreds of entries with
        names like "GA4".

        A property the caller cannot read is not an error here: it simply
        contributes no URLs and therefore never auto-matches.
        """
        response = await self._http.get(
            ANALYTICS_DATA_STREAMS.format(property=property_uri),
            headers={"Authorization": f"Bearer {access_token}"},
            params={"pageSize": 50},
        )
        if response.status_code in (403, 404):
            return []
        _raise_for(response)

        urls: list[str] = []
        for stream in response.json().get("dataStreams", []):
            uri = (stream.get("webStreamData") or {}).get("defaultUri")
            if uri:
                urls.append(uri)
        return urls
