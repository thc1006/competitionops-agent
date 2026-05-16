"""``runtime._token_provider`` selection + Settings + registry wiring.

The factory picks the highest-capability provider the operator has
configured, the new ``google_oauth_refresh_token`` Settings field is a
masked secret, and ``build_default_registry`` threads the chosen
provider into the four Google adapters so ``real_mode`` reflects it.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from conftest import reset_runtime_caches  # noqa: I001

from competitionops.adapters.google_drive import GoogleDriveAdapter
from competitionops.adapters.registry import build_default_registry
from competitionops.adapters.token_provider_google import GoogleOAuthTokenProvider
from competitionops.adapters.token_provider_static import StaticTokenProvider
from competitionops.config import Settings

_GOOGLE_ENV = (
    "GOOGLE_OAUTH_ACCESS_TOKEN",
    "GOOGLE_OAUTH_REFRESH_TOKEN",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
)
_GOOGLE_TARGETS = ("google_drive", "google_docs", "google_sheets", "google_calendar")


def _clear_google_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _GOOGLE_ENV:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# runtime._token_provider — selection
# ---------------------------------------------------------------------------


def test_factory_returns_none_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_google_env(monkeypatch)
    reset_runtime_caches()
    from competitionops import runtime

    assert runtime._token_provider() is None


def test_factory_returns_static_provider_for_bearer_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_google_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "ya29.bearer")
    reset_runtime_caches()
    from competitionops import runtime

    assert isinstance(runtime._token_provider(), StaticTokenProvider)


def test_factory_returns_google_provider_for_refresh_trio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_google_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_OAUTH_REFRESH_TOKEN", "1//refresh")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")
    reset_runtime_caches()
    from competitionops import runtime

    assert isinstance(runtime._token_provider(), GoogleOAuthTokenProvider)


def test_factory_prefers_refresh_trio_over_static_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A static bearer AND a full refresh trio — the auto-refreshing
    # provider wins so the operator is not stuck on an hourly token.
    monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "ya29.bearer")
    monkeypatch.setenv("GOOGLE_OAUTH_REFRESH_TOKEN", "1//refresh")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")
    reset_runtime_caches()
    from competitionops import runtime

    assert isinstance(runtime._token_provider(), GoogleOAuthTokenProvider)


def test_factory_falls_back_to_static_when_refresh_trio_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Refresh token without client credentials cannot refresh — the
    # factory must not build a half-configured GoogleOAuthTokenProvider.
    _clear_google_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_OAUTH_REFRESH_TOKEN", "1//refresh")
    monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "ya29.bearer")
    reset_runtime_caches()
    from competitionops import runtime

    assert isinstance(runtime._token_provider(), StaticTokenProvider)


# ---------------------------------------------------------------------------
# Settings field
# ---------------------------------------------------------------------------


def test_settings_loads_refresh_token_as_secret() -> None:
    settings = Settings(google_oauth_refresh_token=SecretStr("1//refresh-secret"))
    assert settings.google_oauth_refresh_token is not None
    assert settings.google_oauth_refresh_token.get_secret_value() == "1//refresh-secret"


def test_settings_refresh_token_is_masked_in_repr() -> None:
    settings = Settings(google_oauth_refresh_token=SecretStr("1//refresh-secret"))
    assert "1//refresh-secret" not in repr(settings)
    assert "1//refresh-secret" not in settings.model_dump_json()


# ---------------------------------------------------------------------------
# build_default_registry — provider threading
# ---------------------------------------------------------------------------


def test_build_default_registry_threads_provider_into_google_adapters() -> None:
    registry = build_default_registry(
        token_provider=StaticTokenProvider("ya29.bearer")
    )
    for target in _GOOGLE_TARGETS:
        adapter = registry.get(target)
        assert adapter is not None
        assert adapter.real_mode is True, f"{target} should be in real mode"


def test_build_default_registry_without_provider_stays_mock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_google_env(monkeypatch)
    reset_runtime_caches()
    registry = build_default_registry()
    for target in _GOOGLE_TARGETS:
        adapter = registry.get(target)
        assert adapter is not None
        assert adapter.real_mode is False, f"{target} should stay in mock mode"


def test_google_adapter_accepts_explicit_token_provider() -> None:
    adapter = GoogleDriveAdapter(token_provider=StaticTokenProvider("ya29.bearer"))
    assert adapter.real_mode is True
