"""OpenTelemetry bootstrap surface for CompetitionOps.

Sprint 0 ships only the TracerProvider setup. Later sprints will add
manual spans inside ExecutionService, FastAPI auto-instrumentation, and
metric counters.
"""

from competitionops.telemetry.setup import setup_tracer_provider

__all__ = ["setup_tracer_provider"]
