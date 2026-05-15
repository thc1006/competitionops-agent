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
    # Base URL alone is not enough — we still need a bearer. Honest gate
    # (issue 1): real mode is bearer-only; base URL is configuration,
    # not a gate. This test asserts the bearer-required half.
    settings = Settings(google_drive_api_base="https://drive-test.example.invalid")
    adapter = GoogleDriveAdapter(settings=settings)
    assert adapter.real_mode is False


def test_real_mode_on_with_access_token_and_base() -> None:
    adapter = GoogleDriveAdapter(settings=_real_settings())
    assert adapter.real_mode is True


def test_real_mode_on_with_access_token_and_default_base() -> None:
    """Issue 1 — the honest gate. ``google_drive_api_base`` has a prod-URL
    default that is non-empty (the URL validator rejects ``""``), so a
    bearer alone is enough to flip real mode on. Previously the
    ``real_mode`` property AND-ed token with base URL, implying both
    were required — but base was always truthy, so the second clause
    was dead code. Pin the actual contract: bearer-only."""
    settings = Settings(
        google_oauth_access_token=SecretStr("ya29.token-without-base-override"),
        # Note: NO google_drive_api_base override — relies on the
        # ``https://www.googleapis.com`` default.
    )
    adapter = GoogleDriveAdapter(settings=settings)
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


@pytest.mark.asyncio
async def test_real_create_folder_surfaces_invalid_url_as_failed_action() -> None:
    """Round-3 M4: ``httpx.InvalidURL`` is its OWN exception class —
    not a subclass of ``httpx.HTTPError`` — so the existing
    ``except httpx.HTTPError`` clause didn't catch it. Drive's search
    URL embeds ``q=name='<folder_name>'``; a folder name containing
    bytes that fail URL parsing (e.g. raw newlines from a
    copy-pasted secret) raised ``httpx.InvalidURL`` whose ``str(exc)``
    typically echoes the URL fragment. M4 closes the gap: catch it
    alongside HTTPError, return the class name only."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        raise httpx.InvalidURL("bad url: SECRET-LEAKED-IN-MESSAGE")

    client = _mock_transport(handler)
    adapter = GoogleDriveAdapter(settings=_real_settings(), client=client)

    action = ExternalAction(
        action_id="act_drive_invalid_url",
        type="google.drive.create_competition_folder",
        target_system="google_drive",
        payload={"folder_name": "Bad Folder Name"},
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )
    result = await adapter.execute(action, dry_run=False)

    assert result.status == "failed"
    assert "InvalidURL" in (result.error or "")
    err = result.error or ""
    assert "SECRET-LEAKED-IN-MESSAGE" not in err
    assert "bad url" not in err


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


def test_dry_run_preview_falls_back_to_action_id_when_payload_empty() -> None:
    """Round-4 PR A (High#1) — when the action payload carries no
    ``folder_name`` / ``competition_name`` / ``name``, the synthetic
    preview id previously hashed the literal ``"Untitled"`` — so two
    distinct empty-payload Drive actions collided on the same
    ``dry_run_<sha1("Untitled|root")>`` id. Falling back to
    ``action.action_id`` restores the deterministic-per-action
    property (issue-5 pattern, already in Docs / Sheets / Calendar).

    Drive's real ``create_folder`` still defaults a nameless folder to
    ``"Untitled"`` — that's a valid Drive behaviour and unchanged.
    Only the preview HASH KEY moves to ``action_id``."""
    adapter = GoogleDriveAdapter(settings=_real_settings())
    action_a = ExternalAction(
        action_id="act_alpha",
        type="google.drive.create_competition_folder",
        target_system="google_drive",
        payload={},  # no folder name of any kind
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )
    action_b = ExternalAction(
        action_id="act_beta",
        type="google.drive.create_competition_folder",
        target_system="google_drive",
        payload={},  # same empty payload, different action
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )
    preview_a = adapter._dry_run_preview(action_a)
    preview_b = adapter._dry_run_preview(action_b)

    assert preview_a.external_id is not None
    assert preview_a.external_id.startswith("dry_run_")
    assert preview_b.external_id is not None
    assert preview_b.external_id.startswith("dry_run_")
    assert preview_a.external_id != preview_b.external_id, (
        "Two empty-payload Drive preview actions with different "
        "action_ids must produce different synthetic ids — otherwise "
        "the approval UI de-dupes distinct actions into one."
    )


# ---------------------------------------------------------------------------
# P2-005 Sprint 5 — download_file (read-only PDF ingestion path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_file_mock_returns_registered_content() -> None:
    """Mock mode — ``register_file_content`` injects fixture bytes;
    ``download_file`` returns them verbatim. Read-only path, no
    ExternalAction / dry_run machinery."""
    adapter = GoogleDriveAdapter(settings=Settings())  # no token → mock
    adapter.register_file_content("file-abc", b"%PDF-fixture-bytes")

    content = await adapter.download_file(file_id="file-abc")
    assert content == b"%PDF-fixture-bytes"


@pytest.mark.asyncio
async def test_download_file_mock_synthetic_for_unregistered() -> None:
    """An unregistered file id yields deterministic synthetic PDF bytes
    (starts with the ``%PDF-`` magic so the endpoint's magic check
    passes). Mirrors MockWebAdapter's two-mode design."""
    adapter = GoogleDriveAdapter(settings=Settings())
    content = await adapter.download_file(file_id="unknown-xyz")
    assert content.startswith(b"%PDF-")
    # Deterministic per id.
    assert content == await adapter.download_file(file_id="unknown-xyz")


@pytest.mark.asyncio
async def test_download_file_real_gets_files_media_endpoint() -> None:
    """Real mode — ``GET /drive/v3/files/{id}`` with ``alt=media`` query
    param + Bearer auth. Returns the raw response bytes."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["query"] = dict(request.url.params)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, content=b"%PDF-real-drive-bytes")

    async with _mock_transport(handler) as client:
        adapter = GoogleDriveAdapter(settings=_real_settings(), client=client)
        content = await adapter.download_file(file_id="real-file-1")

    assert captured["method"] == "GET"
    assert captured["path"] == "/drive/v3/files/real-file-1"
    assert captured["query"].get("alt") == "media"
    assert captured["auth"] == "Bearer ya29.test-bearer"
    assert content == b"%PDF-real-drive-bytes"


@pytest.mark.asyncio
async def test_download_file_real_raises_httpstatuserror_on_404() -> None:
    """A 404 from Drive surfaces as ``httpx.HTTPStatusError`` — the
    download path does ``raise_for_status()`` and lets the caller
    (the endpoint) map it to a clean HTTP response."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    async with _mock_transport(handler) as client:
        adapter = GoogleDriveAdapter(settings=_real_settings(), client=client)
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.download_file(file_id="missing")
