"""GoogleOAuthTokenProvider — refresh-token-backed access tokens.

Exchanges a long-lived OAuth refresh token for short-lived access
tokens against Google's token endpoint, caching each token until
``expiry_skew_seconds`` before it expires. This closes the
``07_backlog.md`` deferral that left OAuth refresh "operator-driven via
the access-token field" — PMs no longer re-paste hourly tokens.

Design mirrors the real Google adapters:

- The httpx ``AsyncClient`` is injectable so tests drive the token
  endpoint through ``httpx.MockTransport`` — no real socket opens.
- The monotonic ``clock`` is injectable so expiry behaviour is
  deterministic in tests without ``sleep``.
- Refresh failures are redacted via ``safe_error_summary`` /
  ``safe_network_summary`` (M8 / round-3 M4): the token endpoint error
  body and httpx exception strings never reach the audit log or a
  FastAPI traceback. The refresh token / client secret are never
  interpolated into an error message.
- ``get_access_token`` holds an ``asyncio.Lock`` so concurrent callers
  (the four Google adapters share one instance) collapse a burst of
  expired-cache reads into a single refresh round-trip.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

import httpx

from competitionops.adapters._http_errors import (
    HTTP_TIMEOUT_SECONDS,
    safe_error_summary,
    safe_network_summary,
)

_GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
_DEFAULT_EXPIRES_IN = 3600.0
_DEFAULT_EXPIRY_SKEW_SECONDS = 60.0


class TokenRefreshError(RuntimeError):
    """Raised when the OAuth token endpoint cannot supply an access token.

    The message carries only a redacted summary (status line / httpx
    exception class name) — never the refresh token, client secret, or
    a raw response body.
    """


class GoogleOAuthTokenProvider:
    """Refresh-token-backed ``TokenProvider`` for the Google adapters."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        token_uri: str = _GOOGLE_TOKEN_URI,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        expiry_skew_seconds: float = _DEFAULT_EXPIRY_SKEW_SECONDS,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._token_uri = token_uri
        self._injected_client = client
        self._clock = clock
        self._skew = expiry_skew_seconds
        self._cached_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_access_token(self) -> str:
        """Return a cached token if still fresh, else refresh once.

        The lock collapses concurrent expired-cache reads into a single
        refresh: the first caller refreshes, the rest re-check the
        cache and return the freshly-stored token.
        """
        async with self._lock:
            now = self._clock()
            if (
                self._cached_token is not None
                and now < self._expires_at - self._skew
            ):
                return self._cached_token
            return await self._refresh(now)

    async def _refresh(self, now: float) -> str:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        try:
            async with self._client_session() as client:
                response = await client.post(
                    self._token_uri, data=form, timeout=HTTP_TIMEOUT_SECONDS
                )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TokenRefreshError(
                safe_error_summary(exc.response, target="oauth")
            ) from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise TokenRefreshError(
                safe_network_summary(exc, target="oauth")
            ) from exc

        try:
            data: dict[str, Any] = response.json()
        except ValueError as exc:
            raise TokenRefreshError(
                "oauth token endpoint returned a non-JSON body"
            ) from exc
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise TokenRefreshError(
                "oauth token endpoint response missing 'access_token'"
            )
        expires_in = data.get("expires_in", _DEFAULT_EXPIRES_IN)
        try:
            lifetime = float(expires_in)
        except (TypeError, ValueError):
            lifetime = _DEFAULT_EXPIRES_IN
        self._cached_token = token
        self._expires_at = now + lifetime
        return token

    @asynccontextmanager
    async def _client_session(self) -> AsyncIterator[httpx.AsyncClient]:
        """Yield the injected client (tests) or a freshly-managed one."""
        if self._injected_client is not None:
            yield self._injected_client
            return
        async with httpx.AsyncClient() as client:
            yield client
