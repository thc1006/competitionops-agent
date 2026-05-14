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
- The search step degrades gracefully: if Plane responds with a
  non-auth 4xx / 5xx on the GET (e.g. search disabled on some
  self-hosted instances) we fall through to POST rather than blocking
  the entire flow.

Search-step hardening (M7):
- The ``search`` query parameter is capped at
  ``_SEARCH_QUERY_MAX_CHARS`` (512) so a pathologically long title
  cannot push the GET URL past typical proxy / origin limits (~8 KiB
  after URL-encoding). The full title still lands in the POST body
  and in the exact-match comparison on the search response, so
  idempotency holds for legitimate cases.
- Auth failures during search (401 / 403) propagate immediately as
  ``httpx.HTTPStatusError`` — falling through to POST would just
  produce the same 401 / 403 from a less specific code path. The
  audit record surfaces the real failure point.

Out of scope for this commit:
- 429 / 5xx retry + exponential backoff on the POST itself.

Dry-run gate (C1):
- Real mode short-circuits ``dry_run=True`` BEFORE any HTTP call and
  returns a synthetic ``dry_run_<sha1(title)[:8]>`` external_id. This
  closes the C1 finding where ``Settings.dry_run_default=True`` could
  silently write to Plane on the very first preview. Mock mode has no
  side effects so it still flows through ``_mock_create_issue``
  unchanged — preserves the deterministic ``mock_issue_*`` ids existing
  audit fixtures depend on.

The httpx client is injectable through ``__init__(client=...)`` so tests
use ``httpx.MockTransport`` and never touch the network.
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

_PLANE_API_PATH = "/api/v1/workspaces/{slug}/projects/{project_id}/issues/"
_HTTP_TIMEOUT_SECONDS = 30.0
# M7 — Cap the ``search`` query parameter to keep the GET URL well under
# typical proxy / origin limits (~8 KiB) even after URL-encoding picks
# up multi-byte unicode in competition titles. 512 chars × 4 bytes max
# UTF-8 × ~3x URL-encoding overhead ≈ 6 KiB, leaves headroom for the
# rest of the URL (base, path, other query params, headers proxies
# sometimes count toward the same budget).
_SEARCH_QUERY_MAX_CHARS = 512
# M7 — Auth-class failures during search are NOT recoverable by
# falling through to POST (the POST will hit the same 401/403 with
# less context). Surface them immediately so audit records the real
# failure point. Other 4xx / 5xx keep degrading to POST so a self-
# hosted Plane with search disabled still creates issues.
_AUTH_FAILURE_STATUSES = frozenset({401, 403})


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

        Search behaviour (M7 hardening):

        - The ``search`` query parameter is truncated to
          ``_SEARCH_QUERY_MAX_CHARS`` so a long title cannot blow up the
          GET URL past typical proxy / origin limits. The full title is
          still compared exactly against each returned issue's ``name``,
          so this only loses idempotency for the pathological case of
          two long titles sharing the first 512 chars (which we resolve
          by falling through to POST anyway).
        - Auth failures (401 / 403) raise immediately instead of
          degrading to POST. Falling through would just produce the
          same auth failure from a less specific code path.
        - Other 4xx / 5xx / malformed JSON / network errors keep
          degrading to POST — self-hosted Plane instances with search
          disabled still work, they just lose idempotency for that one
          call.
        """
        search_value = (
            title if len(title) <= _SEARCH_QUERY_MAX_CHARS
            else title[:_SEARCH_QUERY_MAX_CHARS]
        )
        try:
            response = await client.get(
                list_url,
                params={"search": search_value},
                headers=headers,
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            return None
        if response.status_code in _AUTH_FAILURE_STATUSES:
            # Propagates as httpx.HTTPStatusError → execute() catches
            # it in the same branch as a 401/403 on POST and surfaces
            # a clean ``status="failed"`` with the real status code.
            response.raise_for_status()
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
        # C1 — Real-mode adapters MUST honor dry_run BEFORE any HTTP call.
        # Mock mode has no side effects so it stays on the normal path.
        if dry_run and self.real_mode and action.type == "plane.create_issue":
            title = action.payload.get("title")
            if title:
                return self._dry_run_preview(action, title=title)
            # Fall through so the KeyError path below reports the
            # missing-title error consistently.
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
            # M8 — never echo the raw response body. ``safe_error_summary``
            # surfaces structured JSON error fields ONLY and falls back to
            # ``<status> <reason>`` for HTML / opaque payloads so a
            # self-hosted Plane 500 page cannot leak stack traces /
            # internal hostnames into the audit log.
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="plane",
                status="failed",
                error=safe_error_summary(exc.response, target="plane"),
                message="Plane REST returned an error status.",
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # Round-2 M8 — ``str(exc)`` in httpx exceptions typically
            # embeds the request URL, and Plane's search URL embeds
            # ``search=<issue_title>``. A copy-pasted secret in a title
            # would leak via that branch. ``safe_network_summary``
            # keeps the class name (operator signal) and drops the
            # body entirely.
            #
            # Round-3 M4 — ``httpx.InvalidURL`` does NOT inherit from
            # ``httpx.HTTPError``; it sits on plain ``Exception``. Before
            # this widening the ``InvalidURL`` raised by httpx on a
            # malformed URL (e.g. user-supplied workspace_slug with raw
            # newlines) propagated uncaught out of ``execute`` — the
            # FastAPI handler then echoed the URL fragment into its 500
            # response, re-introducing the M8 leak through a different
            # exception. Tuple-catch closes both at once.
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="plane",
                status="failed",
                error=safe_network_summary(exc, target="plane"),
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

    def _dry_run_preview(
        self, action: ExternalAction, *, title: str
    ) -> ExternalActionResult:
        """Build a synthetic preview without hitting Plane.

        The external_id is hashed off the title so the same plan
        produces a stable preview id across re-runs — handy for the
        approval UI showing PMs which issue would be created.

        Cross-adapter contract — Drive's ``_dry_run_preview`` shares
        the same shape, and ``tests/test_dry_run_contract.py`` pins
        the contract (status, id prefix, None URL, message suffix).
        Any change to the format below must be paired with the test
        update; a drift would fail CI and surface during PR review.
        """
        preview_id = f"dry_run_{_hash(title)}"
        return ExternalActionResult(
            action_id=action.action_id,
            target_system="plane",
            status="dry_run",
            external_id=preview_id,
            external_url=None,
            message=f"Plane issue preview ({self._mode_label()}, dry_run).",
        )
