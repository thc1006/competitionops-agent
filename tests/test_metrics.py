"""Sprint 5 — Counter / Histogram metric coverage.

The OTel MeterProvider's metric readers must be fixed at construction
time, so this test file installs a single ``InMemoryMetricReader``
attached to one ``MeterProvider`` for the whole session. Tests compare
counter values BEFORE and AFTER their action to defeat cross-test
accumulation (other test files also exercise ExecutionService and
contribute to the same counters once the SDK MeterProvider is set).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from competitionops.adapters.memory_audit import InMemoryAuditLog
from competitionops.adapters.memory_plan_store import InMemoryPlanRepository
from competitionops.adapters.registry import build_default_registry
from competitionops.config import Settings
from competitionops.schemas import (
    ActionPlan,
    CompetitionBrief,
    Deliverable,
    TeamMember,
)
from competitionops.services.execution import ExecutionService
from competitionops.services.planner import CompetitionPlanner

TZ = ZoneInfo("Asia/Taipei")
FIXED_NOW = datetime(2026, 5, 13, 9, 0, tzinfo=TZ)


# ---------------------------------------------------------------------------
# Session-scoped MeterProvider with InMemory reader
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def metric_reader() -> InMemoryMetricReader:
    """Install one SDK MeterProvider with an InMemory reader for the session.

    OTel's ``metrics.set_meter_provider`` is once-only; we honor that by
    only setting if no SDK provider is already in place.
    """
    reader = InMemoryMetricReader()
    current = metrics.get_meter_provider()
    if not isinstance(current, MeterProvider):
        metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
        return reader
    # Already set by a previous fixture/test: we cannot replace it.
    # The current readers (if InMemory) become our source.
    # This branch is defensive; in practice this file is the only one
    # that wires MeterProvider.
    return reader  # may be a fresh reader not actually attached


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_data_points(
    reader: InMemoryMetricReader, metric_name: str
) -> list[Any]:
    data = reader.get_metrics_data()
    if data is None:
        return []
    points: list[Any] = []
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == metric_name:
                    for point in metric.data.data_points:
                        points.append(point)
    return points


def _counter_value(
    reader: InMemoryMetricReader,
    metric_name: str,
    *,
    must_have: dict[str, Any],
) -> int:
    """Sum all counter data points whose attributes include the given subset."""
    total = 0
    for point in _collect_data_points(reader, metric_name):
        attrs = dict(point.attributes)
        if all(attrs.get(k) == v for k, v in must_have.items()):
            total += int(point.value)
    return total


def _histogram_count(
    reader: InMemoryMetricReader,
    metric_name: str,
    *,
    must_have: dict[str, Any],
) -> int:
    """Sum histogram counts whose attributes include the given subset."""
    total = 0
    for point in _collect_data_points(reader, metric_name):
        attrs = dict(point.attributes)
        if all(attrs.get(k) == v for k, v in must_have.items()):
            total += int(point.count)
    return total


def _build_setup() -> tuple[ExecutionService, ActionPlan]:
    """Per-test fresh repos/registry/audit + a unique plan_id via competition_id."""
    settings = Settings()
    plan_repo = InMemoryPlanRepository()
    audit = InMemoryAuditLog()
    registry = build_default_registry()
    brief = CompetitionBrief(
        competition_id="metrics-test",
        name="Metrics Test Cup",
        submission_deadline=datetime(2026, 9, 30, 23, 59, tzinfo=TZ),
        deliverables=[
            Deliverable(title="Pitch deck", owner_role="business"),
        ],
    )
    team = [
        TeamMember(
            member_id="m1",
            name="Alice",
            role="business",
            weekly_capacity_hours=20,
        )
    ]
    plan = CompetitionPlanner(settings).generate(
        brief, team_capacity=team, now=FIXED_NOW
    )
    plan_repo.save(plan)
    service = ExecutionService(
        plan_repo=plan_repo, registry=registry, audit_log=audit, settings=settings
    )
    return service, plan


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_actions_counter_increments_per_lifecycle_state(
    metric_reader: InMemoryMetricReader,
) -> None:
    """approve_and_execute over 1 approved action increments approved+executed
    counters by 1, and rejected counter by len(other_actions)."""
    service, plan = _build_setup()
    target_action = plan.actions[0]
    approved_id = target_action.action_id

    before_approved = _counter_value(
        metric_reader,
        "competitionops.actions.total",
        must_have={"state": "approved", "target_system": target_action.target_system},
    )
    before_executed = _counter_value(
        metric_reader,
        "competitionops.actions.total",
        must_have={"state": "executed", "target_system": target_action.target_system},
    )
    before_skipped_total = sum(
        _counter_value(
            metric_reader,
            "competitionops.actions.total",
            must_have={"state": "skipped"},
        )
        for _ in (None,)
    )

    await service.approve_and_execute(
        plan_id=plan.plan_id,
        approved_action_ids=[approved_id],
        approved_by="pm@example.com",
    )

    after_approved = _counter_value(
        metric_reader,
        "competitionops.actions.total",
        must_have={"state": "approved", "target_system": target_action.target_system},
    )
    after_executed = _counter_value(
        metric_reader,
        "competitionops.actions.total",
        must_have={"state": "executed", "target_system": target_action.target_system},
    )
    after_skipped_total = _counter_value(
        metric_reader,
        "competitionops.actions.total",
        must_have={"state": "skipped"},
    )

    assert after_approved - before_approved == 1
    assert after_executed - before_executed == 1
    # All other plan.actions get a skipped event (approve_and_execute path)
    expected_skipped = len(plan.actions) - 1
    assert after_skipped_total - before_skipped_total == expected_skipped


@pytest.mark.asyncio
async def test_audit_records_counter_tracks_every_lifecycle_event(
    metric_reader: InMemoryMetricReader,
) -> None:
    """audit_records_total has exactly one increment per AuditRecord written.
    For approve_and_execute over 1 approved id: 1 approved + 1 executed +
    (N-1) skipped audit events = N+1 total increments.
    """
    service, plan = _build_setup()
    target_action_id = plan.actions[0].action_id

    before = sum(
        int(point.value)
        for point in _collect_data_points(
            metric_reader, "competitionops.audit.records.total"
        )
    )

    await service.approve_and_execute(
        plan_id=plan.plan_id,
        approved_action_ids=[target_action_id],
        approved_by="pm@example.com",
    )

    after = sum(
        int(point.value)
        for point in _collect_data_points(
            metric_reader, "competitionops.audit.records.total"
        )
    )
    # 1 approved-audit + 1 executed-audit + (len(actions)-1) skipped-audits
    assert after - before == len(plan.actions) + 1


@pytest.mark.asyncio
async def test_action_execution_duration_histogram_records_adapter_latency(
    metric_reader: InMemoryMetricReader,
) -> None:
    """Every adapter dispatch contributes one histogram observation, attributed
    by target_system + result_status (dry_run / executed / failed / raised)."""
    service, plan = _build_setup()
    target_action = plan.actions[0]
    target_system = target_action.target_system

    before = _histogram_count(
        metric_reader,
        "competitionops.action.execution.duration_seconds",
        must_have={"target_system": target_system},
    )

    await service.approve_and_execute(
        plan_id=plan.plan_id,
        approved_action_ids=[target_action.action_id],
        approved_by="pm@example.com",
    )

    after = _histogram_count(
        metric_reader,
        "competitionops.action.execution.duration_seconds",
        must_have={"target_system": target_system},
    )
    assert after - before == 1

    # The recorded result_status must be one of the documented labels.
    points = _collect_data_points(
        metric_reader, "competitionops.action.execution.duration_seconds"
    )
    statuses_seen = {
        dict(point.attributes).get("result_status")
        for point in points
        if dict(point.attributes).get("target_system") == target_system
    }
    assert statuses_seen.issubset({"dry_run", "executed", "failed", "raised"})
    assert statuses_seen  # at least one observation present
