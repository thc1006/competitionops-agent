"""FastAPI contract tests for the seven HTTP endpoints exposed by the
CompetitionOps API.

The two-phase flow exposes ``/approvals/{plan_id}/approve`` and
``/executions/{plan_id}/run`` so PMs can review the diff between approval
and execution. The legacy combined endpoint ``/plans/{plan_id}/approve`` is
preserved for callers that want one-shot dry-run execution.
"""

from typing import Any

from fastapi.testclient import TestClient

from competitionops import main as main_module
from competitionops.main import app


def _fresh_client() -> TestClient:
    """Clear lru_cache singletons so each test gets a clean in-memory store."""
    main_module._plan_repo.cache_clear()
    main_module._audit_log.cache_clear()
    main_module._registry.cache_clear()
    return TestClient(app)


def _seed_plan(client: TestClient) -> dict[str, Any]:
    brief_resp = client.post(
        "/briefs/extract",
        json={
            "source_type": "text",
            "content": "Demo Cup\nSubmission deadline: 2026-09-30\nRequired: pitch deck.\n",
        },
    )
    assert brief_resp.status_code == 200
    plan_resp = client.post(
        "/plans/generate",
        json={
            "competition": brief_resp.json(),
            "team_capacity": [
                {
                    "member_id": "m1",
                    "name": "Alice",
                    "role": "business",
                    "weekly_capacity_hours": 20,
                }
            ],
            "preferences": {"pm_approval_required": True},
        },
    )
    assert plan_resp.status_code == 200
    return plan_resp.json()


# ----------------------------------------------------------------------
# 1. health
# ----------------------------------------------------------------------


def test_health_endpoint_returns_ok() -> None:
    client = _fresh_client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ----------------------------------------------------------------------
# 2. extract brief happy path
# ----------------------------------------------------------------------


def test_extract_brief_happy_path() -> None:
    client = _fresh_client()
    response = client.post(
        "/briefs/extract",
        json={
            "source_type": "text",
            "source_uri": "test://brief",
            "content": (
                "Sample Competition\n"
                "Submission deadline: 2026-09-30\n"
                "Required: 10-page pitch deck, 90-second video.\n"
            ),
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Sample Competition"
    assert data["source_uri"] == "test://brief"
    assert data["submission_deadline"].startswith("2026-09-30")
    assert data["deliverables"]


# ----------------------------------------------------------------------
# 3. invalid brief returns 422
# ----------------------------------------------------------------------


def test_invalid_brief_returns_422() -> None:
    client = _fresh_client()
    # Missing required `content`
    assert client.post("/briefs/extract", json={}).status_code == 422
    # Empty content (Pydantic min_length=1)
    assert (
        client.post(
            "/briefs/extract", json={"source_type": "text", "content": ""}
        ).status_code
        == 422
    )
    # Unsupported source_type Literal
    assert (
        client.post(
            "/briefs/extract",
            json={"source_type": "drive", "content": "x"},
        ).status_code
        == 422
    )


# ----------------------------------------------------------------------
# 4. generate plan
# ----------------------------------------------------------------------


def test_generate_plan_returns_proposed_actions() -> None:
    client = _fresh_client()
    plan = _seed_plan(client)
    assert plan["dry_run"] is True
    assert plan["requires_approval"] is True
    assert plan["actions"], "planner must emit at least one external action"
    # All actions are 'pending' at this stage
    assert all(action["status"] == "pending" for action in plan["actions"])
    # All actions require approval
    assert all(action["requires_approval"] for action in plan["actions"])


# ----------------------------------------------------------------------
# 5. execute without approval should fail
# ----------------------------------------------------------------------


def test_execute_without_approval_does_not_run_any_action() -> None:
    client = _fresh_client()
    plan = _seed_plan(client)
    action_ids = [a["action_id"] for a in plan["actions"]]

    response = client.post(
        f"/executions/{plan['plan_id']}/run",
        json={"executed_by": "pm@example.com", "action_ids": action_ids},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["executed"] == []
    assert len(result["skipped"]) == len(action_ids)
    assert all("not approved" in entry["message"].lower() for entry in result["skipped"])

    # Run without action_ids (default = all approved) should also yield zero
    response = client.post(
        f"/executions/{plan['plan_id']}/run",
        json={"executed_by": "pm@example.com"},
    )
    assert response.status_code == 200
    assert response.json()["executed"] == []


# ----------------------------------------------------------------------
# 6. approve then execute should pass with mock adapter
# ----------------------------------------------------------------------


def test_approve_then_execute_runs_actions_through_mock_adapter() -> None:
    client = _fresh_client()
    plan = _seed_plan(client)
    first_id = plan["actions"][0]["action_id"]

    approve_resp = client.post(
        f"/approvals/{plan['plan_id']}/approve",
        json={"approved_action_ids": [first_id], "approved_by": "pm@example.com"},
    )
    assert approve_resp.status_code == 200
    decision = approve_resp.json()
    assert decision["approved"] == [first_id]
    # Every other action was explicitly rejected (or blocked if dangerous)
    rejected_or_blocked = set(decision["rejected"]) | set(decision["blocked"])
    other_ids = {a["action_id"] for a in plan["actions"] if a["action_id"] != first_id}
    assert rejected_or_blocked == other_ids

    # Verify state via /executions/run
    run_resp = client.post(
        f"/executions/{plan['plan_id']}/run",
        json={"executed_by": "pm@example.com", "action_ids": [first_id]},
    )
    assert run_resp.status_code == 200
    result = run_resp.json()
    assert [r["action_id"] for r in result["executed"]] == [first_id]
    assert result["skipped"] == []
    assert result["failed"] == []

    # Second run without allow_reexecute should NOT execute again
    rerun_resp = client.post(
        f"/executions/{plan['plan_id']}/run",
        json={"executed_by": "pm@example.com", "action_ids": [first_id]},
    )
    assert rerun_resp.status_code == 200
    assert rerun_resp.json()["executed"] == []


# ----------------------------------------------------------------------
# 7. malformed request body
# ----------------------------------------------------------------------


def test_malformed_request_body_returns_422() -> None:
    client = _fresh_client()

    # Body type mismatch — competition should be a dict, not a string
    response = client.post(
        "/plans/generate",
        json={"competition": "not-an-object", "team_capacity": []},
    )
    assert response.status_code == 422

    # Body type mismatch — approved_action_ids should be a list of strings
    response = client.post(
        "/approvals/plan_anything/approve",
        json={"approved_action_ids": "not-a-list", "approved_by": "pm@example.com"},
    )
    assert response.status_code == 422

    # Invalid JSON syntax
    raw = client.post(
        "/briefs/extract",
        content=b"not a json",
        headers={"content-type": "application/json"},
    )
    assert raw.status_code == 422


# ----------------------------------------------------------------------
# 8. backward compat: legacy combined /plans/{id}/approve still works
# ----------------------------------------------------------------------


def test_legacy_combined_approve_endpoint_still_executes() -> None:
    client = _fresh_client()
    plan = _seed_plan(client)
    first_id = plan["actions"][0]["action_id"]

    response = client.post(
        f"/plans/{plan['plan_id']}/approve",
        json={"approved_action_ids": [first_id], "approved_by": "pm@example.com"},
    )
    assert response.status_code == 200
    result = response.json()
    assert any(r["action_id"] == first_id for r in result["executed"])


# ----------------------------------------------------------------------
# 9. unknown plan_id returns 404 on all plan-bound endpoints
# ----------------------------------------------------------------------


def test_unknown_plan_returns_404() -> None:
    client = _fresh_client()
    bad = "plan_does_not_exist"
    body = {"approved_action_ids": [], "approved_by": "pm@example.com"}
    assert client.post(f"/plans/{bad}/approve", json=body).status_code == 404
    assert client.post(f"/approvals/{bad}/approve", json=body).status_code == 404
    assert (
        client.post(
            f"/executions/{bad}/run", json={"executed_by": "pm@example.com"}
        ).status_code
        == 404
    )
