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
from opentelemetry.trace import StatusCode

from competitionops.adapters.memory_audit import InMemoryAuditLog
from competitionops.adapters.memory_plan_store import InMemoryPlanRepository
from competitionops.adapters.registry import build_default_registry
from competitionops.config import Settings
from competitionops.schemas import (
    ActionPlan,
    CompetitionBrief,
    Deliverable,
    ExternalAction,
    ExternalActionResult,
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


# ---------------------------------------------------------------------------
# Sprint 3 — attribute coverage + error status mapping
# ---------------------------------------------------------------------------


class _RaisingAdapter:
    """Mock adapter that raises an exception instead of returning a result.

    Used to verify OTel's default ``set_status_on_exception=True`` behavior
    on the adapter_call span (M1).
    """

    async def execute(
        self, action: ExternalAction, dry_run: bool = True
    ) -> ExternalActionResult:
        raise RuntimeError(
            f"simulated network failure on adapter for {action.action_id}"
        )


class _FailingResultAdapter:
    """Mock adapter that returns ``status='failed'`` without raising.

    Used to verify the explicit ``set_status(ERROR)`` mapping (M2) — without
    this, the with-block exits cleanly and the span shows UNSET / OK while
    the audit log says ``failed``.
    """

    async def execute(
        self, action: ExternalAction, dry_run: bool = True
    ) -> ExternalActionResult:
        return ExternalActionResult(
            action_id=action.action_id,
            target_system="google_drive",
            status="failed",
            error="simulated business-level failure",
            message="adapter returned failed status",
        )


def _build_setup_with_drive_override(drive_adapter: object) -> tuple[ExecutionService, ActionPlan]:
    """Same shape as ``_build_setup`` but with google_drive overridden."""
    settings = Settings()
    plan_repo = InMemoryPlanRepository()
    audit = InMemoryAuditLog()
    registry = build_default_registry()
    registry.register("google_drive", drive_adapter)  # type: ignore[arg-type]
    brief = CompetitionBrief(
        competition_id="span-error",
        name="Span Error Cup",
        submission_deadline=datetime(2026, 9, 30, 23, 59, tzinfo=TZ),
        deliverables=[Deliverable(title="Pitch deck", owner_role="business")],
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


@pytest.mark.asyncio
async def test_adapter_call_span_records_exception_when_adapter_raises(
    span_exporter: InMemorySpanExporter,
) -> None:
    """M1: when adapter raises, OTel auto-records exception event + ERROR status."""
    service, plan = _build_setup_with_drive_override(_RaisingAdapter())
    drive_action = next(
        action for action in plan.actions if action.target_system == "google_drive"
    )

    service.approve_actions(
        plan_id=plan.plan_id,
        approved_action_ids=[drive_action.action_id],
        approved_by="pm@example.com",
    )
    span_exporter.clear()

    response = await service.run_approved(
        plan_id=plan.plan_id,
        executed_by="pm@example.com",
        action_ids=[drive_action.action_id],
    )
    assert [r.action_id for r in response.failed] == [drive_action.action_id]

    adapter_spans = [
        span
        for span in span_exporter.get_finished_spans()
        if span.name == "execution.adapter_call"
    ]
    assert len(adapter_spans) == 1
    drive_span = adapter_spans[0]
    assert drive_span.status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" for event in drive_span.events)


@pytest.mark.asyncio
async def test_adapter_call_span_marks_error_when_result_status_failed(
    span_exporter: InMemorySpanExporter,
) -> None:
    """M2: adapter returns status=failed (no raise) -> span must still be ERROR."""
    service, plan = _build_setup_with_drive_override(_FailingResultAdapter())
    drive_action = next(
        action for action in plan.actions if action.target_system == "google_drive"
    )

    service.approve_actions(
        plan_id=plan.plan_id,
        approved_action_ids=[drive_action.action_id],
        approved_by="pm@example.com",
    )
    span_exporter.clear()

    response = await service.run_approved(
        plan_id=plan.plan_id,
        executed_by="pm@example.com",
        action_ids=[drive_action.action_id],
    )
    assert [r.action_id for r in response.failed] == [drive_action.action_id]

    adapter_spans = [
        span
        for span in span_exporter.get_finished_spans()
        if span.name == "execution.adapter_call"
    ]
    assert len(adapter_spans) == 1
    drive_span = adapter_spans[0]
    assert drive_span.status.status_code == StatusCode.ERROR
    assert "simulated business-level failure" in (drive_span.status.description or "")


@pytest.mark.asyncio
async def test_root_spans_carry_plan_id_and_actor_attributes(
    span_exporter: InMemorySpanExporter,
) -> None:
    """M3-root: approve_and_execute / run_approved / approve_* spans carry plan_id + actor."""
    service, plan = _build_setup()
    target = plan.actions[0].action_id

    # Exercise all four public methods to capture each root-span shape.
    await service.approve_and_execute(
        plan_id=plan.plan_id,
        approved_action_ids=[target],
        approved_by="pm@example.com",
    )

    service.approve_actions(
        plan_id=plan.plan_id,
        approved_action_ids=[target],
        approved_by="pm@example.com",
    )

    service.approve_single_action(
        plan_id=plan.plan_id,
        action_id=target,
        approved_by="pm@example.com",
    )

    await service.run_approved(
        plan_id=plan.plan_id,
        executed_by="executor@example.com",
        action_ids=[target],
        allow_reexecute=True,
    )

    finished = span_exporter.get_finished_spans()
    root_names_to_expected_actor = {
        "execution.approve_and_execute": "pm@example.com",
        "execution.approve_actions": "pm@example.com",
        "execution.approve_single_action": "pm@example.com",
        "execution.run_approved": "executor@example.com",
    }
    for span_name, expected_actor in root_names_to_expected_actor.items():
        candidates = [span for span in finished if span.name == span_name]
        assert candidates, f"missing root span {span_name}"
        root = candidates[-1]
        assert root.attributes is not None
        assert root.attributes["plan_id"] == plan.plan_id
        assert root.attributes["actor"] == expected_actor


@pytest.mark.asyncio
async def test_adapter_call_span_carries_plan_id_attribute(
    span_exporter: InMemorySpanExporter,
) -> None:
    """M3-child: adapter_call spans expose plan_id alongside action_id / target_system / action_type."""
    service, plan = _build_setup()
    drive_action = next(
        action for action in plan.actions if action.target_system == "google_drive"
    )

    await service.approve_and_execute(
        plan_id=plan.plan_id,
        approved_action_ids=[drive_action.action_id],
        approved_by="pm@example.com",
    )

    matching = [
        span
        for span in span_exporter.get_finished_spans()
        if span.name == "execution.adapter_call"
        and span.attributes is not None
        and span.attributes.get("action_id") == drive_action.action_id
    ]
    assert len(matching) == 1
    drive_span = matching[0]
    attrs = drive_span.attributes
    assert attrs is not None
    assert attrs["plan_id"] == plan.plan_id
    assert attrs["target_system"] == "google_drive"
    assert attrs["action_type"]
    assert attrs["action_id"] == drive_action.action_id
