"""Google Drive adapter — mock-first with real-mode switch (P1-005).

The Stage 4 stateful mock survives unchanged for unit tests and dry-run
previews. P1-005 layers a real httpx-backed implementation behind the
exact same ``GoogleDriveAdapter`` interface so domain code does not
notice the switch. Activation is binary: real mode flips on only when
both ``Settings.google_oauth_access_token`` and
``Settings.google_drive_api_base`` are present. Partial config falls
back to the mock to avoid surprise half-real behaviour.

Real-mode contract (folder creation only — the highest-frequency op
for the planner; ``move_file`` / ``search_files`` stay mock until a
later sprint):

- ``POST {base}/drive/v3/files`` with ``Authorization: Bearer <token>``
  and a JSON body ``{name, mimeType: application/vnd.google-apps.folder,
  parents?: [parent_id]}``.
- Tier 0 #5 — Before each POST, the adapter issues
  ``GET {base}/drive/v3/files?q=...`` to find an existing folder with
  the same name under the same parent. On a hit, that folder is
  returned and no POST is issued (idempotent re-approval).
- Search failures (4xx/5xx, network, malformed JSON) fall through to
  POST rather than blocking — same degradation contract as Plane.
- Deep-review C1 — dry_run must be honored. ``execute()`` short-circuits
  before any HTTP call when ``dry_run=True`` and returns a synthetic
  ``dry_run_<hash>`` external_id so audit/preview still has something
  to display.
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx

from competitionops.adapters._http_errors import (
    safe_error_summary,
    safe_network_summary,
)
from competitionops.config import Settings, get_settings
from competitionops.schemas import ExternalAction, ExternalActionResult

_FOLDER_TYPES = frozenset(
    {"google.drive.create_competition_folder", "google.drive.create_folder"}
)
_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
_DRIVE_FILES_PATH = "/drive/v3/files"
_HTTP_TIMEOUT_SECONDS = 30.0


def _hash(text: str, length: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def _escape_drive_query_literal(value: str) -> str:
    """Escape a string literal for Drive's ``q=`` search syntax.

    Drive uses single-quoted literals and escapes ``\\`` and ``'``.
    Reference: https://developers.google.com/drive/api/guides/search-files
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveAdapter:
    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings if settings is not None else get_settings()
        self._injected_client = client
        # Mock-mode state — used in tests, dry-run previews, and audit trail.
        self.folders: dict[str, dict[str, Any]] = {}
        self.files: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []

    @property
    def real_mode(self) -> bool:
        """Real REST mode is enabled iff ``google_oauth_access_token`` is set.

        ``google_drive_api_base`` defaults to the prod URL and is validated
        as non-empty at Settings construction (the URL validator rejects
        ``""`` / ``None``), so it is never falsy at runtime — it is a
        configuration knob, not a gate. Earlier revisions wrote this as
        ``bool(token) and bool(base)``, implying both were required;
        the second clause was dead code (review issue 1). The single-
        condition form below matches the actual contract operators
        observe."""
        return bool(self.settings.google_oauth_access_token)

    # ---- high-level operations -------------------------------------

    async def create_folder(
        self, *, name: str, parent_id: str | None = None
    ) -> dict[str, Any]:
        if self.real_mode:
            return await self._real_create_folder(name=name, parent_id=parent_id)
        return await self._mock_create_folder(name=name, parent_id=parent_id)

    async def _mock_create_folder(
        self, *, name: str, parent_id: str | None
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

    async def _real_create_folder(
        self, *, name: str, parent_id: str | None
    ) -> dict[str, Any]:
        s = self.settings
        assert s.google_oauth_access_token is not None  # for mypy
        token = s.google_oauth_access_token.get_secret_value()
        list_url = s.google_drive_api_base.rstrip("/") + _DRIVE_FILES_PATH
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        body: dict[str, Any] = {"name": name, "mimeType": _FOLDER_MIME_TYPE}
        if parent_id:
            body["parents"] = [parent_id]

        async with self._client_session() as client:
            existing = await self._search_existing_folder(
                client, list_url, name=name, parent_id=parent_id, headers=headers
            )
            if existing is not None:
                return self._with_url(existing)
            response = await client.post(
                list_url,
                json=body,
                headers=headers,
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            return self._with_url(data)

    async def _search_existing_folder(
        self,
        client: httpx.AsyncClient,
        list_url: str,
        *,
        name: str,
        parent_id: str | None,
        headers: dict[str, str],
    ) -> dict[str, Any] | None:
        """Return the matching Drive folder or None.

        Failure modes (HTTP error status, network errors, malformed JSON)
        all degrade to "no match" so the caller falls through to POST.
        """
        clauses = [
            f"name = '{_escape_drive_query_literal(name)}'",
            f"mimeType = '{_FOLDER_MIME_TYPE}'",
            "trashed = false",
        ]
        if parent_id:
            clauses.append(f"'{_escape_drive_query_literal(parent_id)}' in parents")
        query = " and ".join(clauses)
        try:
            response = await client.get(
                list_url,
                params={"q": query, "fields": "files(id,name,mimeType,parents)"},
                headers=headers,
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        files = payload.get("files")
        if not isinstance(files, list):
            return None
        for item in files:
            if isinstance(item, dict) and item.get("name") == name:
                return item
        return None

    def _with_url(self, data: dict[str, Any]) -> dict[str, Any]:
        """Ensure the folder dict has a ``url`` for audit-link rendering."""
        folder_id = data.get("id", "")
        data.setdefault("url", f"https://drive.google.com/drive/folders/{folder_id}")
        return data

    @asynccontextmanager
    async def _client_session(self) -> AsyncIterator[httpx.AsyncClient]:
        """Yield the injected client (test) or a freshly-managed one (prod)."""
        if self._injected_client is not None:
            yield self._injected_client
            return
        async with httpx.AsyncClient() as client:
            yield client

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
        # Deep-review C1 — real-mode adapters MUST honor dry_run before
        # touching the network. The mock has no side effects so it's
        # fine to keep running through it during dry_run; only real mode
        # needs the short-circuit.
        if dry_run and self.real_mode and action.type in _FOLDER_TYPES:
            return self._dry_run_preview(action)
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
                    external_url=folder.get("url"),
                    message=f"Created Drive folder ({self._mode_label()}).",
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
                    external_url=moved.get("url"),
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
        except httpx.HTTPStatusError as exc:
            # M8 — never echo the raw response body. ``safe_error_summary``
            # surfaces structured JSON error fields ONLY and falls back to
            # ``<status> <reason>`` for HTML / opaque payloads. Captive
            # portals and corporate proxies often interpose HTML 4xx / 5xx
            # pages on Drive endpoints; this prevents their body content
            # from leaking into the audit log.
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="google_drive",
                status="failed",
                error=safe_error_summary(exc.response, target="drive"),
                message="Drive REST returned an error status.",
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # Round-2 M8 — ``str(exc)`` in httpx exceptions typically
            # embeds the request URL, and Drive's search URL embeds
            # ``q=name='<folder_name>'``. A copy-pasted secret in a
            # folder name would leak via that branch.
            # ``safe_network_summary`` keeps the class name (operator
            # signal) and drops the body entirely.
            #
            # Round-3 M4 — ``httpx.InvalidURL`` does NOT subclass
            # ``httpx.HTTPError`` (parent is plain ``Exception``).
            # Without this tuple-catch, a folder name that broke URL
            # parsing (raw newlines, control characters) raised
            # ``InvalidURL`` uncaught, leaking the URL chunk via the
            # FastAPI 500 traceback.
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="google_drive",
                status="failed",
                error=safe_network_summary(exc, target="drive"),
                message="Drive network error.",
            )

        return ExternalActionResult(
            action_id=action.action_id,
            target_system="google_drive",
            status="failed",
            error=f"unknown action type {action.type!r}",
            message="Drive adapter has no handler for this action type.",
        )

    def _dry_run_preview(self, action: ExternalAction) -> ExternalActionResult:
        """Build a synthetic preview without hitting the network.

        The external_id is hashed off the folder name so the same plan
        produces a stable preview id across re-runs — useful for the
        approval UI showing PMs which folder will be created.

        Cross-adapter contract — Plane's ``_dry_run_preview`` shares
        the same shape, and ``tests/test_dry_run_contract.py`` pins
        the contract (status, id prefix, None URL, message suffix).
        Any change to the format below must be paired with the test
        update; a drift would fail CI and surface during PR review.
        """
        name = (
            action.payload.get("folder_name")
            or action.payload.get("competition_name")
            or action.payload.get("name")
            or "Untitled"
        )
        parent_key = action.payload.get("parent_id") or "root"
        preview_id = f"dry_run_{_hash(f'{name}|{parent_key}')}"
        return ExternalActionResult(
            action_id=action.action_id,
            target_system="google_drive",
            status="dry_run",
            external_id=preview_id,
            external_url=None,
            message=f"Drive folder preview ({self._mode_label()}, dry_run).",
        )

    def _mode_label(self) -> str:
        return "real" if self.real_mode else "mock"

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
