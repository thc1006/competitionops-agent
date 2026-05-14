"""H2 follow-up integration — ``Settings.plan_repo_dir`` switches the
``_plan_repo`` factory.

Mirrors ``test_audit_persistence_integration.py``: ``main._plan_repo()``
returns the in-memory adapter when ``PLAN_REPO_DIR`` is unset and a
file-backed one when it is. With this switch operators can opt their
prod deployment into restart-resilient plan storage by mounting a PVC
and pointing ``PLAN_REPO_DIR`` at it — same shape as Tier 0 #4's
``AUDIT_LOG_DIR``.

This commit does NOT lift the prod ``replicas: 1`` pin from H2 — that
gate also depends on H3 (audit-log multi-writer safety), which is a
separate PR. Once both ship, operators can scale prod horizontally.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from competitionops import main as main_module
from competitionops.adapters.file_plan_store import FilePlanRepository
from competitionops.adapters.memory_plan_store import InMemoryPlanRepository

# Round-2 M6 — the per-test cache teardown lives in ``tests/conftest.py``
# as an autouse fixture (so future runtime singletons are reset
# everywhere by default). Tests that need to clear caches mid-body
# import the helper from conftest (pytest adds the conftest's directory
# to sys.path automatically).
from conftest import reset_runtime_caches as _reset_all_caches  # noqa: E402, I001


def test_plan_repo_factory_returns_in_memory_when_dir_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLAN_REPO_DIR", raising=False)
    _reset_all_caches()
    repo = main_module._plan_repo()
    assert isinstance(repo, InMemoryPlanRepository)


def test_plan_repo_factory_returns_file_backed_when_dir_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLAN_REPO_DIR", str(tmp_path))
    _reset_all_caches()
    repo = main_module._plan_repo()
    assert isinstance(repo, FilePlanRepository)
    assert repo.base_dir == tmp_path


def test_plan_repo_factory_returns_same_singleton_across_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lru_cache singleton must NOT spin up a new FilePlanRepository
    on every call; otherwise tests / handlers would see inconsistent
    state if the dir's contents change (e.g., between requests)."""
    monkeypatch.setenv("PLAN_REPO_DIR", str(tmp_path))
    _reset_all_caches()
    first = main_module._plan_repo()
    second = main_module._plan_repo()
    assert first is second


def test_plan_survives_simulated_pod_restart_via_file_backed_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full lifecycle: extract → plan with one process, then drop the
    in-process singleton (lru_cache.clear() simulates pod restart) and
    verify the plan re-appears via a freshly constructed
    FilePlanRepository pointing at the same dir.

    This is the core property H2 needed: plans must outlive the pod
    that created them so a load balancer can route the approve request
    to a different replica (once H3 also lands and the pin is lifted).
    """
    monkeypatch.setenv("PLAN_REPO_DIR", str(tmp_path))
    _reset_all_caches()
    client = TestClient(main_module.app)

    # Extract a brief then generate a plan.
    brief_response = client.post(
        "/briefs/extract",
        json={
            "source_type": "text",
            "content": (
                "Persistence Cup\nSubmission deadline: 2026-09-30\n"
                "Required: pitch deck.\n"
            ),
        },
    )
    assert brief_response.status_code == 200, brief_response.text
    brief = brief_response.json()

    plan_response = client.post(
        "/plans/generate",
        json={
            "competition": brief,
            "team_capacity": [
                {
                    "member_id": "m1",
                    "name": "Alice",
                    "role": "business",
                    "weekly_capacity_hours": 20,
                }
            ],
        },
    )
    assert plan_response.status_code == 200, plan_response.text
    plan = plan_response.json()
    plan_id = plan["plan_id"]

    # File must exist on disk under the configured dir.
    files = list(tmp_path.glob("*.json"))
    assert any(plan_id in f.name for f in files), (
        f"plan file for {plan_id!r} not found in {tmp_path}; "
        f"saw {[f.name for f in files]!r}"
    )

    # Simulate pod restart by clearing the lru_cache singleton. The
    # next call to _plan_repo() instantiates a fresh
    # FilePlanRepository — and it must still see the previously-saved
    # plan because the file is on the shared volume.
    main_module._plan_repo.cache_clear()
    repo = main_module._plan_repo()
    assert isinstance(repo, FilePlanRepository)
    loaded = repo.get(plan_id)
    assert loaded is not None
    assert loaded.plan_id == plan_id


def test_two_processes_against_the_same_dir_see_each_others_plans(
    tmp_path: Path,
) -> None:
    """Direct integration: two FilePlanRepository instances on the
    same dir (no FastAPI involvement) act like two pods on a shared
    PVC. Catches any global mutable state in the adapter that would
    prevent multi-replica deployments from working."""
    pod_a = FilePlanRepository(base_dir=tmp_path)
    pod_b = FilePlanRepository(base_dir=tmp_path)

    # Plan saved on A is visible on B.
    from competitionops.schemas import ActionPlan

    plan = ActionPlan(plan_id="plan_cross_pod", competition_id="comp")
    pod_a.save(plan)
    loaded = pod_b.get("plan_cross_pod")
    assert loaded is not None
    assert loaded.plan_id == "plan_cross_pod"
