from competitionops.schemas import ActionPlan, ExternalAction, ExternalActionResult


class ApprovalGate:
    def select_approved_actions(
        self,
        plan: ActionPlan,
        approved_action_ids: set[str],
    ) -> list[ExternalAction]:
        approved: list[ExternalAction] = []
        for action in plan.actions:
            if action.action_id in approved_action_ids:
                action.approved = True
                approved.append(action)
        return approved

    def mark_unapproved_as_skipped(
        self,
        plan: ActionPlan,
        approved_action_ids: set[str],
    ) -> list[ExternalActionResult]:
        results: list[ExternalActionResult] = []
        for action in plan.actions:
            if action.action_id not in approved_action_ids:
                results.append(
                    ExternalActionResult(
                        action_id=action.action_id,
                        target_system=action.target_system,
                        status="skipped",
                        message="Action was not approved.",
                    )
                )
        return results
