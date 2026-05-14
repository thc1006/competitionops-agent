"""TracerProvider + MeterProvider bootstrap.

Idempotent by design: OTel's ``trace.set_tracer_provider`` and
``metrics.set_meter_provider`` are both guarded by an internal
``_*_SET_ONCE`` flag that ignores re-installation. We honor that contract
by returning the existing provider whenever one is already in place.
The first caller wins.
"""

from __future__ import annotations

from typing import Sequence

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.trace import TracerProvider


def setup_tracer_provider() -> TracerProvider:
    """Return the process-global TracerProvider, creating it if missing."""
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        return current
    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    return provider


def setup_meter_provider(
    readers: Sequence[MetricReader] | None = None,
) -> MeterProvider:
    """Return the process-global MeterProvider, creating it if missing.

    Unlike the TracerProvider, MeterProvider's MetricReaders must be
    fixed at construction time — they cannot be added after. So either
    the production code wires its OTLP reader through ``readers``, or
    tests pass an InMemoryMetricReader, or the dev default is empty
    (metric instruments silently no-op).
    """
    current = metrics.get_meter_provider()
    if isinstance(current, MeterProvider):
        return current
    provider = MeterProvider(metric_readers=list(readers or []))
    metrics.set_meter_provider(provider)
    return provider
