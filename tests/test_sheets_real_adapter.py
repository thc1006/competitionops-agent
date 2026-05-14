"""P1-002 — GoogleSheetsAdapter real-mode contract.

Mirrors the P1-005 (Drive) / P1-001 (Docs) / P1-004 (Plane) design:

- ``real_mode`` flips on iff ``google_oauth_access_token`` is set.
  ``google_sheets_api_base`` has a non-empty prod-URL default + URL
  validator, so it is a configuration knob, not a gate (issue 1 /
  AST guard already enforces this structurally).
- Real mode posts to two distinct Sheets v4 endpoints:
  - ``POST /v4/spreadsheets/{id}/values/{range}:append`` with
    ``valueInputOption=USER_ENTERED`` query param. Body is
    ``{"values": [[...]]}`` (2D array — one inner list per row).
    Default range when none supplied: the first sheet (``"Sheet1"``).
  - ``POST /v4/spreadsheets/{id}/values:batchUpdate`` for
    ``update_cells``. Body is
    ``{"valueInputOption": "USER_ENTERED", "data": [{"range": "A1",
    "values": [["v"]]}, …]}``.
- Deep-review C1 — ``dry_run=True`` short-circuits before any HTTP
  call. Synthetic ``dry_run_<sha1(sheet_id)[:8]>`` preview.
- M8 + round-3 M4 redaction: ``HTTPStatusError`` → ``safe_error_summary``;
  ``HTTPError | InvalidURL`` → ``safe_network_summary``. Row payloads
  and cell values carry user content — leaking ``str(exc)`` would
  re-introduce M8/M4.

Out of scope:
- Idempotency: Sheets has no native dedup endpoint. Re-running an
  append produces duplicate rows. Operators wire idempotency at the
  orchestrator layer (e.g., write the action_id into a hidden column
  and check before append).
- Column-order inference across rows with heterogeneous keys. v1
  serialises dict.values() per row in insertion order; rows must
  share keys for the resulting 2D matrix to align with sheet columns.
- OAuth refresh — bearer wiring is operator-side.
- 429 backoff / retry.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from competitionops.adapters.google_sheets import GoogleSheetsAdapter
from competitionops.config import Settings
from competitionops.schemas import ExternalAction, RiskLevel


def _real_settings(**overrides: Any) -> Settings:
    """Settings that flip Sheets into real mode."""
    base: dict[str, Any] = {
        "google_oauth_access_token": SecretStr("ya29.test-bearer"),
        "google_sheets_api_base": "https://sheets-test.example.invalid",
    }
    base.update(overrides)
    return Settings(**base)


def _mock_transport(handler: Any) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


def _make_append_action(
    *, sheet_id: str = "tracker_xyz", rows: list[dict[str, Any]] | None = None,
    sheet_range: str | None = None, action_id: str = "act_sheets_append",
) -> ExternalAction:
    payload: dict[str, Any] = {
        "sheet_id": sheet_id,
        "rows": rows
        if rows is not None
        else [{"name": "RunSpace", "deadline": "2026-09-30"}],
    }
    if sheet_range is not None:
        payload["range"] = sheet_range
    return ExternalAction(
        action_id=action_id,
        type="google.sheets.append_tracking_row",
        target_system="google_sheets",
        payload=payload,
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )


def _make_update_action(
    *, sheet_id: str = "tracker_xyz",
    cell_updates: dict[str, Any] | None = None,
) -> ExternalAction:
    return ExternalAction(
        action_id="act_sheets_update",
        type="google.sheets.update_cells",
        target_system="google_sheets",
        payload={
            "sheet_id": sheet_id,
            "cell_updates": cell_updates
            if cell_updates is not None
            else {"A1": "RunSpace", "B1": "2026-09-30"},
        },
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )


# ---------------------------------------------------------------------------
# real_mode toggle — bearer-only (issue 1 pattern)
# ---------------------------------------------------------------------------


def test_real_mode_off_by_default() -> None:
    adapter = GoogleSheetsAdapter(settings=Settings())
    assert adapter.real_mode is False


def test_real_mode_off_when_access_token_missing() -> None:
    """Base URL alone is not enough — bearer is the actual gate."""
    settings = Settings(google_sheets_api_base="https://sheets-test.example.invalid")
    adapter = GoogleSheetsAdapter(settings=settings)
    assert adapter.real_mode is False


def test_real_mode_on_with_access_token_alone() -> None:
    """Bearer alone flips real mode on — base URL defaults to the prod
    Sheets URL, validator ensures non-empty at construction. Mirrors
    the issue-1 contract enforced for Drive + Docs by the AST guard."""
    settings = Settings(
        google_oauth_access_token=SecretStr("ya29.token-default-base"),
    )
    adapter = GoogleSheetsAdapter(settings=settings)
    assert adapter.real_mode is True


# ---------------------------------------------------------------------------
# append_rows — POST to values/{range}:append + valueInputOption query param
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_append_rows_posts_to_values_append_endpoint() -> None:
    """Endpoint is ``POST /v4/spreadsheets/{id}/values/{range}:append``
    with ``valueInputOption=USER_ENTERED`` query param. Bearer in
    Authorization header. Default range is ``Sheet1`` when payload
    omits one — Google appends to first available row of the first
    sheet."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["query"] = dict(request.url.params)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200, json={"spreadsheetId": "sheet-real-1", "updates": {"updatedRows": 1}}
        )

    async with _mock_transport(handler) as client:
        adapter = GoogleSheetsAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(
            _make_append_action(sheet_id="sheet-real-1"), dry_run=False
        )

    assert captured["method"] == "POST"
    # Path shape: /v4/spreadsheets/{id}/values/{range}:append
    assert captured["path"].startswith("/v4/spreadsheets/sheet-real-1/values/")
    assert captured["path"].endswith(":append")
    # Default range when none supplied.
    assert "Sheet1" in captured["path"]
    assert captured["query"].get("valueInputOption") == "USER_ENTERED"
    assert captured["auth"] == "Bearer ya29.test-bearer"
    assert result.status == "executed"
    assert result.external_id == "sheet-real-1"


@pytest.mark.asyncio
async def test_real_append_rows_serialises_dict_rows_to_2d_values_array() -> None:
    """Google Sheets API expects ``{"values": [[...]]}`` — a 2D array
    where each inner list is a row. The adapter must convert from the
    domain shape (``list[dict[str, Any]]``) by emitting each row's
    ``dict.values()`` in insertion order. v1 assumes rows share keys
    in the same order (the planner produces this naturally)."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"spreadsheetId": "s-x"})

    async with _mock_transport(handler) as client:
        adapter = GoogleSheetsAdapter(settings=_real_settings(), client=client)
        await adapter.execute(
            _make_append_action(
                rows=[
                    {"name": "RunSpace", "deadline": "2026-09-30"},
                    {"name": "DemoCup", "deadline": "2026-10-15"},
                ],
            ),
            dry_run=False,
        )

    values = captured.get("values")
    assert values == [
        ["RunSpace", "2026-09-30"],
        ["DemoCup", "2026-10-15"],
    ], f"unexpected serialisation: {captured!r}"


@pytest.mark.asyncio
async def test_real_append_rows_respects_explicit_range() -> None:
    """When the payload supplies ``range``, the adapter uses it verbatim
    (e.g., ``Tracker!A:Z``). Don't override or sanitise — the operator
    knows their sheet structure."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json={"spreadsheetId": "s"})

    async with _mock_transport(handler) as client:
        adapter = GoogleSheetsAdapter(settings=_real_settings(), client=client)
        await adapter.execute(
            _make_append_action(sheet_range="Tracker!A:Z"), dry_run=False
        )

    # The range goes into the URL path between /values/ and :append
    # (URL-encoded — ! becomes %21).
    assert "Tracker" in captured["path"]
    assert "%21A:Z" in captured["path"] or "Tracker!A:Z" in captured["path"]
    assert captured["path"].endswith(":append")


@pytest.mark.asyncio
async def test_real_append_rows_honors_dry_run_and_makes_no_http_call() -> None:
    """C1 — dry_run never touches the network. Returns synthetic
    ``dry_run_<hash>`` preview so audit/UI still have something to
    render."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(500, text="must not reach")

    async with _mock_transport(handler) as client:
        adapter = GoogleSheetsAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(_make_append_action(), dry_run=True)

    assert seen == [], "Sheets adapter hit the network during dry_run"
    assert result.status == "dry_run"
    assert result.external_id is not None and result.external_id.startswith("dry_run_")


# ---------------------------------------------------------------------------
# update_cells — POST to values:batchUpdate with per-cell data entries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_update_cells_posts_to_values_batchUpdate_endpoint() -> None:
    """Endpoint is ``POST /v4/spreadsheets/{id}/values:batchUpdate``.
    No range in the path — each cell update carries its own range
    inside the body."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode("utf-8"))
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "spreadsheetId": "sheet-update-1",
                "totalUpdatedCells": 2,
            },
        )

    async with _mock_transport(handler) as client:
        adapter = GoogleSheetsAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(
            _make_update_action(sheet_id="sheet-update-1"), dry_run=False
        )

    assert captured["method"] == "POST"
    assert captured["path"] == "/v4/spreadsheets/sheet-update-1/values:batchUpdate"
    assert captured["auth"] == "Bearer ya29.test-bearer"
    assert result.status == "executed"
    assert result.external_id == "sheet-update-1"


@pytest.mark.asyncio
async def test_real_update_cells_body_carries_each_cell_as_range_values_pair() -> None:
    """The batchUpdate body shape is::

        {
          "valueInputOption": "USER_ENTERED",
          "data": [
            {"range": "A1", "values": [["RunSpace"]]},
            {"range": "B1", "values": [["2026-09-30"]]}
          ]
        }

    Each cell is a separate ``data`` entry with a 1x1 ``values`` array.
    Pin the shape so a future regression that flattens to a single
    range trips here, not in production."""
    captured_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"spreadsheetId": "x"})

    async with _mock_transport(handler) as client:
        adapter = GoogleSheetsAdapter(settings=_real_settings(), client=client)
        await adapter.execute(
            _make_update_action(
                cell_updates={"A1": "RunSpace", "B1": "2026-09-30"}
            ),
            dry_run=False,
        )

    assert captured_body.get("valueInputOption") == "USER_ENTERED"
    data = captured_body.get("data", [])
    # Build the (range -> single value) mapping back so the assertion
    # doesn't depend on dict iteration order.
    reconstructed = {
        entry["range"]: entry["values"][0][0]
        for entry in data
        if isinstance(entry, dict) and "range" in entry and "values" in entry
    }
    assert reconstructed == {"A1": "RunSpace", "B1": "2026-09-30"}


@pytest.mark.asyncio
async def test_real_update_cells_honors_dry_run() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(500, text="must not reach")

    async with _mock_transport(handler) as client:
        adapter = GoogleSheetsAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(_make_update_action(), dry_run=True)

    assert seen == []
    assert result.status == "dry_run"


# ---------------------------------------------------------------------------
# Error surfaces — auth, network, InvalidURL (M8 + round-3 M4 redaction)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_append_rows_surfaces_401_as_failed_action() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    async with _mock_transport(handler) as client:
        adapter = GoogleSheetsAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(_make_append_action(), dry_run=False)

    assert result.status == "failed"
    assert "401" in (result.error or "")


@pytest.mark.asyncio
async def test_real_append_rows_surfaces_network_error_as_failed_action() -> None:
    """M8 redaction — exception class name is the operator's diagnostic
    signal but the body is not surfaced. Sheets URLs embed the spreadsheet
    id and range, which can carry user content (sheet names with secrets,
    auto-generated tracker ids); the redaction prevents leak."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "synthetic SECRET-NET-MSG", request=request
        )

    async with _mock_transport(handler) as client:
        adapter = GoogleSheetsAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(
            _make_append_action(sheet_id="SECRET-SHEET-ID"), dry_run=False
        )

    assert result.status == "failed"
    err = result.error or ""
    assert "ConnectError" in err
    assert "SECRET-NET-MSG" not in err
    assert "SECRET-SHEET-ID" not in err


@pytest.mark.asyncio
async def test_real_update_cells_surfaces_invalid_url_as_failed_action() -> None:
    """Round-3 M4 — ``httpx.InvalidURL`` is its own exception class
    outside ``HTTPError``. Tuple-catch keeps the same leak surface
    closed across both classes."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.InvalidURL("bad url: SECRET-URL-LEAK")

    async with _mock_transport(handler) as client:
        adapter = GoogleSheetsAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(_make_update_action(), dry_run=False)

    assert result.status == "failed"
    err = result.error or ""
    assert "InvalidURL" in err
    assert "SECRET-URL-LEAK" not in err
    assert "bad url" not in err
