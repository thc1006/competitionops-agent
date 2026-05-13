import inspect
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from competitionops.adapters import (
    google_calendar as calendar_mod,
    google_docs as docs_mod,
    google_drive as drive_mod,
    google_sheets as sheets_mod,
)
from competitionops.adapters.google_calendar import GoogleCalendarAdapter
from competitionops.adapters.google_docs import GoogleDocsAdapter
from competitionops.adapters.google_drive import GoogleDriveAdapter
from competitionops.adapters.google_sheets import GoogleSheetsAdapter
from competitionops.adapters.memory_audit import InMemoryAuditLog
from competitionops.adapters.memory_plan_store import InMemoryPlanRepository
from competitionops.adapters.registry import AdapterRegistry
from competitionops.config import Settings
from competitionops.schemas import (
    CompetitionBrief,
    Deliverable,
    ExternalAction,
    RiskLevel,
    TeamMember,
)
from competitionops.services.execution import ExecutionService
from competitionops.services.planner import CompetitionPlanner

TZ = ZoneInfo("Asia/Taipei")
FIXED_NOW = datetime(2026, 5, 13, 9, 0, tzinfo=TZ)


@pytest.mark.asyncio
async def test_drive_create_folder_mock_is_idempotent() -> None:
    adapter = GoogleDriveAdapter()
    first = await adapter.create_folder(name="Competition RunSpace")
    second = await adapter.create_folder(name="Competition RunSpace")
    other = await adapter.create_folder(name="Different")

    assert first["id"].startswith("mock_folder_")
    assert first["id"] == second["id"]
    assert first["id"] != other["id"]
    assert first["url"].startswith("https://drive.example.invalid/")


@pytest.mark.asyncio
async def test_drive_move_file_and_search_mock() -> None:
    adapter = GoogleDriveAdapter()
    target = await adapter.create_folder(name="Target Folder")
    moved = await adapter.move_file(file_id="file_xyz", target_parent_id=target["id"])
    assert moved["file_id"] == "file_xyz"
    assert moved["parent_id"] == target["id"]

    results = await adapter.search_files(query="Target")
    assert any(item.get("name") == "Target Folder" for item in results)


@pytest.mark.asyncio
async def test_docs_create_doc_and_append_section_mock() -> None:
    adapter = GoogleDocsAdapter()
    doc = await adapter.create_doc(
        title="RunSpace Proposal",
        sections=["Problem", "Solution"],
    )
    assert doc["id"].startswith("mock_doc_")
    assert doc["sections"] == ["Problem", "Solution"]

    updated = await adapter.append_section(
        doc_id=doc["id"], heading="Risks", body="Anonymous rule risk."
    )
    assert "Risks" in updated["sections"]
    assert updated["body"]["Risks"] == "Anonymous rule risk."


@pytest.mark.asyncio
async def test_sheets_append_rows_and_update_cells_mock() -> None:
    adapter = GoogleSheetsAdapter()
    append_result = await adapter.append_rows(
        sheet_id="tracker",
        rows=[
            {"name": "RunSpace", "deadline": "2026-09-30"},
            {"name": "DemoCup", "deadline": "2026-10-15"},
        ],
    )
    assert append_result["sheet_id"] == "tracker"
    assert append_result["row_count"] == 2

    updated = await adapter.update_cells(
        sheet_id="tracker",
        cell_updates={"A1": "Competition", "B1": "Deadline"},
    )
    assert updated["cells"]["A1"] == "Competition"
    assert updated["cells"]["B1"] == "Deadline"


@pytest.mark.asyncio
async def test_calendar_checkpoint_series_mock() -> None:
    adapter = GoogleCalendarAdapter()
    deadline = datetime(2026, 9, 30, 23, 59, tzinfo=TZ)

    series = await adapter.create_checkpoint_series(
        competition_name="RunSpace",
        deadline=deadline,
    )

    assert len(series) >= 3
    for event in series:
        assert event["id"].startswith("mock_event_")
        start = datetime.fromisoformat(event["start"])
        end = datetime.fromisoformat(event["end"])
        assert start < end <= deadline


@pytest.mark.asyncio
async def test_unapproved_action_never_reaches_adapter() -> None:
    settings = Settings()
    drive = GoogleDriveAdapter()
    docs = GoogleDocsAdapter()
    sheets = GoogleSheetsAdapter()
    calendar = GoogleCalendarAdapter()

    registry = AdapterRegistry()
    registry.register("google_drive", drive)
    registry.register("google_docs", docs)
    registry.register("google_sheets", sheets)
    registry.register("google_calendar", calendar)

    plan_repo = InMemoryPlanRepository()
    audit = InMemoryAuditLog()
    service = ExecutionService(
        plan_repo=plan_repo, registry=registry, audit_log=audit, settings=settings
    )

    competition = CompetitionBrief(
        competition_id="unapproved",
        name="Unapproved Cup",
        submission_deadline=datetime(2026, 9, 30, 23, 59, tzinfo=TZ),
        deliverables=[Deliverable(title="Pitch deck", owner_role="business")],
    )
    plan = CompetitionPlanner(settings).generate(
        competition,
        team_capacity=[
            TeamMember(member_id="m1", name="Alice", role="business", weekly_capacity_hours=20)
        ],
        now=FIXED_NOW,
    )
    plan_repo.save(plan)

    result = await service.approve_and_execute(
        plan_id=plan.plan_id, approved_action_ids=[], approved_by="pm@example.com"
    )

    assert result.executed == []
    assert drive.calls == []
    assert docs.calls == []
    assert sheets.calls == []
    assert calendar.calls == []
    # mock state must be untouched
    assert drive.folders == {}
    assert docs.docs == {}
    assert sheets.sheets == {}
    assert calendar.events == {}


@pytest.mark.asyncio
async def test_audit_event_generated_for_every_executed_adapter_call() -> None:
    settings = Settings()
    drive = GoogleDriveAdapter()
    docs = GoogleDocsAdapter()
    sheets = GoogleSheetsAdapter()
    calendar = GoogleCalendarAdapter()

    registry = AdapterRegistry()
    registry.register("google_drive", drive)
    registry.register("google_docs", docs)
    registry.register("google_sheets", sheets)
    registry.register("google_calendar", calendar)

    plan_repo = InMemoryPlanRepository()
    audit = InMemoryAuditLog()
    service = ExecutionService(
        plan_repo=plan_repo, registry=registry, audit_log=audit, settings=settings
    )

    competition = CompetitionBrief(
        competition_id="audited",
        name="Audited Cup",
        submission_deadline=datetime(2026, 9, 30, 23, 59, tzinfo=TZ),
        deliverables=[Deliverable(title="Pitch deck", owner_role="business")],
    )
    plan = CompetitionPlanner(settings).generate(
        competition,
        team_capacity=[
            TeamMember(member_id="m1", name="Alice", role="business", weekly_capacity_hours=20)
        ],
        now=FIXED_NOW,
    )
    plan_repo.save(plan)

    google_targets = {"google_drive", "google_docs", "google_sheets", "google_calendar"}
    google_actions = [a for a in plan.actions if a.target_system in google_targets]
    assert google_actions, "planner should emit at least one google action per target"

    approved_ids = [a.action_id for a in google_actions]
    await service.approve_and_execute(
        plan_id=plan.plan_id,
        approved_action_ids=approved_ids,
        approved_by="pm@example.com",
    )

    records = audit.list_for_plan(plan.plan_id)
    seen_targets: set[str] = set()
    for action in google_actions:
        executed_records = [
            r
            for r in records
            if r.action_id == action.action_id and r.status == "executed"
        ]
        assert len(executed_records) == 1, f"missing audit for {action.action_id}"
        assert executed_records[0].target_external_id is not None
        seen_targets.add(action.target_system)
    assert google_targets.issubset(seen_targets)


def test_no_real_google_or_network_imports_in_adapter_modules() -> None:
    forbidden = [
        "googleapiclient",
        "google.oauth2",
        "google.auth",
        "google_auth_oauthlib",
        "requests",
        "httpx",
        "urllib.request",
        "http.client",
        "socket.socket",
        "open('.env",
        'open(".env',
        "credentials.json",
        "client_secret.json",
    ]
    for module in (drive_mod, docs_mod, sheets_mod, calendar_mod):
        source = inspect.getsource(module)
        for needle in forbidden:
            assert needle not in source, (
                f"{module.__name__} must not reference {needle!r} in Stage 4 (mock-first)"
            )


@pytest.mark.asyncio
async def test_drive_adapter_blocks_unknown_action_type() -> None:
    adapter = GoogleDriveAdapter()
    action = ExternalAction(
        action_id="act_unknown",
        type="google.drive.does_not_exist",
        target_system="google_drive",
        payload={},
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )
    result = await adapter.execute(action, dry_run=True)
    assert result.status == "failed"
    assert "unknown action type" in (result.error or "")
