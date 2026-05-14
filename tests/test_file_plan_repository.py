"""H2 follow-up — ``FilePlanRepository`` persistence contract.

The default ``InMemoryPlanRepository`` is a process-bound
``dict[str, ActionPlan]``. With >1 pod a plan created on pod A is
invisible to pod B, which is precisely why prod is still pinned to
``replicas: 1`` (see ``infra/k8s/overlays/prod/deployment-patch.yaml``).
This file pins down a file-backed alternative that:

- Survives pod restart (mirrors Tier 0 #4's ``FileAuditLog``).
- Is multi-writer-safe **for plans** because each ``plan_id`` lives in
  its own JSON file and ``save`` uses an atomic rename
  (``os.replace``). POSIX guarantees readers see either the old or the
  new file content, never a partial one. Audit-log multi-writer safety
  (H3) is a separate concern and stays out of this commit's scope —
  the prod replicas=1 pin therefore stays in place even after this PR
  lands.

Tests use ``tmp_path`` so no global filesystem state survives runs.
"""

from __future__ import annotations

from pathlib import Path

from competitionops.adapters.file_plan_store import FilePlanRepository
from competitionops.schemas import (
    ActionPlan,
    ExternalAction,
    RiskLevel,
    TaskDraft,
)


def _plan(plan_id: str = "plan_test_001", **overrides: object) -> ActionPlan:
    """Synthetic ActionPlan for tests — never represents real activity."""
    base = ActionPlan(
        plan_id=plan_id,
        competition_id="comp_xyz",
        dry_run=True,
        actions=[
            ExternalAction(
                action_id="act_test_1",
                type="google.drive.create_competition_folder",
                target_system="google_drive",
                payload={"folder_name": "RunSpace"},
                requires_approval=True,
                risk_level=RiskLevel.medium,
            ),
        ],
        task_drafts=[
            TaskDraft(
                task_id="tk_1",
                title="Pitch deck",
                owner_role="business",
                estimated_hours=8.0,
            ),
        ],
        risk_level=RiskLevel.medium,
        requires_approval=True,
        risk_flags=[],
    )
    return base.model_copy(update=overrides)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_file_plan_repository_creates_base_dir_if_missing(tmp_path: Path) -> None:
    base = tmp_path / "plans_does_not_exist_yet"
    assert not base.exists()
    FilePlanRepository(base_dir=base)
    assert base.is_dir()


# ---------------------------------------------------------------------------
# save → get round-trip
# ---------------------------------------------------------------------------


def test_file_plan_repository_save_then_get_round_trips(tmp_path: Path) -> None:
    repo = FilePlanRepository(base_dir=tmp_path)
    plan = _plan("plan_round_trip")

    repo.save(plan)
    loaded = repo.get("plan_round_trip")

    assert loaded is not None
    assert loaded.plan_id == "plan_round_trip"
    assert loaded.competition_id == "comp_xyz"
    assert len(loaded.actions) == 1
    assert loaded.actions[0].action_id == "act_test_1"
    assert loaded.actions[0].target_system == "google_drive"
    assert loaded.task_drafts[0].title == "Pitch deck"
    assert loaded.risk_level == RiskLevel.medium


def test_file_plan_repository_get_missing_returns_none(tmp_path: Path) -> None:
    repo = FilePlanRepository(base_dir=tmp_path)
    assert repo.get("plan_never_written") is None


# ---------------------------------------------------------------------------
# Overwrite semantics — same plan_id, second save replaces first
# ---------------------------------------------------------------------------


def test_file_plan_repository_save_same_plan_id_overwrites(tmp_path: Path) -> None:
    repo = FilePlanRepository(base_dir=tmp_path)
    repo.save(_plan("plan_overwrite", competition_id="first"))
    repo.save(_plan("plan_overwrite", competition_id="second"))

    loaded = repo.get("plan_overwrite")
    assert loaded is not None
    assert loaded.competition_id == "second", (
        "second save must completely replace the first"
    )

    # Only one file exists for that plan_id.
    written = list(tmp_path.glob("plan_overwrite*"))
    assert len(written) == 1, (
        f"overwrite must not leave behind temp / backup files, got {written!r}"
    )


# ---------------------------------------------------------------------------
# list_all
# ---------------------------------------------------------------------------


def test_file_plan_repository_list_all_returns_every_saved_plan(
    tmp_path: Path,
) -> None:
    repo = FilePlanRepository(base_dir=tmp_path)
    repo.save(_plan("plan_a"))
    repo.save(_plan("plan_b"))
    repo.save(_plan("plan_c"))

    plans = repo.list_all()
    assert {p.plan_id for p in plans} == {"plan_a", "plan_b", "plan_c"}


def test_file_plan_repository_list_all_on_empty_dir_returns_empty(
    tmp_path: Path,
) -> None:
    repo = FilePlanRepository(base_dir=tmp_path)
    assert repo.list_all() == []


# ---------------------------------------------------------------------------
# Cross-instance / multi-writer safety
# ---------------------------------------------------------------------------


def test_file_plan_repository_survives_reconstruction(tmp_path: Path) -> None:
    """Simulates pod restart: write with one instance, read with another."""
    writer = FilePlanRepository(base_dir=tmp_path)
    writer.save(_plan("plan_persist"))
    del writer

    reader = FilePlanRepository(base_dir=tmp_path)
    loaded = reader.get("plan_persist")
    assert loaded is not None
    assert loaded.plan_id == "plan_persist"


def test_file_plan_repository_two_instances_share_state(tmp_path: Path) -> None:
    """Simulates two pods backed by the same RWX volume: a save on pod A
    must be visible to pod B's ``get``. Each plan_id is its own file so
    no flock is required for this property."""
    pod_a = FilePlanRepository(base_dir=tmp_path)
    pod_b = FilePlanRepository(base_dir=tmp_path)

    pod_a.save(_plan("plan_from_a"))
    pod_b.save(_plan("plan_from_b"))

    assert pod_b.get("plan_from_a") is not None
    assert pod_a.get("plan_from_b") is not None
    assert {p.plan_id for p in pod_a.list_all()} == {"plan_from_a", "plan_from_b"}


# ---------------------------------------------------------------------------
# Atomic-rename invariant — no partial files visible mid-save
# ---------------------------------------------------------------------------


def test_file_plan_repository_save_uses_atomic_rename_no_temp_leftover(
    tmp_path: Path,
) -> None:
    """After a successful save, only the final ``<plan_id>.json`` file
    exists — no ``.tmp`` / ``.partial`` / lock files lingering. This
    catches an implementation that forgets to rename or leaves the
    temp file around if rename succeeds (e.g., copy-then-delete)."""
    repo = FilePlanRepository(base_dir=tmp_path)
    repo.save(_plan("plan_atomic"))

    files = sorted(p.name for p in tmp_path.iterdir())
    # Exactly one entry, and it's the canonical name.
    assert files == ["plan_atomic.json"], (
        f"unexpected files in plan store: {files!r}. "
        "save() must use atomic rename and leave no temp residue."
    )


# ---------------------------------------------------------------------------
# Filename sanitisation — defensive against path-traversal plan_ids
# ---------------------------------------------------------------------------


def test_file_plan_repository_sanitises_path_separators_in_plan_id(
    tmp_path: Path,
) -> None:
    """Hash-based plan_ids never contain ``/`` or ``..``, but a malformed
    id must NOT escape ``base_dir``. Mirrors the same defence
    ``FileAuditLog`` applies."""
    repo = FilePlanRepository(base_dir=tmp_path)
    repo.save(_plan("../../etc/passwd"))

    # Save + get should still round-trip the exact same id.
    loaded = repo.get("../../etc/passwd")
    assert loaded is not None
    assert loaded.plan_id == "../../etc/passwd"

    # No file written outside tmp_path.
    written = list(tmp_path.glob("*.json"))
    assert len(written) == 1
    assert "/" not in written[0].name
    assert ".." not in written[0].name


# ---------------------------------------------------------------------------
# Protocol satisfaction
# ---------------------------------------------------------------------------


def test_file_plan_repository_satisfies_plan_repository_port(
    tmp_path: Path,
) -> None:
    """Structural typing — FilePlanRepository can stand in anywhere
    ``PlanRepository`` is required (ExecutionService, MCP server, etc.)."""
    from competitionops.ports import PlanRepository

    repo: PlanRepository = FilePlanRepository(base_dir=tmp_path)
    repo.save(_plan("plan_port"))
    assert repo.get("plan_port") is not None
    # list_all is on the Protocol so the MCP server can call
    # _plan_repo().list_all() against either adapter.
    assert len(repo.list_all()) == 1


# ---------------------------------------------------------------------------
# Round-2 L4 — N=100 scale smoke. ``list_all`` opens + parses every
# JSON in ``base_dir`` per call. Establish a baseline that 100 plans
# round-trip cleanly so a future change degrading the I/O pattern
# (e.g. a regression that introduces O(N^2) scan) surfaces here
# rather than waiting for a prod slowdown.
# ---------------------------------------------------------------------------


def test_file_plan_repository_list_all_handles_n_100_round_trip(
    tmp_path: Path,
) -> None:
    """100 plans → save → list_all → all present, ids unique."""
    repo = FilePlanRepository(base_dir=tmp_path)

    for index in range(100):
        repo.save(_plan(plan_id=f"plan_scale_{index:03d}"))

    plans = repo.list_all()
    assert len(plans) == 100
    seen = {p.plan_id for p in plans}
    expected = {f"plan_scale_{i:03d}" for i in range(100)}
    assert seen == expected, (
        f"missing plans at N=100; diff = {expected - seen!r}"
    )
    # Sanity check the on-disk shape: exactly N files, all .json.
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 100
