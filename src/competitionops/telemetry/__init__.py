"""OpenTelemetry bootstrap surface for CompetitionOps.

Sprint 0 — TracerProvider setup.
Sprint 4 — exposes the shared ``traced_sync`` / ``traced_async`` / ``annotate_span``
primitives that both ExecutionService and the MCP server use to wrap
public entry points with root spans.
"""

from competitionops.telemetry.decorators import (
    annotate_span,
    traced_async,
    traced_sync,
)
from competitionops.telemetry.setup import setup_tracer_provider

__all__ = [
    "annotate_span",
    "setup_tracer_provider",
    "traced_async",
    "traced_sync",
]
