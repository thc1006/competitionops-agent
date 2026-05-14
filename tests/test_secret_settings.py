"""Tier 0 #2 — secret fields use ``pydantic.SecretStr``.

Locks the post-Stage-7 finding L4: ``Settings.anthropic_api_key``,
``Settings.google_oauth_client_secret``, ``Settings.plane_api_key`` must
never surface their raw value through repr / str / model_dump /
model_dump_json. The only way to read the underlying secret is the
explicit ``.get_secret_value()`` call.

These guards protect against accidental leakage via:
- ``log.info(settings)`` / ``print(settings)``
- error responses that include ``settings.model_dump()``
- structured-log middleware that JSON-dumps the Settings instance
- traceback frames printing local variables
"""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from competitionops.config import Settings

_SENSITIVE_FIELDS = (
    "anthropic_api_key",
    "google_oauth_client_secret",
    "plane_api_key",
)

# A high-entropy synthetic value that any masking failure would obviously
# expose. Never use a real credential here.
_SYNTHETIC_SECRET = "synthetic-supersecret-xyz789-ABCDEF-tier0-test"


@pytest.fixture
def populated_settings() -> Settings:
    """Build a Settings instance with all three secret fields populated."""
    return Settings(
        anthropic_api_key=_SYNTHETIC_SECRET + "-anthropic",
        google_oauth_client_secret=_SYNTHETIC_SECRET + "-google",
        plane_api_key=_SYNTHETIC_SECRET + "-plane",
    )


def test_secret_fields_default_to_none_without_env() -> None:
    """Unset secret fields are None, not the empty string."""
    settings = Settings()
    for field_name in _SENSITIVE_FIELDS:
        assert getattr(settings, field_name) is None, (
            f"{field_name} should default to None"
        )


def test_secret_fields_are_secretstr_instances_when_populated(
    populated_settings: Settings,
) -> None:
    for field_name in _SENSITIVE_FIELDS:
        value = getattr(populated_settings, field_name)
        assert isinstance(value, SecretStr), (
            f"{field_name} must be SecretStr, got {type(value).__name__}"
        )


def test_repr_does_not_leak_secret_values(populated_settings: Settings) -> None:
    rendered = repr(populated_settings)
    assert _SYNTHETIC_SECRET not in rendered, (
        "repr(Settings) leaked the synthetic secret"
    )


def test_str_does_not_leak_secret_values(populated_settings: Settings) -> None:
    rendered = str(populated_settings)
    assert _SYNTHETIC_SECRET not in rendered


def test_model_dump_does_not_leak_secret_values(
    populated_settings: Settings,
) -> None:
    dumped = populated_settings.model_dump()
    rendered = str(dumped)
    assert _SYNTHETIC_SECRET not in rendered


def test_model_dump_json_does_not_leak_secret_values(
    populated_settings: Settings,
) -> None:
    payload = populated_settings.model_dump_json()
    assert _SYNTHETIC_SECRET not in payload
    parsed = json.loads(payload)
    # The string form in JSON is the masking sentinel
    for field_name in _SENSITIVE_FIELDS:
        assert parsed[field_name] == "**********", (
            f"{field_name} JSON form should be the mask sentinel"
        )


def test_get_secret_value_returns_underlying_secret(
    populated_settings: Settings,
) -> None:
    """Adapters that legitimately need the secret call .get_secret_value()."""
    assert populated_settings.anthropic_api_key is not None
    assert populated_settings.anthropic_api_key.get_secret_value() == (
        _SYNTHETIC_SECRET + "-anthropic"
    )
    assert populated_settings.google_oauth_client_secret is not None
    assert populated_settings.google_oauth_client_secret.get_secret_value() == (
        _SYNTHETIC_SECRET + "-google"
    )
    assert populated_settings.plane_api_key is not None
    assert populated_settings.plane_api_key.get_secret_value() == (
        _SYNTHETIC_SECRET + "-plane"
    )


def test_non_secret_fields_remain_plain_strings() -> None:
    """Public fields like client_id / redirect_uri / base_url must NOT be
    SecretStr — they need to be loggable for debugging."""
    settings = Settings(
        google_oauth_client_id="public-client-id-12345",
        google_oauth_redirect_uri="http://example.invalid/callback",
        plane_base_url="https://plane.example.invalid",
    )
    assert settings.google_oauth_client_id == "public-client-id-12345"
    assert settings.google_oauth_redirect_uri == "http://example.invalid/callback"
    assert settings.plane_base_url == "https://plane.example.invalid"
    # These appear plainly in repr
    rendered = repr(settings)
    assert "public-client-id-12345" in rendered
