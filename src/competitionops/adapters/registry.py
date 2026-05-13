from competitionops.adapters.fake import FakeExternalActionExecutor
from competitionops.adapters.google_calendar import GoogleCalendarAdapter
from competitionops.adapters.google_docs import GoogleDocsAdapter
from competitionops.adapters.google_drive import GoogleDriveAdapter
from competitionops.adapters.google_sheets import GoogleSheetsAdapter
from competitionops.adapters.plane import PlaneAdapter
from competitionops.ports import ExternalActionExecutor


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


def build_default_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register("google_drive", GoogleDriveAdapter())
    registry.register("google_docs", GoogleDocsAdapter())
    registry.register("google_sheets", GoogleSheetsAdapter())
    registry.register("google_calendar", GoogleCalendarAdapter())
    registry.register("plane", PlaneAdapter())
    registry.register("internal", FakeExternalActionExecutor())
    return registry
