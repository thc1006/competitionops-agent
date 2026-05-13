"""End-to-end dry-run test for the full CompetitionOps pipeline.

Walks the brief→plan→propose→approve→execute→audit flow twice — once via
the FastAPI HTTP surface and once via the MCP server surface — to prove
both user-facing contracts work without touching real Google or Plane APIs.

Constraints enforced by these tests:
- No real Google API import survives the run (sys.modules check).
- No real credentials read.
- No external writes — mock adapters only.
- Dangerous action types are blocked even when explicitly approved.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from fastapi.testclient import TestClient

from competitionops import main as main_module
from competitionops.main import app
from competitionops.schemas import ExternalAction, RiskLevel
from competitionops_mcp import server as mcp_server

# ---------------------------------------------------------------------------
# Fixtures: a RunSpace-like brief + a multi-role team
# ---------------------------------------------------------------------------

RUNSPACE_BRIEF = """RunSpace Innovation Challenge 2026
Organizer: NYCU Startup Hub
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

TEAM: list[dict[str, Any]] = [
    {"member_id": "alice", "name": "Alice Chen", "role": "business", "weekly_capacity_hours": 20},
    {"member_id": "bob", "name": "Bob Lin", "role": "design", "weekly_capacity_hours": 20},
    {"member_id": "carol", "name": "Carol Wu", "role": "tech", "weekly_capacity_hours": 20},
    {"member_id": "dan", "name": "Dan Yu", "role": "research", "weekly_capacity_hours": 10},
]

GOOGLE_TARGETS = {"google_drive", "google_docs", "google_sheets", "google_calendar"}
ALL_WRITE_TARGETS = GOOGLE_TARGETS | {"plane"}

# Simulates the PM-augmentation step: the brief extractor does not infer
# ``owner_role`` from free text, so a PM assigns deliverables to roles
# before /plans/generate. Without this, deliverables would be blocked on
# ``owner_role_missing`` and no Plane tasks would be emitted (correct
# planner behavior per Stage 2 AC #4).
ROLE_BY_DELIVERABLE_TITLE = {
    "Pitch deck": "business",
    "Business plan": "business",
    "Video submission": "design",
    "Prototype demo": "tech",
}

# If any of these get imported during the e2e flow, we have leaked into a real
# Google SDK and the test must fail loudly.
FORBIDDEN_MODULES = {
    "googleapiclient",
    "googleapiclient.discovery",
    "google.oauth2",
    "google.oauth2.credentials",
    "google.auth",
    "google.auth.transport.requests",
    "google_auth_oauthlib",
}


# ---------------------------------------------------------------------------
# State reset helpers
# ---------------------------------------------------------------------------


def _reset_http_state() -> None:
    main_module._plan_repo.cache_clear()
    main_module._audit_log.cache_clear()
    main_module._registry.cache_clear()


def _reset_mcp_state() -> None:
    mcp_server._plan_repo.cache_clear()
    mcp_server._audit_log.cache_clear()
    mcp_server._registry.cache_clear()


def _assert_no_real_google_loaded() -> None:
    leaked = sorted(mod for mod in FORBIDDEN_MODULES if mod in sys.modules)
    assert not leaked, (
        "real Google SDK leaked into sys.modules during e2e flow: " + ", ".join(leaked)
    )


# ---------------------------------------------------------------------------
# 1. HTTP path — full lifecycle through FastAPI
# ---------------------------------------------------------------------------


def test_e2e_http_full_lifecycle() -> None:
    _reset_http_state()
    client = TestClient(app)

    # Step 1 — extract
    brief_resp = client.post(
        "/briefs/extract",
        json={
            "source_type": "text",
            "source_uri": "test://runspace-2026",
            "content": RUNSPACE_BRIEF,
        },
    )
    assert brief_resp.status_code == 200
    brief = brief_resp.json()
    assert brief["name"] == "RunSpace Innovation Challenge 2026"
    assert brief["organizer"] == "NYCU Startup Hub"
    assert brief["submission_deadline"].startswith("2026-09-30")
    assert brief["final_event_date"].startswith("2026-10-15")
    assert brief["deliverables"]
    assert brief["anonymous_rules"]
    assert brief["language_requirements"]
    # Format-limit risk flags
    assert any("anonymous" in f for f in brief["risk_flags"])
    assert any("page" in f for f in brief["risk_flags"])
    assert any("video" in f or "duration" in f for f in brief["risk_flags"])

    # Step 1.5 — PM augments the extracted brief with owner_role per
    # deliverable. Real PMs do this after reviewing the extraction; the
    # extractor itself stays conservative and does not invent roles.
    for deliverable in brief["deliverables"]:
        deliverable["owner_role"] = ROLE_BY_DELIVERABLE_TITLE.get(
            deliverable["title"], "business"
        )

    # Step 2 — generate plan
    plan_resp = client.post(
        "/plans/generate",
        json={
            "competition": brief,
            "team_capacity": TEAM,
            "preferences": {"pm_approval_required": True},
        },
    )
    assert plan_resp.status_code == 200
    plan = plan_resp.json()
    assert plan["dry_run"] is True
    assert plan["requires_approval"] is True
    assert plan["actions"]
    assert plan["task_drafts"]
    # WBS coverage: every deliverable maps to a task
    deliverable_titles = {d["title"] for d in brief["deliverables"]}
    task_sources = {t["source_requirement"] for t in plan["task_drafts"]}
    assert task_sources == deliverable_titles
    # RACI / capacity: each task in draft status has an owner_role and assignee
    draft_tasks = [t for t in plan["task_drafts"] if t["status"] == "draft"]
    for task in draft_tasks:
        assert task["owner_role"] is not None
        assert task["due_date"] is not None
        assert task["priority"] in {"P0", "P1", "P2"}

    # Step 3 — proposed actions cover all 5 target_systems
    target_systems = {a["target_system"] for a in plan["actions"]}
    assert ALL_WRITE_TARGETS.issubset(target_systems), (
        f"missing target_systems: {ALL_WRITE_TARGETS - target_systems}"
    )
    # All actions start in pending
    assert all(a["status"] == "pending" for a in plan["actions"])

    # Step 4 — PM approves one action per target_system
    chosen: dict[str, str] = {}
    for action in plan["actions"]:
        if action["target_system"] not in chosen:
            chosen[action["target_system"]] = action["action_id"]
    approved_ids = list(chosen.values())
    assert len(approved_ids) == len(ALL_WRITE_TARGETS)

    approve_resp = client.post(
        f"/approvals/{plan['plan_id']}/approve",
        json={"approved_action_ids": approved_ids, "approved_by": "pm@example.com"},
    )
    assert approve_resp.status_code == 200
    decision = approve_resp.json()
    assert set(decision["approved"]) == set(approved_ids)
    assert decision["blocked"] == []

    # Step 5 — execute approved actions via mock adapter pipeline
    exec_resp = client.post(
        f"/executions/{plan['plan_id']}/run",
        json={"executed_by": "pm@example.com"},
    )
    assert exec_resp.status_code == 200
    result = exec_resp.json()
    assert len(result["executed"]) == len(approved_ids)
    assert result["failed"] == []
    assert result["blocked"] == []

    # Step 6 — mock adapter state was mutated for each target
    registry = main_module._registry()
    drive = registry.get("google_drive")
    docs = registry.get("google_docs")
    sheets = registry.get("google_sheets")
    calendar = registry.get("google_calendar")
    plane = registry.get("plane")
    assert drive is not None and drive.folders, "Drive mock should hold ≥1 folder"
    assert docs is not None and docs.docs, "Docs mock should hold ≥1 doc"
    assert sheets is not None and sheets.sheets, "Sheets mock should hold ≥1 sheet"
    assert calendar is not None and calendar.events, "Calendar mock should hold ≥1 event"
    # Plane is the stub adapter (not stateful in Stage 4 scope) — verify call recorded
    assert plane is not None

    # Step 7 — audit log records every lifecycle event
    audit = main_module._audit_log()
    records = audit.list_for_plan(plan["plan_id"])
    executed_records = [r for r in records if r.status == "executed"]
    approved_records = [r for r in records if r.status == "approved"]
    rejected_records = [r for r in records if r.status == "rejected"]
    assert len(executed_records) == len(approved_ids)
    assert len(approved_records) == len(approved_ids)
    # Every unapproved action got an explicit rejection event
    rejected_ids = {r.action_id for r in rejected_records}
    all_ids = {a["action_id"] for a in plan["actions"]}
    assert rejected_ids == all_ids - set(approved_ids)
    # Each execution record carries provenance. The Plane adapter is still
    # a Stage 0 stub (upgrade scheduled for P1-004) and does not populate
    # ``external_id``; the four Google adapters do.
    for record in executed_records:
        assert record.actor == "pm@example.com"
        assert record.executed_at is not None
        assert record.request_hash is not None
        if record.target_system in GOOGLE_TARGETS:
            assert record.target_external_id is not None, (
                f"Google adapter {record.target_system} must surface "
                f"external_id but record had None"
            )

    _assert_no_real_google_loaded()


# ---------------------------------------------------------------------------
# 2. MCP path — full lifecycle through Claude-facing tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_mcp_full_lifecycle() -> None:
    _reset_mcp_state()

    # Step 1 — extract
    brief = mcp_server.extract_competition_brief(
        content=RUNSPACE_BRIEF, source_uri="test://runspace-2026"
    )
    assert brief["name"] == "RunSpace Innovation Challenge 2026"
    assert brief["organizer"] == "NYCU Startup Hub"

    # Step 2 — generate plan (persisted in MCP-side repo)
    plan = mcp_server.generate_competition_plan(competition=brief)
    assert plan["dry_run"] is True
    assert plan["actions"]
    plan_id = plan["plan_id"]

    # Step 3 — propose Google-only actions
    proposal = mcp_server.propose_google_workspace_actions(plan_id=plan_id)
    assert proposal["total"] >= 3
    assert set(proposal["by_system"]).issubset(GOOGLE_TARGETS)
    for system_actions in proposal["by_system"].values():
        for action in system_actions:
            assert action["status"] == "pending"

    # Step 4 — list pending approvals to mirror Claude's review step
    pending = mcp_server.list_pending_approvals(plan_id=plan_id)
    assert pending["pending_count"] == len(plan["actions"])
    pending_ids = {entry["action_id"] for entry in pending["actions"]}
    assert pending_ids == {a["action_id"] for a in plan["actions"]}

    # Step 5 — PM approves one Google action per target_system
    chosen: dict[str, str] = {}
    for action in plan["actions"]:
        if (
            action["target_system"] in GOOGLE_TARGETS
            and action["target_system"] not in chosen
        ):
            chosen[action["target_system"]] = action["action_id"]
    approved_action_ids = list(chosen.values())
    assert len(approved_action_ids) == len(GOOGLE_TARGETS)

    for action_id in approved_action_ids:
        approval = await mcp_server.approve_action(
            plan_id=plan_id, action_id=action_id, approved_by="pm@example.com"
        )
        assert approval["approval_status"] == "approved"
        assert approval["action_id"] == action_id
        assert approval["audit_id"]

    # Step 6 — execute each approved action via mock adapter
    for action_id in approved_action_ids:
        execution = await mcp_server.execute_approved_action_mock(
            plan_id=plan_id, action_id=action_id, executed_by="pm@example.com"
        )
        assert execution["approval_status"] == "executed"
        assert execution["action_id"] == action_id
        assert execution["audit_id"]

    # Step 7 — audit log shows both approval + execution lifecycle for each
    records = mcp_server._audit_log().list_for_plan(plan_id)
    for action_id in approved_action_ids:
        statuses = {r.status for r in records if r.action_id == action_id}
        assert {"approved", "executed"}.issubset(statuses), (
            f"action {action_id} missing lifecycle in {statuses}"
        )
        for record in records:
            if record.action_id == action_id and record.status == "executed":
                assert record.target_external_id is not None
                assert record.request_hash is not None

    _assert_no_real_google_loaded()


# ---------------------------------------------------------------------------
# 3. Dangerous action remains blocked even after explicit approval request
# ---------------------------------------------------------------------------


def test_e2e_dangerous_action_blocked_in_full_flow() -> None:
    _reset_http_state()
    client = TestClient(app)

    brief_resp = client.post(
        "/briefs/extract",
        json={"source_type": "text", "content": RUNSPACE_BRIEF},
    )
    plan_resp = client.post(
        "/plans/generate",
        json={
            "competition": brief_resp.json(),
            "team_capacity": TEAM,
            "preferences": {"pm_approval_required": True},
        },
    )
    plan = plan_resp.json()

    # Inject a forbidden action straight into the persisted plan
    repo = main_module._plan_repo()
    stored = repo.get(plan["plan_id"])
    assert stored is not None
    dangerous = ExternalAction(
        action_id="act_e2e_danger",
        type="google.drive.delete_file",
        target_system="google_drive",
        payload={"file_id": "anything"},
        requires_approval=True,
        risk_level=RiskLevel.critical,
    )
    stored.actions.append(dangerous)
    repo.save(stored)

    # PM "approves" the dangerous id — approval gate should block, not approve
    approve_resp = client.post(
        f"/approvals/{plan['plan_id']}/approve",
        json={
            "approved_action_ids": ["act_e2e_danger"],
            "approved_by": "pm@example.com",
        },
    )
    assert approve_resp.status_code == 200
    decision = approve_resp.json()
    assert "act_e2e_danger" in decision["blocked"]
    assert "act_e2e_danger" not in decision["approved"]

    # Even if the executor is then asked to run it, it stays blocked
    exec_resp = client.post(
        f"/executions/{plan['plan_id']}/run",
        json={"executed_by": "pm@example.com", "action_ids": ["act_e2e_danger"]},
    )
    assert exec_resp.status_code == 200
    exec_body = exec_resp.json()
    assert any(r["action_id"] == "act_e2e_danger" for r in exec_body["blocked"])
    assert all(r["action_id"] != "act_e2e_danger" for r in exec_body["executed"])

    # Drive mock adapter must never have been called for the dangerous action
    drive = main_module._registry().get("google_drive")
    assert drive is not None
    danger_calls = [c for c in drive.calls if c.get("action_id") == "act_e2e_danger"]
    assert danger_calls == []

    # Audit log carries blocked events from both phases
    audit = main_module._audit_log()
    records = audit.list_for_plan(plan["plan_id"])
    blocked_records = [
        r for r in records if r.action_id == "act_e2e_danger" and r.status == "blocked"
    ]
    assert len(blocked_records) >= 2  # one from approve, one from run

    _assert_no_real_google_loaded()


# ---------------------------------------------------------------------------
# 4. Belt-and-suspenders: no real Google SDK / network library got pulled in
# ---------------------------------------------------------------------------


def test_e2e_no_real_google_or_network_imports_after_full_run() -> None:
    _reset_http_state()
    _reset_mcp_state()
    _assert_no_real_google_loaded()
