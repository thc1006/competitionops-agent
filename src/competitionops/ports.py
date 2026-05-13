from typing import Protocol

from competitionops.schemas import ActionPlan, AuditRecord, ExternalAction, ExternalActionResult


class ExternalActionExecutor(Protocol):
    async def execute(
        self, action: ExternalAction, dry_run: bool = True
    ) -> ExternalActionResult: ...


class PlanRepository(Protocol):
    def save(self, plan: ActionPlan) -> None: ...

    def get(self, plan_id: str) -> ActionPlan | None: ...


class AuditLogPort(Protocol):
    def append(self, record: AuditRecord) -> None: ...

    def list_for_plan(self, plan_id: str) -> list[AuditRecord]: ...
