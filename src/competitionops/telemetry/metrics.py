"""OTel metric instruments for CompetitionOps.

Three module-level instruments are created on import via the global
ProxyMeter. They stay silent no-ops until a real SDK MeterProvider with
at least one MetricReader is installed (typically by tests through
``setup_meter_provider(readers=[InMemoryMetricReader()])``, or in
production by wiring an OTLP exporter through ``setup_meter_provider``).

Why proxy-then-resolve: it lets ``services/execution.py`` import these
counters unconditionally without forcing every test file (or dev run)
to attach an exporter. Production code can opt in by setting a provider
once at app startup.
"""

from __future__ import annotations

from opentelemetry import metrics

_meter = metrics.get_meter("competitionops")

actions_total = _meter.create_counter(
    name="competitionops.actions.total",
    description=(
        "Lifecycle transitions emitted by ExecutionService: one count per "
        "approved / rejected / blocked / skipped / executed / failed event."
    ),
    unit="1",
)

audit_records_total = _meter.create_counter(
    name="competitionops.audit.records.total",
    description="AuditRecord rows written to the audit log port.",
    unit="1",
)

action_execution_duration_seconds = _meter.create_histogram(
    name="competitionops.action.execution.duration_seconds",
    description=(
        "Wall-clock duration of a single adapter.execute() call, "
        "including both successful and failed dispatches."
    ),
    unit="s",
)
