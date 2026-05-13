from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from competitionops.adapters.memory_audit import InMemoryAuditLog
from competitionops.adapters.memory_plan_store import InMemoryPlanRepository
from competitionops.adapters.registry import AdapterRegistry
from competitionops.config import Settings
from competitionops.schemas import (
    ActionStatus,
    CompetitionBrief,
    Deliverable,
    ExternalAction,
    ExternalActionResult,
    RiskLevel,
    TeamMember,
)
from competitionops.services.execution import FORBIDDEN_ACTION_TYPES, ExecutionService
from competitionops.services.planner import CompetitionPlanner

TZ = ZoneInfo("Asia/Taipei")
FIXED_NOW = datetime(2026, 5, 13, 9, 0, tzinfo=TZ)


class TrackingAdapter:
    """Mock executor that records every call and never reaches the network."""

    def __init__(self, target_system: str) -> None:
        self.target_system = target_system
        self.calls: list[dict[str, Any]] = []

    async def execute(
        self, action: ExternalAction, dry_run: bool = True
    ) -> ExternalActionResult:
        self.calls.append(
            {"action_id": action.action_id, "type": action.type, "dry_run": dry_run}
        )
        return ExternalActionResult(
            action_id=action.action_id,
            target_system=action.target_system,
            status="executed" if not dry_run else "dry_run",
            external_id=f"ext_{action.action_id}",
            external_url=f"https://example.invalid/{action.action_id}",
            message=f"Tracked execution of {action.type}",
        )


def _build_setup() -> tuple[
    ExecutionService,
    InMemoryPlanRepository,
    InMemoryAuditLog,
    dict[str, TrackingAdapter],
    str,
]:
    settings = Settings()
    plan_repo = InMemoryPlanRepository()
    audit = InMemoryAuditLog()
    registry = AdapterRegistry()

    adapters: dict[str, TrackingAdapter] = {
        target: TrackingAdapter(target)
        for target in (
            "google_drive",
            "google_docs",
            "google_sheets",
            "google_calendar",
            "plane",
        )
    }
    for target, adapter in adapters.items():
        registry.register(target, adapter)

    brief = CompetitionBrief(
        competition_id="demo",
        name="Demo Cup",
        submission_deadline=datetime(2026, 9, 30, 23, 59, tzinfo=TZ),
        deliverables=[
            Deliverable(title="Pitch deck", owner_role="business"),
        ],
    )
    team = [
        TeamMember(member_id="m1", name="Alice", role="business", weekly_capacity_hours=20)
    ]
    plan = CompetitionPlanner(settings).generate(brief, team_capacity=team, now=FIXED_NOW)
    plan_repo.save(plan)

    service = ExecutionService(
        plan_repo=plan_repo, registry=registry, audit_log=audit, settings=settings
    )
    return service, plan_repo, audit, adapters, plan.plan_id


@pytest.mark.asyncio
async def test_pending_action_cannot_execute_without_approval() -> None:
    service, plan_repo, audit, adapters, plan_id = _build_setup()
    plan = plan_repo.get(plan_id)
    assert plan is not None
    for action in plan.actions:
        assert action.status == ActionStatus.pending

    result = await service.approve_and_execute(
        plan_id=plan_id, approved_action_ids=[], approved_by="pm@example.com"
    )

    assert result.executed == []
    assert result.failed == []
    assert {r.action_id for r in result.skipped} == {a.action_id for a in plan.actions}
    assert sum(len(a.calls) for a in adapters.values()) == 0
    assert all(
        record.status == "skipped"
        for record in audit.list_for_plan(plan_id)
    )


@pytest.mark.asyncio
async def test_approved_action_executes_through_mock_adapter() -> None:
    service, plan_repo, audit, adapters, plan_id = _build_setup()
    plan = plan_repo.get(plan_id)
    assert plan is not None
    approved_ids = [a.action_id for a in plan.actions]

    result = await service.approve_and_execute(
        plan_id=plan_id, approved_action_ids=approved_ids, approved_by="pm@example.com"
    )

    assert len(result.executed) == len(plan.actions)
    assert result.failed == []
    assert result.skipped == []
    assert result.blocked == []
    assert sum(len(a.calls) for a in adapters.values()) == len(plan.actions)

    updated = plan_repo.get(plan_id)
    assert updated is not None
    for action in updated.actions:
        assert action.status == ActionStatus.executed
        assert action.approved is True


@pytest.mark.asyncio
async def test_rejected_actions_remain_unexecuted() -> None:
    service, plan_repo, audit, adapters, plan_id = _build_setup()
    plan = plan_repo.get(plan_id)
    assert plan is not None
    target_action = plan.actions[0]

    result = await service.approve_and_execute(
        plan_id=plan_id,
        approved_action_ids=[target_action.action_id],
        approved_by="pm@example.com",
    )

    assert [r.action_id for r in result.executed] == [target_action.action_id]
    assert len(result.skipped) == len(plan.actions) - 1
    assert sum(len(a.calls) for a in adapters.values()) == 1

    updated = plan_repo.get(plan_id)
    assert updated is not None
    for action in updated.actions:
        if action.action_id == target_action.action_id:
            assert action.status == ActionStatus.executed
        else:
            assert action.status == ActionStatus.rejected


@pytest.mark.asyncio
async def test_dangerous_action_is_blocked_before_adapter_call() -> None:
    service, plan_repo, audit, adapters, plan_id = _build_setup()
    plan = plan_repo.get(plan_id)
    assert plan is not None
    dangerous_type = "google.drive.delete_file"
    assert dangerous_type in FORBIDDEN_ACTION_TYPES

    dangerous = ExternalAction(
        action_id="act_danger",
        type=dangerous_type,
        target_system="google_drive",
        payload={"file_id": "anything"},
        requires_approval=True,
        risk_level=RiskLevel.critical,
    )
    plan.actions.append(dangerous)
    plan_repo.save(plan)

    result = await service.approve_and_execute(
        plan_id=plan_id,
        approved_action_ids=["act_danger"],
        approved_by="pm@example.com",
    )

    blocked_ids = [r.action_id for r in result.blocked]
    assert "act_danger" in blocked_ids
    assert all(r.action_id != "act_danger" for r in result.executed)

    drive_calls = [c for c in adapters["google_drive"].calls if c["action_id"] == "act_danger"]
    assert drive_calls == []

    blocked_records = [
        r for r in audit.list_for_plan(plan_id) if r.action_id == "act_danger"
    ]
    assert any(r.status == "blocked" for r in blocked_records)


@pytest.mark.asyncio
async def test_audit_log_generated_for_each_lifecycle_event() -> None:
    service, plan_repo, audit, adapters, plan_id = _build_setup()
    plan = plan_repo.get(plan_id)
    assert plan is not None
    first, second = plan.actions[0], plan.actions[1]

    await service.approve_and_execute(
        plan_id=plan_id,
        approved_action_ids=[first.action_id],
        approved_by="pm@example.com",
    )

    records = audit.list_for_plan(plan_id)
    approval_for_first = [
        r for r in records if r.action_id == first.action_id and r.status == "approved"
    ]
    execution_for_first = [
        r for r in records if r.action_id == first.action_id and r.status == "executed"
    ]
    skipped_for_second = [
        r for r in records if r.action_id == second.action_id and r.status == "skipped"
    ]

    assert len(approval_for_first) == 1
    assert len(execution_for_first) == 1
    assert len(skipped_for_second) == 1

    for record in approval_for_first + execution_for_first:
        assert record.plan_id == plan_id
        assert record.actor == "pm@example.com"
        assert record.action_type == first.type
        assert record.target_system == first.target_system
        assert record.request_hash is not None
        assert record.approved_by == "pm@example.com"
    assert execution_for_first[0].executed_at is not None
    assert execution_for_first[0].target_external_id == f"ext_{first.action_id}"


@pytest.mark.asyncio
async def test_idempotent_approval_blocks_double_execution_unless_explicit() -> None:
    service, plan_repo, audit, adapters, plan_id = _build_setup()
    plan = plan_repo.get(plan_id)
    assert plan is not None
    target = plan.actions[0]
    target_system = target.target_system

    first_run = await service.approve_and_execute(
        plan_id=plan_id,
        approved_action_ids=[target.action_id],
        approved_by="pm@example.com",
    )
    assert [r.action_id for r in first_run.executed] == [target.action_id]
    assert len(adapters[target_system].calls) == 1

    second_run = await service.approve_and_execute(
        plan_id=plan_id,
        approved_action_ids=[target.action_id],
        approved_by="pm@example.com",
    )
    assert all(r.action_id != target.action_id for r in second_run.executed)
    assert any(
        r.action_id == target.action_id and r.message.lower().__contains__("already")
        for r in second_run.skipped
    )
    assert len(adapters[target_system].calls) == 1

    third_run = await service.approve_and_execute(
        plan_id=plan_id,
        approved_action_ids=[target.action_id],
        approved_by="pm@example.com",
        allow_reexecute=True,
    )
    assert any(r.action_id == target.action_id for r in third_run.executed)
    assert len(adapters[target_system].calls) == 2
