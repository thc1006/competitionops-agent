"""Sprint 0 — OpenTelemetry bootstrap.

These tests lock in two contracts for the telemetry module:

1. ``setup_tracer_provider()`` returns an SDK-backed ``TracerProvider`` and
   that provider is the one OTel returns through ``trace.get_tracer_provider()``.
2. ``setup_tracer_provider()`` is idempotent — re-calling it never installs
   a new provider, so unit tests can call it freely without polluting the
   OTel global state across test cases.

Later sprints (1+) build on this bootstrap to add manual spans, FastAPI
instrumentation, and metric counters.
"""

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from competitionops.telemetry import setup_tracer_provider


def test_setup_creates_tracer_provider() -> None:
    provider = setup_tracer_provider()
    assert isinstance(provider, TracerProvider)
    assert trace.get_tracer_provider() is provider


def test_setup_idempotent() -> None:
    first = setup_tracer_provider()
    second = setup_tracer_provider()
    assert first is second
    assert trace.get_tracer_provider() is first
