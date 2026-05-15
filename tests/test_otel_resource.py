"""P2-004 Sprint 6+ — OpenTelemetry Resource attributes.

Without a ``Resource`` the SDK's default ``service.name`` is
``unknown_service:python`` — every trace + metric exported through
OTLP shows up unattributed in Jaeger / Grafana / Tempo, with no way
to filter by service or version. This is the gap that undermines the
whole P2-004 observability investment.

``telemetry.setup._build_resource()`` constructs a Resource carrying:

- ``service.name`` — ``OTEL_SERVICE_NAME`` env if set, else the
  default ``competitionops-api``.
- ``service.version`` — the installed package version via
  ``importlib.metadata`` (the running code's version IS the version,
  no env override needed).
- ``deployment.environment`` and any other operator attributes flow
  in through the OTel-standard ``OTEL_RESOURCE_ATTRIBUTES`` env, which
  ``Resource.create`` merges automatically — we deliberately do NOT
  invent a custom env var for that.

Both ``setup_tracer_provider`` and ``setup_meter_provider`` pass the
resource into their provider constructors.
"""

from __future__ import annotations

import pytest

from competitionops.telemetry.setup import (
    _build_resource,
    setup_meter_provider,
    setup_tracer_provider,
)


# ---------------------------------------------------------------------------
# _build_resource — attribute composition
# ---------------------------------------------------------------------------


def test_build_resource_default_service_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no ``OTEL_SERVICE_NAME`` env, the resource carries the
    project default ``competitionops-api`` — never the SDK's
    ``unknown_service`` placeholder."""
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    resource = _build_resource()
    assert resource.attributes.get("service.name") == "competitionops-api"


def test_build_resource_honors_otel_service_name_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators override via the OTel-standard ``OTEL_SERVICE_NAME``.
    The resource must reflect that, not the hard-coded default —
    otherwise a multi-deployment operator can't distinguish services."""
    monkeypatch.setenv("OTEL_SERVICE_NAME", "competitionops-staging")
    resource = _build_resource()
    assert resource.attributes.get("service.name") == "competitionops-staging"


def test_build_resource_carries_service_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``service.version`` must be present and non-empty so traces /
    metrics can be sliced by release. Sourced from the installed
    package metadata; the exact value depends on the environment but
    it must never be blank."""
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    resource = _build_resource()
    version = resource.attributes.get("service.version")
    assert isinstance(version, str) and version, (
        f"service.version must be a non-empty string; got {version!r}"
    )


def test_build_resource_merges_otel_resource_attributes_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``deployment.environment`` (and arbitrary operator attributes)
    flow in through the OTel-standard ``OTEL_RESOURCE_ATTRIBUTES`` env
    — ``Resource.create`` merges it automatically. We deliberately do
    NOT invent a custom env var. This test pins that the standard
    mechanism survives our explicit-attribute dict (no key collision)."""
    monkeypatch.setenv(
        "OTEL_RESOURCE_ATTRIBUTES",
        "deployment.environment=staging,competition.region=apac",
    )
    resource = _build_resource()
    assert resource.attributes.get("deployment.environment") == "staging"
    assert resource.attributes.get("competition.region") == "apac"
    # Our explicit attrs must still be present alongside the env ones.
    assert resource.attributes.get("service.name") == "competitionops-api"


# ---------------------------------------------------------------------------
# Provider wiring — the resource reaches the actual providers
# ---------------------------------------------------------------------------


def test_setup_tracer_provider_attaches_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``setup_tracer_provider`` must construct its ``TracerProvider``
    WITH the resource. Force the construct branch by making
    ``get_tracer_provider`` report a non-SDK provider; ``set_*`` is
    swallowed so the real process-global is untouched. The function
    returns the freshly-built provider — inspect its ``.resource``."""
    from opentelemetry import trace
    from opentelemetry.trace import NoOpTracerProvider

    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: NoOpTracerProvider())
    monkeypatch.setattr(trace, "set_tracer_provider", lambda _p: None)

    provider = setup_tracer_provider()
    assert provider.resource.attributes.get("service.name") == "competitionops-api"
    assert provider.resource.attributes.get("service.version")


def test_setup_meter_provider_attaches_resource(
    monkeypatch: pytest.MonkeyPatch,
    isolated_meter_provider: None,
) -> None:
    """``setup_meter_provider`` must construct its ``MeterProvider``
    WITH the resource. The conftest ``isolated_meter_provider`` fixture
    reports a proxy provider (so the construct branch fires) and
    swallows the install — the function returns the freshly-built
    provider."""
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    provider = setup_meter_provider()
    # MeterProvider exposes the resource via its private _sdk_config
    # in current OTel; use the public-ish accessor when available.
    resource = getattr(provider, "_sdk_config", None)
    if resource is not None:
        attrs = resource.resource.attributes
    else:  # pragma: no cover - fallback for OTel API shifts
        attrs = provider.resource.attributes  # type: ignore[attr-defined]
    assert attrs.get("service.name") == "competitionops-api"
    assert attrs.get("service.version")
