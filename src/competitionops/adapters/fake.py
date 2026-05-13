from competitionops.schemas import ExternalAction, ExternalActionResult


class FakeExternalActionExecutor:
    async def execute(self, action: ExternalAction, dry_run: bool = True) -> ExternalActionResult:
        return ExternalActionResult(
            action_id=action.action_id,
            target_system=action.target_system,
            status="dry_run" if dry_run else "executed",
            external_id=f"fake_{action.action_id}",
            message=f"Fake executor handled {action.type}",
        )
