"""LangGraph node functions for the CompetitionOps workflow.

Each node is a pure function ``(state) -> partial_state_update``:
- Reads what it needs from ``CompetitionOpsState``.
- Returns a partial dict that LangGraph merges into the next state.
- Does not directly mutate ``state``.

The execute / audit nodes reach into ``competitionops.main`` to reuse
the same ``_plan_repo / _audit_log / _registry`` singletons that the
FastAPI surface uses. This means a workflow-driven run lands its audit
records into the same store as an HTTP-driven run — the persistence
configuration (``AUDIT_LOG_DIR``) applies uniformly.
"""

from __future__ import annotations

from typing import Any

from competitionops.config import get_settings
from competitionops.schemas import (
    ActionPlan,
    CompetitionBrief,
    TeamMember,
)
from competitionops.services.brief_extractor import BriefExtractor
from competitionops.services.execution import ExecutionService
from competitionops.services.planner import CompetitionPlanner
from competitionops.workflows.state import CompetitionOpsState


def extract_node(state: CompetitionOpsState) -> dict[str, Any]:
    """Run ``BriefExtractor`` against ``state.raw_brief_text``."""
    raw_text = state.get("raw_brief_text", "")
    source_uri = state.get("source_uri")
    settings = get_settings()
    brief = BriefExtractor(settings=settings).extract_from_text(
        content=raw_text, source_uri=source_uri
    )
    return {"brief": brief.model_dump(mode="json")}


def plan_node(state: CompetitionOpsState) -> dict[str, Any]:
    """Run ``CompetitionPlanner`` against ``state.brief`` + team capacity."""
    brief_dict = state.get("brief")
    if not brief_dict:
        raise ValueError("plan_node requires state.brief — extract_node first")
    brief = CompetitionBrief.model_validate(brief_dict)

    team_list = state.get("team_capacity") or []
    team_members = [TeamMember.model_validate(item) for item in team_list]

    settings = get_settings()
    plan = CompetitionPlanner(settings=settings).generate(
        competition=brief, team_capacity=team_members
    )
    return {"plan": plan.model_dump(mode="json")}


def approve_node(state: CompetitionOpsState) -> dict[str, Any]:
    """Derive ``rejected_action_ids`` from ``approved_action_ids`` + plan.

    Runs AFTER the graph's interrupt — by the time this node executes
    the caller has set ``state.approved_action_ids`` via
    ``graph.update_state``. The node's only job is to compute the
    complementary rejected set so downstream observability has the full
    decision in one place.
    """
    plan_dict = state.get("plan")
    if not plan_dict:
        return {}
    plan = ActionPlan.model_validate(plan_dict)
    approved = set(state.get("approved_action_ids") or [])
    rejected = [
        action.action_id
        for action in plan.actions
        if action.action_id not in approved
    ]
    return {"rejected_action_ids": rejected}


async def execute_node(state: CompetitionOpsState) -> dict[str, Any]:
    """Run ``ExecutionService.approve_and_execute`` over the approved ids."""
    # Imported locally to avoid a top-level circular import — ``main``
    # depends on this package via the upcoming HTTP workflow endpoints.
    from competitionops import main as main_module

    plan_dict = state.get("plan")
    if not plan_dict:
        return {
            "executed": [],
            "skipped": [],
            "failed": [],
            "blocked": [],
        }
    plan = ActionPlan.model_validate(plan_dict)

    plan_repo = main_module._plan_repo()
    plan_repo.save(plan)

    service = ExecutionService(
        plan_repo=plan_repo,
        registry=main_module._registry(),
        audit_log=main_module._audit_log(),
        settings=get_settings(),
    )

    actor = state.get("actor") or "workflow@example.com"
    approved_ids = state.get("approved_action_ids") or []
    response = await service.approve_and_execute(
        plan_id=plan.plan_id,
        approved_action_ids=approved_ids,
        approved_by=actor,
    )

    return {
        "executed": [r.model_dump(mode="json") for r in response.executed],
        "skipped": [r.model_dump(mode="json") for r in response.skipped],
        "failed": [r.model_dump(mode="json") for r in response.failed],
        "blocked": [r.model_dump(mode="json") for r in response.blocked],
    }


def audit_node(state: CompetitionOpsState) -> dict[str, Any]:
    """Snapshot all audit records for the plan into the final state."""
    from competitionops import main as main_module

    plan_dict = state.get("plan")
    if not plan_dict:
        return {"audit_records": []}
    plan_id = plan_dict.get("plan_id", "")
    records = main_module._audit_log().list_for_plan(plan_id)
    return {"audit_records": [r.model_dump(mode="json") for r in records]}
