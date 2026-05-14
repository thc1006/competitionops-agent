"""P2-002 — Windmill workflow script contract.

Each script under ``infra/windmill/scripts/`` is a standalone Python file
with a single ``def main(...) -> dict`` entry point (Windmill's idiomatic
shape). The scripts call the CompetitionOps HTTP API via httpx, using
``WINDMILL_API_BASE`` env to switch between dev / staging / prod.

These tests:
- Load each script via ``importlib`` (mirrors how Windmill itself loads
  rawscript modules — by file path, not Python package).
- Replace ``httpx.Client`` with one wrapped around ``httpx.ASGITransport``
  pointing at the in-process FastAPI app, so no real TCP connection is
  ever made.
- Verify input validation, single-script behaviour, and end-to-end
  composition of the three scripts.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from competitionops import main as main_module
from competitionops.main import app

_SCRIPTS_DIR = (
    Path(__file__).resolve().parent.parent / "infra" / "windmill" / "scripts"
)

_RUNSPACE_BRIEF = (
    "RunSpace Innovation Challenge 2026\n"
    "Submission deadline: 2026-09-30\n"
    "Required deliverables: pitch deck.\n"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_script(name: str) -> ModuleType:
    """Load a Windmill script file the way Windmill does — by path, not import."""
    path = _SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def in_process_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect script ``httpx.Client`` calls to the ASGI app in-process.

    ``httpx.ASGITransport`` is async-only, so a sync script using
    ``httpx.Client`` can't drive it directly. Instead we build a
    ``TestClient(app)`` (which internally wires ASGI under sync) BEFORE
    patching ``httpx.Client``, then install a sync ``MockTransport``
    whose handler forwards each request through that pre-built
    TestClient. End result: scripts use plain ``httpx.Client`` with no
    real TCP, and the FastAPI app receives the request as if a real
    Windmill worker had sent it.
    """
    main_module._plan_repo.cache_clear()
    main_module._audit_log.cache_clear()
    main_module._registry.cache_clear()

    # Build TestClient FIRST so it captures the unpatched httpx.Client.
    test_client = TestClient(app)

    def forward_handler(request: httpx.Request) -> httpx.Response:
        response = test_client.request(
            method=request.method,
            url=request.url.path,
            params=request.url.params,
            content=request.content,
            headers=request.headers,
        )
        return httpx.Response(
            status_code=response.status_code,
            content=response.content,
            headers=response.headers,
        )

    transport = httpx.MockTransport(forward_handler)
    original_client_cls = httpx.Client

    def patched_client(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = transport
        kwargs["base_url"] = "http://testserver"
        return original_client_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched_client)


# ---------------------------------------------------------------------------
# Sprint 0 — input validation
# ---------------------------------------------------------------------------


def test_extract_brief_script_rejects_empty_content() -> None:
    module = _load_script("extract_brief")
    with pytest.raises(ValueError, match="content"):
        module.main(content="")


def test_generate_plan_script_rejects_non_dict_competition() -> None:
    module = _load_script("generate_plan")
    with pytest.raises(ValueError, match="competition must be a dict"):
        module.main(competition="not a dict")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="competition.name"):
        module.main(competition={})


def test_approve_and_execute_script_rejects_missing_inputs() -> None:
    module = _load_script("approve_and_execute")
    with pytest.raises(ValueError, match="plan_id"):
        module.main(plan_id="", approved_action_ids=[], approved_by="pm@example.com")
    with pytest.raises(ValueError, match="approved_by"):
        module.main(plan_id="plan_x", approved_action_ids=[], approved_by="")
    with pytest.raises(ValueError, match="approved_action_ids"):
        module.main(
            plan_id="plan_x",
            approved_action_ids=None,  # type: ignore[arg-type]
            approved_by="pm@example.com",
        )


# ---------------------------------------------------------------------------
# Sprint 2 — single-script behaviour against ASGI test server
# ---------------------------------------------------------------------------


def test_extract_brief_script_returns_structured_brief(
    in_process_api: None,
) -> None:
    module = _load_script("extract_brief")
    brief = module.main(content=_RUNSPACE_BRIEF, source_uri="test://runspace")
    assert brief["name"].startswith("RunSpace")
    assert brief["submission_deadline"].startswith("2026-09-30")
    assert brief["source_uri"] == "test://runspace"


def test_generate_plan_script_returns_action_plan(
    in_process_api: None,
) -> None:
    extract = _load_script("extract_brief")
    generate = _load_script("generate_plan")

    brief = extract.main(content=_RUNSPACE_BRIEF)
    plan = generate.main(
        competition=brief,
        team_capacity=[
            {
                "member_id": "m1",
                "name": "Alice",
                "role": "business",
                "weekly_capacity_hours": 20,
            }
        ],
    )
    assert plan["dry_run"] is True
    assert plan["requires_approval"] is True
    assert plan["actions"]
    assert plan["plan_id"]


def test_approve_and_execute_script_runs_lifecycle(
    in_process_api: None,
) -> None:
    extract = _load_script("extract_brief")
    generate = _load_script("generate_plan")
    approve = _load_script("approve_and_execute")

    brief = extract.main(content=_RUNSPACE_BRIEF)
    for deliverable in brief["deliverables"]:
        deliverable["owner_role"] = "business"
    plan = generate.main(
        competition=brief,
        team_capacity=[
            {
                "member_id": "m1",
                "name": "Alice",
                "role": "business",
                "weekly_capacity_hours": 20,
            }
        ],
    )
    first_action = plan["actions"][0]["action_id"]

    result = approve.main(
        plan_id=plan["plan_id"],
        approved_action_ids=[first_action],
        approved_by="pm@example.com",
    )
    assert result["plan_id"] == plan["plan_id"]
    assert any(r["action_id"] == first_action for r in result["executed"])
    assert result["failed"] == []
    assert result["blocked"] == []


# ---------------------------------------------------------------------------
# Sprint 2 — end-to-end composition: extract → generate → approve+execute
# ---------------------------------------------------------------------------


def test_three_scripts_compose_into_full_e2e_pipeline(
    in_process_api: None,
) -> None:
    """Mirrors what the Windmill flow does: run all three scripts in
    sequence and verify the audit log records both approved + executed
    events for the chosen action."""
    extract = _load_script("extract_brief")
    generate = _load_script("generate_plan")
    approve = _load_script("approve_and_execute")

    brief = extract.main(content=_RUNSPACE_BRIEF)
    for deliverable in brief["deliverables"]:
        deliverable["owner_role"] = "business"

    plan = generate.main(
        competition=brief,
        team_capacity=[
            {
                "member_id": "m1",
                "name": "A",
                "role": "business",
                "weekly_capacity_hours": 20,
            }
        ],
    )
    target = plan["actions"][0]["action_id"]

    result = approve.main(
        plan_id=plan["plan_id"],
        approved_action_ids=[target],
        approved_by="pm@example.com",
    )

    executed_ids = {r["action_id"] for r in result["executed"]}
    assert target in executed_ids

    # Sanity: the audit log on the in-process app has both lifecycle events.
    audit = main_module._audit_log().list_for_plan(plan["plan_id"])
    statuses = {r.status for r in audit if r.action_id == target}
    assert {"approved", "executed"}.issubset(statuses)


# ---------------------------------------------------------------------------
# Env-driven base URL switching (proves the same script file targets
# dev / staging / prod by env alone)
# ---------------------------------------------------------------------------


def test_windmill_scripts_honor_windmill_api_base_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The script must read ``WINDMILL_API_BASE`` from env at call time,
    not bake it in at import time. We verify by setting a sentinel URL
    and intercepting the request — the URL the script sends to must
    match."""
    sentinel_base = "http://intercept.example.invalid:9999"
    monkeypatch.setenv("WINDMILL_API_BASE", sentinel_base)

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "competition_id": "x",
                "name": "x",
                "deliverables": [],
                "risk_flags": [],
                "eligibility": [],
                "scoring_rubric": [],
                "anonymous_rules": [],
                "language_requirements": [],
            },
        )

    transport = httpx.MockTransport(handler)
    original = httpx.Client

    def patched_client(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched_client)

    module = _load_script("extract_brief")
    module.main(content="x")

    assert len(captured) == 1
    request_url = str(captured[0].url)
    assert request_url.startswith(sentinel_base), (
        f"expected {sentinel_base} prefix, got {request_url}"
    )
