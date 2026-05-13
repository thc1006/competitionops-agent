"""Mock-first Google Drive adapter.

No network, no credentials, no Google SDK imports. Real implementation
lands in P1-005 behind the same interface.
"""

from __future__ import annotations

import hashlib
from typing import Any

from competitionops.schemas import ExternalAction, ExternalActionResult

_FOLDER_TYPES = frozenset(
    {"google.drive.create_competition_folder", "google.drive.create_folder"}
)


def _hash(text: str, length: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


class GoogleDriveAdapter:
    def __init__(self) -> None:
        self.folders: dict[str, dict[str, Any]] = {}
        self.files: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []

    # ---- high-level mock operations ---------------------------------

    async def create_folder(
        self, *, name: str, parent_id: str | None = None
    ) -> dict[str, Any]:
        parent_key = parent_id or "root"
        folder_id = f"mock_folder_{_hash(f'{name}|{parent_key}')}"
        if folder_id not in self.folders:
            self.folders[folder_id] = {
                "id": folder_id,
                "name": name,
                "parent_id": parent_id,
                "url": f"https://drive.example.invalid/folders/{folder_id}",
            }
        return self.folders[folder_id]

    async def move_file(
        self, *, file_id: str, target_parent_id: str
    ) -> dict[str, Any]:
        record = self.files.get(file_id, {"file_id": file_id})
        record["parent_id"] = target_parent_id
        record["url"] = f"https://drive.example.invalid/files/{file_id}"
        self.files[file_id] = record
        return record

    async def search_files(self, *, query: str) -> list[dict[str, Any]]:
        needle = query.lower()
        results: list[dict[str, Any]] = []
        for folder in self.folders.values():
            if needle in folder["name"].lower():
                results.append(folder)
        for file_record in self.files.values():
            haystack = " ".join(
                str(value).lower() for value in file_record.values() if value
            )
            if needle in haystack:
                results.append(file_record)
        return results

    # ---- ExternalActionExecutor dispatch ----------------------------

    async def execute(
        self, action: ExternalAction, dry_run: bool = True
    ) -> ExternalActionResult:
        self.calls.append(
            {"action_id": action.action_id, "type": action.type, "dry_run": dry_run}
        )
        try:
            if action.type in _FOLDER_TYPES:
                name = (
                    action.payload.get("folder_name")
                    or action.payload.get("competition_name")
                    or action.payload.get("name")
                    or "Untitled"
                )
                folder = await self.create_folder(
                    name=name, parent_id=action.payload.get("parent_id")
                )
                return self._success(
                    action,
                    dry_run=dry_run,
                    external_id=folder["id"],
                    external_url=folder["url"],
                    message="Created Drive folder (mock).",
                )

            if action.type == "google.drive.move_file":
                moved = await self.move_file(
                    file_id=action.payload["file_id"],
                    target_parent_id=action.payload["target_parent_id"],
                )
                return self._success(
                    action,
                    dry_run=dry_run,
                    external_id=moved["file_id"],
                    external_url=moved["url"],
                    message="Moved Drive file (mock).",
                )

            if action.type == "google.drive.search_files":
                hits = await self.search_files(query=action.payload["query"])
                return self._success(
                    action,
                    dry_run=dry_run,
                    external_id=f"search_{len(hits)}",
                    external_url=None,
                    message=f"Found {len(hits)} matches (mock).",
                )
        except KeyError as exc:
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="google_drive",
                status="failed",
                error=f"missing payload field: {exc}",
                message="Drive adapter rejected payload.",
            )

        return ExternalActionResult(
            action_id=action.action_id,
            target_system="google_drive",
            status="failed",
            error=f"unknown action type {action.type!r}",
            message="Drive adapter has no handler for this action type.",
        )

    @staticmethod
    def _success(
        action: ExternalAction,
        *,
        dry_run: bool,
        external_id: str,
        external_url: str | None,
        message: str,
    ) -> ExternalActionResult:
        return ExternalActionResult(
            action_id=action.action_id,
            target_system="google_drive",
            status="dry_run" if dry_run else "executed",
            external_id=external_id,
            external_url=external_url,
            message=message,
        )
