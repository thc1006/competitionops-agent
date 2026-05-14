"""Tier 0 #4 integration — Settings.audit_log_dir switches the factory.

These tests sit one layer above ``test_file_audit.py``: they exercise
the full FastAPI lifecycle with ``AUDIT_LOG_DIR`` set, then verify the
audit log survives a simulated process restart (lru_cache clear +
fresh factory instance).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from competitionops import config as config_module
from competitionops import main as main_module
from competitionops.adapters.file_audit import FileAuditLog
from competitionops.adapters.memory_audit import InMemoryAuditLog


def _reset_all_caches() -> None:
    config_module.get_settings.cache_clear()
    main_module._plan_repo.cache_clear()
    main_module._audit_log.cache_clear()
    main_module._registry.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_settings_cache_per_test():
    """Stop AUDIT_LOG_DIR leaking into later test files via the
    ``get_settings()`` lru_cache singleton. Monkeypatch restores env at
    teardown but does not invalidate the cached Settings instance, so we
    clear every cache at the END of each test here (the post-yield
    branch runs before monkeypatch restores env).
    """
    yield
    _reset_all_caches()


@pytest.fixture
def file_backed_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    monkeypatch.setenv("AUDIT_LOG_DIR", str(tmp_path))
    _reset_all_caches()
    return TestClient(main_module.app)


def test_audit_log_factory_returns_in_memory_when_dir_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUDIT_LOG_DIR", raising=False)
    _reset_all_caches()
    audit = main_module._audit_log()
    assert isinstance(audit, InMemoryAuditLog)


def test_audit_log_factory_returns_file_backed_when_dir_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUDIT_LOG_DIR", str(tmp_path))
    _reset_all_caches()
    audit = main_module._audit_log()
    assert isinstance(audit, FileAuditLog)
    assert audit.base_dir == tmp_path


def test_audit_records_written_to_file_on_full_lifecycle(
    file_backed_client: TestClient, tmp_path: Path
) -> None:
    brief = file_backed_client.post(
        "/briefs/extract",
        json={
            "source_type": "text",
            "content": (
                "Persistence Cup\nSubmission deadline: 2026-09-30\n"
                "Required: pitch deck.\n"
            ),
        },
    ).json()
    for deliverable in brief["deliverables"]:
        deliverable["owner_role"] = "business"

    plan = file_backed_client.post(
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
            "preferences": {"pm_approval_required": True},
        },
    ).json()
    target = plan["actions"][0]["action_id"]

    file_backed_client.post(
        f"/approvals/{plan['plan_id']}/approve",
        json={"approved_action_ids": [target], "approved_by": "pm@example.com"},
    )
    file_backed_client.post(
        f"/executions/{plan['plan_id']}/run",
        json={"executed_by": "pm@example.com", "action_ids": [target]},
    )

    # JSONL file for this plan exists and contains at least the
    # approved + executed lifecycle records for the targeted action.
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert plan["plan_id"] in content
    assert target in content
    assert '"status":"approved"' in content
    assert '"status":"executed"' in content


def test_audit_records_survive_simulated_process_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write records via one TestClient, simulate restart by clearing every
    lru_cache, then read them back via a fresh FileAuditLog instance."""
    monkeypatch.setenv("AUDIT_LOG_DIR", str(tmp_path))
    _reset_all_caches()
    client = TestClient(main_module.app)

    brief = client.post(
        "/briefs/extract",
        json={
            "source_type": "text",
            "content": (
                "Restart Cup\nSubmission deadline: 2026-09-30\n"
                "Required: pitch deck.\n"
            ),
        },
    ).json()
    for deliverable in brief["deliverables"]:
        deliverable["owner_role"] = "business"
    plan = client.post(
        "/plans/generate",
        json={
            "competition": brief,
            "team_capacity": [
                {
                    "member_id": "m1",
                    "name": "A",
                    "role": "business",
                    "weekly_capacity_hours": 20,
                }
            ],
            "preferences": {"pm_approval_required": True},
        },
    ).json()
    target = plan["actions"][0]["action_id"]
    client.post(
        f"/approvals/{plan['plan_id']}/approve",
        json={"approved_action_ids": [target], "approved_by": "pm@example.com"},
    )

    # Simulate process restart: every cache (plan_repo, audit_log,
    # registry, settings) gets wiped, then a fresh factory rebuilds them.
    _reset_all_caches()

    # The plan_repo is gone (in-memory), so we can't continue the HTTP
    # flow. But the audit log persisted to disk — a fresh factory must
    # see the original records.
    fresh_audit = main_module._audit_log()
    assert isinstance(fresh_audit, FileAuditLog)

    records = fresh_audit.list_for_plan(plan["plan_id"])
    assert records, "audit records must survive restart"
    statuses = {r.status for r in records if r.action_id == target}
    assert "approved" in statuses


def test_audit_dir_factory_idempotent_within_single_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lru_cache singleton: two calls inside one process return same instance."""
    monkeypatch.setenv("AUDIT_LOG_DIR", str(tmp_path))
    _reset_all_caches()
    first = main_module._audit_log()
    second = main_module._audit_log()
    assert first is second
