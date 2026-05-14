"""P1-001 — GoogleDocsAdapter real-mode contract.

Mirrors the P1-005 (Drive) and P1-004 (Plane) real-mode design:

- ``real_mode`` flips on only when ``Settings.google_oauth_access_token``
  is set AND ``Settings.google_docs_api_base`` is non-default (or
  explicitly set to the prod default). The Sprint-0 mock survives
  unchanged so partial config falls back to deterministic mock and
  the PM never sees half-real behaviour.
- Real mode posts to ``POST {base}/v1/documents`` (Google Docs
  ``documents.create``). When ``sections`` is provided, the adapter
  follows with ``POST {base}/v1/documents/{documentId}:batchUpdate``
  to insert each section heading.
- Deep-review C1 — ``dry_run=True`` short-circuits BEFORE any HTTP
  call and returns a synthetic ``dry_run_<sha1(title)[:8]>`` preview
  (same contract as Plane / Drive). CLAUDE.md rule #3.
- Network errors AND ``httpx.InvalidURL`` are redacted through
  ``safe_network_summary`` / ``safe_error_summary`` so a copy-pasted
  secret in a document title can't leak via the exception body.

Out of scope (later PRs):
- Cross-API idempotency via Drive ``files.list`` search. Docs API has
  no native name lookup; doing a Drive search here would couple the
  Docs adapter to Drive auth scope and parent folder semantics that
  the current ``ExternalAction`` payload doesn't carry. Documented in
  the adapter source.
- OAuth refresh — operators wire a short-lived bearer via
  ``Settings.google_oauth_access_token`` for now.
- Rate-limit / retry on 429.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from competitionops.adapters.google_docs import GoogleDocsAdapter
from competitionops.config import Settings
from competitionops.schemas import ExternalAction, RiskLevel


def _real_settings(**overrides: Any) -> Settings:
    """Settings that flip Docs into real mode."""
    base: dict[str, Any] = {
        "google_oauth_access_token": SecretStr("ya29.test-bearer"),
        "google_docs_api_base": "https://docs-test.example.invalid",
    }
    base.update(overrides)
    return Settings(**base)


def _mock_transport(handler: Any) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


def _make_create_action(
    *, action_id: str = "act_docs_create", title: str = "RunSpace Proposal",
    sections: list[str] | None = None,
) -> ExternalAction:
    payload: dict[str, Any] = {"title": title}
    if sections is not None:
        payload["sections"] = sections
    return ExternalAction(
        action_id=action_id,
        type="google.docs.create_proposal_outline",
        target_system="google_docs",
        payload=payload,
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )


def _make_append_action(
    *, doc_id: str, heading: str = "Risks", body: str = "Anonymous-rule risk.",
) -> ExternalAction:
    return ExternalAction(
        action_id="act_docs_append",
        type="google.docs.append_section",
        target_system="google_docs",
        payload={"doc_id": doc_id, "heading": heading, "body": body},
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )


# ---------------------------------------------------------------------------
# real_mode toggle — partial config must stay on mock
# ---------------------------------------------------------------------------


def test_real_mode_off_by_default() -> None:
    adapter = GoogleDocsAdapter(settings=Settings())
    assert adapter.real_mode is False


def test_real_mode_off_when_access_token_missing() -> None:
    """``google_docs_api_base`` defaults to the prod Docs URL — non-None
    on its own doesn't constitute real-mode opt-in. Without a bearer
    we still mock."""
    settings = Settings(google_docs_api_base="https://docs-test.example.invalid")
    adapter = GoogleDocsAdapter(settings=settings)
    assert adapter.real_mode is False


def test_real_mode_on_with_access_token_and_base() -> None:
    adapter = GoogleDocsAdapter(settings=_real_settings())
    assert adapter.real_mode is True


# ---------------------------------------------------------------------------
# create_doc — POST + bearer + body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_create_doc_posts_to_documents_endpoint() -> None:
    """The endpoint is ``POST /v1/documents`` with ``Authorization: Bearer <token>``
    and a JSON body whose ``title`` field carries the action payload's
    title. Anything else is wrong and would either 400 or silently
    create a doc with the literal word ``Untitled``."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"documentId": "real-doc-123", "title": "RunSpace Proposal"})

    async with _mock_transport(handler) as client:
        adapter = GoogleDocsAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(
            _make_create_action(title="RunSpace Proposal"), dry_run=False
        )

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/v1/documents")
    assert captured["auth"] == "Bearer ya29.test-bearer"
    assert captured["body"] == {"title": "RunSpace Proposal"}
    assert result.status == "executed"
    assert result.external_id == "real-doc-123"


@pytest.mark.asyncio
async def test_real_create_doc_returns_googleapis_documentId() -> None:
    """``ExternalActionResult.external_id`` must be the ``documentId``
    Google returns (not the request title hashed locally). Downstream
    code uses it to wire later batchUpdate calls."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"documentId": "1a2b3c4d", "title": "X"})

    async with _mock_transport(handler) as client:
        adapter = GoogleDocsAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(_make_create_action(), dry_run=False)

    assert result.external_id == "1a2b3c4d"
    assert result.external_url is not None
    # The user-facing URL must point at the real Google Docs UI so PM
    # can click through from the audit log.
    assert "1a2b3c4d" in result.external_url
    assert "docs.google.com" in result.external_url


@pytest.mark.asyncio
async def test_real_create_doc_with_sections_triggers_batchUpdate() -> None:
    """When the action payload includes ``sections``, the adapter must
    follow ``documents.create`` with a ``documents.batchUpdate`` call
    that inserts each section heading. Otherwise the created doc is an
    empty title page and the PM has to add the outline manually."""
    requests_seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append((request.method, str(request.url)))
        if request.url.path == "/v1/documents":
            return httpx.Response(200, json={"documentId": "doc-7", "title": "RP"})
        # batchUpdate endpoint shape:
        if request.url.path == "/v1/documents/doc-7:batchUpdate":
            return httpx.Response(200, json={"documentId": "doc-7", "replies": []})
        return httpx.Response(404, text="unexpected endpoint")

    async with _mock_transport(handler) as client:
        adapter = GoogleDocsAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(
            _make_create_action(
                title="RP", sections=["Problem", "Solution", "Risks"]
            ),
            dry_run=False,
        )

    # Two calls: create + batchUpdate
    assert len(requests_seen) == 2
    assert requests_seen[0][1].endswith("/v1/documents")
    assert requests_seen[1][1].endswith("/v1/documents/doc-7:batchUpdate")
    assert result.status == "executed"
    assert result.external_id == "doc-7"


@pytest.mark.asyncio
async def test_real_create_doc_batchUpdate_carries_section_headings() -> None:
    """The batchUpdate body must contain an ``insertText`` request for
    each section heading. Pin the request shape so a future regression
    that drops the headings (e.g., wrong field name) trips here, not
    in production."""
    batchupdate_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/documents":
            return httpx.Response(200, json={"documentId": "d-1", "title": "X"})
        # batchUpdate
        batchupdate_body.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"documentId": "d-1", "replies": []})

    async with _mock_transport(handler) as client:
        adapter = GoogleDocsAdapter(settings=_real_settings(), client=client)
        await adapter.execute(
            _make_create_action(sections=["Problem", "Solution"]),
            dry_run=False,
        )

    requests = batchupdate_body.get("requests", [])
    # Every section heading should appear in some insertText request.
    inserted_texts = [
        req["insertText"]["text"]
        for req in requests
        if isinstance(req, dict) and "insertText" in req
    ]
    blob = " ".join(inserted_texts)
    assert "Problem" in blob
    assert "Solution" in blob


# ---------------------------------------------------------------------------
# Dry-run safety (CLAUDE.md rule #3 / deep-review C1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_create_doc_honors_dry_run_and_makes_no_http_call() -> None:
    """A dry_run=True approval must NEVER call out to Docs, even in
    real mode. ``Settings.dry_run_default=True`` is the hot path —
    silent writes here would violate CLAUDE.md rule #3."""
    seen_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_methods.append(request.method)
        return httpx.Response(500, text="should not reach the network in dry_run")

    async with _mock_transport(handler) as client:
        adapter = GoogleDocsAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(_make_create_action(), dry_run=True)

    assert seen_methods == [], "Docs adapter hit the network during dry_run"
    assert result.status == "dry_run"
    assert result.external_id is not None
    # Synthetic preview shape — mirrors Plane / Drive.
    assert result.external_id.startswith("dry_run_")


@pytest.mark.asyncio
async def test_real_append_section_honors_dry_run() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(500, text="must not reach")

    async with _mock_transport(handler) as client:
        adapter = GoogleDocsAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(
            _make_append_action(doc_id="real-doc-9"), dry_run=True
        )

    assert seen == []
    assert result.status == "dry_run"


# ---------------------------------------------------------------------------
# append_section — batchUpdate endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_append_section_posts_to_batchUpdate_endpoint() -> None:
    """``append_section`` maps to ``POST /v1/documents/{id}:batchUpdate``
    with an ``insertText`` at ``endOfSegmentLocation``. The heading and
    body must both appear in the request body so the resulting doc
    actually shows them."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"documentId": "doc-42", "replies": []})

    async with _mock_transport(handler) as client:
        adapter = GoogleDocsAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(
            _make_append_action(
                doc_id="doc-42", heading="Timeline", body="Phase 1 Q3."
            ),
            dry_run=False,
        )

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/v1/documents/doc-42:batchUpdate")
    assert captured["auth"] == "Bearer ya29.test-bearer"
    requests_field = captured["body"].get("requests", [])
    text_blob = " ".join(
        req.get("insertText", {}).get("text", "")
        for req in requests_field
        if isinstance(req, dict)
    )
    assert "Timeline" in text_blob
    assert "Phase 1 Q3." in text_blob
    assert result.status == "executed"
    assert result.external_id == "doc-42"


# ---------------------------------------------------------------------------
# Error surfaces — auth, network, InvalidURL leak (round-3 M4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_create_doc_surfaces_401_as_failed_action() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    async with _mock_transport(handler) as client:
        adapter = GoogleDocsAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(_make_create_action(), dry_run=False)

    assert result.status == "failed"
    assert "401" in (result.error or "")


@pytest.mark.asyncio
async def test_real_create_doc_surfaces_network_error_as_failed_action() -> None:
    """Mirrors the M8 redaction contract used in Plane / Drive: the
    audit ``error`` field carries the httpx exception class name but
    NOT the exception body. A leaked secret in a doc title would
    otherwise reach the audit log via ``str(exc)`` which embeds the
    request URL — and Docs URLs can carry title query strings in
    some batchUpdate shapes."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "synthetic SECRET-TITLE-NETWORK-MSG", request=request
        )

    async with _mock_transport(handler) as client:
        adapter = GoogleDocsAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(
            _make_create_action(title="SECRET-TITLE-INPUT"), dry_run=False
        )

    assert result.status == "failed"
    assert "ConnectError" in (result.error or "")
    err = result.error or ""
    assert "SECRET-TITLE-NETWORK-MSG" not in err
    assert "SECRET-TITLE-INPUT" not in err


@pytest.mark.asyncio
async def test_real_create_doc_surfaces_invalid_url_as_failed_action() -> None:
    """Round-3 M4 — ``httpx.InvalidURL`` is NOT a subclass of
    ``httpx.HTTPError``. Without the tuple-catch a doc title that
    broke URL parsing would leak via the FastAPI 500 traceback."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.InvalidURL("bad url: SECRET-IN-URL-FRAGMENT")

    async with _mock_transport(handler) as client:
        adapter = GoogleDocsAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(_make_create_action(), dry_run=False)

    assert result.status == "failed"
    assert "InvalidURL" in (result.error or "")
    err = result.error or ""
    assert "SECRET-IN-URL-FRAGMENT" not in err
    assert "bad url" not in err


@pytest.mark.asyncio
async def test_real_append_section_surfaces_network_error_as_failed_action() -> None:
    """Append-path mirrors the same redaction contract."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout(
            "synthetic SECRET-MSG-CONTENT", request=request
        )

    async with _mock_transport(handler) as client:
        adapter = GoogleDocsAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(
            _make_append_action(doc_id="doc-x", body="SECRET-BODY-CONTENT"),
            dry_run=False,
        )

    assert result.status == "failed"
    assert "ConnectTimeout" in (result.error or "")
    err = result.error or ""
    assert "SECRET-MSG-CONTENT" not in err
    assert "SECRET-BODY-CONTENT" not in err
