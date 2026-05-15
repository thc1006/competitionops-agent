from datetime import datetime
from enum import Enum
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator


class Priority(str, Enum):
    p0 = "P0"
    p1 = "P1"
    p2 = "P2"


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class TaskStatus(str, Enum):
    draft = "draft"
    blocked_owner_missing = "blocked_owner_missing"
    blocked_no_capacity = "blocked_no_capacity"


class ActionStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    blocked = "blocked"
    executed = "executed"
    failed = "failed"


class Deliverable(BaseModel):
    title: str
    description: str = ""
    format: str | None = None
    page_limit: int | None = None
    duration_limit_seconds: int | None = None
    language: str | None = None
    deadline: datetime | None = None
    owner_role: str | None = None


class ScoringRubricItem(BaseModel):
    title: str
    weight_percent: float | None = Field(default=None, ge=0, le=100)
    description: str = ""


class WebIngestionResult(BaseModel):
    """P1-006 — result of fetching a competition URL.

    ``url`` is the **canonical** URL after redirects (may differ from
    the request URL) and is used as ``CompetitionBrief.source_uri``.
    ``title`` and ``text`` feed the deterministic brief extractor.
    Real adapters (Crawl4AI / Playwright, P1-006 Sprint 2) populate
    ``text`` with cleaned markdown; the mock returns canned strings.
    """

    url: str
    title: str
    text: str


class CompetitionBrief(BaseModel):
    competition_id: str
    name: str
    organizer: str | None = None
    source_uri: str | None = None
    submission_deadline: datetime | None = None
    final_event_date: datetime | None = None
    eligibility: list[str] = Field(default_factory=list)
    deliverables: list[Deliverable] = Field(default_factory=list)
    scoring_rubric: list[ScoringRubricItem] = Field(default_factory=list)
    anonymous_rules: list[str] = Field(default_factory=list)
    language_requirements: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)


class TeamMember(BaseModel):
    member_id: str
    name: str
    role: str
    skills: list[str] = Field(default_factory=list)
    weekly_capacity_hours: float = Field(default=5, ge=0)
    current_load_hours: float = Field(default=0, ge=0)
    unavailable_dates: list[datetime] = Field(default_factory=list)


class TaskDraft(BaseModel):
    title: str
    description: str = ""
    priority: Priority = Priority.p1
    owner_role: str | None = None
    suggested_assignee: str | None = None
    due_date: datetime | None = None
    source_requirement: str | None = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.draft
    estimated_effort_hours: float = Field(default=8.0, ge=0)
    task_id: str | None = None


class CalendarEventDraft(BaseModel):
    title: str
    start: datetime
    end: datetime
    description: str = ""
    attendees: list[str] = Field(default_factory=list)
    requires_approval: bool = True


class ExternalAction(BaseModel):
    action_id: str
    type: str
    target_system: Literal[
        "google_drive",
        "google_docs",
        "google_sheets",
        "google_calendar",
        "plane",
        "internal",
    ]
    payload: dict[str, Any]
    requires_approval: bool = True
    risk_level: RiskLevel = RiskLevel.medium
    approved: bool = False
    status: ActionStatus = ActionStatus.pending


class ActionPlan(BaseModel):
    plan_id: str
    competition_id: str
    dry_run: bool = True
    actions: list[ExternalAction] = Field(default_factory=list)
    task_drafts: list[TaskDraft] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.medium
    requires_approval: bool = True
    risk_flags: list[str] = Field(default_factory=list)


class ExternalActionResult(BaseModel):
    action_id: str
    target_system: str
    status: Literal["dry_run", "executed", "skipped", "failed", "blocked"]
    external_id: str | None = None
    external_url: str | None = None
    message: str = ""
    error: str | None = None


class AuditRecord(BaseModel):
    """Audit log entry aligned with docs/05_security_oauth.md fields."""

    action_id: str
    plan_id: str
    actor: str
    action_type: str
    target_system: str
    target_external_id: str | None = None
    dry_run: bool = True
    approved_by: str | None = None
    approved_at: datetime | None = None
    executed_at: datetime | None = None
    status: Literal[
        "approved", "rejected", "blocked", "skipped", "executed", "failed"
    ]
    error: str | None = None
    request_hash: str | None = None


class BriefExtractRequest(BaseModel):
    """Request body for ``POST /briefs/extract``.

    ``source_type="text"`` is the only ingestion path implemented in the
    MVP — ``content`` is required. ``"url"`` and ``"drive"`` are accepted
    at validation time (SSRF allow-list runs here, Tier 0 #1) but the
    endpoint returns 501 until P1-006 lands the real fetcher.
    """

    source_type: Literal["text", "url", "drive"] = "text"
    source_uri: str | None = None
    content: str = ""

    @model_validator(mode="after")
    def _validate_source(self) -> Self:
        # Imported locally to keep schemas.py importable even if the security
        # subpackage is ever vendored separately.
        from competitionops.security.source_uri_validator import (
            UnsafeSourceURIError,
            assert_safe_drive_uri,
            assert_safe_url,
        )

        if self.source_type == "text":
            if not self.content:
                raise ValueError(
                    "content must be non-empty when source_type='text'"
                )
            return self

        if not self.source_uri:
            raise ValueError(
                f"source_uri is required when source_type={self.source_type!r}"
            )

        try:
            if self.source_type == "url":
                assert_safe_url(self.source_uri)
            elif self.source_type == "drive":
                assert_safe_drive_uri(self.source_uri)
        except UnsafeSourceURIError as exc:
            raise ValueError(f"unsafe source_uri: {exc}") from exc

        return self


class PlanPreferences(BaseModel):
    calendar_name: str | None = None
    pm_approval_required: bool = True


class PlanGenerateRequest(BaseModel):
    competition: CompetitionBrief
    team_capacity: list[TeamMember] = Field(default_factory=list)
    preferences: PlanPreferences = Field(default_factory=PlanPreferences)


class ApprovalRequest(BaseModel):
    approved_action_ids: list[str] = Field(default_factory=list)
    approved_by: str
    allow_reexecute: bool = False


class ApprovalResponse(BaseModel):
    plan_id: str
    executed: list[ExternalActionResult] = Field(default_factory=list)
    skipped: list[ExternalActionResult] = Field(default_factory=list)
    failed: list[ExternalActionResult] = Field(default_factory=list)
    blocked: list[ExternalActionResult] = Field(default_factory=list)


class ApprovalDecision(BaseModel):
    """Result of a pure-approval call (no execution)."""

    plan_id: str
    approved: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)
    blocked: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


class ExecutionRequest(BaseModel):
    """Body for ``POST /executions/{plan_id}/run``."""

    executed_by: str
    action_ids: list[str] | None = None
    allow_reexecute: bool = False
