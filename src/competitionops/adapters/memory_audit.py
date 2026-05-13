from competitionops.schemas import AuditRecord


class InMemoryAuditLog:
    """Local, append-only audit log used by tests and dev runs."""

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    def append(self, record: AuditRecord) -> None:
        self._records.append(record)

    def list_for_plan(self, plan_id: str) -> list[AuditRecord]:
        return [record for record in self._records if record.plan_id == plan_id]

    def all(self) -> list[AuditRecord]:
        return list(self._records)
