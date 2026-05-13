"""Mock-first Google Docs adapter — no network, no credentials."""

from __future__ import annotations

import hashlib
from typing import Any

from competitionops.schemas import ExternalAction, ExternalActionResult

_CREATE_TYPES = frozenset(
    {"google.docs.create_proposal_outline", "google.docs.create_doc"}
)


def _hash(text: str, length: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


class GoogleDocsAdapter:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []

    # ---- high-level operations --------------------------------------

    async def create_doc(
        self, *, title: str, sections: list[str] | None = None
    ) -> dict[str, Any]:
        doc_id = f"mock_doc_{_hash(title)}"
        if doc_id not in self.docs:
            self.docs[doc_id] = {
                "id": doc_id,
                "title": title,
                "sections": list(sections or []),
                "body": {section: "" for section in (sections or [])},
                "url": f"https://docs.example.invalid/d/{doc_id}",
            }
        return self.docs[doc_id]

    async def append_section(
        self, *, doc_id: str, heading: str, body: str = ""
    ) -> dict[str, Any]:
        doc = self.docs.get(doc_id)
        if doc is None:
            raise KeyError(f"doc {doc_id!r} not found")
        if heading not in doc["sections"]:
            doc["sections"].append(heading)
        doc["body"][heading] = body
        return doc

    # ---- dispatch ----------------------------------------------------

    async def execute(
        self, action: ExternalAction, dry_run: bool = True
    ) -> ExternalActionResult:
        self.calls.append(
            {"action_id": action.action_id, "type": action.type, "dry_run": dry_run}
        )
        try:
            if action.type in _CREATE_TYPES:
                doc = await self.create_doc(
                    title=action.payload["title"],
                    sections=action.payload.get("sections"),
                )
                return self._success(
                    action,
                    dry_run=dry_run,
                    external_id=doc["id"],
                    external_url=doc["url"],
                    message="Created Doc (mock).",
                )
            if action.type == "google.docs.append_section":
                doc = await self.append_section(
                    doc_id=action.payload["doc_id"],
                    heading=action.payload["heading"],
                    body=action.payload.get("body", ""),
                )
                return self._success(
                    action,
                    dry_run=dry_run,
                    external_id=doc["id"],
                    external_url=doc["url"],
                    message="Appended Doc section (mock).",
                )
        except KeyError as exc:
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="google_docs",
                status="failed",
                error=f"missing payload field: {exc}",
                message="Docs adapter rejected payload.",
            )

        return ExternalActionResult(
            action_id=action.action_id,
            target_system="google_docs",
            status="failed",
            error=f"unknown action type {action.type!r}",
            message="Docs adapter has no handler for this action type.",
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
            target_system="google_docs",
            status="dry_run" if dry_run else "executed",
            external_id=external_id,
            external_url=external_url,
            message=message,
        )
