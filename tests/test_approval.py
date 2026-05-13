from competitionops.schemas import ActionPlan, ExternalAction
from competitionops.services.approval import ApprovalGate


def test_approval_gate_selects_only_approved_actions() -> None:
    plan = ActionPlan(
        plan_id="plan_1",
        competition_id="comp_1",
        actions=[
            ExternalAction(action_id="a1", type="google.docs.create", target_system="google_docs", payload={}),
            ExternalAction(action_id="a2", type="google.calendar.create", target_system="google_calendar", payload={}),
        ],
    )

    gate = ApprovalGate()
    approved = gate.select_approved_actions(plan, {"a1"})
    skipped = gate.mark_unapproved_as_skipped(plan, {"a1"})

    assert [a.action_id for a in approved] == ["a1"]
    assert skipped[0].action_id == "a2"
    assert skipped[0].status == "skipped"
