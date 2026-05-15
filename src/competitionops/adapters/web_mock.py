"""P1-006 Sprint 0 — Mock web ingestion adapter.

Returns canned ``WebIngestionResult`` records, never touches the
network. Two modes:

1. Registered fixtures via ``adapter.register(result)``: ``fetch``
   returns the exact pre-populated record. Used by integration tests
   that need known text content for the brief extractor.
2. Unregistered URLs: ``fetch`` returns a deterministic synthetic
   record (title + short placeholder text derived from the URL).
   Deterministic per-URL so repeat fetches return identical content.
   Useful for unit-testing endpoint plumbing without explicit
   fixtures.

Real adapter (Crawl4AI / Playwright) lands in P1-006 Sprint 2 behind
the ``[web]`` optional extra. The mock survives unchanged for the
test suite — production never reaches it once ``WEB_ADAPTER=crawl4ai``
is set in Settings.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlparse

from competitionops.schemas import WebIngestionResult


def _short_hash(text: str, length: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


class MockWebAdapter:
    def __init__(self) -> None:
        self._fixtures: dict[str, WebIngestionResult] = {}
        self.calls: list[str] = []

    def register(self, result: WebIngestionResult) -> None:
        """Add a fixture keyed on its ``url``. Subsequent ``fetch(url)``
        calls return this exact result. Idempotent — re-registering
        the same URL overwrites."""
        self._fixtures[result.url] = result

    async def fetch(self, url: str) -> WebIngestionResult:
        self.calls.append(url)
        fixture = self._fixtures.get(url)
        if fixture is not None:
            return fixture
        # Deterministic synthetic — title includes the URL's hostname so
        # downstream brief extraction has something non-empty to inspect.
        host = urlparse(url).netloc or "unknown.invalid"
        return WebIngestionResult(
            url=url,
            title=f"Mock competition at {host}",
            text=(
                f"Mock content for {url}. "
                f"Token-hash: {_short_hash(url)}. "
                "This is a placeholder text body produced by "
                "MockWebAdapter — register a fixture via "
                "adapter.register(...) to inject known content for "
                "tests, or wire a real adapter (Crawl4AI / Playwright) "
                "for production via WEB_ADAPTER env."
            ),
        )
