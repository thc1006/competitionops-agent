from functools import lru_cache

from fastapi import Depends, FastAPI, HTTPException, status

from competitionops.adapters.memory_audit import InMemoryAuditLog
from competitionops.adapters.memory_plan_store import InMemoryPlanRepository
from competitionops.adapters.registry import AdapterRegistry, build_default_registry
from competitionops.config import Settings, get_settings
from competitionops.schemas import (
    ActionPlan,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalResponse,
    BriefExtractRequest,
    CompetitionBrief,
    ExecutionRequest,
    PlanGenerateRequest,
)
from competitionops.services.brief_extractor import BriefExtractor
from competitionops.services.execution import ExecutionService, PlanNotFoundError
from competitionops.services.planner import CompetitionPlanner

app = FastAPI(title="CompetitionOps Agent", version="0.1.0")


@lru_cache(maxsize=1)
def _plan_repo() -> InMemoryPlanRepository:
    return InMemoryPlanRepository()


@lru_cache(maxsize=1)
def _audit_log() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@lru_cache(maxsize=1)
def _registry() -> AdapterRegistry:
    return build_default_registry()


def get_plan_repo() -> InMemoryPlanRepository:
    return _plan_repo()


def get_audit_log() -> InMemoryAuditLog:
    return _audit_log()


def get_registry() -> AdapterRegistry:
    return _registry()


def get_execution_service(
    plan_repo: InMemoryPlanRepository = Depends(get_plan_repo),
    registry: AdapterRegistry = Depends(get_registry),
    audit: InMemoryAuditLog = Depends(get_audit_log),
    settings: Settings = Depends(get_settings),
) -> ExecutionService:
    return ExecutionService(
        plan_repo=plan_repo,
        registry=registry,
        audit_log=audit,
        settings=settings,
    )


# ----------------------------------------------------------------------
# Health
# ----------------------------------------------------------------------


@app.get("/healthz")
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ----------------------------------------------------------------------
# Brief extraction
# ----------------------------------------------------------------------


@app.post("/briefs/extract", response_model=CompetitionBrief)
async def extract_brief(
    payload: BriefExtractRequest,
    settings: Settings = Depends(get_settings),
) -> CompetitionBrief:
    extractor = BriefExtractor(settings=settings)
    return extractor.extract_from_text(
        content=payload.content, source_uri=payload.source_uri
    )


# ----------------------------------------------------------------------
# Plan generation
# ----------------------------------------------------------------------


@app.post("/plans/generate", response_model=ActionPlan)
async def generate_plan(
    payload: PlanGenerateRequest,
    settings: Settings = Depends(get_settings),
    plan_repo: InMemoryPlanRepository = Depends(get_plan_repo),
) -> ActionPlan:
    planner = CompetitionPlanner(settings=settings)
    plan = planner.generate(
        competition=payload.competition,
        team_capacity=payload.team_capacity,
    )
    plan.requires_approval = (
        payload.preferences.pm_approval_required or plan.requires_approval
    )
    plan_repo.save(plan)
    return plan


# ----------------------------------------------------------------------
# Combined approve + execute (legacy, kept for backward compatibility)
# ----------------------------------------------------------------------


@app.post("/plans/{plan_id}/approve", response_model=ApprovalResponse)
async def approve_and_execute_plan(
    plan_id: str,
    payload: ApprovalRequest,
    service: ExecutionService = Depends(get_execution_service),
) -> ApprovalResponse:
    try:
        return await service.approve_and_execute(
            plan_id=plan_id,
            approved_action_ids=payload.approved_action_ids,
            approved_by=payload.approved_by,
            allow_reexecute=payload.allow_reexecute,
        )
    except PlanNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


# ----------------------------------------------------------------------
# Two-phase API: approve-only and run-only
# ----------------------------------------------------------------------


@app.post("/approvals/{plan_id}/approve", response_model=ApprovalDecision)
async def approve_plan(
    plan_id: str,
    payload: ApprovalRequest,
    service: ExecutionService = Depends(get_execution_service),
) -> ApprovalDecision:
    try:
        return service.approve_actions(
            plan_id=plan_id,
            approved_action_ids=payload.approved_action_ids,
            approved_by=payload.approved_by,
        )
    except PlanNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


@app.post("/executions/{plan_id}/run", response_model=ApprovalResponse)
async def run_plan_executions(
    plan_id: str,
    payload: ExecutionRequest,
    service: ExecutionService = Depends(get_execution_service),
) -> ApprovalResponse:
    try:
        return await service.run_approved(
            plan_id=plan_id,
            executed_by=payload.executed_by,
            action_ids=payload.action_ids,
            allow_reexecute=payload.allow_reexecute,
        )
    except PlanNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
