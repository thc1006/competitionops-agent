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


# ---------------------------------------------------------------------------
# H3 — per-writer (per-pod) filename layout
#
# Background: a single ``<plan_id>.jsonl`` shared by N concurrent
# writers is unsafe on most RWX filesystems (POSIX append-atomicity
# applies only up to ``PIPE_BUF`` AND only on local FS — NFS / Azure
# Files / EFS routinely violate this). Rather than rely on
# ``fcntl.flock`` whose semantics differ across RWX backends, we give
# each writer its own file. By construction, there is no shared
# resource between writers, so torn writes are impossible regardless
# of filesystem.
#
# Storage layout::
#
#     <base_dir>/
#       ├── <safe(plan_id)>.<safe(writer_id_a)>.jsonl
#       ├── <safe(plan_id)>.<safe(writer_id_b)>.jsonl
#       └── ...
#
# ``list_for_plan`` globs ``<safe>.*.jsonl`` and merges. Backward
# compatibility: legacy ``<safe>.jsonl`` files from pre-H3 deployments
# are also picked up by the same listing, so an upgrade does not lose
# historical audit records.
# ---------------------------------------------------------------------------


def test_file_audit_log_writer_id_defaults_to_hostname(tmp_path: Path) -> None:
    """In a k8s pod, ``socket.gethostname()`` returns the pod name
    automatically (k8s sets ``HOSTNAME`` to ``metadata.name``). On a
    laptop it returns the local hostname. Either way the default gives
    each writer its own filename without any explicit config."""
    import socket

    log = FileAuditLog(base_dir=tmp_path)
    assert log.writer_id, "writer_id must default to a non-empty value"

    expected_stem = log._sanitise(socket.gethostname() or "writer")
    log.append(_record(plan_id="plan_default_writer", action_id="act_1"))
    written = list(tmp_path.glob("plan_default_writer.*.jsonl"))
    assert len(written) == 1
    # The writer stem the adapter chose appears in the filename.
    assert f".{expected_stem}." in f".{written[0].stem}." or expected_stem in written[
        0
    ].name


def test_file_audit_log_explicit_writer_id_appears_in_filename(
    tmp_path: Path,
) -> None:
    """Operators (or tests) can set writer_id explicitly. The name then
    appears between the plan_id and the .jsonl suffix."""
    log = FileAuditLog(base_dir=tmp_path, writer_id="pod-alpha")
    log.append(_record(plan_id="plan_named_writer", action_id="act_1"))

    written = list(tmp_path.glob("*.jsonl"))
    assert len(written) == 1
    assert written[0].name == "plan_named_writer.pod-alpha.jsonl", (
        f"unexpected filename {written[0].name!r}; expected "
        "``<plan_id>.<writer_id>.jsonl`` layout"
    )


def test_file_audit_log_separate_writers_separate_files(tmp_path: Path) -> None:
    """The structural invariant of the H3 fix: two writers against the
    same plan_id write to two *different* files. No shared resource ⇒
    no torn-write risk regardless of filesystem semantics."""
    pod_a = FileAuditLog(base_dir=tmp_path, writer_id="pod-a")
    pod_b = FileAuditLog(base_dir=tmp_path, writer_id="pod-b")

    pod_a.append(_record(plan_id="plan_shared", action_id="from_a_1"))
    pod_b.append(_record(plan_id="plan_shared", action_id="from_b_1"))
    pod_a.append(_record(plan_id="plan_shared", action_id="from_a_2"))

    files = sorted(p.name for p in tmp_path.glob("*.jsonl"))
    assert files == [
        "plan_shared.pod-a.jsonl",
        "plan_shared.pod-b.jsonl",
    ], f"expected one file per writer, got {files!r}"


def test_file_audit_log_list_for_plan_merges_across_writers(tmp_path: Path) -> None:
    """``list_for_plan`` must merge records from every writer's file so
    callers see the complete audit trail for that plan_id regardless of
    which pod wrote which record."""
    pod_a = FileAuditLog(base_dir=tmp_path, writer_id="pod-a")
    pod_b = FileAuditLog(base_dir=tmp_path, writer_id="pod-b")
    pod_c = FileAuditLog(base_dir=tmp_path, writer_id="pod-c")

    pod_a.append(_record(plan_id="plan_merge", action_id="a1"))
    pod_b.append(_record(plan_id="plan_merge", action_id="b1"))
    pod_a.append(_record(plan_id="plan_merge", action_id="a2"))
    pod_c.append(_record(plan_id="plan_merge", action_id="c1"))

    # Any reader sees the full picture.
    for reader in (pod_a, pod_b, pod_c):
        records = reader.list_for_plan("plan_merge")
        assert {r.action_id for r in records} == {"a1", "b1", "a2", "c1"}, (
            f"reader {reader.writer_id!r} saw {[r.action_id for r in records]!r}"
        )


def test_file_audit_log_does_not_leak_across_plan_ids_in_multi_writer_layout(
    tmp_path: Path,
) -> None:
    """A second plan_id from any writer must NOT appear in the listing
    for the first plan_id (catches a glob that's too permissive)."""
    pod_a = FileAuditLog(base_dir=tmp_path, writer_id="pod-a")
    pod_b = FileAuditLog(base_dir=tmp_path, writer_id="pod-b")

    pod_a.append(_record(plan_id="plan_target", action_id="t1"))
    pod_b.append(_record(plan_id="plan_target", action_id="t2"))
    pod_a.append(_record(plan_id="plan_OTHER", action_id="other1"))

    records = pod_a.list_for_plan("plan_target")
    assert {r.action_id for r in records} == {"t1", "t2"}


def test_file_audit_log_high_volume_multi_writer_loses_no_records(
    tmp_path: Path,
) -> None:
    """Sanity check the by-construction invariant: 100 records from
    each of three writers, totalling 300, all readable. Because each
    writer has its own file there's nothing to torn-write — this test
    is fast and deterministic, no threading needed."""
    writers = [
        FileAuditLog(base_dir=tmp_path, writer_id=f"pod-{name}")
        for name in ("alpha", "beta", "gamma")
    ]
    expected: set[str] = set()
    for index in range(100):
        for writer in writers:
            action_id = f"{writer.writer_id}-act_{index:03d}"
            writer.append(_record(plan_id="plan_volume", action_id=action_id))
            expected.add(action_id)

    seen = {r.action_id for r in writers[0].list_for_plan("plan_volume")}
    assert seen == expected, (
        f"lost {len(expected - seen)} records across writers; "
        f"first 5 missing: {sorted(expected - seen)[:5]!r}"
    )


def test_file_audit_log_sanitises_writer_id_against_path_traversal(
    tmp_path: Path,
) -> None:
    """An attacker-controlled or malformed writer_id must not be able
    to escape ``base_dir``. Operators normally set writer_id via the
    k8s pod name (already alphanumeric+dash), but defence in depth."""
    log = FileAuditLog(base_dir=tmp_path, writer_id="../../etc/passwd")
    log.append(_record(plan_id="plan_writer_escape", action_id="evil"))

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    name = files[0].name
    assert "/" not in name
    assert ".." not in name


def test_file_audit_log_reads_legacy_single_file_layout(tmp_path: Path) -> None:
    """Backward compatibility for upgrade-in-place: a deployment that
    has been running pre-H3 already has ``<plan_id>.jsonl`` files (no
    writer_id segment). After upgrade, those records must still be
    surfaced by ``list_for_plan`` so historical audit trails aren't
    silently lost.
    """
    legacy_path = tmp_path / "plan_legacy.jsonl"
    legacy_record = _record(plan_id="plan_legacy", action_id="historic")
    legacy_path.write_text(legacy_record.model_dump_json() + "\n", encoding="utf-8")

    # New writer joins the mix.
    new_writer = FileAuditLog(base_dir=tmp_path, writer_id="pod-new")
    new_writer.append(_record(plan_id="plan_legacy", action_id="fresh"))

    records = new_writer.list_for_plan("plan_legacy")
    assert {r.action_id for r in records} == {"historic", "fresh"}
