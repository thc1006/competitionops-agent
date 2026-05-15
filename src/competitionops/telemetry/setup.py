"""TracerProvider + MeterProvider bootstrap.

OTel's ``trace.set_tracer_provider`` and ``metrics.set_meter_provider``
are both **first-call-wins** — once a provider is global, subsequent
``set_*`` calls are ignored. Combined with the fact that a
``MeterProvider``'s ``metric_readers`` are constructor-only and cannot
be added after the fact, a careless second call to
``setup_meter_provider(readers=...)`` silently drops those readers on
the floor. That's a production-grade footgun: a deployment that
expected its OTLP reader to flow metrics could find itself emitting
nothing while every existing "no-crash" test still passed.

The helpers in this module fail loudly via ``OtelInstallOrderError``
when they detect a bad install order:

- ``setup_meter_provider(readers=[...])`` after a MeterProvider is
  already global ⇒ raises (M1 fix).
- ``_wire_otel_exporters`` in ``main`` checking for an SDK
  ``TracerProvider`` and not finding one ⇒ raises (M2 fix).

The legitimate idempotent path — calling either helper a second time
with no constructor-only arguments — still returns the existing
provider unchanged.
"""

from __future__ import annotations

import importlib.metadata
import os
from typing import Sequence

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

# P2-004 Sprint 6+ — resource attributes. Without a Resource the SDK
# default ``service.name`` is ``unknown_service:python``, leaving every
# exported trace + metric unattributed in the observability backend.
_DEFAULT_SERVICE_NAME = "competitionops-api"
_PACKAGE_NAME = "competitionops-agent"


def _package_version() -> str:
    """Installed package version, for the ``service.version`` resource
    attribute. Falls back to a sentinel when the package isn't
    installed as a distribution (e.g. a bare source checkout)."""
    try:
        return importlib.metadata.version(_PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        return "0.0.0+unknown"


def _build_resource() -> Resource:
    """Build the OTel ``Resource`` shared by the tracer + meter providers.

    Attributes:
    - ``service.name`` — ``OTEL_SERVICE_NAME`` env if set (resolved
      here so the operator's choice wins; ``Resource.create`` would
      otherwise let our explicit dict override the env), else the
      project default.
    - ``service.version`` — the installed package version.

    ``deployment.environment`` and any other operator attributes are
    NOT set here — operators supply them through the OTel-standard
    ``OTEL_RESOURCE_ATTRIBUTES`` env, which ``Resource.create`` merges
    automatically. Inventing a custom env var would fragment the
    config surface for no gain.
    """
    return Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME")
            or _DEFAULT_SERVICE_NAME,
            "service.version": _package_version(),
        }
    )


class OtelInstallOrderError(RuntimeError):
    """Raised when an OpenTelemetry provider has already been installed
    globally and the current caller is trying to attach configuration
    that can only land at provider construction time (notably:
    ``MeterReader``s on a ``MeterProvider``).

    The fix is always on the operator side: ensure the call that
    requests the configuration happens BEFORE any other call that
    installs a provider with default / different configuration.
    """


def setup_tracer_provider() -> TracerProvider:
    """Return the process-global TracerProvider, creating it if missing.

    Idempotent: re-calling never installs a new provider. Called once
    at FastAPI / MCP module-init so auto-instrumentation has a real
    SDK provider to attach to. The wiring path
    (``main._wire_otel_exporters``) deliberately does NOT call this
    again — it uses ``trace.get_tracer_provider()`` with an isinstance
    check so an embedder-installed foreign provider is detected and
    flagged via ``OtelInstallOrderError`` (M2 defence).
    """
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        return current
    provider = TracerProvider(resource=_build_resource())
    trace.set_tracer_provider(provider)
    return provider


def setup_meter_provider(
    readers: Sequence[MetricReader] | None = None,
) -> MeterProvider:
    """Return the process-global MeterProvider, creating it if missing.

    ``readers`` are attached at construction time only — OTel's
    MeterProvider has no API to add them later. So:

    - ``readers=None`` / ``readers=[]`` is the idempotent path: if a
      provider is already installed we return it; if not, we install
      one with an empty reader list (instruments silently no-op).
    - ``readers=[...]`` requires no provider to be installed yet. If
      one already is, we raise ``OtelInstallOrderError`` rather than
      silently dropping the readers (M1 fix). The caller is then
      responsible for either accepting the no-op (drop ``readers=``)
      or rearranging import order so this is the first install.
    """
    current = metrics.get_meter_provider()
    requested = list(readers or [])
    if isinstance(current, MeterProvider):
        if requested:
            raise OtelInstallOrderError(
                f"MeterProvider already installed; cannot attach "
                f"{len(requested)} new reader(s). OTel SDK requires "
                "metric readers at construction time. Fix by either "
                "(a) installing readers on the first call to "
                "``setup_meter_provider``, or (b) calling this without "
                "``readers=`` to no-op accept the existing provider."
            )
        return current
    provider = MeterProvider(
        resource=_build_resource(), metric_readers=requested
    )
    metrics.set_meter_provider(provider)
    return provider
