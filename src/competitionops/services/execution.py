"""Approval gate + dry-run execution service.

Wraps the planner-produced ActionPlan with a hard policy:
- Every external write must be explicitly approved by action_id.
- Forbidden action types are blocked even when approved.
- Already-executed actions are skipped unless allow_reexecute=True.
- Every lifecycle event emits one AuditRecord.

The service never reaches the network itself — adapters do, and they are
swappable through AdapterRegistry so tests can inject tracking mocks.
"""

from __future__ import annotations

import functools
import hashlib
from datetime import datetime
from typing import Awaitable, Callable, Final, ParamSpec, TypeVar
from zoneinfo import ZoneInfo

from opentelemetry import trace

from competitionops.adapters.registry import AdapterRegistry
from competitionops.config import Settings
from competitionops.ports import AuditLogPort, PlanRepository
from competitionops.schemas import (
    ActionPlan,
    ActionStatus,
    ApprovalDecision,
    ApprovalResponse,
    AuditRecord,
    ExternalAction,
    ExternalActionResult,
)

_TZ = ZoneInfo("Asia/Taipei")
_tracer = trace.get_tracer("competitionops.execution")

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _traced_sync(name: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Wrap a sync method in an OTel root span named ``name``."""

    def decorator(func: Callable[_P, _R]) -> Callable[_P, _R]:
        @functools.wraps(func)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with _tracer.start_as_current_span(name):
                return func(*args, **kwargs)

        return wrapper

    return decorator


def _traced_async(
    name: str,
) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]:
    """Wrap an async method in an OTel root span named ``name``."""

    def decorator(
        func: Callable[_P, Awaitable[_R]],
    ) -> Callable[_P, Awaitable[_R]]:
        @functools.wraps(func)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with _tracer.start_as_current_span(name):
                return await func(*args, **kwargs)

        return wrapper

    return decorator

FORBIDDEN_ACTION_TYPES: Final[frozenset[str]] = frozenset(
    {
        "google.drive.delete_file",
        "google.drive.permissions.set_public",
        "google.drive.permissions.share_external",
        "gmail.send",
        "competition.external_submit",
    }
)


class PlanNotFoundError(LookupError):
    """Raised when an unknown plan_id is supplied to approve_and_execute."""


class ExecutionService:
    def __init__(
        self,
        plan_repo: PlanRepository,
        registry: AdapterRegistry,
        audit_log: AuditLogPort,
        settings: Settings,
    ) -> None:
        self.plan_repo = plan_repo
        self.registry = registry
        self.audit_log = audit_log
        self.settings = settings

    @_traced_async("execution.approve_and_execute")
    async def approve_and_execute(
        self,
        plan_id: str,
        approved_action_ids: list[str],
        approved_by: str,
        allow_reexecute: bool = False,
    ) -> ApprovalResponse:
        plan = self.plan_repo.get(plan_id)
        if plan is None:
            raise PlanNotFoundError(f"plan_id={plan_id!r} not found")

        approved_set = set(approved_action_ids)
        executed: list[ExternalActionResult] = []
        skipped: list[ExternalActionResult] = []
        failed: list[ExternalActionResult] = []
        blocked: list[ExternalActionResult] = []

        for action in plan.actions:
            if action.action_id not in approved_set:
                skipped.append(
                    self._handle_unapproved(plan, action, approved_by)
                )
                continue

            if action.type in FORBIDDEN_ACTION_TYPES:
                blocked.append(
                    self._handle_blocked(plan, action, approved_by)
                )
                continue

            if action.status == ActionStatus.executed and not allow_reexecute:
                skipped.append(
                    self._handle_already_executed(plan, action, approved_by)
                )
                continue

            result = await self._execute_approved(plan, action, approved_by)
            if result.status == "failed":
                failed.append(result)
            else:
                executed.append(result)

        self.plan_repo.save(plan)
        return ApprovalResponse(
            plan_id=plan_id,
            executed=executed,
            skipped=skipped,
            failed=failed,
            blocked=blocked,
        )

    # ------------------------------------------------------------------
    # Single-action approval (MCP incremental flow)
    # ------------------------------------------------------------------

    @_traced_sync("execution.approve_single_action")
    def approve_single_action(
        self,
        plan_id: str,
        action_id: str,
        approved_by: str,
    ) -> ApprovalDecision:
        """Approve exactly one action; leave the other actions untouched.

        Designed for the MCP incremental ``approve_action`` tool. The batch
        ``approve_actions`` rejects every non-listed action, which is the
        right semantics for ``POST /approvals/{plan_id}/approve`` but the
        wrong semantics when Claude wants to approve actions one at a time
        across multiple tool calls.
        """
        plan = self.plan_repo.get(plan_id)
        if plan is None:
            raise PlanNotFoundError(f"plan_id={plan_id!r} not found")

        approved: list[str] = []
        rejected: list[str] = []
        blocked: list[str] = []
        skipped: list[str] = []

        target = next(
            (a for a in plan.actions if a.action_id == action_id), None
        )
        if target is None:
            skipped.append(action_id)
            return ApprovalDecision(
                plan_id=plan_id,
                approved=approved,
                rejected=rejected,
                blocked=blocked,
                skipped=skipped,
            )

        if target.status == ActionStatus.executed:
            skipped.append(action_id)
        elif target.type in FORBIDDEN_ACTION_TYPES:
            target.status = ActionStatus.blocked
            target.approved = False
            blocked.append(action_id)
            self._emit_audit(
                plan,
                target,
                actor=approved_by,
                event_status="blocked",
                approved=False,
                error="forbidden_action_type",
            )
        else:
            target.status = ActionStatus.approved
            target.approved = True
            approved.append(action_id)
            self._emit_audit(
                plan,
                target,
                actor=approved_by,
                event_status="approved",
                approved=True,
            )

        self.plan_repo.save(plan)
        return ApprovalDecision(
            plan_id=plan_id,
            approved=approved,
            rejected=rejected,
            blocked=blocked,
            skipped=skipped,
        )

    # ------------------------------------------------------------------
    # Two-phase API: approve-only and run-only
    # ------------------------------------------------------------------

    @_traced_sync("execution.approve_actions")
    def approve_actions(
        self,
        plan_id: str,
        approved_action_ids: list[str],
        approved_by: str,
    ) -> ApprovalDecision:
        """Pure approval: transitions action statuses without calling adapters.

        - Actions whose type is in FORBIDDEN_ACTION_TYPES move to ``blocked``
          regardless of inclusion in ``approved_action_ids``.
        - Actions in ``approved_action_ids`` move to ``approved``.
        - Other actions move to ``rejected``.
        - Already-``executed`` actions are left untouched and surfaced as
          ``skipped`` in the decision so callers see the no-op explicitly.
        """
        plan = self.plan_repo.get(plan_id)
        if plan is None:
            raise PlanNotFoundError(f"plan_id={plan_id!r} not found")

        approved_set = set(approved_action_ids)
        approved: list[str] = []
        rejected: list[str] = []
        blocked: list[str] = []
        skipped: list[str] = []

        for action in plan.actions:
            if action.status == ActionStatus.executed:
                skipped.append(action.action_id)
                continue
            if action.type in FORBIDDEN_ACTION_TYPES:
                action.status = ActionStatus.blocked
                action.approved = False
                blocked.append(action.action_id)
                self._emit_audit(
                    plan,
                    action,
                    actor=approved_by,
                    event_status="blocked",
                    approved=False,
                    error="forbidden_action_type",
                )
                continue
            if action.action_id in approved_set:
                action.status = ActionStatus.approved
                action.approved = True
                approved.append(action.action_id)
                self._emit_audit(
                    plan,
                    action,
                    actor=approved_by,
                    event_status="approved",
                    approved=True,
                )
            else:
                action.status = ActionStatus.rejected
                action.approved = False
                rejected.append(action.action_id)
                self._emit_audit(
                    plan,
                    action,
                    actor=approved_by,
                    event_status="rejected",
                    approved=False,
                )

        self.plan_repo.save(plan)
        return ApprovalDecision(
            plan_id=plan_id,
            approved=approved,
            rejected=rejected,
            blocked=blocked,
            skipped=skipped,
        )

    @_traced_async("execution.run_approved")
    async def run_approved(
        self,
        plan_id: str,
        executed_by: str,
        action_ids: list[str] | None = None,
        allow_reexecute: bool = False,
    ) -> ApprovalResponse:
        """Run actions that have already been approved.

        If ``action_ids`` is None, every action with status ``approved`` is
        run. If ``action_ids`` is provided, the runner targets exactly that
        set; any id whose action is not in ``approved`` status surfaces as
        ``skipped`` with a ``not approved`` message — never as ``executed``.
        """
        plan = self.plan_repo.get(plan_id)
        if plan is None:
            raise PlanNotFoundError(f"plan_id={plan_id!r} not found")

        target_set: set[str] | None = (
            set(action_ids) if action_ids is not None else None
        )

        executed: list[ExternalActionResult] = []
        skipped: list[ExternalActionResult] = []
        failed: list[ExternalActionResult] = []
        blocked: list[ExternalActionResult] = []

        for action in plan.actions:
            if target_set is not None and action.action_id not in target_set:
                continue

            if action.type in FORBIDDEN_ACTION_TYPES:
                action.status = ActionStatus.blocked
                action.approved = False
                blocked.append(
                    ExternalActionResult(
                        action_id=action.action_id,
                        target_system=action.target_system,
                        status="blocked",
                        message=f"Action type {action.type!r} is forbidden in MVP.",
                    )
                )
                self._emit_audit(
                    plan,
                    action,
                    actor=executed_by,
                    event_status="blocked",
                    approved=False,
                    error="forbidden_action_type",
                )
                continue

            if action.status == ActionStatus.executed:
                if not allow_reexecute:
                    skipped.append(
                        ExternalActionResult(
                            action_id=action.action_id,
                            target_system=action.target_system,
                            status="skipped",
                            message=(
                                "Action already executed; "
                                "pass allow_reexecute=true to retry."
                            ),
                        )
                    )
                    self._emit_audit(
                        plan,
                        action,
                        actor=executed_by,
                        event_status="skipped",
                        approved=True,
                    )
                    continue
                # else fall through to re-execute

            elif action.status != ActionStatus.approved:
                skipped.append(
                    ExternalActionResult(
                        action_id=action.action_id,
                        target_system=action.target_system,
                        status="skipped",
                        message=(
                            f"Action is not approved (current status: "
                            f"{action.status.value}); call "
                            f"/approvals/{plan_id}/approve first."
                        ),
                    )
                )
                self._emit_audit(
                    plan,
                    action,
                    actor=executed_by,
                    event_status="skipped",
                    approved=False,
                )
                continue

            result = await self._dispatch_with_execution_audit(
                plan, action, executed_by
            )
            if result.status == "failed":
                failed.append(result)
            else:
                executed.append(result)

        self.plan_repo.save(plan)
        return ApprovalResponse(
            plan_id=plan_id,
            executed=executed,
            skipped=skipped,
            failed=failed,
            blocked=blocked,
        )

    async def _dispatch_with_execution_audit(
        self, plan: ActionPlan, action: ExternalAction, actor: str
    ) -> ExternalActionResult:
        """Call the adapter and emit only execution-phase audit events.

        Approval audit must already have been emitted by ``approve_actions``
        for this action_id before this helper runs.
        """
        adapter = self.registry.get(action.target_system)
        if adapter is None:
            action.status = ActionStatus.failed
            error_msg = f"No adapter registered for {action.target_system!r}"
            self._emit_audit(
                plan,
                action,
                actor=actor,
                event_status="failed",
                approved=True,
                error=error_msg,
            )
            return ExternalActionResult(
                action_id=action.action_id,
                target_system=action.target_system,
                status="failed",
                error=error_msg,
                message="Adapter registry miss.",
            )

        try:
            with _tracer.start_as_current_span(
                "execution.adapter_call",
                attributes={
                    "action_id": action.action_id,
                    "target_system": action.target_system,
                    "action_type": action.type,
                },
            ):
                result = await adapter.execute(
                    action, dry_run=self.settings.dry_run_default
                )
        except Exception as exc:  # noqa: BLE001 — per-action isolation
            action.status = ActionStatus.failed
            error_msg = f"{type(exc).__name__}: {exc}"
            self._emit_audit(
                plan,
                action,
                actor=actor,
                event_status="failed",
                approved=True,
                error=error_msg,
            )
            return ExternalActionResult(
                action_id=action.action_id,
                target_system=action.target_system,
                status="failed",
                error=error_msg,
                message="Adapter raised.",
            )

        if result.status == "failed":
            action.status = ActionStatus.failed
            self._emit_audit(
                plan,
                action,
                actor=actor,
                event_status="failed",
                approved=True,
                error=result.error,
                external_id=result.external_id,
            )
            return result

        action.status = ActionStatus.executed
        self._emit_audit(
            plan,
            action,
            actor=actor,
            event_status="executed",
            approved=True,
            external_id=result.external_id,
        )
        return result

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def _handle_unapproved(
        self, plan: ActionPlan, action: ExternalAction, actor: str
    ) -> ExternalActionResult:
        if action.status != ActionStatus.executed:
            action.status = ActionStatus.rejected
            action.approved = False
        result = ExternalActionResult(
            action_id=action.action_id,
            target_system=action.target_system,
            status="skipped",
            message="Action was not approved.",
        )
        self._emit_audit(
            plan,
            action,
            actor=actor,
            event_status="skipped",
            approved=False,
        )
        return result

    def _handle_blocked(
        self, plan: ActionPlan, action: ExternalAction, actor: str
    ) -> ExternalActionResult:
        action.status = ActionStatus.blocked
        action.approved = False
        result = ExternalActionResult(
            action_id=action.action_id,
            target_system=action.target_system,
            status="blocked",
            message=f"Action type {action.type!r} is forbidden in MVP.",
        )
        self._emit_audit(
            plan,
            action,
            actor=actor,
            event_status="blocked",
            approved=False,
            error="forbidden_action_type",
        )
        return result

    def _handle_already_executed(
        self, plan: ActionPlan, action: ExternalAction, actor: str
    ) -> ExternalActionResult:
        result = ExternalActionResult(
            action_id=action.action_id,
            target_system=action.target_system,
            status="skipped",
            message="Action already executed; pass allow_reexecute=true to retry.",
        )
        self._emit_audit(
            plan,
            action,
            actor=actor,
            event_status="skipped",
            approved=True,
        )
        return result

    async def _execute_approved(
        self, plan: ActionPlan, action: ExternalAction, actor: str
    ) -> ExternalActionResult:
        # Approval audit first — proves intent even if execution fails.
        action.status = ActionStatus.approved
        action.approved = True
        self._emit_audit(
            plan,
            action,
            actor=actor,
            event_status="approved",
            approved=True,
        )

        adapter = self.registry.get(action.target_system)
        if adapter is None:
            action.status = ActionStatus.failed
            error_msg = f"No adapter registered for {action.target_system!r}"
            self._emit_audit(
                plan,
                action,
                actor=actor,
                event_status="failed",
                approved=True,
                error=error_msg,
            )
            return ExternalActionResult(
                action_id=action.action_id,
                target_system=action.target_system,
                status="failed",
                error=error_msg,
                message="Adapter registry miss.",
            )

        try:
            with _tracer.start_as_current_span(
                "execution.adapter_call",
                attributes={
                    "action_id": action.action_id,
                    "target_system": action.target_system,
                    "action_type": action.type,
                },
            ):
                result = await adapter.execute(
                    action, dry_run=self.settings.dry_run_default
                )
        except Exception as exc:  # noqa: BLE001 — adapter isolation per spec UC3 AC #3
            action.status = ActionStatus.failed
            error_msg = f"{type(exc).__name__}: {exc}"
            self._emit_audit(
                plan,
                action,
                actor=actor,
                event_status="failed",
                approved=True,
                error=error_msg,
            )
            return ExternalActionResult(
                action_id=action.action_id,
                target_system=action.target_system,
                status="failed",
                error=error_msg,
                message="Adapter raised.",
            )

        if result.status == "failed":
            action.status = ActionStatus.failed
            self._emit_audit(
                plan,
                action,
                actor=actor,
                event_status="failed",
                approved=True,
                error=result.error,
                external_id=result.external_id,
            )
            return result

        action.status = ActionStatus.executed
        self._emit_audit(
            plan,
            action,
            actor=actor,
            event_status="executed",
            approved=True,
            external_id=result.external_id,
        )
        return result

    # ------------------------------------------------------------------
    # Audit emission
    # ------------------------------------------------------------------

    def _emit_audit(
        self,
        plan: ActionPlan,
        action: ExternalAction,
        *,
        actor: str,
        event_status: str,
        approved: bool,
        error: str | None = None,
        external_id: str | None = None,
    ) -> None:
        now = datetime.now(_TZ)
        approved_at: datetime | None = now if approved else None
        executed_at = now if event_status in {"executed", "failed"} else None
        request_hash = self._request_hash(
            plan_id=plan.plan_id,
            action_id=action.action_id,
            actor=actor,
            event_status=event_status,
            timestamp=now,
        )
        # mypy-friendly cast via Literal-typed dict isn't worth the import dance;
        # the AuditRecord schema enforces the allowed values at runtime.
        record = AuditRecord(
            action_id=action.action_id,
            plan_id=plan.plan_id,
            actor=actor,
            action_type=action.type,
            target_system=action.target_system,
            target_external_id=external_id,
            dry_run=plan.dry_run,
            approved_by=actor if approved else None,
            approved_at=approved_at,
            executed_at=executed_at,
            status=event_status,  # type: ignore[arg-type]
            error=error,
            request_hash=request_hash,
        )
        self.audit_log.append(record)

    @staticmethod
    def _request_hash(
        *,
        plan_id: str,
        action_id: str,
        actor: str,
        event_status: str,
        timestamp: datetime,
    ) -> str:
        digest = hashlib.sha1(
            f"{plan_id}|{action_id}|{actor}|{event_status}|{timestamp.isoformat()}".encode()
        ).hexdigest()
        return digest[:16]
