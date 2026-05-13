from competitionops.schemas import ExternalAction, ExternalActionResult


class PlaneAdapter:
    async def execute(self, action: ExternalAction, dry_run: bool = True) -> ExternalActionResult:
        if dry_run:
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="plane",
                status="dry_run",
                message="Would create/update Plane issue.",
            )
        raise NotImplementedError("Implement Plane REST API adapter in P1-004.")
