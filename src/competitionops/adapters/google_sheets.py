"""Google Sheets adapter — mock-first with real-mode switch (P1-002).

The Sprint 0 stateful mock survives unchanged for unit tests and
dry-run previews. P1-002 layers a real httpx-backed implementation
behind the same ``GoogleSheetsAdapter`` interface. Activation is
bearer-only (issue-1 pattern shared with Drive / Docs and pinned by
the AST guard in ``tests/test_google_workspace_adapters.py``):
``real_mode`` flips on iff ``Settings.google_oauth_access_token`` is
set. ``google_sheets_api_base`` is a configuration knob (operators
staging against a Sheets emulator override it) with a non-empty
prod-URL default — never falsy at runtime.

Real-mode operations:

- ``google.sheets.append_tracking_row`` / ``google.sheets.append_rows`` →
  ``POST {base}/v4/spreadsheets/{id}/values/{range}:append`` with
  ``valueInputOption=USER_ENTERED`` query param. Body is
  ``{"values": [[...]]}`` — a 2D array. Each row dict is serialised
  by ``dict.values()`` in insertion order. v1 assumes rows share keys
  in the same order (the planner emits this naturally); rows with
  heterogeneous keys would produce a misaligned matrix.
- ``google.sheets.update_cells`` →
  ``POST {base}/v4/spreadsheets/{id}/values:batchUpdate``. Body is
  ``{"valueInputOption": "USER_ENTERED", "data": [{"range": "A1",
  "values": [["v"]]}, …]}``. Each cell update is its own ``data``
  entry with a 1x1 ``values`` array.

Safety properties (mirror Plane / Drive / Docs):

- Deep-review C1 — ``dry_run=True`` short-circuits BEFORE any HTTP
  call and returns ``dry_run_<sha1(sheet_id)[:8]>``. CLAUDE.md rule #3.
- M8 + round-3 M4 — HTTPStatusError → ``safe_error_summary``;
  HTTPError + InvalidURL → ``safe_network_summary``. Row values
  and cell contents carry user content; leaking ``str(exc)`` would
  re-introduce M8 / M4 leak surface.

Out of scope:

- Idempotency. Sheets has no native dedup for append; re-running
  produces duplicate rows. Operators wire idempotency at the
  orchestrator (e.g., write the action_id into a hidden column and
  check before append).
- Column-key inference across heterogeneous row dicts.
- OAuth refresh — operator-side via the access token field.
- 429 backoff / retry.
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx

from competitionops.adapters._http_errors import (
    HTTP_TIMEOUT_SECONDS,
    safe_error_summary,
    safe_network_summary,
)
from competitionops.adapters.token_provider_google import TokenRefreshError
from competitionops.adapters.token_provider_static import StaticTokenProvider
from competitionops.config import Settings, get_settings
from competitionops.ports import TokenProvider
from competitionops.schemas import ExternalAction, ExternalActionResult

_APPEND_TYPES = frozenset(
    {"google.sheets.append_tracking_row", "google.sheets.append_rows"}
)
_UPDATE_TYPE = "google.sheets.update_cells"
_DEFAULT_RANGE = "Sheet1"


def _hash(text: str, length: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def _sheets_ui_url(spreadsheet_id: str) -> str:
    """User-facing Google Sheets URL for audit-link rendering."""
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


class GoogleSheetsAdapter:
    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        token_provider: TokenProvider | None = None,
    ) -> None:
        self.settings = settings if settings is not None else get_settings()
        self._injected_client = client
        # The registry injects the process-wide provider (static bearer
        # or refresh-backed). Direct construction without one falls back
        # to a static bearer derived from Settings — a test / dev
        # affordance; the refresh path is wired only via the registry.
        # An empty-string bearer counts as "no token" (mock mode), not a
        # real-mode bearer that would 401 every call.
        if token_provider is None:
            bearer = self.settings.google_oauth_access_token
            if bearer is not None and bearer.get_secret_value():
                token_provider = StaticTokenProvider(bearer.get_secret_value())
        self._token_provider = token_provider
        # Mock-mode state — used in tests, dry-run previews, and audit trail.
        self.sheets: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []

    @property
    def real_mode(self) -> bool:
        """Real REST mode is enabled iff a ``TokenProvider`` is wired.

        The registry injects a provider whenever the operator configured
        either a static bearer (``GOOGLE_OAUTH_ACCESS_TOKEN``) or a
        refresh-token trio; with neither, the provider is ``None`` and
        the adapter stays in deterministic mock mode. ``google_sheets_api_base``
        is a configuration knob (staging / emulator override), never a
        gate. Pinned structurally by
        ``test_real_mode_property_does_not_reference_api_base_attribute``.
        """
        return self._token_provider is not None

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
        self,
        *,
        sheet_id: str,
        rows: list[dict[str, Any]],
        sheet_range: str | None = None,
    ) -> dict[str, Any]:
        if self.real_mode:
            return await self._real_append_rows(
                sheet_id=sheet_id, rows=rows, sheet_range=sheet_range
            )
        return await self._mock_append_rows(sheet_id=sheet_id, rows=rows)

    async def update_cells(
        self, *, sheet_id: str, cell_updates: dict[str, Any]
    ) -> dict[str, Any]:
        if self.real_mode:
            return await self._real_update_cells(
                sheet_id=sheet_id, cell_updates=cell_updates
            )
        return await self._mock_update_cells(
            sheet_id=sheet_id, cell_updates=cell_updates
        )

    # ---- mock path ---------------------------------------------------

    async def _mock_append_rows(
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

    async def _mock_update_cells(
        self, *, sheet_id: str, cell_updates: dict[str, Any]
    ) -> dict[str, Any]:
        sheet = self._ensure_sheet(sheet_id)
        sheet["cells"].update(cell_updates)
        return {
            "sheet_id": sheet_id,
            "cells": dict(sheet["cells"]),
            "url": sheet["url"],
        }

    # ---- real path ---------------------------------------------------

    async def _real_append_rows(
        self,
        *,
        sheet_id: str,
        rows: list[dict[str, Any]],
        sheet_range: str | None,
    ) -> dict[str, Any]:
        """Real-mode append. Returns ``{sheet_id, row_count, url}``.

        Return-shape divergence from mock — the mock additionally
        tracks ``total_rows`` (accumulated across calls) from its
        in-process state; real mode has no such state. ``execute()``
        only reads ``sheet_id`` + ``url`` so the audit path is
        mode-agnostic. PR #27 review issue 4.
        """
        # PR #27 review issue 1 — guard against heterogeneous row keys
        # before serialising. ``dict.values()`` per row produces a 2D
        # matrix that silently misaligns when rows have different key
        # orders or different key sets: row 1's "name" can land under
        # row 2's "deadline" column. Sheets accepts the data; the PM
        # sees a corrupted tracker with no surface error. Fail loudly
        # at the adapter boundary instead.
        if rows:
            first_keys = tuple(rows[0].keys())
            for idx, row in enumerate(rows[1:], start=1):
                if tuple(row.keys()) != first_keys:
                    raise ValueError(
                        f"heterogeneous row keys at row {idx}: rows must "
                        "share the same key sequence (same order, same set) "
                        "to produce a column-aligned values matrix"
                    )
        s = self.settings
        assert self._token_provider is not None  # for mypy — real_mode guards
        token = await self._token_provider.get_access_token()
        effective_range = sheet_range or _DEFAULT_RANGE
        append_url = (
            s.google_sheets_api_base.rstrip("/")
            + f"/v4/spreadsheets/{sheet_id}/values/{effective_range}:append"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        values = [list(row.values()) for row in rows]
        async with self._client_session() as client:
            response = await client.post(
                append_url,
                params={"valueInputOption": "USER_ENTERED"},
                json={"values": values},
                headers=headers,
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()

        return {
            "sheet_id": sheet_id,
            "row_count": len(rows),
            "url": _sheets_ui_url(sheet_id),
        }

    async def _real_update_cells(
        self, *, sheet_id: str, cell_updates: dict[str, Any]
    ) -> dict[str, Any]:
        """Real-mode cell update. Returns ``{sheet_id, cells, url}``.

        Return-shape divergence from mock — the mock's ``cells`` field
        is the accumulated stateful map across calls (built up via
        ``_ensure_sheet`` + ``.update``); real mode returns ONLY the
        cells touched by this request. ``execute()`` reads only
        ``sheet_id`` + ``url`` so audit path is mode-agnostic. PR #27
        review issue 4 — same surface as Docs P1-001.
        """
        s = self.settings
        assert self._token_provider is not None  # for mypy — real_mode guards
        token = await self._token_provider.get_access_token()
        batchupdate_url = (
            s.google_sheets_api_base.rstrip("/")
            + f"/v4/spreadsheets/{sheet_id}/values:batchUpdate"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        # Each cell update becomes its own data entry with a 1x1 values array.
        data_entries = [
            {"range": cell_ref, "values": [[value]]}
            for cell_ref, value in cell_updates.items()
        ]
        async with self._client_session() as client:
            response = await client.post(
                batchupdate_url,
                json={
                    "valueInputOption": "USER_ENTERED",
                    "data": data_entries,
                },
                headers=headers,
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()

        return {
            "sheet_id": sheet_id,
            "cells": dict(cell_updates),
            "url": _sheets_ui_url(sheet_id),
        }

    # ---- ExternalActionExecutor dispatch ----------------------------

    async def execute(
        self, action: ExternalAction, dry_run: bool = True
    ) -> ExternalActionResult:
        self.calls.append(
            {"action_id": action.action_id, "type": action.type, "dry_run": dry_run}
        )
        # Deep-review C1 — real-mode must honor dry_run before touching
        # the network. The mock has no side effects so it's fine to keep
        # running through it during dry_run; only real mode needs the
        # short-circuit.
        if dry_run and self.real_mode and (
            action.type in _APPEND_TYPES or action.type == _UPDATE_TYPE
        ):
            return self._dry_run_preview(action)
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
                result = await self.append_rows(
                    sheet_id=sheet_id,
                    rows=rows_payload,
                    sheet_range=action.payload.get("range"),
                )
                return self._success(
                    action,
                    dry_run=dry_run,
                    external_id=result["sheet_id"],
                    external_url=result["url"],
                    message=(
                        f"Appended {result['row_count']} row(s) "
                        f"({self._mode_label()})."
                    ),
                )
            if action.type == _UPDATE_TYPE:
                result = await self.update_cells(
                    sheet_id=action.payload["sheet_id"],
                    cell_updates=action.payload["cell_updates"],
                )
                return self._success(
                    action,
                    dry_run=dry_run,
                    external_id=result["sheet_id"],
                    external_url=result["url"],
                    message=f"Updated cells ({self._mode_label()}).",
                )
        except TokenRefreshError as exc:
            # The TokenProvider could not supply an access token (refresh
            # token expired / revoked, OAuth endpoint down). ``exc`` carries
            # a pre-redacted summary — safe to surface verbatim.
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="google_sheets",
                status="failed",
                error=str(exc),
                message="Sheets adapter could not obtain an access token.",
            )
        except KeyError as exc:
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="google_sheets",
                status="failed",
                error=f"missing payload field: {exc}",
                message="Sheets adapter rejected payload.",
            )
        except ValueError as exc:
            # PR #27 review issue 1 — heterogeneous-row guard raises
            # before any HTTP call. ``str(exc)`` is adapter-authored
            # (lists row index + named constraint), safe to surface.
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="google_sheets",
                status="failed",
                error=str(exc),
                message="Sheets adapter rejected row payload shape.",
            )
        except httpx.HTTPStatusError as exc:
            # M8 — never echo the raw response body. Captive portals
            # and corporate proxies often interpose HTML 4xx / 5xx pages
            # on Google endpoints; safe_error_summary structures the
            # output and caps it at 200 chars.
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="google_sheets",
                status="failed",
                error=safe_error_summary(exc.response, target="sheets"),
                message="Sheets REST returned an error status.",
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # Round-2 M8 + round-3 M4 — drop ``str(exc)`` body; the
            # exception message embeds the request URL which carries
            # user content (spreadsheet id, range, cell values).
            # InvalidURL is its own exception class outside HTTPError,
            # so the tuple-catch is necessary.
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="google_sheets",
                status="failed",
                error=safe_network_summary(exc, target="sheets"),
                message="Sheets network error.",
            )

        return ExternalActionResult(
            action_id=action.action_id,
            target_system="google_sheets",
            status="failed",
            error=f"unknown action type {action.type!r}",
            message="Sheets adapter has no handler for this action type.",
        )

    def _dry_run_preview(self, action: ExternalAction) -> ExternalActionResult:
        """Synthetic preview for dry-run real-mode. Mirrors Plane / Drive / Docs.

        Deterministic over the resolved sheet_id (falls back to
        action_id when sheet_id is missing — same issue-5 fix applied
        in Docs). external_url is None — no real spreadsheet was
        touched.
        """
        key = action.payload.get("sheet_id") or action.action_id
        synthetic_id = f"dry_run_{_hash(str(key))}"
        return ExternalActionResult(
            action_id=action.action_id,
            target_system="google_sheets",
            status="dry_run",
            external_id=synthetic_id,
            external_url=None,
            message=f"Sheets preview ({self._mode_label()}, dry_run).",
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

    def _mode_label(self) -> str:
        return "real" if self.real_mode else "mock"

    @asynccontextmanager
    async def _client_session(self) -> AsyncIterator[httpx.AsyncClient]:
        """Yield the injected client (test) or a freshly-managed one (prod)."""
        if self._injected_client is not None:
            yield self._injected_client
            return
        async with httpx.AsyncClient() as client:
            yield client
