"""File-based audit log adapter — append-only JSONL per plan_id.

Closes Stage 7 finding M2 / Tier 0 #4 ``audit log 落地``: production
deployments can now mount a PVC (or any persistent volume) at
``Settings.audit_log_dir`` and survive pod restart with the full
audit trail intact.

Storage layout::

    <base_dir>/
      ├── <safe(plan_id_a)>.jsonl    # one line per AuditRecord
      ├── <safe(plan_id_b)>.jsonl
      └── ...

Each line is the JSON form of one ``AuditRecord``. Append is atomic on
POSIX for writes under ``PIPE_BUF`` (4096 bytes) — an AuditRecord JSON
fits comfortably below that. Multi-process write coordination is out of
scope for the MVP single-process layout; switch to a sqlite-backed
implementation behind the same ``AuditLogPort`` if multi-replica writes
are needed.

Plan_id sanitisation: hash-based ids never contain a slash, but we
defensively replace anything outside ``[A-Za-z0-9._-]`` with ``_`` so
that a malformed id cannot escape ``base_dir`` via path traversal.
"""

from __future__ import annotations

from pathlib import Path

from competitionops.schemas import AuditRecord


class FileAuditLog:
    """Append-only JSONL audit log keyed by plan_id."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def append(self, record: AuditRecord) -> None:
        path = self._path_for(record.plan_id)
        line = record.model_dump_json() + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def list_for_plan(self, plan_id: str) -> list[AuditRecord]:
        path = self._path_for(plan_id)
        if not path.exists():
            return []
        records: list[AuditRecord] = []
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                records.append(AuditRecord.model_validate_json(line))
        return records

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _path_for(self, plan_id: str) -> Path:
        safe = self._sanitise(plan_id)
        return self.base_dir / f"{safe}.jsonl"

    @staticmethod
    def _sanitise(plan_id: str) -> str:
        """Map plan_id to a safe filename stem.

        Hash-based plan_ids only contain ``[A-Za-z0-9_]`` so this is a
        no-op for legitimate values; the conservative replacement is a
        defense against path-traversal-style malformed ids (e.g.,
        ``../../etc/passwd``).
        """
        allowed = "-._"
        cleaned = "".join(
            char if char.isalnum() or char in allowed else "_" for char in plan_id
        )
        return cleaned or "_"
