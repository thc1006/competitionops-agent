from competitionops.adapters.fake import FakeExternalActionExecutor
from competitionops.adapters.google_calendar import GoogleCalendarAdapter
from competitionops.adapters.google_docs import GoogleDocsAdapter
from competitionops.adapters.google_drive import GoogleDriveAdapter
from competitionops.adapters.google_sheets import GoogleSheetsAdapter
from competitionops.adapters.plane import PlaneAdapter
from competitionops.ports import ExternalActionExecutor, TokenProvider


class AdapterRegistry:
    """Routes ExternalAction target_system → ExternalActionExecutor.

    Domain layer never references concrete adapters; only this registry does.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, ExternalActionExecutor] = {}

    def register(self, target_system: str, adapter: ExternalActionExecutor) -> None:
        self._adapters[target_system] = adapter

    def get(self, target_system: str) -> ExternalActionExecutor | None:
        return self._adapters.get(target_system)


def build_default_registry(
    token_provider: TokenProvider | None = None,
) -> AdapterRegistry:
    """Build the adapter set every FastAPI request / MCP tool / workflow uses.

    ``token_provider`` is threaded into the four Google adapters so they
    share one process-wide provider (static bearer or refresh-backed).
    When ``None`` — direct callers and older tests — each Google adapter
    falls back to a static bearer derived from Settings. The registry
    singleton in ``runtime`` passes ``runtime._token_provider()``.
    """
    registry = AdapterRegistry()
    registry.register(
        "google_drive", GoogleDriveAdapter(token_provider=token_provider)
    )
    registry.register(
        "google_docs", GoogleDocsAdapter(token_provider=token_provider)
    )
    registry.register(
        "google_sheets", GoogleSheetsAdapter(token_provider=token_provider)
    )
    registry.register(
        "google_calendar", GoogleCalendarAdapter(token_provider=token_provider)
    )
    registry.register("plane", PlaneAdapter())
    registry.register("internal", FakeExternalActionExecutor())
    return registry
