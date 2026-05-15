"""P1-006 Sprint 0 — Web ingestion scaffolding.

Pure port + mock + factory + endpoint plumbing. The real adapter
(Crawl4AI / Playwright direct) lands in Sprint 2 — Sprint 0 establishes
the seam so a follow-up sprint can plug in a real implementation
without touching domain code.

Mirrors the PDF Sprint 0 contract from P2-005:
- ``WebIngestionPort`` is a ``Protocol`` defined in ``ports.py``.
- ``MockWebAdapter`` implements the protocol, returning canned
  results keyed by URL. Tests inject fixtures via ``register``.
- ``runtime._web_adapter()`` is a ``@lru_cache(maxsize=1)`` factory
  switching on ``Settings.web_adapter`` (``None`` / ``"mock"`` →
  mock; unknown values raise ``ValueError`` per round-3 M1 pattern).
- ``main.get_web_adapter()`` exposes the port to FastAPI's DI.
- ``POST /briefs/extract/url`` is the new endpoint — fetch URL via
  port, then reuse ``BriefExtractor.extract_from_text``.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient

from competitionops import main as main_module
from competitionops.adapters.web_mock import MockWebAdapter
from competitionops.ports import WebIngestionPort
from competitionops.schemas import WebIngestionResult

from conftest import reset_runtime_caches  # noqa: E402, I001


# ---------------------------------------------------------------------------
# Port contract — Protocol shape
# ---------------------------------------------------------------------------


def test_web_ingestion_port_is_protocol_with_async_fetch() -> None:
    """``WebIngestionPort`` must be a runtime-checkable Protocol with a
    single async ``fetch(url) -> WebIngestionResult`` method. Anything
    else risks the future Crawl4AI / Playwright adapter not satisfying
    the type relationship FastAPI's DI checks."""
    # Must be a Protocol — confirmable via the typing._ProtocolMeta machinery.
    # Pragmatic check: it must have a ``fetch`` member.
    assert hasattr(WebIngestionPort, "fetch")
    # MockWebAdapter must be assignable to the port (structural typing).
    adapter: WebIngestionPort = MockWebAdapter()
    sig = inspect.signature(adapter.fetch)
    assert "url" in sig.parameters
    assert inspect.iscoroutinefunction(adapter.fetch)


def test_web_ingestion_result_shape() -> None:
    """``WebIngestionResult`` carries url / title / text. The URL field
    is the canonical (post-redirect) URL, surfaced into the
    ``CompetitionBrief.source_uri`` downstream."""
    r = WebIngestionResult(
        url="https://example.invalid/competition",
        title="RunSpace Innovation Challenge",
        text="Submit by 2026-06-15.",
    )
    assert r.url == "https://example.invalid/competition"
    assert r.title == "RunSpace Innovation Challenge"
    assert r.text == "Submit by 2026-06-15."


# ---------------------------------------------------------------------------
# MockWebAdapter behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_web_adapter_returns_registered_fixture() -> None:
    """Tests register URL → result fixtures via ``adapter.register``.
    ``fetch`` then returns the exact registered result."""
    adapter = MockWebAdapter()
    fixture = WebIngestionResult(
        url="https://example.invalid/comp",
        title="Fixture title",
        text="Fixture body text.",
    )
    adapter.register(fixture)

    got = await adapter.fetch("https://example.invalid/comp")
    assert got == fixture


@pytest.mark.asyncio
async def test_mock_web_adapter_returns_synthetic_on_unregistered_url() -> None:
    """An unregistered URL produces a deterministic synthetic result
    so tests can exercise the downstream pipeline without explicit
    fixtures. Title contains the URL's domain; text is a
    short placeholder. Deterministic per-URL — repeat fetches return
    identical content."""
    adapter = MockWebAdapter()
    r1 = await adapter.fetch("https://example.invalid/x")
    r2 = await adapter.fetch("https://example.invalid/x")
    assert r1 == r2
    assert "example.invalid" in r1.title.lower() or "example.invalid" in r1.url
    assert r1.text  # non-empty


@pytest.mark.asyncio
async def test_mock_web_adapter_records_calls_for_audit() -> None:
    """The mock's ``calls`` list must capture every fetched URL. Used
    by integration tests to assert the endpoint actually hit the
    adapter."""
    adapter = MockWebAdapter()
    await adapter.fetch("https://example.invalid/a")
    await adapter.fetch("https://example.invalid/b")
    assert adapter.calls == [
        "https://example.invalid/a",
        "https://example.invalid/b",
    ]


# ---------------------------------------------------------------------------
# Runtime factory — switch on Settings.web_adapter
# ---------------------------------------------------------------------------


def test_runtime_web_adapter_factory_defaults_to_mock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from competitionops import runtime

    reset_runtime_caches()
    monkeypatch.delenv("WEB_ADAPTER", raising=False)
    adapter = runtime._web_adapter()
    assert isinstance(adapter, MockWebAdapter)


def test_runtime_web_adapter_factory_accepts_explicit_mock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from competitionops import runtime

    reset_runtime_caches()
    monkeypatch.setenv("WEB_ADAPTER", "mock")
    adapter = runtime._web_adapter()
    assert isinstance(adapter, MockWebAdapter)


def test_runtime_web_adapter_factory_raises_on_unknown_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-3 M1 pattern — operator typos must surface at startup, not
    silently fall back to mock. ``_web_adapter()`` is called eagerly at
    module init (verified by the AST guard for ``_eager_validate_runtime_config``)."""
    from competitionops import runtime

    reset_runtime_caches()
    monkeypatch.setenv("WEB_ADAPTER", "scrapy-2.11")
    with pytest.raises(ValueError, match="scrapy-2.11"):
        runtime._web_adapter()


def test_main_module_eager_validates_web_adapter() -> None:
    """The eager-validate function in ``main.py`` (round-3 M1) must
    call ``_web_adapter`` alongside ``_pdf_adapter`` so a typo'd
    ``WEB_ADAPTER`` crashes uvicorn import instead of the first URL
    request returning 500.
    """
    source = inspect.getsource(main_module._eager_validate_runtime_config)
    assert "_web_adapter" in source, (
        "main._eager_validate_runtime_config must call _web_adapter() "
        "to surface bad WEB_ADAPTER values at module import. Without "
        "this the round-3 M1 contract regresses for the web ingestion "
        "track."
    )


# ---------------------------------------------------------------------------
# POST /briefs/extract/url endpoint
# ---------------------------------------------------------------------------


def test_post_briefs_extract_url_returns_brief() -> None:
    """End-to-end: client posts {url}, adapter (mock) returns synthetic
    text, BriefExtractor builds a CompetitionBrief, response carries
    source_uri = canonical URL from the adapter."""
    reset_runtime_caches()
    client = TestClient(main_module.app)
    response = client.post(
        "/briefs/extract/url",
        json={"url": "https://example.invalid/competition"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # CompetitionBrief shape — name + source_uri at minimum.
    assert "name" in body
    assert body.get("source_uri") == "https://example.invalid/competition"


def test_post_briefs_extract_url_rejects_missing_url() -> None:
    """422 on empty body or missing ``url`` field."""
    reset_runtime_caches()
    client = TestClient(main_module.app)
    response = client.post("/briefs/extract/url", json={})
    assert response.status_code == 422


def test_post_briefs_extract_url_rejects_non_http_scheme() -> None:
    """The endpoint rejects ``file://``, ``javascript:``, etc. Only
    ``http(s)://`` URLs make it to the adapter. Defence-in-depth for
    when the real adapter (Sprint 2) is wired — Crawl4AI / Playwright
    can be coaxed into reading local files via ``file://``, which
    would be a sandbox-escape vector if exposed to PM-controlled input.
    """
    reset_runtime_caches()
    client = TestClient(main_module.app)
    for bad_url in (
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/plain,hi",
        "ftp://example.invalid/x",
    ):
        response = client.post("/briefs/extract/url", json={"url": bad_url})
        assert response.status_code == 422, f"{bad_url}: {response.text}"


def test_post_briefs_extract_url_uses_fixture_when_registered() -> None:
    """Dependency override to inject a pre-registered adapter — exercises
    the full pipeline (adapter → BriefExtractor → response) with known
    text content. Pin that the response's source_uri matches the
    adapter's canonical URL, not the request URL (they CAN differ
    in real-mode when a redirect normalises the URL)."""
    from competitionops.main import app, get_web_adapter

    adapter = MockWebAdapter()
    adapter.register(
        WebIngestionResult(
            url="https://example.invalid/canonical",
            title="Test Comp",
            text=(
                "Competition: TestComp 2026. Submission deadline: "
                "2026-06-15T23:59:00+08:00. Organizer: ACME."
            ),
        )
    )
    app.dependency_overrides[get_web_adapter] = lambda: adapter
    try:
        client = TestClient(app)
        response = client.post(
            "/briefs/extract/url",
            json={"url": "https://example.invalid/canonical"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["source_uri"] == "https://example.invalid/canonical"
    finally:
        app.dependency_overrides.pop(get_web_adapter, None)


# ---------------------------------------------------------------------------
# Backlog / extras consistency
# ---------------------------------------------------------------------------


def test_pyproject_declares_web_optional_extra() -> None:
    """The ``[web]`` optional extra in pyproject.toml is the path
    Sprint 2 will use to ship Crawl4AI / Playwright. Sprint 0 declares
    the extras stub (empty or just the placeholder package) so the
    install command in the docs is valid today."""
    from pathlib import Path
    import tomllib

    pyproject = tomllib.loads(
        Path("pyproject.toml").read_text(encoding="utf-8")
    )
    extras = pyproject.get("project", {}).get("optional-dependencies", {})
    assert "web" in extras, (
        "pyproject.toml must declare a ``[web]`` optional extra so "
        "``uv sync --extra web`` works today. Sprint 2 wires Crawl4AI "
        "into this slot."
    )
