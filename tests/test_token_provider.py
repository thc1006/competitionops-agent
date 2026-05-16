"""TokenProvider port — StaticTokenProvider + GoogleOAuthTokenProvider.

Closes the ``07_backlog.md`` deferral "OAuth refresh stays
operator-driven via the access-token field". The Google adapters no
longer read a static Settings bearer directly — they ask a
``TokenProvider`` for a currently-valid access token.

- ``StaticTokenProvider`` preserves the operator-wired-bearer path
  (e.g. an OAuth Playground access token pasted into ``.env``).
- ``GoogleOAuthTokenProvider`` exchanges a long-lived refresh token
  for short-lived access tokens automatically, caching each token
  until just before it expires, so PMs stop re-pasting hourly tokens.

Network is exercised through ``httpx.MockTransport`` — no real socket
opens. The monotonic clock is injected so token-expiry behaviour is
deterministic without ``sleep``.
"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import parse_qs

import httpx
import pytest

from competitionops.adapters.token_provider_google import (
    GoogleOAuthTokenProvider,
    TokenRefreshError,
)
from competitionops.adapters.token_provider_static import StaticTokenProvider
from competitionops.ports import TokenProvider


class _FakeClock:
    """Deterministic stand-in for ``time.monotonic`` — tests advance it
    explicitly instead of sleeping."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _refresh_handler(
    *,
    access_token: str = "ya29.fresh",
    expires_in: int = 3600,
    status: int = 200,
    body: dict[str, Any] | None = None,
    capture: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a MockTransport handler emulating Google's token endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        if body is not None:
            return httpx.Response(status, json=body)
        return httpx.Response(
            status,
            json={
                "access_token": access_token,
                "expires_in": expires_in,
                "token_type": "Bearer",
            },
        )

    return handler


# ---------------------------------------------------------------------------
# StaticTokenProvider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_provider_returns_configured_token() -> None:
    provider = StaticTokenProvider("ya29.static-bearer")
    assert await provider.get_access_token() == "ya29.static-bearer"


@pytest.mark.asyncio
async def test_static_provider_is_stable_across_calls() -> None:
    provider = StaticTokenProvider("ya29.static-bearer")
    first = await provider.get_access_token()
    second = await provider.get_access_token()
    assert first == second == "ya29.static-bearer"


def test_static_provider_satisfies_token_provider_protocol() -> None:
    # The annotation is the mypy-checked conformance anchor; the runtime
    # assertion guards against the method being renamed.
    provider: TokenProvider = StaticTokenProvider("ya29.x")
    assert callable(provider.get_access_token)


# ---------------------------------------------------------------------------
# GoogleOAuthTokenProvider — refresh + caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_google_provider_refreshes_and_returns_access_token() -> None:
    provider = GoogleOAuthTokenProvider(
        client_id="cid",
        client_secret="csecret",
        refresh_token="1//rt",
        client=_mock_client(_refresh_handler(access_token="ya29.fresh")),
    )
    assert await provider.get_access_token() == "ya29.fresh"


@pytest.mark.asyncio
async def test_google_provider_sends_refresh_grant_body() -> None:
    captured: list[httpx.Request] = []
    provider = GoogleOAuthTokenProvider(
        client_id="cid",
        client_secret="csecret",
        refresh_token="1//rt",
        client=_mock_client(_refresh_handler(capture=captured)),
    )
    await provider.get_access_token()

    assert len(captured) == 1
    sent = parse_qs(captured[0].content.decode())
    assert sent["grant_type"] == ["refresh_token"]
    assert sent["refresh_token"] == ["1//rt"]
    assert sent["client_id"] == ["cid"]
    assert sent["client_secret"] == ["csecret"]


@pytest.mark.asyncio
async def test_google_provider_caches_token_within_lifetime() -> None:
    captured: list[httpx.Request] = []
    clock = _FakeClock()
    provider = GoogleOAuthTokenProvider(
        client_id="cid",
        client_secret="cs",
        refresh_token="1//rt",
        client=_mock_client(_refresh_handler(expires_in=3600, capture=captured)),
        clock=clock,
    )
    await provider.get_access_token()
    clock.advance(1800)  # half the lifetime — still fresh
    await provider.get_access_token()

    assert len(captured) == 1  # second call served from cache


@pytest.mark.asyncio
async def test_google_provider_refreshes_again_after_expiry() -> None:
    captured: list[httpx.Request] = []
    clock = _FakeClock()
    provider = GoogleOAuthTokenProvider(
        client_id="cid",
        client_secret="cs",
        refresh_token="1//rt",
        client=_mock_client(_refresh_handler(expires_in=3600, capture=captured)),
        clock=clock,
    )
    await provider.get_access_token()
    clock.advance(3600)  # full lifetime elapsed
    await provider.get_access_token()

    assert len(captured) == 2  # cache expired — refreshed again


@pytest.mark.asyncio
async def test_google_provider_refreshes_within_expiry_skew() -> None:
    # With a 60s skew the token is treated as stale 60s early so an
    # adapter never carries a token that expires mid-request.
    captured: list[httpx.Request] = []
    clock = _FakeClock()
    provider = GoogleOAuthTokenProvider(
        client_id="cid",
        client_secret="cs",
        refresh_token="1//rt",
        client=_mock_client(_refresh_handler(expires_in=3600, capture=captured)),
        clock=clock,
        expiry_skew_seconds=60.0,
    )
    await provider.get_access_token()
    clock.advance(3600 - 30)  # inside the 60s skew window
    await provider.get_access_token()

    assert len(captured) == 2


# ---------------------------------------------------------------------------
# GoogleOAuthTokenProvider — error handling + redaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_google_provider_raises_on_error_status() -> None:
    provider = GoogleOAuthTokenProvider(
        client_id="cid",
        client_secret="cs",
        refresh_token="1//rt",
        client=_mock_client(
            _refresh_handler(status=400, body={"error": "invalid_grant"})
        ),
    )
    with pytest.raises(TokenRefreshError):
        await provider.get_access_token()


@pytest.mark.asyncio
async def test_google_provider_error_does_not_leak_refresh_token() -> None:
    provider = GoogleOAuthTokenProvider(
        client_id="cid",
        client_secret="cs",
        refresh_token="1//SUPER-SECRET-RT",
        client=_mock_client(
            _refresh_handler(status=401, body={"error": "invalid_client"})
        ),
    )
    with pytest.raises(TokenRefreshError) as excinfo:
        await provider.get_access_token()
    assert "SUPER-SECRET-RT" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_google_provider_raises_when_response_missing_access_token() -> None:
    provider = GoogleOAuthTokenProvider(
        client_id="cid",
        client_secret="cs",
        refresh_token="1//rt",
        client=_mock_client(_refresh_handler(body={"expires_in": 3600})),
    )
    with pytest.raises(TokenRefreshError):
        await provider.get_access_token()


@pytest.mark.asyncio
async def test_google_provider_network_error_raises_token_refresh_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    provider = GoogleOAuthTokenProvider(
        client_id="cid",
        client_secret="cs",
        refresh_token="1//rt",
        client=_mock_client(handler),
    )
    with pytest.raises(TokenRefreshError):
        await provider.get_access_token()


@pytest.mark.asyncio
async def test_google_provider_raises_on_non_dict_json_body() -> None:
    # A 200 response whose JSON body is an array (not an object) must
    # raise TokenRefreshError, not a bare AttributeError from .get().
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    provider = GoogleOAuthTokenProvider(
        client_id="cid",
        client_secret="cs",
        refresh_token="1//rt",
        client=_mock_client(handler),
    )
    with pytest.raises(TokenRefreshError):
        await provider.get_access_token()


def test_google_provider_satisfies_token_provider_protocol() -> None:
    provider: TokenProvider = GoogleOAuthTokenProvider(
        client_id="c", client_secret="s", refresh_token="r"
    )
    assert callable(provider.get_access_token)
