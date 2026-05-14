"""OpenTelemetry bootstrap surface for CompetitionOps.

Sprint 0 — TracerProvider setup.
Sprint 4 — shared ``traced_sync`` / ``traced_async`` / ``annotate_span``
primitives that both ExecutionService and the MCP server use to wrap
public entry points with root spans.
Sprint 5 — Counter / Histogram metric instruments + MeterProvider setup.
"""

from competitionops.telemetry.decorators import (
    annotate_span,
    traced_async,
    traced_sync,
)
from competitionops.telemetry.metrics import (
    action_execution_duration_seconds,
    actions_total,
    audit_records_total,
)
from competitionops.telemetry.setup import (
    setup_meter_provider,
    setup_tracer_provider,
)

__all__ = [
    "action_execution_duration_seconds",
    "actions_total",
    "annotate_span",
    "audit_records_total",
    "setup_meter_provider",
    "setup_tracer_provider",
    "traced_async",
    "traced_sync",
]
