from typing import Protocol

from competitionops.schemas import (
    ActionPlan,
    AuditRecord,
    ExternalAction,
    ExternalActionResult,
    WebIngestionResult,
)


class ExternalActionExecutor(Protocol):
    async def execute(
        self, action: ExternalAction, dry_run: bool = True
    ) -> ExternalActionResult: ...


class PlanRepository(Protocol):
    def save(self, plan: ActionPlan) -> None: ...

    def get(self, plan_id: str) -> ActionPlan | None: ...

    def list_all(self) -> list[ActionPlan]:
        """Return every saved plan as a snapshot.

        Called by the MCP ``preview_external_actions`` flow when no
        explicit ``plan_id`` is supplied. Both InMemory and File
        implementations satisfy this, so MCP can call
        ``_plan_repo().list_all()`` against either backend.
        """
        ...


class AuditLogPort(Protocol):
    def append(self, record: AuditRecord) -> None: ...

    def list_for_plan(self, plan_id: str) -> list[AuditRecord]: ...


class PdfIngestionPort(Protocol):
    """Renders a PDF byte stream into plain text for the brief extractor.

    Sprint 0-2 (P2-005): ``MockPdfAdapter`` is the only implementation
    and treats the bytes after the ``%PDF-`` header as UTF-8 text.

    Sprint 3 (P2-005): swap in a Docling-backed adapter that parses
    real PDF layout. Both adapters MUST keep this signature so the
    extractor never knows which engine produced the text.
    """

    def extract(self, pdf_bytes: bytes) -> str: ...


class WebIngestionPort(Protocol):
    """Fetches a URL and returns plain text suitable for brief extraction.

    P1-006 Sprint 0: ``MockWebAdapter`` is the only implementation.
    Returns registered fixtures or a deterministic synthetic result per
    URL — no network. Tests inject fixtures via ``register``.

    P1-006 Sprint 2 (future): swap in a Crawl4AI / Playwright adapter
    that renders JS-heavy competition pages. Both adapters MUST keep
    this signature so the brief extractor never knows which engine
    produced the text.
    """

    async def fetch(self, url: str) -> WebIngestionResult: ...


class TokenProvider(Protocol):
    """Supplies a currently-valid OAuth access token to the Google adapters.

    The Drive / Docs / Sheets / Calendar adapters call
    ``get_access_token`` for each real-mode request instead of reading a
    static Settings bearer. Two implementations:

    - ``StaticTokenProvider`` — returns an operator-wired bearer verbatim
      (e.g. an OAuth Playground token pasted into ``GOOGLE_OAUTH_ACCESS_TOKEN``).
      No refresh; the operator re-supplies the token when it expires.
    - ``GoogleOAuthTokenProvider`` — exchanges a long-lived refresh token
      for short-lived access tokens on demand, caching each until just
      before expiry, so PMs stop re-pasting hourly tokens.

    Implementations must be safe to call concurrently — the four Google
    adapters share one provider instance via the registry.
    """

    async def get_access_token(self) -> str: ...
