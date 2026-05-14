"""P1-004 — Plane adapter mock + real-mode contract.

The adapter operates in two modes:
- **Mock mode** (default): stateful in-process, deterministic ids.
- **Real mode**: httpx-backed REST when all four Plane Settings fields
  are configured. Tests inject an ``httpx.MockTransport`` so no real
  network call ever leaves the process — pytest stays offline.

Tier 0 #3 progress: this commit lands the real adapter so the audit log
finally surfaces ``target_external_id`` for plane actions. The Stage 8
e2e exception is removed in the same commit (see test_e2e_dry_run.py
diff for "GOOGLE_TARGETS" removal).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from competitionops.adapters.plane import PlaneAdapter
from competitionops.config import Settings
from competitionops.schemas import ExternalAction, RiskLevel

_REAL_BASE_URL = "https://plane.example.invalid"
_REAL_API_KEY = "test-api-key-xyz-secret"
_REAL_WORKSPACE = "acme-workspace"
_REAL_PROJECT = "00000000-0000-0000-0000-000000000abc"


def _settings_real() -> Settings:
    return Settings(
        plane_base_url=_REAL_BASE_URL,
        plane_api_key=_REAL_API_KEY,
        plane_workspace_slug=_REAL_WORKSPACE,
        plane_project_id=_REAL_PROJECT,
    )


def _settings_mock() -> Settings:
    return Settings()  # all plane_* fields default to None → mock mode


def _make_action(title: str = "Pitch deck draft", **overrides: Any) -> ExternalAction:
    payload: dict[str, Any] = {"title": title}
    payload.update(overrides)
    return ExternalAction(
        action_id="act_plane_test",
        type="plane.create_issue",
        target_system="plane",
        payload=payload,
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )


# ---------------------------------------------------------------------------
# Mock-mode behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_create_issue_returns_deterministic_id() -> None:
    adapter = PlaneAdapter(settings=_settings_mock())
    first = await adapter.create_issue(title="Deterministic title")
    assert first["id"].startswith("mock_issue_")
    assert first["url"].startswith("https://plane.example.invalid/")


@pytest.mark.asyncio
async def test_mock_create_issue_idempotent_for_same_title() -> None:
    adapter = PlaneAdapter(settings=_settings_mock())
    a = await adapter.create_issue(title="Same")
    b = await adapter.create_issue(title="Same")
    assert a["id"] == b["id"]


@pytest.mark.asyncio
async def test_mock_create_issue_different_titles_produce_different_ids() -> None:
    adapter = PlaneAdapter(settings=_settings_mock())
    a = await adapter.create_issue(title="Title A")
    b = await adapter.create_issue(title="Title B")
    assert a["id"] != b["id"]


@pytest.mark.asyncio
async def test_mock_execute_dispatches_plane_create_issue() -> None:
    adapter = PlaneAdapter(settings=_settings_mock())
    result = await adapter.execute(_make_action("Demo video"), dry_run=True)

    assert result.status == "dry_run"
    assert result.target_system == "plane"
    assert result.external_id is not None
    assert result.external_id.startswith("mock_issue_")
    assert result.external_url is not None
    assert "plane.example.invalid" in result.external_url
    assert "mock" in (result.message or "").lower()


@pytest.mark.asyncio
async def test_mock_execute_handles_unknown_action_type() -> None:
    adapter = PlaneAdapter(settings=_settings_mock())
    action = ExternalAction(
        action_id="act_unknown",
        type="plane.does_not_exist",
        target_system="plane",
        payload={},
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )
    result = await adapter.execute(action, dry_run=True)
    assert result.status == "failed"
    assert "unknown action type" in (result.error or "")


@pytest.mark.asyncio
async def test_mock_execute_handles_missing_payload_field() -> None:
    adapter = PlaneAdapter(settings=_settings_mock())
    action = ExternalAction(
        action_id="act_missing",
        type="plane.create_issue",
        target_system="plane",
        payload={},  # no 'title'
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )
    result = await adapter.execute(action, dry_run=True)
    assert result.status == "failed"
    assert "title" in (result.error or "")


# ---------------------------------------------------------------------------
# Real-mode switching
# ---------------------------------------------------------------------------


def test_real_mode_requires_all_four_settings_fields() -> None:
    # Full config → real
    assert PlaneAdapter(settings=_settings_real()).real_mode is True
    # Missing key → mock
    s = Settings(
        plane_base_url=_REAL_BASE_URL,
        plane_workspace_slug=_REAL_WORKSPACE,
        plane_project_id=_REAL_PROJECT,
    )
    assert PlaneAdapter(settings=s).real_mode is False
    # Missing url → mock
    s = Settings(
        plane_api_key=_REAL_API_KEY,
        plane_workspace_slug=_REAL_WORKSPACE,
        plane_project_id=_REAL_PROJECT,
    )
    assert PlaneAdapter(settings=s).real_mode is False
    # No config at all → mock
    assert PlaneAdapter(settings=_settings_mock()).real_mode is False


# ---------------------------------------------------------------------------
# Real-mode REST calls (no network — httpx.MockTransport)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_create_issue_posts_to_plane_workspace_endpoint() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            201,
            json={
                "id": "real-issue-uuid-123",
                "name": "Pitch deck",
                "description_html": "",
                "project": _REAL_PROJECT,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        issue = await adapter.create_issue(title="Pitch deck")

    assert issue["id"] == "real-issue-uuid-123"
    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert request.method == "POST"
    expected_url_suffix = (
        f"/api/v1/workspaces/{_REAL_WORKSPACE}"
        f"/projects/{_REAL_PROJECT}/issues/"
    )
    assert str(request.url).endswith(expected_url_suffix)


@pytest.mark.asyncio
async def test_real_create_issue_sends_x_api_key_header() -> None:
    captured_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.append(request.headers)
        return httpx.Response(201, json={"id": "ok", "name": "x"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        await adapter.create_issue(title="x")

    assert captured_headers[0].get("X-API-Key") == _REAL_API_KEY
    assert "application/json" in (captured_headers[0].get("Accept") or "")


@pytest.mark.asyncio
async def test_real_create_issue_body_contains_title_and_description() -> None:
    captured_bodies: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured_bodies.append(_json.loads(request.content.decode()))
        return httpx.Response(201, json={"id": "real-1", "name": "x"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        await adapter.create_issue(
            title="The Pitch", description="10 pages", owner_role="business"
        )

    body = captured_bodies[0]
    assert body["name"] == "The Pitch"
    assert "10 pages" in body["description_html"]
    assert "business" in body["description_html"]  # owner_role merged in


@pytest.mark.asyncio
async def test_real_execute_surfaces_external_id_and_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "id": "real-uuid-from-api",
                "name": "Demo task",
                "project": _REAL_PROJECT,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        result = await adapter.execute(_make_action("Demo task"), dry_run=False)

    assert result.status == "executed"
    assert result.external_id == "real-uuid-from-api"
    assert result.external_url is not None
    assert _REAL_WORKSPACE in result.external_url
    assert _REAL_PROJECT in result.external_url
    assert "real" in (result.message or "").lower()


@pytest.mark.asyncio
async def test_real_execute_maps_http_status_error_to_failed_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        result = await adapter.execute(_make_action(), dry_run=False)

    assert result.status == "failed"
    assert "401" in (result.error or "")


@pytest.mark.asyncio
async def test_real_execute_maps_network_error_to_failed_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic network failure")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        result = await adapter.execute(_make_action(), dry_run=False)

    assert result.status == "failed"
    assert "network" in (result.message or "").lower()
    assert "synthetic" in (result.error or "")


@pytest.mark.asyncio
async def test_adapter_source_does_not_reference_real_plane_hosts() -> None:
    """Defense in depth: the adapter source code must never hardcode a
    real Plane production URL. Configuration must come from Settings."""
    import inspect

    from competitionops.adapters import plane as plane_mod

    source = inspect.getsource(plane_mod)
    forbidden = ("app.plane.so", "https://api.plane.so")
    for needle in forbidden:
        assert needle not in source, (
            f"Plane adapter source must not hardcode {needle!r}; configure via Settings"
        )
