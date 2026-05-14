from typing import Protocol

from competitionops.schemas import ActionPlan, AuditRecord, ExternalAction, ExternalActionResult


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
