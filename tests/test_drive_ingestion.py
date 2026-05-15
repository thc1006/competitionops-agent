"""P2-005 Sprint 5 — ``POST /briefs/extract/drive`` endpoint.

Pulls a PDF straight from a Google Drive file id, runs it through the
same ``BriefExtractor.extract_from_pdf`` pipeline the multipart
``/briefs/extract/pdf`` endpoint uses, and stamps
``source_uri = drive://<file_id>`` for audit provenance.

Reading a Drive file is NOT a high-risk action (CLAUDE.md rule #4
lists move / delete / permission-change — not read), so this path has
no dry_run / approval-gate machinery.

Tests inject a Drive adapter via ``app.dependency_overrides`` so the
suite stays offline.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from competitionops import main as main_module
from competitionops.adapters.google_drive import GoogleDriveAdapter
from competitionops.config import Settings

from conftest import reset_runtime_caches  # noqa: E402, I001


def _mock_transport(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_post_briefs_extract_drive_returns_brief() -> None:
    """End-to-end: client posts {file_id}, the Drive adapter (mock)
    returns registered PDF bytes, BriefExtractor builds a
    CompetitionBrief, response carries source_uri = drive://<id>."""
    from competitionops.main import app, get_drive_adapter

    adapter = GoogleDriveAdapter(settings=Settings())
    adapter.register_file_content(
        "comp-brief-1",
        b"%PDF-Competition: DriveCup 2026. Submission deadline "
        b"2026-06-15T23:59:00+08:00.",
    )
    app.dependency_overrides[get_drive_adapter] = lambda: adapter
    try:
        reset_runtime_caches()
        client = TestClient(app)
        response = client.post(
            "/briefs/extract/drive", json={"file_id": "comp-brief-1"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["source_uri"] == "drive://comp-brief-1"
        assert "name" in body
    finally:
        app.dependency_overrides.pop(get_drive_adapter, None)


def test_post_briefs_extract_drive_rejects_non_pdf() -> None:
    """A Drive file whose bytes don't start with ``%PDF-`` is rejected
    422 — same magic-byte gate as the multipart endpoint, so garbage
    never reaches the extractor."""
    from competitionops.main import app, get_drive_adapter

    adapter = GoogleDriveAdapter(settings=Settings())
    adapter.register_file_content("not-a-pdf", b"<html>this is not a pdf</html>")
    app.dependency_overrides[get_drive_adapter] = lambda: adapter
    try:
        reset_runtime_caches()
        client = TestClient(app)
        response = client.post(
            "/briefs/extract/drive", json={"file_id": "not-a-pdf"}
        )
        assert response.status_code == 422, response.text
    finally:
        app.dependency_overrides.pop(get_drive_adapter, None)


def test_post_briefs_extract_drive_rejects_oversize_file() -> None:
    """A Drive file over the 10 MiB cap is rejected 413 — defends the
    pod against an accidentally-huge file. (Operator-chosen file id,
    so this is a guard-rail, not an attack surface like the multipart
    upload.)"""
    from competitionops.main import app, get_drive_adapter

    adapter = GoogleDriveAdapter(settings=Settings())
    oversize = b"%PDF-" + b"x" * (10 * 1024 * 1024 + 1)
    adapter.register_file_content("huge", oversize)
    app.dependency_overrides[get_drive_adapter] = lambda: adapter
    try:
        reset_runtime_caches()
        client = TestClient(app)
        response = client.post("/briefs/extract/drive", json={"file_id": "huge"})
        assert response.status_code == 413, response.text
    finally:
        app.dependency_overrides.pop(get_drive_adapter, None)


def test_post_briefs_extract_drive_rejects_empty_file_id() -> None:
    """422 on missing / empty / whitespace-only ``file_id``."""
    reset_runtime_caches()
    client = TestClient(main_module.app)
    for body in ({}, {"file_id": ""}, {"file_id": "   "}):
        response = client.post("/briefs/extract/drive", json=body)
        assert response.status_code == 422, f"{body}: {response.text}"


def test_post_briefs_extract_drive_maps_drive_404() -> None:
    """When Drive returns 404 for the file id, the endpoint maps it to
    a 404 (not a 500) so the PM sees ``file not found`` rather than an
    opaque server error."""
    from competitionops.main import app, get_drive_adapter

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "File not found"})

    def _real_adapter() -> GoogleDriveAdapter:
        settings = Settings(
            google_oauth_access_token=SecretStr("ya29.test"),
            google_drive_api_base="https://drive-test.example.invalid",
        )
        return GoogleDriveAdapter(settings=settings, client=_mock_transport(handler))

    app.dependency_overrides[get_drive_adapter] = _real_adapter
    try:
        reset_runtime_caches()
        client = TestClient(app)
        response = client.post(
            "/briefs/extract/drive", json={"file_id": "ghost-file"}
        )
        assert response.status_code == 404, response.text
    finally:
        app.dependency_overrides.pop(get_drive_adapter, None)


def test_post_briefs_extract_drive_redacts_network_error() -> None:
    """A network-class failure during the Drive download surfaces as
    502 with the exception CLASS NAME only — never ``str(exc)``, which
    embeds the request URL (Drive URLs carry the file id, and the file
    id could be PM-pasted content). Same M8/M4 redaction discipline as
    the adapters."""
    from competitionops.main import app, get_drive_adapter

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic SECRET-NET-DETAIL", request=request)

    def _real_adapter() -> GoogleDriveAdapter:
        settings = Settings(
            google_oauth_access_token=SecretStr("ya29.test"),
            google_drive_api_base="https://drive-test.example.invalid",
        )
        return GoogleDriveAdapter(settings=settings, client=_mock_transport(handler))

    app.dependency_overrides[get_drive_adapter] = _real_adapter
    try:
        reset_runtime_caches()
        client = TestClient(app)
        response = client.post(
            "/briefs/extract/drive", json={"file_id": "SECRET-FILE-ID"}
        )
        assert response.status_code == 502, response.text
        detail = response.json().get("detail", "")
        assert "ConnectError" in detail
        assert "SECRET-NET-DETAIL" not in detail
        assert "SECRET-FILE-ID" not in detail
    finally:
        app.dependency_overrides.pop(get_drive_adapter, None)


def test_post_briefs_extract_drive_rejects_malformed_file_id() -> None:
    """Deep-review Medium — ``file_id`` is interpolated raw into the
    Drive API URL path. Without charset validation, a crafted id
    injects query params (e.g. ``?acknowledgeAbuse=true`` forces
    download of an abuse-flagged file) or path-traverses within
    googleapis.com (``../../oauth2/v3/userinfo``). The Pydantic
    validator must reject anything outside the Drive id charset
    ``[A-Za-z0-9_-]`` with a 422, before the adapter builds the URL."""
    reset_runtime_caches()
    client = TestClient(main_module.app)
    malformed = [
        "realid?acknowledgeAbuse=true",  # query-param injection
        "realid&supportsAllDrives=true",  # param injection
        "../../oauth2/v3/userinfo",       # path traversal
        "realid#fragment",               # fragment injection
        "folder/realid",                 # slash — extra path segment
        "realid with spaces",            # space
    ]
    for bad_id in malformed:
        response = client.post(
            "/briefs/extract/drive", json={"file_id": bad_id}
        )
        assert response.status_code == 422, f"{bad_id!r}: {response.text}"


def test_post_briefs_extract_drive_accepts_well_formed_file_id() -> None:
    """The charset validator must NOT reject legitimate Drive ids —
    they are ``[A-Za-z0-9_-]`` (base64url-ish, ~33 chars). Pin a
    realistic id so the regex isn't accidentally too strict."""
    from competitionops.main import app, get_drive_adapter

    real_id = "1AbCd_Ef-GhIjKlMnOpQrStUvWxYz0123"
    adapter = GoogleDriveAdapter(settings=Settings())
    adapter.register_file_content(real_id, b"%PDF-RunSpace 2026 brief.")
    app.dependency_overrides[get_drive_adapter] = lambda: adapter
    try:
        reset_runtime_caches()
        client = TestClient(app)
        response = client.post(
            "/briefs/extract/drive", json={"file_id": real_id}
        )
        assert response.status_code == 200, response.text
        assert response.json()["source_uri"] == f"drive://{real_id}"
    finally:
        app.dependency_overrides.pop(get_drive_adapter, None)


def test_post_briefs_extract_drive_maps_drive_403() -> None:
    """A non-404 Drive error status (e.g. 403 — the OAuth token lacks
    the Drive scope) maps to 502, not a 500 or a leaked body."""
    from competitionops.main import app, get_drive_adapter

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "insufficient scope"})

    def _real_adapter() -> GoogleDriveAdapter:
        settings = Settings(
            google_oauth_access_token=SecretStr("ya29.test"),
            google_drive_api_base="https://drive-test.example.invalid",
        )
        return GoogleDriveAdapter(settings=settings, client=_mock_transport(handler))

    app.dependency_overrides[get_drive_adapter] = _real_adapter
    try:
        reset_runtime_caches()
        client = TestClient(app)
        response = client.post(
            "/briefs/extract/drive", json={"file_id": "scopeless"}
        )
        assert response.status_code == 502, response.text
        detail = response.json().get("detail", "")
        assert "403" in detail
        # The Drive error body must not leak through.
        assert "insufficient scope" not in detail
    finally:
        app.dependency_overrides.pop(get_drive_adapter, None)
