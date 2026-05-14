"""Tier 0 #4 — FileAuditLog persistence contract.

The in-memory audit log loses every record on process restart, which
makes Stage 7 finding M2 / Tier 0 #4 a hard blocker for K8s deployment
(no PVC mount point) and for any post-incident forensics. This file
pins down the append-only JSONL persistence behavior that production
deployments will rely on.

Tests use ``tmp_path`` so no global filesystem state survives between
runs. The synthetic AuditRecord values never represent real activity.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from competitionops.adapters.file_audit import FileAuditLog
from competitionops.schemas import AuditRecord

TZ = ZoneInfo("Asia/Taipei")


def _record(
    *,
    plan_id: str,
    action_id: str = "act_test",
    status: str = "approved",
    actor: str = "pm@example.com",
) -> AuditRecord:
    """Synthetic AuditRecord for tests — never represents real activity."""
    return AuditRecord(
        action_id=action_id,
        plan_id=plan_id,
        actor=actor,
        action_type="google.drive.create_competition_folder",
        target_system="google_drive",
        target_external_id=None,
        dry_run=True,
        approved_by=actor,
        approved_at=datetime(2026, 5, 14, 12, 0, tzinfo=TZ),
        executed_at=None,
        status=status,  # type: ignore[arg-type]
        error=None,
        request_hash="abcdef0123456789",
    )


def test_file_audit_log_creates_base_dir_if_missing(tmp_path: Path) -> None:
    base = tmp_path / "audit_does_not_exist_yet"
    assert not base.exists()
    FileAuditLog(base_dir=base)
    assert base.is_dir()


def test_file_audit_log_append_then_list_round_trips(tmp_path: Path) -> None:
    log = FileAuditLog(base_dir=tmp_path)
    record = _record(plan_id="plan_round_trip", action_id="act_001")

    log.append(record)
    records = log.list_for_plan("plan_round_trip")

    assert len(records) == 1
    assert records[0].action_id == "act_001"
    assert records[0].plan_id == "plan_round_trip"
    assert records[0].status == "approved"
    assert records[0].request_hash == "abcdef0123456789"


def test_file_audit_log_appends_in_order(tmp_path: Path) -> None:
    log = FileAuditLog(base_dir=tmp_path)
    for index in range(5):
        log.append(_record(plan_id="plan_order", action_id=f"act_{index:03d}"))

    records = log.list_for_plan("plan_order")
    assert [r.action_id for r in records] == [f"act_{i:03d}" for i in range(5)]


def test_file_audit_log_isolates_by_plan_id(tmp_path: Path) -> None:
    log = FileAuditLog(base_dir=tmp_path)
    log.append(_record(plan_id="plan_alpha", action_id="act_a1"))
    log.append(_record(plan_id="plan_alpha", action_id="act_a2"))
    log.append(_record(plan_id="plan_beta", action_id="act_b1"))

    alpha = log.list_for_plan("plan_alpha")
    beta = log.list_for_plan("plan_beta")

    assert {r.action_id for r in alpha} == {"act_a1", "act_a2"}
    assert {r.action_id for r in beta} == {"act_b1"}


def test_file_audit_log_survives_reconstruction(tmp_path: Path) -> None:
    """Simulates a process restart: write with one instance, read with another."""
    writer = FileAuditLog(base_dir=tmp_path)
    writer.append(_record(plan_id="plan_persist", action_id="act_first"))
    writer.append(_record(plan_id="plan_persist", action_id="act_second", status="executed"))

    # Drop the original instance, build a fresh one against the same dir.
    del writer
    reader = FileAuditLog(base_dir=tmp_path)
    records = reader.list_for_plan("plan_persist")

    assert len(records) == 2
    assert {r.action_id for r in records} == {"act_first", "act_second"}
    statuses = {r.status for r in records}
    assert statuses == {"approved", "executed"}


def test_file_audit_log_list_missing_plan_returns_empty(tmp_path: Path) -> None:
    log = FileAuditLog(base_dir=tmp_path)
    assert log.list_for_plan("plan_never_written") == []


def test_file_audit_log_sanitises_slash_in_plan_id(tmp_path: Path) -> None:
    """Defensive: hash-based plan_ids should never contain a slash, but if a
    legacy id sneaks through we must not allow path traversal."""
    log = FileAuditLog(base_dir=tmp_path)
    log.append(_record(plan_id="../../etc/passwd", action_id="act_evil"))

    # The append must succeed and the record must be retrievable via the
    # exact same plan_id. Crucially, no file appears outside tmp_path.
    records = log.list_for_plan("../../etc/passwd")
    assert len(records) == 1
    assert records[0].action_id == "act_evil"

    # tmp_path is the only directory FileAuditLog touched.
    written_files = list(tmp_path.glob("*.jsonl"))
    assert len(written_files) == 1
    # The filename should not contain the slashes that would escape tmp_path.
    assert "/" not in written_files[0].name


def test_file_audit_log_satisfies_audit_log_port(tmp_path: Path) -> None:
    """Structural typing check — FileAuditLog can stand in for AuditLogPort."""
    from competitionops.ports import AuditLogPort

    log: AuditLogPort = FileAuditLog(base_dir=tmp_path)
    log.append(_record(plan_id="plan_port"))
    assert log.list_for_plan("plan_port")
