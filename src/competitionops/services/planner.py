import hashlib
import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from competitionops.config import Settings
from competitionops.schemas import (
    ActionPlan,
    CompetitionBrief,
    Deliverable,
    ExternalAction,
    Priority,
    RiskLevel,
    TaskDraft,
    TaskStatus,
    TeamMember,
)

_TZ = ZoneInfo("Asia/Taipei")
_DEFAULT_TASK_EFFORT_HOURS = 8.0
_DEFAULT_BUFFER_DAYS = 2
_CHECKPOINT_OFFSETS: list[tuple[str, timedelta]] = [
    ("Kickoff", timedelta(days=30)),
    ("First draft ready", timedelta(days=14)),
    ("Mock pitch", timedelta(days=7)),
    ("Submission dry run", timedelta(days=1)),
]
_PROPOSAL_SECTIONS = [
    "Problem",
    "Solution",
    "Technical Innovation",
    "Business Feasibility",
    "Execution Plan",
    "Risks",
]


def _stable_hash(text: str, length: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def _canonical_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)


class CompetitionPlanner:
    """Deterministic, hexagonal-friendly planner.

    Pure domain logic: given a CompetitionBrief plus optional team capacity,
    produces an ActionPlan with task drafts and dry-run external actions.
    Never reaches network or external systems.
    """

    def __init__(
        self,
        settings: Settings,
        buffer_days: int = _DEFAULT_BUFFER_DAYS,
        task_effort_hours: float = _DEFAULT_TASK_EFFORT_HOURS,
    ) -> None:
        self.settings = settings
        self.buffer_days = buffer_days
        self.task_effort_hours = task_effort_hours

    def generate(
        self,
        competition: CompetitionBrief,
        team_capacity: list[TeamMember] | None = None,
        now: datetime | None = None,
    ) -> ActionPlan:
        anchor_now = now or datetime.now(_TZ)
        team = team_capacity or []
        risk_flags: list[str] = []

        task_drafts = self._build_task_drafts(competition, team, anchor_now, risk_flags)
        ready_titles = {
            t.source_requirement for t in task_drafts if t.status == TaskStatus.draft
        }

        actions: list[ExternalAction] = []
        actions.append(self._build_drive_folder_action(competition))
        actions.append(self._build_docs_outline_action(competition))
        actions.append(self._build_sheets_tracking_action(competition))
        actions.extend(
            self._build_calendar_checkpoint_actions(competition, anchor_now, risk_flags)
        )
        actions.extend(
            self._build_plane_issue_actions(competition, task_drafts, ready_titles)
        )

        plan_id = self._stable_plan_id(competition, actions)
        return ActionPlan(
            plan_id=plan_id,
            competition_id=competition.competition_id,
            dry_run=self.settings.dry_run_default,
            actions=actions,
            task_drafts=task_drafts,
            risk_level=RiskLevel.medium,
            requires_approval=self.settings.approval_required,
            risk_flags=risk_flags,
        )

    # ------------------------------------------------------------------
    # Task drafts
    # ------------------------------------------------------------------

    def _build_task_drafts(
        self,
        competition: CompetitionBrief,
        team: list[TeamMember],
        anchor_now: datetime,
        risk_flags: list[str],
    ) -> list[TaskDraft]:
        deadline = competition.submission_deadline
        baseline_due = self._compute_baseline_due(deadline, anchor_now)

        total_effort = self.task_effort_hours * len(competition.deliverables)
        total_capacity = sum(
            max(member.weekly_capacity_hours - member.current_load_hours, 0.0)
            for member in team
        )
        capacity_insufficient = bool(team) and total_capacity < total_effort
        if capacity_insufficient:
            risk_flags.append("team_capacity_insufficient")

        role_index = self._build_role_index(team)
        tasks: list[TaskDraft] = []
        owner_missing = False
        for deliverable in competition.deliverables:
            owner_role = deliverable.owner_role
            status = TaskStatus.draft
            suggested: str | None = None

            if owner_role is None:
                status = TaskStatus.blocked_owner_missing
                owner_missing = True
            elif capacity_insufficient:
                status = TaskStatus.blocked_no_capacity
            elif team:
                suggested = self._suggest_assignee(role_index, owner_role)

            due_date = self._compute_due_date(
                deliverable, baseline_due, anchor_now, deadline
            )
            priority = self._compute_priority(due_date, anchor_now)

            tasks.append(
                TaskDraft(
                    title=f"Prepare {deliverable.title}",
                    description=deliverable.description
                    or f"Deliverable: {deliverable.title}",
                    priority=priority,
                    owner_role=owner_role,
                    suggested_assignee=suggested,
                    due_date=due_date,
                    source_requirement=deliverable.title,
                    acceptance_criteria=[
                        f"Deliverable '{deliverable.title}' meets the brief's format requirements",
                    ],
                    status=status,
                    estimated_effort_hours=self.task_effort_hours,
                    task_id=self._stable_task_id(competition, deliverable),
                )
            )

        if owner_missing:
            risk_flags.append("deliverable_missing_owner")
        return tasks

    def _compute_baseline_due(
        self, deadline: datetime | None, anchor_now: datetime
    ) -> datetime | None:
        if deadline is None:
            return None
        target = deadline - timedelta(days=self.buffer_days)
        if target < anchor_now:
            target = max(deadline - timedelta(hours=1), anchor_now)
        if target > deadline:
            target = deadline
        return target

    def _compute_due_date(
        self,
        deliverable: Deliverable,
        baseline_due: datetime | None,
        anchor_now: datetime,
        deadline: datetime | None,
    ) -> datetime | None:
        candidate = deliverable.deadline or baseline_due
        if candidate is None:
            return None
        if deadline is not None and candidate > deadline:
            candidate = deadline
        if candidate < anchor_now:
            candidate = deadline if deadline is not None else anchor_now
        return candidate

    def _compute_priority(
        self, due_date: datetime | None, anchor_now: datetime
    ) -> Priority:
        if due_date is None:
            return Priority.p1
        if due_date - anchor_now <= timedelta(days=7):
            return Priority.p0
        return Priority.p1

    def _build_role_index(self, team: list[TeamMember]) -> dict[str, list[TeamMember]]:
        ordered = sorted(
            team,
            key=lambda m: (
                -(m.weekly_capacity_hours - m.current_load_hours),
                m.member_id,
            ),
        )
        result: dict[str, list[TeamMember]] = {}
        for member in ordered:
            result.setdefault(member.role.lower(), []).append(member)
        return result

    def _suggest_assignee(
        self, role_index: dict[str, list[TeamMember]], owner_role: str
    ) -> str | None:
        candidates = role_index.get(owner_role.lower())
        if not candidates:
            return None
        return candidates[0].member_id

    # ------------------------------------------------------------------
    # External actions
    # ------------------------------------------------------------------

    def _build_drive_folder_action(self, competition: CompetitionBrief) -> ExternalAction:
        payload: dict[str, Any] = {
            "competition_id": competition.competition_id,
            "competition_name": competition.name,
            "folder_name": f"Competition - {competition.name}",
        }
        return self._make_action(
            competition,
            action_type="google.drive.create_competition_folder",
            target_system="google_drive",
            payload=payload,
            risk_level=RiskLevel.medium,
        )

    def _build_docs_outline_action(self, competition: CompetitionBrief) -> ExternalAction:
        payload: dict[str, Any] = {
            "competition_id": competition.competition_id,
            "title": f"{competition.name} Proposal Discussion",
            "sections": _PROPOSAL_SECTIONS,
        }
        return self._make_action(
            competition,
            action_type="google.docs.create_proposal_outline",
            target_system="google_docs",
            payload=payload,
            risk_level=RiskLevel.medium,
        )

    def _build_sheets_tracking_action(self, competition: CompetitionBrief) -> ExternalAction:
        payload: dict[str, Any] = {
            "competition_id": competition.competition_id,
            "row": {
                "name": competition.name,
                "organizer": competition.organizer,
                "submission_deadline": (
                    competition.submission_deadline.isoformat()
                    if competition.submission_deadline is not None
                    else None
                ),
                "deliverable_count": len(competition.deliverables),
            },
        }
        return self._make_action(
            competition,
            action_type="google.sheets.append_tracking_row",
            target_system="google_sheets",
            payload=payload,
            risk_level=RiskLevel.medium,
        )

    def _build_calendar_checkpoint_actions(
        self,
        competition: CompetitionBrief,
        anchor_now: datetime,
        risk_flags: list[str],
    ) -> list[ExternalAction]:
        deadline = competition.submission_deadline
        if deadline is None:
            return []
        emitted: list[ExternalAction] = []
        for title, offset in _CHECKPOINT_OFFSETS:
            start = deadline - offset
            if start < anchor_now:
                continue
            end = start + timedelta(hours=1)
            if end > deadline:
                end = deadline
            payload: dict[str, Any] = {
                "competition_id": competition.competition_id,
                "title": f"{competition.name}: {title}",
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
            emitted.append(
                self._make_action(
                    competition,
                    action_type="google.calendar.create_event",
                    target_system="google_calendar",
                    payload=payload,
                    risk_level=RiskLevel.medium,
                )
            )
        if len(emitted) < len(_CHECKPOINT_OFFSETS):
            risk_flags.append("short_deadline")
        return emitted

    def _build_plane_issue_actions(
        self,
        competition: CompetitionBrief,
        task_drafts: list[TaskDraft],
        ready_titles: set[str | None],
    ) -> list[ExternalAction]:
        actions: list[ExternalAction] = []
        for task in task_drafts:
            if task.source_requirement not in ready_titles:
                continue
            payload: dict[str, Any] = {
                "competition_id": competition.competition_id,
                "title": task.title,
                "description": task.description,
                "owner_role": task.owner_role,
                "suggested_assignee": task.suggested_assignee,
                "due_date": task.due_date.isoformat() if task.due_date else None,
                "priority": task.priority.value,
                "source_requirement": task.source_requirement,
                "task_id": task.task_id,
            }
            actions.append(
                self._make_action(
                    competition,
                    action_type="plane.create_issue",
                    target_system="plane",
                    payload=payload,
                    risk_level=RiskLevel.medium,
                )
            )
        return actions

    # ------------------------------------------------------------------
    # Stable identifiers
    # ------------------------------------------------------------------

    def _make_action(
        self,
        competition: CompetitionBrief,
        *,
        action_type: str,
        target_system: str,
        payload: dict[str, Any],
        risk_level: RiskLevel,
    ) -> ExternalAction:
        action_id = self._stable_action_id(competition, action_type, payload)
        return ExternalAction(
            action_id=action_id,
            type=action_type,
            target_system=target_system,  # type: ignore[arg-type]
            payload=payload,
            requires_approval=True,
            risk_level=risk_level,
        )

    def _stable_action_id(
        self,
        competition: CompetitionBrief,
        action_type: str,
        payload: dict[str, Any],
    ) -> str:
        canonical = _canonical_payload(payload)
        digest = _stable_hash(
            f"{competition.competition_id}|{action_type}|{canonical}"
        )
        return f"act_{digest}"

    def _stable_task_id(
        self, competition: CompetitionBrief, deliverable: Deliverable
    ) -> str:
        digest = _stable_hash(
            f"{competition.competition_id}|task|{deliverable.title}"
        )
        return f"task_{digest}"

    def _stable_plan_id(
        self, competition: CompetitionBrief, actions: list[ExternalAction]
    ) -> str:
        sorted_ids = sorted(action.action_id for action in actions)
        digest = _stable_hash(
            competition.competition_id + "|" + "|".join(sorted_ids), length=10
        )
        return f"plan_{digest}"
