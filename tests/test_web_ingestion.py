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


def test_runtime_web_adapter_factory_constructs_crawl4ai_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint 2 — ``WEB_ADAPTER=crawl4ai`` constructs the real
    ``Crawl4AIWebAdapter`` (replaces Sprint 0's placeholder RuntimeError).

    No real Crawl4AI install required for this test — the adapter's
    constructor is side-effect-free (lazy import inside ``fetch``).
    Sprint 2's Sprint-0 placeholder test was REPLACED (not deleted)
    by this positive test per the Sprint-0 docstring directive."""
    from competitionops import runtime
    from competitionops.adapters.web_crawl4ai import Crawl4AIWebAdapter

    reset_runtime_caches()
    monkeypatch.setenv("WEB_ADAPTER", "crawl4ai")
    adapter = runtime._web_adapter()
    assert isinstance(adapter, Crawl4AIWebAdapter)


def test_runtime_web_adapter_factory_surfaces_missing_crawl4ai_dep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``WEB_ADAPTER=crawl4ai`` but the ``[web]`` extra hasn't been
    installed, the FIRST ``fetch`` call must raise ``RuntimeError`` with
    operator guidance — not a bare ``ImportError`` that's hard to act on.

    The adapter constructor doesn't import Crawl4AI (lazy pattern), so
    factory construction succeeds. The ImportError surfaces on the
    first fetch. Tests simulate "package not installed" by intercepting
    the adapter's resolver indirection."""
    import asyncio
    from competitionops.adapters import web_crawl4ai

    def fake_resolver():  # type: ignore[no-untyped-def]
        raise ImportError("No module named 'crawl4ai'")

    monkeypatch.setattr(web_crawl4ai, "_resolve_async_web_crawler", fake_resolver)

    adapter = web_crawl4ai.Crawl4AIWebAdapter()
    with pytest.raises(RuntimeError, match="uv sync --extra web"):
        asyncio.run(adapter.fetch("https://example.com/comp"))


def test_crawl4ai_adapter_maps_crawl_result_to_web_ingestion_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint 2 mapping contract — Crawl4AI's ``CrawlResult`` shape
    (``success``, ``url``, ``markdown``, ``metadata``) lands cleanly
    into our ``WebIngestionResult`` (``url``, ``title``, ``text``).

    ``CrawlResult.url`` is the canonical post-redirect URL → our
    ``url``. ``CrawlResult.metadata['title']`` → our ``title``.
    ``CrawlResult.markdown`` → our ``text`` (cleaned LLM-ready content).
    """
    import asyncio
    from competitionops.adapters import web_crawl4ai

    class FakeCrawlResult:
        success = True
        url = "https://example.com/canonical-redirected"
        markdown = "# RunSpace 2026\n\nSubmission deadline 2026-06-15."
        metadata = {"title": "RunSpace Innovation Challenge"}
        error_message: str | None = None

    class FakeAsyncWebCrawler:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *exc):  # type: ignore[no-untyped-def]
            return False

        async def arun(self, *, url: str, **_kwargs):  # type: ignore[no-untyped-def]
            return FakeCrawlResult()

    monkeypatch.setattr(
        web_crawl4ai, "_resolve_async_web_crawler", lambda: FakeAsyncWebCrawler
    )

    adapter = web_crawl4ai.Crawl4AIWebAdapter()
    result = asyncio.run(adapter.fetch("https://example.com/comp"))

    assert result.url == "https://example.com/canonical-redirected"
    assert result.title == "RunSpace Innovation Challenge"
    assert "RunSpace 2026" in result.text
    assert "2026-06-15" in result.text


def test_crawl4ai_adapter_raises_on_failed_crawl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Crawl4AI returns ``success=False``, the adapter must
    surface a ``RuntimeError`` with the upstream error message — not
    return a half-empty ``WebIngestionResult`` that the brief extractor
    would interpret as "no content found"."""
    import asyncio
    from competitionops.adapters import web_crawl4ai

    class FakeCrawlResult:
        success = False
        url = "https://example.com/x"
        markdown = ""
        metadata: dict[str, str] = {}
        error_message = "net::ERR_NAME_NOT_RESOLVED"

    class FakeAsyncWebCrawler:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *exc):  # type: ignore[no-untyped-def]
            return False

        async def arun(self, *, url: str, **_kwargs):  # type: ignore[no-untyped-def]
            return FakeCrawlResult()

    monkeypatch.setattr(
        web_crawl4ai, "_resolve_async_web_crawler", lambda: FakeAsyncWebCrawler
    )

    adapter = web_crawl4ai.Crawl4AIWebAdapter()
    with pytest.raises(RuntimeError, match="ERR_NAME_NOT_RESOLVED"):
        asyncio.run(adapter.fetch("https://example.com/x"))


def test_main_module_eager_validates_web_adapter() -> None:
    """The eager-validate function in ``main.py`` (round-3 M1) must
    CALL ``_web_adapter()`` alongside ``_pdf_adapter()`` so a typo'd
    ``WEB_ADAPTER`` crashes uvicorn import instead of the first URL
    request returning 500.

    PR #30 review tightening — AST inspection rather than substring
    grep. A commented-out call or docstring mention would slip past
    the loose form; structural Call-node check pins the contract.
    """
    import ast

    tree = ast.parse(inspect.getsource(main_module._eager_validate_runtime_config))
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_web_adapter" in called_names, (
        f"main._eager_validate_runtime_config must CALL ``_web_adapter()`` "
        f"at top level. Saw calls to: {sorted(called_names)}. "
        "A bare mention in a docstring or comment does not satisfy the "
        "round-3 M1 contract — the function must actually invoke the "
        "factory so unknown WEB_ADAPTER values surface at module import."
    )
    # Symmetry — ``_pdf_adapter`` must also still be called (regression
    # guard against someone refactoring this function and dropping a
    # factory).
    assert "_pdf_adapter" in called_names


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


def test_post_briefs_extract_url_rejects_ip_literal_in_banned_ranges() -> None:
    """P1-006 Sprint 1 — SSRF filter. IP-literal URLs targeting
    loopback / RFC-1918 private / link-local (incl. cloud metadata
    169.254.169.254) / IPv6 loopback + link-local + ULA / unspecified
    must 422 BEFORE the adapter dispatch.

    Each entry is an IP literal — no DNS lookup involved. The filter
    parses with ``ipaddress.ip_address`` and rejects on
    ``is_private | is_loopback | is_link_local | is_reserved |
    is_multicast | is_unspecified``.
    """
    reset_runtime_caches()
    client = TestClient(main_module.app)
    banned = [
        # IPv4
        ("http://127.0.0.1/x", "loopback"),
        ("http://10.0.0.1/x", "RFC1918 10/8"),
        ("http://172.16.0.1/x", "RFC1918 172.16/12"),
        ("http://192.168.1.1/x", "RFC1918 192.168/16"),
        ("http://169.254.169.254/latest/meta-data/", "cloud metadata"),
        ("http://0.0.0.0/x", "unspecified"),
        # IPv6
        ("http://[::1]/x", "v6 loopback"),
        ("http://[fe80::1]/x", "v6 link-local"),
        ("http://[fc00::1]/x", "v6 ULA"),
    ]
    for url, label in banned:
        response = client.post("/briefs/extract/url", json={"url": url})
        assert response.status_code == 422, (
            f"{label} ({url}) should be rejected — got {response.status_code}: "
            f"{response.text}"
        )


def test_post_briefs_extract_url_rejects_hostname_resolving_to_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSRF filter handles the DNS-resolution path: a public-looking
    hostname that resolves to a private IP must be rejected. Tests
    monkey-patch ``socket.getaddrinfo`` to avoid real DNS in CI."""
    import socket

    def fake_getaddrinfo(host: str, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        # Return an A record pointing at RFC-1918.
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.20.30.40", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    reset_runtime_caches()
    client = TestClient(main_module.app)
    response = client.post(
        "/briefs/extract/url",
        json={"url": "https://attacker-controlled.example.com/page"},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert "ssrf" in response.text.lower() or "private" in response.text.lower() or (
        "non-routable" in response.text.lower()
    ), body


def test_post_briefs_extract_url_accepts_hostname_resolving_to_public_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DNS path's happy case — a hostname that resolves to a
    public IP must pass through. Mock adapter handles the actual
    fetch so no real network access."""
    import socket

    def fake_getaddrinfo(host: str, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        # 93.184.216.34 is example.com's historical public IP. Any
        # non-banned address works for the test.
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    reset_runtime_caches()
    client = TestClient(main_module.app)
    response = client.post(
        "/briefs/extract/url",
        json={"url": "https://example.com/competition"},
    )
    assert response.status_code == 200, response.text


def test_post_briefs_extract_url_lenient_on_dns_resolution_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``getaddrinfo`` raises ``gaierror`` (hostname doesn't resolve),
    the filter is lenient — accept the URL and let the adapter handle
    the network error. Rationale: unresolvable hostnames can't reach
    internal infrastructure either, so the SSRF threat doesn't apply.
    This keeps ``.invalid`` / ``.test`` URLs (RFC 6761) usable as
    offline test fixtures.

    Existing tests use ``https://example.invalid/...`` and rely on
    this leniency."""
    import socket

    def fake_getaddrinfo(host: str, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    reset_runtime_caches()
    client = TestClient(main_module.app)
    response = client.post(
        "/briefs/extract/url",
        json={"url": "https://does-not-resolve.invalid/comp"},
    )
    # Filter accepts; adapter (mock) handles synthetic content.
    assert response.status_code == 200, response.text


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
