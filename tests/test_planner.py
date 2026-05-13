from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from competitionops.config import Settings
from competitionops.schemas import (
    CompetitionBrief,
    Deliverable,
    Priority,
    TaskStatus,
    TeamMember,
)
from competitionops.services.planner import CompetitionPlanner

TZ = ZoneInfo("Asia/Taipei")
WRITE_SYSTEMS = {
    "google_drive",
    "google_docs",
    "google_sheets",
    "google_calendar",
    "plane",
}


def _planner() -> CompetitionPlanner:
    return CompetitionPlanner(Settings())


def _brief(
    *,
    competition_id: str = "demo",
    name: str = "Demo Competition",
    deadline: datetime | None = None,
    deliverables: list[Deliverable] | None = None,
) -> CompetitionBrief:
    return CompetitionBrief(
        competition_id=competition_id,
        name=name,
        submission_deadline=deadline,
        deliverables=deliverables or [],
    )


def test_planner_generates_dry_run_actions_requiring_approval() -> None:
    competition = _brief(
        deadline=datetime(2026, 6, 15, 23, 59, tzinfo=TZ),
        deliverables=[Deliverable(title="Pitch deck", description="10 pages")],
    )

    plan = _planner().generate(competition)

    assert plan.dry_run is True
    assert plan.requires_approval is True
    assert len(plan.actions) >= 3
    assert all(action.requires_approval for action in plan.actions)


def test_normal_planning_assigns_owner_and_due_date() -> None:
    deadline = datetime(2026, 9, 30, 23, 59, tzinfo=TZ)
    competition = _brief(
        competition_id="normal",
        name="Normal Cup",
        deadline=deadline,
        deliverables=[
            Deliverable(title="Pitch deck", owner_role="business"),
            Deliverable(title="Demo video", owner_role="design"),
            Deliverable(title="Prototype", owner_role="tech"),
        ],
    )
    team = [
        TeamMember(member_id="m1", name="Alice", role="business", weekly_capacity_hours=20),
        TeamMember(member_id="m2", name="Bob", role="design", weekly_capacity_hours=20),
        TeamMember(member_id="m3", name="Carol", role="tech", weekly_capacity_hours=20),
    ]

    plan = _planner().generate(competition, team_capacity=team)

    assert len(plan.task_drafts) >= 3
    assert {t.source_requirement for t in plan.task_drafts} == {
        "Pitch deck",
        "Demo video",
        "Prototype",
    }
    for task in plan.task_drafts:
        assert task.title
        assert task.owner_role is not None
        assert task.due_date is not None
        assert task.due_date < deadline
        assert task.priority in {Priority.p0, Priority.p1, Priority.p2}
        assert task.status == TaskStatus.draft
        assert task.suggested_assignee in {"m1", "m2", "m3"}
    assert "team_capacity_insufficient" not in plan.risk_flags
    assert "deliverable_missing_owner" not in plan.risk_flags


def test_planner_short_deadline_clamps_checkpoints_and_flags_risk() -> None:
    now = datetime.now(TZ).replace(microsecond=0)
    near_deadline = now + timedelta(days=3)
    competition = _brief(
        competition_id="rush",
        name="Rush Cup",
        deadline=near_deadline,
        deliverables=[Deliverable(title="Pitch deck", owner_role="business")],
    )

    plan = _planner().generate(competition, now=now)

    assert "short_deadline" in plan.risk_flags
    calendar_actions = [a for a in plan.actions if a.target_system == "google_calendar"]
    for action in calendar_actions:
        start = datetime.fromisoformat(action.payload["start"])
        end = datetime.fromisoformat(action.payload["end"])
        assert start >= now
        assert end <= near_deadline
    for task in plan.task_drafts:
        assert task.due_date is None or task.due_date <= near_deadline


def test_planner_flags_insufficient_team_capacity() -> None:
    deadline = datetime(2026, 9, 30, 23, 59, tzinfo=TZ)
    competition = _brief(
        competition_id="big",
        name="Big Cup",
        deadline=deadline,
        deliverables=[
            Deliverable(title=f"Deliverable {i}", owner_role="tech") for i in range(10)
        ],
    )
    team = [
        TeamMember(member_id="solo", name="Solo", role="tech", weekly_capacity_hours=5)
    ]

    plan = _planner().generate(competition, team_capacity=team)

    assert "team_capacity_insufficient" in plan.risk_flags
    blocked = [t for t in plan.task_drafts if t.status == TaskStatus.blocked_no_capacity]
    assert blocked, "expected at least one task blocked by capacity"
    for task in blocked:
        assert task.suggested_assignee is None
    # plane issue actions must not be emitted for blocked tasks
    plane_actions = [a for a in plan.actions if a.target_system == "plane"]
    blocked_titles = {t.source_requirement for t in blocked}
    for action in plane_actions:
        assert action.payload.get("source_requirement") not in blocked_titles


def test_planner_blocks_task_when_owner_role_missing() -> None:
    deadline = datetime(2026, 9, 30, 23, 59, tzinfo=TZ)
    competition = _brief(
        competition_id="orphan",
        name="Orphan Deliverable Cup",
        deadline=deadline,
        deliverables=[
            Deliverable(title="Mystery Doc", description="No owner specified"),
            Deliverable(title="Pitch deck", description="Owned", owner_role="business"),
        ],
    )
    team = [
        TeamMember(member_id="m1", name="Alice", role="business", weekly_capacity_hours=20)
    ]

    plan = _planner().generate(competition, team_capacity=team)

    blocked = [t for t in plan.task_drafts if t.status == TaskStatus.blocked_owner_missing]
    assert [t.source_requirement for t in blocked] == ["Mystery Doc"]
    assert "deliverable_missing_owner" in plan.risk_flags
    # un-blocked task should be assigned
    ready = [t for t in plan.task_drafts if t.status == TaskStatus.draft]
    assert len(ready) == 1
    assert ready[0].suggested_assignee == "m1"
    # blocked deliverable should not produce a plane issue
    plane_targets = {a.payload.get("source_requirement") for a in plan.actions if a.target_system == "plane"}
    assert "Mystery Doc" not in plane_targets


def test_planner_is_deterministic_across_runs() -> None:
    deadline = datetime(2026, 9, 30, 23, 59, tzinfo=TZ)
    competition = _brief(
        competition_id="determ",
        name="Determinism Cup",
        deadline=deadline,
        deliverables=[
            Deliverable(title="Pitch deck", owner_role="business"),
            Deliverable(title="Demo video", owner_role="design"),
        ],
    )
    team = [
        TeamMember(member_id="m1", name="Alice", role="business", weekly_capacity_hours=20),
        TeamMember(member_id="m2", name="Bob", role="design", weekly_capacity_hours=20),
    ]
    fixed_now = datetime(2026, 5, 13, 9, 0, tzinfo=TZ)

    p1 = _planner().generate(competition, team_capacity=team, now=fixed_now)
    p2 = _planner().generate(competition, team_capacity=team, now=fixed_now)

    assert p1.plan_id == p2.plan_id
    assert [a.action_id for a in p1.actions] == [a.action_id for a in p2.actions]
    assert [t.task_id for t in p1.task_drafts] == [t.task_id for t in p2.task_drafts]
    # action_id slug is hash-based, not uuid
    for action in p1.actions:
        assert action.action_id.startswith("act_")
        assert len(action.action_id) == len("act_") + 8


def test_planner_requires_approval_on_all_external_writes() -> None:
    deadline = datetime(2026, 9, 30, 23, 59, tzinfo=TZ)
    competition = _brief(
        competition_id="approval",
        name="Approval Cup",
        deadline=deadline,
        deliverables=[Deliverable(title="Pitch deck", owner_role="business")],
    )

    plan = _planner().generate(competition)

    assert plan.dry_run is True
    assert plan.requires_approval is True
    for action in plan.actions:
        assert action.target_system in WRITE_SYSTEMS or action.target_system == "internal"
        assert action.requires_approval is True
        assert action.approved is False
