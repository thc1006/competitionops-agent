import inspect
from datetime import datetime
from types import ModuleType
from zoneinfo import ZoneInfo

import pytest

from competitionops.adapters import (
    google_calendar as calendar_mod,
    google_docs as docs_mod,
    google_drive as drive_mod,
    google_sheets as sheets_mod,
    plane as plane_mod,
    web_crawl4ai as web_crawl4ai_mod,
    web_mock as web_mock_mod,
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

    result = await adapter.create_checkpoint_series(
        competition_name="RunSpace",
        deadline=deadline,
    )

    # P1-003 — return shape is a dict carrying ``events`` + optional
    # ``partial_failure``. Mock path never sets partial_failure
    # (no network involved). The ``events`` list mirrors the
    # previous flat-list contract.
    assert result.get("partial_failure") is None
    series = result["events"]
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
    """Stage 4 + P1-005 + P1-001 + P1-002 + P1-003 + round-4 Medium#6 guard.

    All real adapters must avoid the real Google SDK (we keep our own
    httpx-based adapters so domain code never imports googleapiclient).

    ``allow_httpx=True`` — adapters that legitimately make direct
    httpx calls:

    - ``drive_mod``    — P1-005 (real folder creation)
    - ``docs_mod``     — P1-001 (real documents.create + batchUpdate)
    - ``sheets_mod``   — P1-002 (real values.append + values.batchUpdate)
    - ``calendar_mod`` — P1-003 (real events.insert + checkpoint series)
    - ``plane_mod``    — P1-004 (real Plane REST issue creation)

    ``allow_httpx=False`` — adapters that must NOT touch httpx directly:

    - ``web_mock_mod``     — pure mock, no network at all
    - ``web_crawl4ai_mod`` — Crawl4AI owns its own transport (Playwright);
      the adapter is a thin wrapper and must not bypass it with a
      direct httpx call (round-4 Medium#6 — previously the guard never
      checked this module at all).

    Non-httpx network libs (``requests``, ``urllib``, raw sockets) and
    the Google SDKs are still banned across the board.

    Lib bans use AST import inspection rather than a substring grep —
    P1-001 added Docs's ``batchUpdate`` body which carries an upstream
    JSON key literally named ``"requests"``, and substring matching
    against the source would false-positive on that. Credentials / .env
    paths are still substring-matched because no module would
    legitimately mention ``credentials.json`` in any other context.
    """
    import ast

    # Top-level module names (root segment) that are forbidden as imports
    # in mock-only adapters. ``urllib`` covers ``urllib.request``;
    # ``http`` covers ``http.client``.
    sdk_forbidden_imports = {
        "googleapiclient",
        "google_auth_oauthlib",
    }
    sdk_forbidden_from = {
        # ``from google.oauth2 import ...`` / ``from google.auth import ...``
        "google.oauth2",
        "google.auth",
    }
    network_forbidden_imports = {
        "requests",
        "http",  # banishes http.client
        "socket",  # banishes raw socket.socket
    }
    network_forbidden_from = {
        "urllib.request",
    }
    file_substr_forbidden = [
        "open('.env",
        'open(".env',
        "credentials.json",
        "client_secret.json",
    ]

    def _imported_names(module: ModuleType) -> tuple[set[str], set[str]]:
        """Return (top-level import names, ``from X import …`` X names)."""
        tree = ast.parse(inspect.getsource(module))
        plain: set[str] = set()
        froms: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    plain.add(alias.name.split(".")[0])
                    plain.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    froms.add(node.module)
        return plain, froms

    def _check(module: ModuleType, *, allow_httpx: bool) -> None:
        plain, froms = _imported_names(module)
        plain_banned = sdk_forbidden_imports | network_forbidden_imports
        from_banned = sdk_forbidden_from | network_forbidden_from
        if not allow_httpx:
            plain_banned = plain_banned | {"httpx"}
        offending_plain = plain & plain_banned
        offending_from = froms & from_banned
        assert not offending_plain, (
            f"{module.__name__} imports forbidden module(s) "
            f"{sorted(offending_plain)} — mock-first / real-mode rules "
            "(P1-005 / P1-001) ban real Google SDKs and non-httpx network libs."
        )
        assert not offending_from, (
            f"{module.__name__} has forbidden ``from {sorted(offending_from)[0]}`` import — "
            "real Google SDK or non-httpx network lib."
        )
        # File-path substrings still use the substring check — no
        # legitimate code path would mention ``credentials.json``.
        source = inspect.getsource(module)
        for needle in file_substr_forbidden:
            assert needle not in source, (
                f"{module.__name__} must not reference {needle!r}"
            )

    for module in (drive_mod, docs_mod, sheets_mod, calendar_mod, plane_mod):
        _check(module, allow_httpx=True)
    for module in (web_mock_mod, web_crawl4ai_mod):
        _check(module, allow_httpx=False)


def test_real_mode_property_does_not_reference_api_base_attribute() -> None:
    """Issue 1 — pin the dead-clause refactor structurally.

    The behaviour-only tests (``test_real_mode_on_with_access_token_and_default_base``
    in each adapter's test file) pass against BOTH the old AND new
    implementations: `bool(token) and bool(default_prod_base)` evaluates
    to True just as cleanly as `bool(token)` does. So if a future
    maintainer "adds defence" by reinstating ``and bool(s.google_*_api_base)``,
    no behaviour test catches it — the docstring-vs-code drift the
    issue-1 refactor closed quietly reopens.

    This AST check is the structural backstop: ``real_mode`` must not
    reference the ``*_api_base`` Settings attribute. Since the
    TokenProvider refresh port, ``real_mode`` reads ``_token_provider``
    instead of a Settings field directly — but the api_base ban still
    stands: the base URL is a staging / emulator configuration knob,
    never a gate.
    """
    import ast
    for module, banned in (
        (drive_mod, "google_drive_api_base"),
        (docs_mod, "google_docs_api_base"),
        (sheets_mod, "google_sheets_api_base"),
        (calendar_mod, "google_calendar_api_base"),
    ):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "real_mode":
                attrs = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
                assert banned not in attrs, (
                    f"{module.__name__}.real_mode references {banned!r} again — "
                    "round-3 issue 1 closed this dead clause; do not re-add it. "
                    "The URL validator already guarantees the base is non-empty "
                    "at Settings construction, so AND-ing it into real_mode is "
                    "dead code that misleads future readers."
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
