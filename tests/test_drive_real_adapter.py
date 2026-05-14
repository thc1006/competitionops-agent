"""P1-005 — GoogleDriveAdapter real-mode contract.

Mirrors the P1-004 Plane real-mode design:

- ``real_mode`` flips on only when both ``google_oauth_access_token`` and
  ``google_drive_api_base`` are present in Settings; partial config keeps
  the Stage 4 stateful mock so half-real behaviour cannot surprise a PM.
- Real mode does GET-by-search BEFORE POST (query-then-create idempotency
  from Tier 0 #5). If the Drive ``files.list`` call surfaces a folder
  whose ``name`` matches exactly under the same parent, the adapter
  returns that folder instead of POSTing a duplicate.
- Search failures (4xx, 5xx, network) fall through to POST rather than
  blocking the entire approval — same degraded-but-functional contract
  as Plane.
- The httpx ``AsyncClient`` is injectable so this whole file exercises
  the network layer through ``httpx.MockTransport``; no real socket is
  ever opened.

Out of scope (later PRs): real ``move_file`` and ``search_files``
endpoints, OAuth refresh, Drive shared-drive corpora, 429 retry.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from competitionops.adapters.google_drive import GoogleDriveAdapter
from competitionops.config import Settings
from competitionops.schemas import ExternalAction, RiskLevel


def _real_settings(**overrides: Any) -> Settings:
    """Build a Settings object that flips Drive into real mode."""
    base: dict[str, Any] = {
        "google_oauth_access_token": SecretStr("ya29.test-bearer"),
        "google_drive_api_base": "https://drive-test.example.invalid",
    }
    base.update(overrides)
    return Settings(**base)


def _mock_transport(handler: Any) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


# ---------------------------------------------------------------------------
# real_mode toggle
# ---------------------------------------------------------------------------


def test_real_mode_off_by_default() -> None:
    adapter = GoogleDriveAdapter(settings=Settings())
    assert adapter.real_mode is False


def test_real_mode_off_when_access_token_missing() -> None:
    # base URL alone is not enough — we still need a bearer.
    settings = Settings(google_drive_api_base="https://drive-test.example.invalid")
    adapter = GoogleDriveAdapter(settings=settings)
    assert adapter.real_mode is False


def test_real_mode_on_with_access_token_and_base() -> None:
    adapter = GoogleDriveAdapter(settings=_real_settings())
    assert adapter.real_mode is True


# ---------------------------------------------------------------------------
# Folder creation — POST + bearer + body + idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_create_folder_posts_to_drive_files_endpoint() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "1A2B3C-real-folder-id",
                "name": seen["body"]["name"],
                "mimeType": "application/vnd.google-apps.folder",
            },
        )

    client = _mock_transport(handler)
    adapter = GoogleDriveAdapter(settings=_real_settings(), client=client)

    folder = await adapter.create_folder(name="Competition RunSpace")

    assert folder["id"] == "1A2B3C-real-folder-id"
    assert folder["name"] == "Competition RunSpace"
    assert folder["url"].endswith("1A2B3C-real-folder-id")
    assert seen["method"] == "POST"
    # ``/drive/v3/files`` is Drive v3's canonical create endpoint.
    assert seen["url"].endswith("/drive/v3/files")
    assert seen["auth"] == "Bearer ya29.test-bearer"
    assert seen["body"]["mimeType"] == "application/vnd.google-apps.folder"
    assert "parents" not in seen["body"]  # no parent_id → omit, not [None]


@pytest.mark.asyncio
async def test_real_create_folder_passes_parent_id_into_parents_list() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "child-folder-id",
                "name": captured["body"]["name"],
                "mimeType": "application/vnd.google-apps.folder",
            },
        )

    client = _mock_transport(handler)
    adapter = GoogleDriveAdapter(settings=_real_settings(), client=client)

    await adapter.create_folder(name="Phase 1", parent_id="parent-folder-id")

    assert captured["body"]["parents"] == ["parent-folder-id"]


@pytest.mark.asyncio
async def test_real_create_folder_returns_existing_when_search_finds_match() -> None:
    """Tier 0 #5 — query-then-create idempotency. Re-approving the same plan
    must surface the original folder id, not create a duplicate."""
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "id": "existing-folder-id",
                            "name": "Competition RunSpace",
                            "mimeType": "application/vnd.google-apps.folder",
                        }
                    ]
                },
            )
        posted.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(500, text="should not have POSTed")

    client = _mock_transport(handler)
    adapter = GoogleDriveAdapter(settings=_real_settings(), client=client)

    folder = await adapter.create_folder(name="Competition RunSpace")

    assert folder["id"] == "existing-folder-id"
    assert posted == [], "search hit must short-circuit before POST"


@pytest.mark.asyncio
async def test_real_create_folder_is_idempotent_across_repeated_calls() -> None:
    """Concretely simulates re-approval. Search empty on first call, then
    populated on subsequent calls — caller must observe the same id."""
    state: dict[str, Any] = {"created": None}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if state["created"] is None:
                return httpx.Response(200, json={"files": []})
            return httpx.Response(200, json={"files": [state["created"]]})
        body = json.loads(request.content.decode("utf-8"))
        state["created"] = {
            "id": "newly-minted-id",
            "name": body["name"],
            "mimeType": "application/vnd.google-apps.folder",
        }
        return httpx.Response(200, json=state["created"])

    client = _mock_transport(handler)
    adapter = GoogleDriveAdapter(settings=_real_settings(), client=client)

    first = await adapter.create_folder(name="Repeat Folder")
    second = await adapter.create_folder(name="Repeat Folder")

    assert first["id"] == second["id"] == "newly-minted-id"


@pytest.mark.asyncio
async def test_real_create_folder_falls_through_on_search_5xx() -> None:
    """A failing search step must not block the POST. Self-hosted Drive
    shims with broken search still see folders created — they just lose
    the idempotency guarantee for that one call."""
    state: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(503, text="search temporarily unavailable")
        body = json.loads(request.content.decode("utf-8"))
        state["posted_name"] = body["name"]
        return httpx.Response(
            200,
            json={"id": "fallthrough-id", "name": body["name"]},
        )

    client = _mock_transport(handler)
    adapter = GoogleDriveAdapter(settings=_real_settings(), client=client)

    folder = await adapter.create_folder(name="Search Broken Folder")

    assert folder["id"] == "fallthrough-id"
    assert state["posted_name"] == "Search Broken Folder"


@pytest.mark.asyncio
async def test_real_create_folder_falls_through_on_search_network_error() -> None:
    state: dict[str, bool] = {"get_called": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            state["get_called"] = True
            raise httpx.ConnectError("dns failure")
        return httpx.Response(200, json={"id": "after-network-error", "name": "X"})

    client = _mock_transport(handler)
    adapter = GoogleDriveAdapter(settings=_real_settings(), client=client)

    folder = await adapter.create_folder(name="X")

    assert state["get_called"] is True
    assert folder["id"] == "after-network-error"


# ---------------------------------------------------------------------------
# Error surfaces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_create_folder_surfaces_401_as_failed_action() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        return httpx.Response(401, json={"error": "unauthorized"})

    client = _mock_transport(handler)
    adapter = GoogleDriveAdapter(settings=_real_settings(), client=client)

    action = ExternalAction(
        action_id="act_drive_unauth",
        type="google.drive.create_competition_folder",
        target_system="google_drive",
        payload={"folder_name": "Unauth Folder"},
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )
    result = await adapter.execute(action, dry_run=False)

    assert result.status == "failed"
    assert "401" in (result.error or "")


@pytest.mark.asyncio
async def test_real_create_folder_surfaces_network_error_as_failed_action() -> None:
    """Round-2 M8: like Plane, Drive's audit ``error`` must surface the
    httpx exception class (so operators can grep their logs by error
    type) but NOT the exception body — httpx's ``str(exc)`` typically
    includes the request URL, and Drive's search URL embeds
    ``q=name='<folder_name>'`` where the folder name is user content.
    A token-like substring in a folder name would leak via that
    branch."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        raise httpx.ConnectTimeout(
            "upstream timed out", request=request
        )

    client = _mock_transport(handler)
    adapter = GoogleDriveAdapter(settings=_real_settings(), client=client)

    action = ExternalAction(
        action_id="act_drive_timeout",
        type="google.drive.create_competition_folder",
        target_system="google_drive",
        payload={"folder_name": "SECRET-TOKEN-AS-FOLDER-NAME"},
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )
    result = await adapter.execute(action, dry_run=False)

    assert result.status == "failed"
    # Class name is the operator's diagnostic signal.
    assert "ConnectTimeout" in (result.error or "")
    # Body / URL is the leak surface — must NOT appear.
    err = result.error or ""
    assert "upstream timed out" not in err
    assert "SECRET-TOKEN" not in err


# ---------------------------------------------------------------------------
# Dry-run safety (CLAUDE.md rule #3, ties to deep-review finding C1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_execute_honors_dry_run_and_makes_no_http_call() -> None:
    """A dry_run=True approval must NEVER call out to Drive, even in
    real mode. The audit record must still surface as ``status="dry_run"``
    with a synthetic external_id so the PM can preview the action."""
    seen_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_methods.append(request.method)
        return httpx.Response(500, text="should not reach the network in dry_run")

    client = _mock_transport(handler)
    adapter = GoogleDriveAdapter(settings=_real_settings(), client=client)

    action = ExternalAction(
        action_id="act_dry",
        type="google.drive.create_competition_folder",
        target_system="google_drive",
        payload={"folder_name": "Dry Folder"},
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )
    result = await adapter.execute(action, dry_run=True)

    assert seen_methods == [], "real mode must not touch the network during dry_run"
    assert result.status == "dry_run"
    assert result.external_id is not None
    assert result.external_id.startswith("dry_run_")


# ---------------------------------------------------------------------------
# Executor protocol dispatch — real path must reach POST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_execute_dispatches_to_post_and_returns_external_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"id": "exec-id-9", "name": body["name"]})

    client = _mock_transport(handler)
    adapter = GoogleDriveAdapter(settings=_real_settings(), client=client)

    action = ExternalAction(
        action_id="act_real_exec",
        type="google.drive.create_competition_folder",
        target_system="google_drive",
        payload={"folder_name": "ExecTarget"},
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )
    result = await adapter.execute(action, dry_run=False)

    assert result.status == "executed"
    assert result.external_id == "exec-id-9"
    assert (result.message or "").endswith("(real).")


# ---------------------------------------------------------------------------
# M8 — Drive shares the same HTTP-error-body redaction shape as Plane.
# The deep review specifically called out plane.py, but google_drive.py
# had the identical ``response.text[:200]`` pattern and benefits from
# the same shared helper. Same three tests as the Plane side, against
# the Drive endpoint.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_drive_http_error_redacts_html_body() -> None:
    """An interposing proxy / captive portal returning HTML 502 must
    not leak its body content into the Drive audit error field."""
    html_with_leak = (
        "<html><h1>502</h1>"
        "<pre>upstream connect error to 10.0.42.7:443 "
        "(internal-drive-shim.corp.example)</pre></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        return httpx.Response(502, content=html_with_leak.encode("utf-8"))

    client = _mock_transport(handler)
    adapter = GoogleDriveAdapter(settings=_real_settings(), client=client)
    action = ExternalAction(
        action_id="act_drive_html_502",
        type="google.drive.create_competition_folder",
        target_system="google_drive",
        payload={"folder_name": "HTML 502"},
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )
    result = await adapter.execute(action, dry_run=False)

    assert result.status == "failed"
    assert "502" in (result.error or "")
    for leak in ("10.0.42.7", "internal-drive-shim", "<html>", "upstream"):
        assert leak not in (result.error or ""), (
            f"M8 leak: {leak!r} surfaced into Drive audit error field"
        )


@pytest.mark.asyncio
async def test_real_drive_http_error_extracts_google_json_error() -> None:
    """Real Drive 4xx returns ``{"error": {"message": "..."}}`` (Google
    APIs convention). The helper extracts ONLY string fields, so the
    nested object case falls back to the bare status line — but for a
    flat ``{"error": "msg"}`` (which some Drive shims emit) the string
    does surface."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        return httpx.Response(403, json={"error": "rate limit exceeded"})

    client = _mock_transport(handler)
    adapter = GoogleDriveAdapter(settings=_real_settings(), client=client)
    action = ExternalAction(
        action_id="act_drive_rate_limit",
        type="google.drive.create_competition_folder",
        target_system="google_drive",
        payload={"folder_name": "RateLimited"},
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )
    result = await adapter.execute(action, dry_run=False)

    assert result.status == "failed"
    assert "403" in (result.error or "")
    assert "rate limit exceeded" in (result.error or "")


@pytest.mark.asyncio
async def test_real_drive_http_error_audit_field_capped_at_200_chars() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        return httpx.Response(500, json={"error": "z" * 5000})

    client = _mock_transport(handler)
    adapter = GoogleDriveAdapter(settings=_real_settings(), client=client)
    action = ExternalAction(
        action_id="act_drive_big_error",
        type="google.drive.create_competition_folder",
        target_system="google_drive",
        payload={"folder_name": "Big Error"},
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )
    result = await adapter.execute(action, dry_run=False)

    assert result.status == "failed"
    assert len(result.error or "") <= 200
