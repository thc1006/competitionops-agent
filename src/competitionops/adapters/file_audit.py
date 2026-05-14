"""File-based audit log adapter — append-only JSONL, one file per writer.

Closes Stage 7 finding M2 / Tier 0 #4 ``audit log 落地``: production
deployments mount a PVC (or any persistent volume) at
``Settings.audit_log_dir`` and the full audit trail survives pod
restart.

Closes deep-review H3 (multi-writer torn-write race): rather than a
single ``<plan_id>.jsonl`` shared by every pod (which would rely on
``fcntl.flock`` whose semantics vary across RWX backends — NFS without
lockd, Azure Files, EFS, …), each writer owns its own filename.

Storage layout::

    <base_dir>/
      ├── <safe(plan_id_a)>.<safe(writer_id_x)>.jsonl
      ├── <safe(plan_id_a)>.<safe(writer_id_y)>.jsonl
      ├── <safe(plan_id_b)>.<safe(writer_id_x)>.jsonl
      └── ...

``list_for_plan`` globs every ``<safe(plan_id)>.*.jsonl`` (plus the
legacy single-file form for backward compatibility) and merges. The
file-per-writer invariant means there is no shared resource between
writers, so torn writes are impossible regardless of the underlying
filesystem.

Writer identity:

- Defaults to ``socket.gethostname()``. In a Kubernetes pod this is
  the pod name (which k8s already sets to ``metadata.name``), so a
  3-replica Deployment automatically gets three distinct writer_ids
  without any extra wiring.
- Operators can override via the ``writer_id`` constructor arg
  (mostly for tests and for non-k8s deployments). Sanitisation folds
  anything outside ``[A-Za-z0-9_-]`` to ``_`` so a malformed id cannot
  escape ``base_dir`` via path traversal.

Backward compatibility:

- A pre-H3 deployment has files named ``<plan_id>.jsonl`` (no writer
  segment). ``list_for_plan`` picks those up as well so historical
  audit records survive an in-place upgrade. New writes go to the
  new layout; the legacy file ages out naturally.

Plan_id sanitisation: hash-based ids never contain a slash, but we
defensively replace anything outside ``[A-Za-z0-9_-]`` with ``_`` so a
malformed id cannot escape ``base_dir`` via path traversal.
"""

from __future__ import annotations

import socket
from pathlib import Path

from competitionops.schemas import AuditRecord


class FileAuditLog:
    """Append-only JSONL audit log, one file per (plan_id, writer_id)."""

    def __init__(self, base_dir: Path, writer_id: str | None = None) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        raw = writer_id if writer_id is not None else (socket.gethostname() or "")
        # ``_sanitise`` already collapses empty / odd input to ``"_"``,
        # so the writer_id is always usable as a filename segment.
        self.writer_id = self._sanitise(raw)

    def append(self, record: AuditRecord) -> None:
        path = self._path_for(record.plan_id)
        line = record.model_dump_json() + "\n"
        # ``open("a")`` opens for append; each ``write`` of a single
        # record stays under PIPE_BUF (4 KiB), which is atomic on local
        # FS for a SINGLE writer. The per-writer-file layout ensures we
        # never have to defend against the multi-writer case.
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def list_for_plan(self, plan_id: str) -> list[AuditRecord]:
        safe_plan = self._sanitise(plan_id)
        # New layout: ``<plan_id>.<writer_id>.jsonl``.
        paths: list[Path] = list(self.base_dir.glob(f"{safe_plan}.*.jsonl"))
        # Backward-compat: pre-H3 layout shipped ``<plan_id>.jsonl``.
        # Pick that up too so an in-place upgrade doesn't lose history.
        legacy = self.base_dir / f"{safe_plan}.jsonl"
        if legacy.exists() and legacy not in paths:
            paths.append(legacy)

        records: list[AuditRecord] = []
        for path in sorted(paths):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for raw_line in handle:
                        line = raw_line.strip()
                        if not line:
                            continue
                        records.append(AuditRecord.model_validate_json(line))
            except FileNotFoundError:
                # Tolerate the file vanishing mid-walk — extremely rare
                # but possible if another pod is rotating files.
                continue
        return records

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _path_for(self, plan_id: str) -> Path:
        safe_plan = self._sanitise(plan_id)
        return self.base_dir / f"{safe_plan}.{self.writer_id}.jsonl"

    @staticmethod
    def _sanitise(value: str) -> str:
        """Map an arbitrary id to a safe filename segment.

        Hash-based plan_ids and k8s pod names only contain
        ``[A-Za-z0-9_-]`` so this is a no-op for legitimate values. ``.``
        is intentionally NOT in the allowed set — it would let ``..``
        appear as a filename component, which is the path-traversal
        pattern we are defending against (mirrors the same defence in
        ``FilePlanRepository``).
        """
        cleaned = "".join(
            char if char.isalnum() or char in "-_" else "_" for char in value
        )
        return cleaned or "_"
