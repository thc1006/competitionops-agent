"""Mock-first Google Sheets adapter — no network, no credentials."""

from __future__ import annotations

import hashlib
from typing import Any

from competitionops.schemas import ExternalAction, ExternalActionResult

_APPEND_TYPES = frozenset(
    {"google.sheets.append_tracking_row", "google.sheets.append_rows"}
)


def _hash(text: str, length: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


class GoogleSheetsAdapter:
    def __init__(self) -> None:
        self.sheets: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []

    # ---- high-level operations --------------------------------------

    def _ensure_sheet(self, sheet_id: str) -> dict[str, Any]:
        if sheet_id not in self.sheets:
            self.sheets[sheet_id] = {
                "sheet_id": sheet_id,
                "rows": [],
                "cells": {},
                "url": f"https://sheets.example.invalid/d/{sheet_id}",
            }
        return self.sheets[sheet_id]

    async def append_rows(
        self, *, sheet_id: str, rows: list[dict[str, Any]]
    ) -> dict[str, Any]:
        sheet = self._ensure_sheet(sheet_id)
        sheet["rows"].extend(rows)
        return {
            "sheet_id": sheet_id,
            "row_count": len(rows),
            "total_rows": len(sheet["rows"]),
            "url": sheet["url"],
        }

    async def update_cells(
        self, *, sheet_id: str, cell_updates: dict[str, Any]
    ) -> dict[str, Any]:
        sheet = self._ensure_sheet(sheet_id)
        sheet["cells"].update(cell_updates)
        return {
            "sheet_id": sheet_id,
            "cells": dict(sheet["cells"]),
            "url": sheet["url"],
        }

    # ---- dispatch ----------------------------------------------------

    async def execute(
        self, action: ExternalAction, dry_run: bool = True
    ) -> ExternalActionResult:
        self.calls.append(
            {"action_id": action.action_id, "type": action.type, "dry_run": dry_run}
        )
        try:
            if action.type in _APPEND_TYPES:
                rows_payload = action.payload.get("rows")
                if rows_payload is None and "row" in action.payload:
                    rows_payload = [action.payload["row"]]
                if not rows_payload:
                    raise KeyError("rows")
                sheet_id = (
                    action.payload.get("sheet_id")
                    or f"tracker_{_hash(str(action.payload.get('competition_id', '')))}"
                )
                result = await self.append_rows(sheet_id=sheet_id, rows=rows_payload)
                return self._success(
                    action,
                    dry_run=dry_run,
                    external_id=result["sheet_id"],
                    external_url=result["url"],
                    message=f"Appended {result['row_count']} row(s) (mock).",
                )
            if action.type == "google.sheets.update_cells":
                result = await self.update_cells(
                    sheet_id=action.payload["sheet_id"],
                    cell_updates=action.payload["cell_updates"],
                )
                return self._success(
                    action,
                    dry_run=dry_run,
                    external_id=result["sheet_id"],
                    external_url=result["url"],
                    message="Updated cells (mock).",
                )
        except KeyError as exc:
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="google_sheets",
                status="failed",
                error=f"missing payload field: {exc}",
                message="Sheets adapter rejected payload.",
            )

        return ExternalActionResult(
            action_id=action.action_id,
            target_system="google_sheets",
            status="failed",
            error=f"unknown action type {action.type!r}",
            message="Sheets adapter has no handler for this action type.",
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
            target_system="google_sheets",
            status="dry_run" if dry_run else "executed",
            external_id=external_id,
            external_url=external_url,
            message=message,
        )
