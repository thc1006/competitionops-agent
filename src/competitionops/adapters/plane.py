"""Plane.so REST adapter — mock-first with real-mode switch (P1-004).

Stage 4 pattern: stateful mock by default, real httpx-backed REST when
all four Plane Settings fields are configured (``plane_base_url``,
``plane_api_key``, ``plane_workspace_slug``, ``plane_project_id``).
Either-or — partial config falls back to mock to avoid surprise
half-real behavior.

Mock mode:
- ``create_issue`` returns deterministic ``mock_issue_<sha1[:8]>`` ids
  derived from the title, so re-running the planner does not duplicate
  audit trails (same idempotency contract as the Stage 4 Google mocks).
- URLs use the ``plane.example.invalid`` RFC 2606 reserved suffix so
  even if the URL is followed by accident, no real service is hit.

Real mode:
- ``POST {base_url}/api/v1/workspaces/{slug}/projects/{project_id}/issues/``
  with ``X-API-Key`` auth (Plane's documented convention) and a JSON
  ``{name, description_html}`` body.
- Response ``id`` becomes ``target_external_id`` in the audit log,
  closing Tier 0 #3.

Idempotency (Tier 0 #5):
- Before each create, the real adapter issues
  ``GET .../issues/?search={title}`` and returns the existing issue if
  its ``name`` matches exactly. Only the no-match case falls through to
  POST. So a repeat approval with ``allow_reexecute=true`` no longer
  duplicates the Plane issue — it surfaces the original id.
- The search step degrades gracefully: if Plane responds 4xx/5xx on the
  GET (e.g. search disabled on some self-hosted instances) we fall
  through to POST rather than blocking the entire flow.

Out of scope for this commit:
- 429 / 5xx retry + exponential backoff on the POST itself.

The httpx client is injectable through ``__init__(client=...)`` so tests
use ``httpx.MockTransport`` and never touch the network.
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx

from competitionops.config import Settings, get_settings
from competitionops.schemas import ExternalAction, ExternalActionResult

_PLANE_API_PATH = "/api/v1/workspaces/{slug}/projects/{project_id}/issues/"
_HTTP_TIMEOUT_SECONDS = 30.0


def _hash(text: str, length: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


class PlaneAdapter:
    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings if settings is not None else get_settings()
        self._injected_client = client
        # Mock-mode state — used in tests and audit-trail inspection.
        self.issues: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []

    @property
    def real_mode(self) -> bool:
        """Real REST mode is enabled when all four Plane Settings are set."""
        s = self.settings
        return all(
            [
                bool(s.plane_base_url),
                bool(s.plane_api_key),
                bool(s.plane_workspace_slug),
                bool(s.plane_project_id),
            ]
        )

    # ---- High-level operation ----

    async def create_issue(
        self,
        *,
        title: str,
        description: str = "",
        owner_role: str | None = None,
    ) -> dict[str, Any]:
        if self.real_mode:
            return await self._real_create_issue(
                title=title, description=description, owner_role=owner_role
            )
        return await self._mock_create_issue(
            title=title, description=description, owner_role=owner_role
        )

    async def _mock_create_issue(
        self,
        *,
        title: str,
        description: str,
        owner_role: str | None,
    ) -> dict[str, Any]:
        issue_id = f"mock_issue_{_hash(title)}"
        if issue_id not in self.issues:
            self.issues[issue_id] = {
                "id": issue_id,
                "name": title,
                "description": description,
                "owner_role": owner_role,
                "url": f"https://plane.example.invalid/issues/{issue_id}",
            }
        return self.issues[issue_id]

    async def _real_create_issue(
        self,
        *,
        title: str,
        description: str,
        owner_role: str | None,
    ) -> dict[str, Any]:
        s = self.settings
        # ``real_mode`` already verified non-None; assert for mypy.
        assert s.plane_base_url is not None
        assert s.plane_api_key is not None
        assert s.plane_workspace_slug is not None
        assert s.plane_project_id is not None

        api_key = s.plane_api_key.get_secret_value()
        list_url = (
            s.plane_base_url.rstrip("/")
            + _PLANE_API_PATH.format(
                slug=s.plane_workspace_slug, project_id=s.plane_project_id
            )
        )
        description_html = description
        if owner_role:
            description_html = (
                f"{description_html}\n\nOwner role: {owner_role}".strip()
            )
        headers = {
            "X-API-Key": api_key,
            "Accept": "application/json",
        }

        async with self._client_session() as client:
            # Tier 0 #5 — query-then-create idempotency.
            existing = await self._search_existing_issue(
                client, list_url, title, headers
            )
            if existing is not None:
                return self._with_url(existing)

            response = await client.post(
                list_url,
                json={"name": title, "description_html": description_html},
                headers=headers,
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            return self._with_url(data)

    async def _search_existing_issue(
        self,
        client: httpx.AsyncClient,
        list_url: str,
        title: str,
        headers: dict[str, str],
    ) -> dict[str, Any] | None:
        """Return the existing Plane issue with ``name == title`` or None.

        Search-step failures (4xx/5xx, malformed JSON, network) fall through
        to POST rather than blocking the entire create. Self-hosted Plane
        instances with search disabled therefore still work — they just lose
        the idempotency guarantee for that one call.
        """
        try:
            response = await client.get(
                list_url,
                params={"search": title},
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
        # Plane variants: bare ``[...]`` or ``{"results": [...], ...}``.
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            raw_items = payload.get("results", [])
            items = raw_items if isinstance(raw_items, list) else []
        else:
            return None
        for item in items:
            if isinstance(item, dict) and item.get("name") == title:
                return item
        return None

    def _with_url(self, data: dict[str, Any]) -> dict[str, Any]:
        """Ensure the issue dict has a ``url`` for audit-link rendering."""
        s = self.settings
        assert s.plane_base_url is not None
        data.setdefault(
            "url",
            f"{s.plane_base_url.rstrip('/')}/{s.plane_workspace_slug}"
            f"/projects/{s.plane_project_id}/issues/{data.get('id', '')}",
        )
        return data

    @asynccontextmanager
    async def _client_session(self) -> AsyncIterator[httpx.AsyncClient]:
        """Yield the injected client (test) or a freshly-managed one (prod)."""
        if self._injected_client is not None:
            yield self._injected_client
            return
        async with httpx.AsyncClient() as client:
            yield client

    # ---- Executor protocol dispatch ----

    async def execute(
        self, action: ExternalAction, dry_run: bool = True
    ) -> ExternalActionResult:
        self.calls.append(
            {"action_id": action.action_id, "type": action.type, "dry_run": dry_run}
        )
        try:
            if action.type == "plane.create_issue":
                payload = action.payload
                issue = await self.create_issue(
                    title=payload["title"],
                    description=payload.get("description", ""),
                    owner_role=payload.get("owner_role"),
                )
                return ExternalActionResult(
                    action_id=action.action_id,
                    target_system="plane",
                    status="dry_run" if dry_run else "executed",
                    external_id=str(issue.get("id", "")) or None,
                    external_url=issue.get("url"),
                    message=f"Created Plane issue ({self._mode_label()}).",
                )
        except KeyError as exc:
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="plane",
                status="failed",
                error=f"missing payload field: {exc}",
                message="Plane adapter rejected payload.",
            )
        except httpx.HTTPStatusError as exc:
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="plane",
                status="failed",
                error=(
                    f"plane api status {exc.response.status_code}: "
                    f"{exc.response.text[:200]}"
                ),
                message="Plane REST returned an error status.",
            )
        except httpx.HTTPError as exc:
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="plane",
                status="failed",
                error=f"plane network error: {type(exc).__name__}: {exc}",
                message="Plane network error.",
            )

        return ExternalActionResult(
            action_id=action.action_id,
            target_system="plane",
            status="failed",
            error=f"unknown action type {action.type!r}",
            message="Plane adapter has no handler for this action type.",
        )

    def _mode_label(self) -> str:
        return "real" if self.real_mode else "mock"
