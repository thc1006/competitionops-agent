"""Sprint 2 — ExecutionService span coverage.

These tests attach an ``InMemorySpanExporter`` to the global TracerProvider
once per module, then for each test case clear the exporter and assert the
span shape produced by a single ExecutionService call:

- One root span named ``execution.<method>``.
- N child ``execution.adapter_call`` spans, one per adapter dispatch.

Later sprints add attribute assertions and metric counters; here we only
lock in the names and counts so the rest of the instrumentation work has
a stable contract to build on.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

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
from competitionops.telemetry import setup_tracer_provider

TZ = ZoneInfo("Asia/Taipei")
FIXED_NOW = datetime(2026, 5, 13, 9, 0, tzinfo=TZ)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _attach_exporter() -> InMemorySpanExporter:
    """Install an InMemorySpanExporter onto the process-wide TracerProvider.

    Module-scoped: OTel SDK only allows one SpanProcessor pipeline per provider,
    so we attach once and let each test clear the exporter via the
    ``span_exporter`` fixture below.
    """
    provider = setup_tracer_provider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


@pytest.fixture
def span_exporter(_attach_exporter: InMemorySpanExporter) -> InMemorySpanExporter:
    _attach_exporter.clear()
    return _attach_exporter


# ---------------------------------------------------------------------------
# Setup helper
# ---------------------------------------------------------------------------


def _build_setup() -> tuple[ExecutionService, ActionPlan]:
    settings = Settings()
    plan_repo = InMemoryPlanRepository()
    audit = InMemoryAuditLog()
    registry = build_default_registry()
    brief = CompetitionBrief(
        competition_id="span-test",
        name="Span Test Cup",
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


def _span_names(exporter: InMemorySpanExporter) -> list[str]:
    return [span.name for span in exporter.get_finished_spans()]


# ---------------------------------------------------------------------------
# Span tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_and_execute_emits_root_and_adapter_spans(
    span_exporter: InMemorySpanExporter,
) -> None:
    service, plan = _build_setup()
    approved_ids = [action.action_id for action in plan.actions]

    await service.approve_and_execute(
        plan_id=plan.plan_id,
        approved_action_ids=approved_ids,
        approved_by="pm@example.com",
    )

    names = _span_names(span_exporter)
    assert names.count("execution.approve_and_execute") == 1
    adapter_spans = [
        span
        for span in span_exporter.get_finished_spans()
        if span.name == "execution.adapter_call"
    ]
    assert len(adapter_spans) == len(approved_ids)
    # Every adapter_call span is a child of the root span (same trace)
    root = next(
        span
        for span in span_exporter.get_finished_spans()
        if span.name == "execution.approve_and_execute"
    )
    for span in adapter_spans:
        assert span.context.trace_id == root.context.trace_id


def test_approve_actions_emits_root_only(
    span_exporter: InMemorySpanExporter,
) -> None:
    service, plan = _build_setup()
    target = plan.actions[0].action_id

    service.approve_actions(
        plan_id=plan.plan_id,
        approved_action_ids=[target],
        approved_by="pm@example.com",
    )

    names = _span_names(span_exporter)
    assert names.count("execution.approve_actions") == 1
    assert names.count("execution.adapter_call") == 0


def test_approve_single_action_emits_root_only(
    span_exporter: InMemorySpanExporter,
) -> None:
    service, plan = _build_setup()
    target = plan.actions[0].action_id

    service.approve_single_action(
        plan_id=plan.plan_id,
        action_id=target,
        approved_by="pm@example.com",
    )

    names = _span_names(span_exporter)
    assert names.count("execution.approve_single_action") == 1
    assert names.count("execution.adapter_call") == 0


@pytest.mark.asyncio
async def test_run_approved_emits_adapter_call_per_approved_action(
    span_exporter: InMemorySpanExporter,
) -> None:
    service, plan = _build_setup()
    target = plan.actions[0].action_id

    # Approve first (no adapter spans yet) then clear the exporter so the
    # span counts below measure only run_approved's contribution.
    service.approve_actions(
        plan_id=plan.plan_id,
        approved_action_ids=[target],
        approved_by="pm@example.com",
    )
    span_exporter.clear()

    await service.run_approved(
        plan_id=plan.plan_id,
        executed_by="pm@example.com",
    )

    names = _span_names(span_exporter)
    assert names.count("execution.run_approved") == 1
    # exactly one adapter call because only one action was approved
    assert names.count("execution.adapter_call") == 1


@pytest.mark.asyncio
async def test_run_approved_without_any_approved_emits_no_adapter_call(
    span_exporter: InMemorySpanExporter,
) -> None:
    service, plan = _build_setup()

    # No approvals — call run_approved directly. All actions are still
    # ``pending`` so the dispatcher must skip every one of them.
    await service.run_approved(
        plan_id=plan.plan_id,
        executed_by="pm@example.com",
    )

    names = _span_names(span_exporter)
    assert names.count("execution.run_approved") == 1
    assert names.count("execution.adapter_call") == 0
