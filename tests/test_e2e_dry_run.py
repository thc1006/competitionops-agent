"""Stage 8.1 — End-to-end dry-run behavioral contract.

These 15 tests pin down what the CompetitionOps E2E pipeline must do
*as observed from the outside*, with no peeking at private state.
Each test name reads like a specification line.

Strict invariants enforced here:
- No real Google / Plane API import — sys.modules guard + socket
  monkeypatch.
- No secrets read — fixtures only use synthetic RunSpace-like text.
- Only mock adapters execute approved actions.
- Approval gate is the single chokepoint between proposed and executed.
"""

from __future__ import annotations

import socket
import sys
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from competitionops import main as main_module
from competitionops.main import app
from competitionops.schemas import ExternalAction, RiskLevel

TZ = ZoneInfo("Asia/Taipei")

# ---------------------------------------------------------------------------
# Synthetic test fixtures — strictly non-real
# ---------------------------------------------------------------------------

RUNSPACE_BRIEF = """RunSpace Innovation Challenge 2026
Organizer: NYCU Startup Hub (synthetic)
Submission deadline: 2026-09-30
Final event: 2026-10-15
Eligibility: Open to NYCU students and alumni within 5 years of graduation.
Language: English only.
Required deliverables: pitch deck, demo video, business plan, prototype demo.
Pitch deck must not exceed 10 pages.
Video must not exceed 90 seconds.
Anonymous submission required for the first round.
Rubric: innovation, business feasibility, impact, technical excellence.
"""

BRIEF_NO_DEADLINE = """Quiet Cup
Organizer: TBD
No schedule announced yet. Stay tuned.
"""

BRIEF_MALFORMED_DEADLINE = """Broken Date Cup
Submission deadline: 2026-13-45
Required: pitch deck.
"""

BRIEF_NO_DELIVERABLES = """Hollow Cup
Submission deadline: 2026-09-30
Just register and show up.
"""

TEAM_BALANCED: list[dict[str, Any]] = [
    {"member_id": "alice", "name": "Alice", "role": "business", "weekly_capacity_hours": 20},
    {"member_id": "bob", "name": "Bob", "role": "design", "weekly_capacity_hours": 20},
    {"member_id": "carol", "name": "Carol", "role": "tech", "weekly_capacity_hours": 20},
]
TEAM_SOLO_TIGHT: list[dict[str, Any]] = [
    {"member_id": "solo", "name": "Solo", "role": "tech", "weekly_capacity_hours": 5},
]

ROLE_BY_DELIVERABLE_TITLE = {
    "Pitch deck": "business",
    "Business plan": "business",
    "Video submission": "design",
    "Prototype demo": "tech",
    "Report document": "business",
}

GOOGLE_TARGETS = {"google_drive", "google_docs", "google_sheets", "google_calendar"}
ALL_WRITE_TARGETS = GOOGLE_TARGETS | {"plane"}

FORBIDDEN_TYPES = (
    "google.drive.delete_file",
    "google.drive.permissions.set_public",
    "google.drive.permissions.share_external",
    "gmail.send",
    "competition.external_submit",
)

FORBIDDEN_REAL_GOOGLE_MODULES = {
    "googleapiclient",
    "googleapiclient.discovery",
    "google.oauth2",
    "google.oauth2.credentials",
    "google.auth",
    "google.auth.transport.requests",
    "google_auth_oauthlib",
}


# ---------------------------------------------------------------------------
# Helpers (no test logic — only HTTP plumbing)
# ---------------------------------------------------------------------------


def _fresh_client() -> TestClient:
    main_module._plan_repo.cache_clear()
    main_module._audit_log.cache_clear()
    main_module._registry.cache_clear()
    return TestClient(app)


def _extract(client: TestClient, content: str, source_uri: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"source_type": "text", "content": content}
    if source_uri is not None:
        payload["source_uri"] = source_uri
    response = client.post("/briefs/extract", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _generate(
    client: TestClient,
    brief: dict[str, Any],
    team: list[dict[str, Any]] | None = None,
    pm_approval_required: bool = True,
) -> dict[str, Any]:
    response = client.post(
        "/plans/generate",
        json={
            "competition": brief,
            "team_capacity": team or TEAM_BALANCED,
            "preferences": {"pm_approval_required": pm_approval_required},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _approve(
    client: TestClient,
    plan_id: str,
    action_ids: list[str],
    actor: str = "pm@example.com",
) -> dict[str, Any]:
    response = client.post(
        f"/approvals/{plan_id}/approve",
        json={"approved_action_ids": action_ids, "approved_by": actor},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _run(
    client: TestClient,
    plan_id: str,
    action_ids: list[str] | None = None,
    actor: str = "pm@example.com",
    allow_reexecute: bool = False,
) -> dict[str, Any]:
    body: dict[str, Any] = {"executed_by": actor, "allow_reexecute": allow_reexecute}
    if action_ids is not None:
        body["action_ids"] = action_ids
    response = client.post(f"/executions/{plan_id}/run", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _augment_owner_roles(brief: dict[str, Any]) -> None:
    for deliverable in brief["deliverables"]:
        deliverable["owner_role"] = ROLE_BY_DELIVERABLE_TITLE.get(
            deliverable["title"], "business"
        )


def _inject_action(plan_id: str, action: ExternalAction) -> None:
    repo = main_module._plan_repo()
    stored = repo.get(plan_id)
    assert stored is not None
    stored.actions.append(action)
    repo.save(stored)


# ---------------------------------------------------------------------------
# 1. Happy path: full pipeline from brief text to audit report
# ---------------------------------------------------------------------------


def test_e2e_dry_run_happy_path_from_brief_to_audit_report() -> None:
    client = _fresh_client()

    brief = _extract(client, RUNSPACE_BRIEF, source_uri="test://runspace-2026")
    assert brief["name"] == "RunSpace Innovation Challenge 2026"
    assert brief["submission_deadline"].startswith("2026-09-30")
    assert brief["deliverables"]
    _augment_owner_roles(brief)

    plan = _generate(client, brief)
    assert plan["dry_run"] is True
    assert plan["requires_approval"] is True
    target_systems = {a["target_system"] for a in plan["actions"]}
    assert ALL_WRITE_TARGETS.issubset(target_systems)

    chosen: dict[str, str] = {}
    for action in plan["actions"]:
        if action["target_system"] not in chosen:
            chosen[action["target_system"]] = action["action_id"]
    approved_ids = list(chosen.values())

    decision = _approve(client, plan["plan_id"], approved_ids)
    assert set(decision["approved"]) == set(approved_ids)

    result = _run(client, plan["plan_id"])
    assert len(result["executed"]) == len(approved_ids)
    assert result["failed"] == []
    assert result["blocked"] == []

    # Audit covers every executed action with provenance fields populated.
    audit = main_module._audit_log().list_for_plan(plan["plan_id"])
    executed_records = [r for r in audit if r.status == "executed"]
    assert {r.action_id for r in executed_records} == set(approved_ids)
    for record in executed_records:
        assert record.executed_at is not None
        assert record.request_hash is not None
        assert record.actor == "pm@example.com"


# ---------------------------------------------------------------------------
# 2-4. Brief-level data quality guards
# ---------------------------------------------------------------------------


def test_e2e_dry_run_rejects_missing_deadline() -> None:
    client = _fresh_client()
    brief = _extract(client, BRIEF_NO_DEADLINE)

    assert brief["submission_deadline"] is None
    assert "missing_submission_deadline" in brief["risk_flags"]


def test_e2e_dry_run_rejects_malformed_deadline() -> None:
    client = _fresh_client()
    brief = _extract(client, BRIEF_MALFORMED_DEADLINE)

    # Extractor must not crash on month=13 day=45 — it surfaces the gap as
    # a missing deadline + risk flag rather than raising ValueError.
    assert brief["submission_deadline"] is None
    assert "missing_submission_deadline" in brief["risk_flags"]


def test_e2e_dry_run_requires_deliverables() -> None:
    client = _fresh_client()
    brief = _extract(client, BRIEF_NO_DELIVERABLES)

    assert brief["deliverables"] == []
    assert "missing_deliverables" in brief["risk_flags"]


# ---------------------------------------------------------------------------
# 5. Planner-level deadline-too-close guard
# ---------------------------------------------------------------------------


def test_e2e_dry_run_flags_deadline_too_close() -> None:
    client = _fresh_client()
    soon = (datetime.now(TZ) + timedelta(days=3)).strftime("%Y-%m-%d")
    content = (
        f"Rush Cup\nSubmission deadline: {soon}\nRequired: pitch deck.\n"
    )
    brief = _extract(client, content)
    _augment_owner_roles(brief)
    plan = _generate(client, brief)

    assert "short_deadline" in plan["risk_flags"]


# ---------------------------------------------------------------------------
# 6. Capacity guard
# ---------------------------------------------------------------------------


def test_e2e_dry_run_flags_insufficient_team_capacity() -> None:
    client = _fresh_client()
    brief = _extract(client, RUNSPACE_BRIEF)
    _augment_owner_roles(brief)
    # Force every deliverable onto a single overloaded role.
    for deliverable in brief["deliverables"]:
        deliverable["owner_role"] = "tech"

    plan = _generate(client, brief, team=TEAM_SOLO_TIGHT)
    assert "team_capacity_insufficient" in plan["risk_flags"]
    # No tasks were hard-assigned to the over-capacity member.
    blocked_tasks = [
        t for t in plan["task_drafts"] if t["status"] == "blocked_no_capacity"
    ]
    assert blocked_tasks
    for task in blocked_tasks:
        assert task["suggested_assignee"] is None


# ---------------------------------------------------------------------------
# 7. Proposed actions start in pending — confirms no leakage past planner
# ---------------------------------------------------------------------------


def test_e2e_dry_run_proposed_actions_start_pending() -> None:
    client = _fresh_client()
    brief = _extract(client, RUNSPACE_BRIEF)
    _augment_owner_roles(brief)
    plan = _generate(client, brief)

    assert plan["actions"]
    for action in plan["actions"]:
        assert action["status"] == "pending"
        assert action["requires_approval"] is True
        assert action["approved"] is False


# ---------------------------------------------------------------------------
# 8. Approval-gate enforcement on the executions endpoint
# ---------------------------------------------------------------------------


def test_e2e_dry_run_blocks_execution_without_approval() -> None:
    client = _fresh_client()
    brief = _extract(client, RUNSPACE_BRIEF)
    _augment_owner_roles(brief)
    plan = _generate(client, brief)
    action_ids = [a["action_id"] for a in plan["actions"]]

    result = _run(client, plan["plan_id"], action_ids=action_ids)

    assert result["executed"] == []
    assert {r["action_id"] for r in result["skipped"]} == set(action_ids)
    for skip in result["skipped"]:
        assert "not approved" in skip["message"].lower()


# ---------------------------------------------------------------------------
# 9. Idempotency: same approval cannot drive a second execution
# ---------------------------------------------------------------------------


def test_e2e_dry_run_executes_approved_mock_actions_once() -> None:
    client = _fresh_client()
    brief = _extract(client, RUNSPACE_BRIEF)
    _augment_owner_roles(brief)
    plan = _generate(client, brief)
    target = plan["actions"][0]["action_id"]

    _approve(client, plan["plan_id"], [target])

    first = _run(client, plan["plan_id"], action_ids=[target])
    assert [r["action_id"] for r in first["executed"]] == [target]

    second = _run(client, plan["plan_id"], action_ids=[target])
    assert second["executed"] == []
    assert any(r["action_id"] == target for r in second["skipped"])

    third = _run(client, plan["plan_id"], action_ids=[target], allow_reexecute=True)
    assert [r["action_id"] for r in third["executed"]] == [target]


# ---------------------------------------------------------------------------
# 10. Rejected actions cannot execute even when explicitly addressed
# ---------------------------------------------------------------------------


def test_e2e_dry_run_blocks_rejected_actions() -> None:
    client = _fresh_client()
    brief = _extract(client, RUNSPACE_BRIEF)
    _augment_owner_roles(brief)
    plan = _generate(client, brief)
    approved = plan["actions"][0]["action_id"]
    other_ids = [a["action_id"] for a in plan["actions"][1:]]

    _approve(client, plan["plan_id"], [approved])

    # Try to run an action that was rejected on the batch approve call.
    rejected_target = other_ids[0]
    result = _run(client, plan["plan_id"], action_ids=[rejected_target])

    assert result["executed"] == []
    assert any(
        r["action_id"] == rejected_target and "not approved" in r["message"].lower()
        for r in result["skipped"]
    )


# ---------------------------------------------------------------------------
# 11. Dangerous action types are blocked across approval + execution phases
# ---------------------------------------------------------------------------


def test_e2e_dry_run_blocks_dangerous_delete_share_submit_email_actions() -> None:
    client = _fresh_client()
    brief = _extract(client, RUNSPACE_BRIEF)
    _augment_owner_roles(brief)
    plan = _generate(client, brief)

    # Inject one synthetic action per FORBIDDEN type and try to approve them.
    injected_ids: list[str] = []
    for index, dangerous_type in enumerate(FORBIDDEN_TYPES):
        action_id = f"act_e2e_danger_{index}"
        injected_ids.append(action_id)
        target_system = "google_drive" if "drive" in dangerous_type else (
            "internal" if "submit" in dangerous_type else "google_drive"
        )
        _inject_action(
            plan["plan_id"],
            ExternalAction(
                action_id=action_id,
                type=dangerous_type,
                target_system=target_system,  # type: ignore[arg-type]
                payload={},
                requires_approval=True,
                risk_level=RiskLevel.critical,
            ),
        )

    decision = _approve(client, plan["plan_id"], injected_ids)
    assert set(decision["blocked"]) == set(injected_ids)
    assert all(action_id not in decision["approved"] for action_id in injected_ids)

    result = _run(client, plan["plan_id"], action_ids=injected_ids)
    blocked_run_ids = {r["action_id"] for r in result["blocked"]}
    assert set(injected_ids).issubset(blocked_run_ids)
    assert all(r["action_id"] not in injected_ids for r in result["executed"])


# ---------------------------------------------------------------------------
# 12. Audit log contains at least one event per action
# ---------------------------------------------------------------------------


def test_e2e_dry_run_generates_audit_event_for_every_action() -> None:
    client = _fresh_client()
    brief = _extract(client, RUNSPACE_BRIEF)
    _augment_owner_roles(brief)
    plan = _generate(client, brief)
    approved = [plan["actions"][0]["action_id"]]

    _approve(client, plan["plan_id"], approved)
    _run(client, plan["plan_id"])

    audit = main_module._audit_log().list_for_plan(plan["plan_id"])
    covered_action_ids = {r.action_id for r in audit}
    plan_action_ids = {a["action_id"] for a in plan["actions"]}
    assert plan_action_ids.issubset(covered_action_ids)


# ---------------------------------------------------------------------------
# 13. No real network call is ever made during a dry-run
# ---------------------------------------------------------------------------


def test_e2e_dry_run_prevents_real_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    real_calls: list[Any] = []

    def deny_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        real_calls.append(address)
        raise RuntimeError(
            f"E2E dry-run attempted a real outbound TCP connection to {address!r}"
        )

    monkeypatch.setattr(socket, "create_connection", deny_create_connection)

    client = _fresh_client()
    brief = _extract(client, RUNSPACE_BRIEF)
    _augment_owner_roles(brief)
    plan = _generate(client, brief)
    target = plan["actions"][0]["action_id"]
    _approve(client, plan["plan_id"], [target])
    _run(client, plan["plan_id"], action_ids=[target])

    assert real_calls == [], f"Unexpected outbound connections: {real_calls}"


# ---------------------------------------------------------------------------
# 14. Real Google / Plane client modules are not imported by the flow
# ---------------------------------------------------------------------------


def test_e2e_dry_run_does_not_initialize_real_google_or_plane_clients() -> None:
    leaked_before = FORBIDDEN_REAL_GOOGLE_MODULES & set(sys.modules.keys())

    client = _fresh_client()
    brief = _extract(client, RUNSPACE_BRIEF)
    _augment_owner_roles(brief)
    plan = _generate(client, brief)
    target = plan["actions"][0]["action_id"]
    _approve(client, plan["plan_id"], [target])
    _run(client, plan["plan_id"], action_ids=[target])

    leaked_after = FORBIDDEN_REAL_GOOGLE_MODULES & set(sys.modules.keys())
    assert leaked_after == leaked_before, (
        f"Real Google SDK leaked into sys.modules: {leaked_after - leaked_before}"
    )


# ---------------------------------------------------------------------------
# 15. Determinism: same input -> same plan_id and action_ids
# ---------------------------------------------------------------------------


def test_e2e_dry_run_is_deterministic_for_same_input() -> None:
    client_a = _fresh_client()
    brief_a = _extract(client_a, RUNSPACE_BRIEF)
    _augment_owner_roles(brief_a)
    plan_a = _generate(client_a, brief_a)

    client_b = _fresh_client()  # resets all in-memory singletons
    brief_b = _extract(client_b, RUNSPACE_BRIEF)
    _augment_owner_roles(brief_b)
    plan_b = _generate(client_b, brief_b)

    assert plan_a["plan_id"] == plan_b["plan_id"]
    assert [a["action_id"] for a in plan_a["actions"]] == [
        a["action_id"] for a in plan_b["actions"]
    ]
    assert [t["task_id"] for t in plan_a["task_drafts"]] == [
        t["task_id"] for t in plan_b["task_drafts"]
    ]
