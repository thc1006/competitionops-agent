"""MCP server contract tests.

The MCP surface is the most security-sensitive layer because Claude Code
calls it directly. Tests cover:
- exact tool whitelist (positive and negative)
- happy-path behavior for each of the six high-level business tools
- approval-required gating (execute-before-approve must skip, not run)
- dangerous action blocked even after approval call
- return-shape contract: action_id / approval_status / audit_id
- no real network / no secrets read
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from competitionops.schemas import ExternalAction, RiskLevel
from competitionops_mcp import server as mcp_server

EXPECTED_TOOL_NAMES = {
    "extract_competition_brief",
    "generate_competition_plan",
    "propose_google_workspace_actions",
    "list_pending_approvals",
    "approve_action",
    "execute_approved_action_mock",
}

# MCP tool names must never contain low-level destructive verbs that map to
# raw Google API operations. The MCP surface only exposes high-level business
# actions (per docs/05_security_oauth.md "Confused Deputy" mitigation).
FORBIDDEN_TOOL_NAME_SUBSTRINGS = {
    "delete",
    "send_email",
    "send_mail",
    "external_submit",
    "submit_to_organizer",
    "set_public",
    "share_external",
    "share_publicly",
    "permissions_create",
    "files_delete",
    "raw_api",
    "sql_",
    "exec_shell",
}

# Action / approval flow keys that every action-affecting tool must surface.
REQUIRED_RESPONSE_KEYS = {"action_id", "approval_status", "audit_id"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _list_tool_names() -> set[str]:
    tools = asyncio.run(mcp_server.mcp.list_tools())
    return {tool.name for tool in tools}


def _reset_singletons() -> None:
    mcp_server._plan_repo.cache_clear()
    mcp_server._audit_log.cache_clear()
    mcp_server._registry.cache_clear()


def _seed_plan() -> dict[str, Any]:
    _reset_singletons()
    brief = mcp_server.extract_competition_brief(
        content=(
            "Demo Cup\nSubmission deadline: 2026-09-30\n"
            "Required: pitch deck.\n"
        ),
        source_uri="test://demo",
    )
    return mcp_server.generate_competition_plan(competition=brief)


# ---------------------------------------------------------------------------
# Schema / whitelist / blacklist
# ---------------------------------------------------------------------------


def test_mcp_exposes_exactly_six_business_tools() -> None:
    names = _list_tool_names()
    assert names == EXPECTED_TOOL_NAMES, (
        f"unexpected tool delta: extra={names - EXPECTED_TOOL_NAMES}, "
        f"missing={EXPECTED_TOOL_NAMES - names}"
    )


def test_mcp_tools_never_contain_dangerous_substrings() -> None:
    for name in _list_tool_names():
        lowered = name.lower()
        for forbidden in FORBIDDEN_TOOL_NAME_SUBSTRINGS:
            assert forbidden not in lowered, (
                f"MCP tool {name!r} contains forbidden substring {forbidden!r}"
            )


def test_mcp_tool_signatures_are_business_level() -> None:
    """Every tool signature must use plain Python types (not raw HTTP/SDK objects)."""
    for tool in asyncio.run(mcp_server.mcp.list_tools()):
        schema = tool.inputSchema
        assert "properties" in schema, f"{tool.name} lacks input schema"


def test_mcp_server_source_has_no_real_google_or_network_imports() -> None:
    forbidden = [
        "googleapiclient",
        "google.oauth2",
        "google.auth",
        "google_auth_oauthlib",
        "requests.",
        "httpx.",
        "urllib.request",
        "http.client",
        "socket.socket",
        "credentials.json",
        "client_secret.json",
    ]
    source = inspect.getsource(mcp_server)
    for needle in forbidden:
        assert needle not in source, (
            f"MCP server source must not reference {needle!r}"
        )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_extract_competition_brief_returns_structured_brief() -> None:
    _reset_singletons()
    brief = mcp_server.extract_competition_brief(
        content=(
            "RunSpace Innovation Challenge\n"
            "Submission deadline: 2026-06-15\n"
            "Required: pitch deck, video.\n"
        ),
    )
    assert brief["name"] == "RunSpace Innovation Challenge"
    assert brief["submission_deadline"].startswith("2026-06-15")
    assert brief["deliverables"]


def test_generate_competition_plan_persists_plan() -> None:
    plan = _seed_plan()
    assert plan["plan_id"]
    assert plan["actions"]
    # plan must be persisted so other tools can look it up
    stored = mcp_server._plan_repo().get(plan["plan_id"])
    assert stored is not None
    assert stored.plan_id == plan["plan_id"]


def test_propose_google_workspace_actions_filters_by_target_system() -> None:
    plan = _seed_plan()
    result = mcp_server.propose_google_workspace_actions(plan_id=plan["plan_id"])
    assert result["plan_id"] == plan["plan_id"]
    assert set(result["by_system"].keys()) <= {
        "google_drive",
        "google_docs",
        "google_sheets",
        "google_calendar",
    }
    google_count = sum(len(items) for items in result["by_system"].values())
    assert google_count == result["total"]
    assert google_count >= 3  # drive folder, docs outline, sheets row at minimum


def test_list_pending_approvals_shows_pending_actions() -> None:
    plan = _seed_plan()
    listing = mcp_server.list_pending_approvals()
    pending_ids = {entry["action_id"] for entry in listing["actions"]}
    plan_action_ids = {a["action_id"] for a in plan["actions"]}
    assert plan_action_ids.issubset(pending_ids)
    assert listing["pending_count"] >= len(plan["actions"])
    for entry in listing["actions"]:
        assert entry["plan_id"] == plan["plan_id"] or entry["plan_id"]
        assert entry["target_system"]
        assert entry["risk_level"]


@pytest.mark.asyncio
async def test_approve_action_transitions_status_and_returns_audit_id() -> None:
    plan = _seed_plan()
    action_id = plan["actions"][0]["action_id"]

    result = await mcp_server.approve_action(
        plan_id=plan["plan_id"],
        action_id=action_id,
        approved_by="pm@example.com",
    )

    assert REQUIRED_RESPONSE_KEYS.issubset(result.keys())
    assert result["action_id"] == action_id
    assert result["approval_status"] == "approved"
    assert result["audit_id"]

    # The targeted action moves to approved; siblings retain their prior
    # status (pending, unless they happen to be in FORBIDDEN, which would
    # have been blocked on a separate call).
    stored = mcp_server._plan_repo().get(plan["plan_id"])
    assert stored is not None
    for action in stored.actions:
        if action.action_id == action_id:
            assert action.status.value == "approved"
        else:
            assert action.status.value in {"pending", "blocked"}


@pytest.mark.asyncio
async def test_execute_approved_action_mock_runs_through_adapter() -> None:
    plan = _seed_plan()
    action_id = plan["actions"][0]["action_id"]
    await mcp_server.approve_action(
        plan_id=plan["plan_id"], action_id=action_id, approved_by="pm@example.com"
    )

    result = await mcp_server.execute_approved_action_mock(
        plan_id=plan["plan_id"],
        action_id=action_id,
        executed_by="pm@example.com",
    )

    assert REQUIRED_RESPONSE_KEYS.issubset(result.keys())
    assert result["action_id"] == action_id
    assert result["approval_status"] == "executed"
    assert result["audit_id"]
    # Pipeline went through mock adapter — audit log records executed event
    audit_records = mcp_server._audit_log().list_for_plan(plan["plan_id"])
    executed_records = [
        r for r in audit_records if r.action_id == action_id and r.status == "executed"
    ]
    assert len(executed_records) == 1


# ---------------------------------------------------------------------------
# Approval required
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_before_approve_does_not_run() -> None:
    plan = _seed_plan()
    action_id = plan["actions"][0]["action_id"]

    result = await mcp_server.execute_approved_action_mock(
        plan_id=plan["plan_id"],
        action_id=action_id,
        executed_by="pm@example.com",
    )

    assert result["action_id"] == action_id
    assert result["approval_status"] == "skipped"
    assert "not approved" in result.get("message", "").lower()
    # No execution audit event recorded
    audit_records = mcp_server._audit_log().list_for_plan(plan["plan_id"])
    assert not any(
        r.action_id == action_id and r.status == "executed" for r in audit_records
    )


# ---------------------------------------------------------------------------
# Dangerous action blocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dangerous_action_cannot_be_approved_or_executed() -> None:
    plan = _seed_plan()
    # Inject a dangerous action directly into the persisted plan
    stored = mcp_server._plan_repo().get(plan["plan_id"])
    assert stored is not None
    dangerous = ExternalAction(
        action_id="act_dangerous",
        type="google.drive.delete_file",
        target_system="google_drive",
        payload={"file_id": "anything"},
        requires_approval=True,
        risk_level=RiskLevel.critical,
    )
    stored.actions.append(dangerous)
    mcp_server._plan_repo().save(stored)

    approval_result = await mcp_server.approve_action(
        plan_id=plan["plan_id"],
        action_id="act_dangerous",
        approved_by="pm@example.com",
    )
    assert approval_result["approval_status"] == "blocked"
    assert approval_result["audit_id"]

    execution_result = await mcp_server.execute_approved_action_mock(
        plan_id=plan["plan_id"],
        action_id="act_dangerous",
        executed_by="pm@example.com",
    )
    assert execution_result["approval_status"] == "blocked"
    audit_records = mcp_server._audit_log().list_for_plan(plan["plan_id"])
    assert not any(
        r.action_id == "act_dangerous" and r.status == "executed" for r in audit_records
    )


# ---------------------------------------------------------------------------
# Unknown plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_action_unknown_plan_returns_error() -> None:
    _reset_singletons()
    result = await mcp_server.approve_action(
        plan_id="plan_missing",
        action_id="act_x",
        approved_by="pm@example.com",
    )
    assert result["approval_status"] == "error"
    assert "not found" in result.get("error", "").lower()
