from competitionops.schemas import ActionPlan


class InMemoryPlanRepository:
    """Local, process-bound store. No persistence, no network."""

    def __init__(self) -> None:
        self._plans: dict[str, ActionPlan] = {}

    def save(self, plan: ActionPlan) -> None:
        self._plans[plan.plan_id] = plan

    def get(self, plan_id: str) -> ActionPlan | None:
        return self._plans.get(plan_id)

    def list_all(self) -> list[ActionPlan]:
        return list(self._plans.values())
