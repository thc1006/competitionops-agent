"""P1-006 Sprint 2 — Crawl4AI-backed ``WebIngestionPort`` adapter.

Real-mode web ingestion via Crawl4AI (which uses Playwright/Chromium
under the hood). Activates on ``WEB_ADAPTER=crawl4ai`` once the
``[web]`` optional extra is installed (``uv sync --extra web``).

Lazy-import pattern (same as ``DoclingPdfAdapter`` from P2-005 Sprint
3): the constructor is side-effect-free; ``crawl4ai`` is only imported
inside the first ``fetch`` call. Missing-dep surfaces as a clear
``RuntimeError`` pointing at ``uv sync --extra web``, never as a bare
``ImportError`` mid-request.

The mapping from Crawl4AI's ``CrawlResult`` to our ``WebIngestionResult``:

- ``CrawlResult.url`` (canonical post-redirect) → ``WebIngestionResult.url``
- ``CrawlResult.metadata['title']`` → ``WebIngestionResult.title``
- ``CrawlResult.markdown`` (LLM-ready cleaned content) → ``WebIngestionResult.text``

**DNS rebinding — operator responsibility.** Sprint 1's Pydantic-layer
SSRF filter resolves the URL's hostname at validation time. This
adapter does NOT re-validate at connect time — the browser engine
owns its own DNS stack, and intercepting it cleanly with Crawl4AI's
Playwright backend is a substantial effort that's out of scope for
Sprint 2. Operators MUST constrain egress at the infrastructure
layer (k8s NetworkPolicy, egress proxy, dedicated network namespace)
so a DNS rebinding to a private IP still can't reach internal
infrastructure even if it slips past the Pydantic check. The
validator + infra restriction together form the SSRF defence; the
adapter is the consumer of that policy, not its enforcer.
"""

from __future__ import annotations

from typing import Any

from competitionops.schemas import WebIngestionResult


def _resolve_async_web_crawler() -> Any:
    """Indirection so tests can monkeypatch the Crawl4AI entrypoint
    without installing the heavy ``crawl4ai`` + Playwright deps in
    CI. Production resolves to the real ``AsyncWebCrawler`` class.

    Raises ``ImportError`` when ``crawl4ai`` is not installed; the
    ``fetch`` caller converts that into a ``RuntimeError`` with
    operator guidance (``uv sync --extra web``).
    """
    from crawl4ai import AsyncWebCrawler  # type: ignore[import-not-found, unused-ignore]
    return AsyncWebCrawler


def _extract_markdown_text(result: Any) -> str:
    """Extract a plain string from a Crawl4AI ``CrawlResult.markdown``.

    PR #32 deep-review polish A — Crawl4AI 0.5+ migrated
    ``result.markdown`` from a plain ``str`` to a
    ``MarkdownGenerationResult`` object exposing ``.raw_markdown`` /
    ``.fit_markdown`` / etc. The exact shape varies across 0.5/0.6/0.7
    patch releases, so the adapter probes for both. Order:

    1. If ``markdown`` has ``raw_markdown`` (the object form), prefer it.
       Fall back to ``fit_markdown`` if raw is empty.
    2. Otherwise coerce via ``str()`` — handles the v0.4-style plain
       string AND any future shape that supports ``__str__``.
    3. Empty / None → empty string.

    Returns a guaranteed ``str`` so Pydantic's
    ``WebIngestionResult.text: str`` accepts it without coercion games.
    """
    markdown = getattr(result, "markdown", None)
    if markdown is None or markdown == "":
        return ""
    if hasattr(markdown, "raw_markdown"):
        return (
            getattr(markdown, "raw_markdown", "")
            or getattr(markdown, "fit_markdown", "")
            or ""
        )
    return str(markdown)


class Crawl4AIWebAdapter:
    """Real-mode ``WebIngestionPort`` implementation backed by Crawl4AI.

    Stateless — every ``fetch`` builds a fresh ``AsyncWebCrawler``
    so headless browser teardown happens deterministically. Future
    iterations may pool crawlers if profiling shows the per-fetch
    spin-up to be the bottleneck.
    """

    async def fetch(self, url: str) -> WebIngestionResult:
        try:
            async_web_crawler_cls = _resolve_async_web_crawler()
        except ImportError as exc:
            raise RuntimeError(
                "Crawl4AI is not installed. Run ``uv sync --extra web`` "
                "to install the Crawl4AI + Playwright deps, or unset "
                "WEB_ADAPTER (defaults to ``mock``)."
            ) from exc

        # PR #32 deep-review polish C — Playwright (under Crawl4AI) can
        # raise its own exception types whose ``str(exc)`` embeds the
        # request URL or DOM state. Bubbling those uncaught to FastAPI
        # surfaces the leak via the 500 traceback. Catch broad here and
        # re-raise class-name-only (same M8/M4 redaction discipline the
        # Google adapters follow). ``raise … from exc`` preserves the
        # chain for server-side logs.
        try:
            async with async_web_crawler_cls() as crawler:
                result = await crawler.arun(url=url)
        except Exception as exc:
            raise RuntimeError(
                f"web fetch failed: {type(exc).__name__}"
            ) from exc

        if not getattr(result, "success", False):
            error_message = getattr(result, "error_message", None) or "unknown"
            raise RuntimeError(
                f"Crawl4AI fetch failed for {url!r}: {error_message}"
            )

        metadata = getattr(result, "metadata", None) or {}
        canonical_url = getattr(result, "url", None) or url
        return WebIngestionResult(
            url=canonical_url,
            title=metadata.get("title", "") or "",
            text=_extract_markdown_text(result),
        )
