"""Google Docs adapter — mock-first with real-mode switch (P1-001).

The Sprint 0 stateful mock survives unchanged for unit tests and
dry-run previews. P1-001 layers a real httpx-backed implementation
behind the exact same ``GoogleDocsAdapter`` interface so domain
code does not notice the switch. Activation is binary: real mode
flips on only when both ``Settings.google_oauth_access_token`` AND
``Settings.google_docs_api_base`` are present (the latter has a
prod-URL default, so a bearer alone is enough). Partial config
keeps the mock — same defensive contract as Plane / Drive.

Real-mode contract:

- ``POST {base}/v1/documents`` (Docs ``documents.create``) with
  ``Authorization: Bearer <token>`` and JSON body ``{"title": ...}``.
- If the create action carries ``sections``, the adapter follows
  with ``POST {base}/v1/documents/{documentId}:batchUpdate`` to
  insert each section heading at the end of the body.
- ``append_section`` maps to the same ``batchUpdate`` endpoint with
  an ``insertText`` at ``endOfSegmentLocation`` — heading then body.
- Deep-review C1 — ``dry_run=True`` short-circuits BEFORE any HTTP
  call and returns a synthetic ``dry_run_<sha1(title)[:8]>`` preview.
  ``Settings.dry_run_default=True`` is the hot path; silent writes
  here would violate CLAUDE.md rule #3.
- Error redaction: HTTPStatusError goes through ``safe_error_summary``
  (M8), all other ``httpx.HTTPError`` and ``httpx.InvalidURL``
  (round-3 M4) go through ``safe_network_summary``. Document titles
  / section bodies can carry user content, so leaking ``str(exc)``
  back to the audit log would re-introduce the M8 / M4 leaks.

Out of scope:

- Cross-API idempotency via Drive ``files.list``. The Docs REST API
  has no native name lookup, and doing the search through Drive
  would couple this adapter to Drive auth scope + a parent_id the
  current ``ExternalAction`` payload doesn't carry. Re-creating a
  doc with the same title produces a new ``documentId`` — operators
  who care must wire Drive search at the orchestrator level (a
  follow-up sprint).
- OAuth refresh — operators wire a short-lived bearer via
  ``Settings.google_oauth_access_token``.
- 429 backoff / retry — relies on caller-side retry today.
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

_CREATE_TYPES = frozenset(
    {"google.docs.create_proposal_outline", "google.docs.create_doc"}
)
_APPEND_TYPE = "google.docs.append_section"
_DOCS_CREATE_PATH = "/v1/documents"
_HTTP_TIMEOUT_SECONDS = 30.0


def _hash(text: str, length: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def _docs_ui_url(document_id: str) -> str:
    """User-facing Google Docs URL for audit-link rendering."""
    return f"https://docs.google.com/document/d/{document_id}/edit"


class _MissingDocumentIdError(Exception):
    """Raised when Docs ``documents.create`` returns 200 without a
    ``documentId`` (upstream contract violation). Caught inside
    ``execute()`` to convert into ``ExternalActionResult.failed``
    without losing the named diagnostic. Review issue 3."""


class GoogleDocsAdapter:
    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings if settings is not None else get_settings()
        self._injected_client = client
        # Mock-mode state — used in tests, dry-run previews, and audit trail.
        self.docs: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []

    @property
    def real_mode(self) -> bool:
        """Real REST mode is enabled iff ``google_oauth_access_token`` is set.

        ``google_docs_api_base`` defaults to the prod URL and is
        non-empty at Settings construction (Pydantic's ``str`` type
        check rejects ``None``; the URL validator raises on ``""``),
        so it is never falsy at runtime — it is a configuration knob
        (operators staging against a Docs-emulator override it), not
        a gate. Earlier revisions wrote this as
        ``bool(token) and bool(base)``, implying both were required;
        the second clause was dead code (review issue 1). The single-
        condition form below matches the actual contract operators
        observe. Pinned structurally by
        ``test_real_mode_property_does_not_reference_api_base_attribute``.
        """
        return bool(self.settings.google_oauth_access_token)

    # ---- high-level operations --------------------------------------

    async def create_doc(
        self, *, title: str, sections: list[str] | None = None
    ) -> dict[str, Any]:
        if self.real_mode:
            return await self._real_create_doc(title=title, sections=sections)
        return await self._mock_create_doc(title=title, sections=sections)

    async def append_section(
        self, *, doc_id: str, heading: str, body: str = ""
    ) -> dict[str, Any]:
        if self.real_mode:
            return await self._real_append_section(
                doc_id=doc_id, heading=heading, body=body
            )
        return await self._mock_append_section(
            doc_id=doc_id, heading=heading, body=body
        )

    # ---- mock path ---------------------------------------------------

    async def _mock_create_doc(
        self, *, title: str, sections: list[str] | None
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

    async def _mock_append_section(
        self, *, doc_id: str, heading: str, body: str
    ) -> dict[str, Any]:
        doc = self.docs.get(doc_id)
        if doc is None:
            raise KeyError(f"doc {doc_id!r} not found")
        if heading not in doc["sections"]:
            doc["sections"].append(heading)
        doc["body"][heading] = body
        return doc

    # ---- real path ---------------------------------------------------

    async def _real_create_doc(
        self, *, title: str, sections: list[str] | None
    ) -> dict[str, Any]:
        s = self.settings
        assert s.google_oauth_access_token is not None  # for mypy
        token = s.google_oauth_access_token.get_secret_value()
        create_url = s.google_docs_api_base.rstrip("/") + _DOCS_CREATE_PATH
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

        async with self._client_session() as client:
            response = await client.post(
                create_url,
                json={"title": title},
                headers=headers,
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            # Review issue 3 — a 200 response without ``documentId`` is
            # a Docs-shim contract violation. Surface it loudly with a
            # named error rather than silently producing ``external_id=""``
            # + a broken ``…/document/d//edit`` UI URL. ``execute()``
            # catches this to keep the adapter contract (always return
            # ExternalActionResult).
            document_id = data.get("documentId")
            if not document_id:
                raise _MissingDocumentIdError(
                    "Docs documents.create response missing documentId"
                )

            result: dict[str, Any] = {
                "id": document_id,
                "title": data.get("title", title),
                "sections": list(sections or []),
                "url": _docs_ui_url(document_id),
            }

            if sections:
                # Two-step contract: create returns an empty doc body,
                # batchUpdate inserts the section headings at the end.
                #
                # Review issue 2 — the doc has ALREADY been created on
                # Google's side. If batchUpdate fails, raising would
                # lose the documentId and produce a ``failed`` audit
                # record that doesn't link to the stray empty doc.
                # Instead capture the failure as a structured
                # ``partial_failure`` field; the dispatcher converts it
                # to ``status=failed`` while keeping ``external_id`` /
                # ``external_url`` so PMs can find and clean up.
                batchupdate_url = (
                    s.google_docs_api_base.rstrip("/")
                    + f"/v1/documents/{document_id}:batchUpdate"
                )
                try:
                    batch_response = await client.post(
                        batchupdate_url,
                        json={"requests": _build_section_insert_requests(sections)},
                        headers=headers,
                        timeout=_HTTP_TIMEOUT_SECONDS,
                    )
                    batch_response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    result["partial_failure"] = (
                        "section insertion failed: "
                        + safe_error_summary(exc.response, target="docs")
                    )
                except (httpx.HTTPError, httpx.InvalidURL) as exc:
                    result["partial_failure"] = (
                        "section insertion failed: "
                        + safe_network_summary(exc, target="docs")
                    )

        return result

    async def _real_append_section(
        self, *, doc_id: str, heading: str, body: str
    ) -> dict[str, Any]:
        """Real-mode append. Returns ``{"id": doc_id, "url": ...}``.

        Review issue 4 — note the **return-shape divergence** from the
        mock. ``_mock_append_section`` returns the full stateful doc
        dict with ``sections`` + ``body`` populated; real mode has no
        local state (Google owns the doc body now), so only ``id`` and
        ``url`` are guaranteed. ``execute()`` only reads ``doc["id"]``
        + ``doc.get("url")`` so the dispatcher is safe across modes,
        but callers that invoke ``adapter.append_section(...)``
        directly and inspect ``["sections"]`` work on mock and
        ``KeyError`` on real. Use the dispatch path (``execute``) or
        re-fetch the doc via Docs ``documents.get`` if you need the
        full body shape.
        """
        s = self.settings
        assert s.google_oauth_access_token is not None  # for mypy
        token = s.google_oauth_access_token.get_secret_value()
        batchupdate_url = (
            s.google_docs_api_base.rstrip("/")
            + f"/v1/documents/{doc_id}:batchUpdate"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        # Single insertText at endOfSegmentLocation containing
        # heading + body. v1 keeps the API surface minimal; a follow-up
        # can split heading and body into separate requests + add
        # updateParagraphStyle(HEADING_2) so the heading renders bold.
        combined_text = f"{heading}\n{body}\n" if body else f"{heading}\n"
        requests_payload = [
            {
                "insertText": {
                    "endOfSegmentLocation": {},
                    "text": combined_text,
                }
            }
        ]
        async with self._client_session() as client:
            response = await client.post(
                batchupdate_url,
                json={"requests": requests_payload},
                headers=headers,
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()

        return {
            "id": doc_id,
            "url": _docs_ui_url(doc_id),
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
        if (
            dry_run
            and self.real_mode
            and (action.type in _CREATE_TYPES or action.type == _APPEND_TYPE)
        ):
            return self._dry_run_preview(action)
        try:
            if action.type in _CREATE_TYPES:
                doc = await self.create_doc(
                    title=action.payload["title"],
                    sections=action.payload.get("sections"),
                )
                # Review issue 2 — partial-failure surface. The doc was
                # created (so ``id`` and ``url`` are real) but
                # ``batchUpdate`` failed to insert the section headings.
                # Surface as ``failed`` so the PM sees something went
                # wrong, while keeping ``external_id`` / ``external_url``
                # so the audit + UI can link to the stray empty doc.
                if doc.get("partial_failure"):
                    return ExternalActionResult(
                        action_id=action.action_id,
                        target_system="google_docs",
                        status="failed",
                        external_id=doc["id"],
                        external_url=doc.get("url"),
                        error=doc["partial_failure"],
                        message=(
                            "Doc created but section insertion failed — "
                            "operator must clean up the stray doc or "
                            "retry section insert."
                        ),
                    )
                return self._success(
                    action,
                    dry_run=dry_run,
                    external_id=doc["id"],
                    external_url=doc.get("url"),
                    message=f"Created Doc ({self._mode_label()}).",
                )
            if action.type == _APPEND_TYPE:
                doc = await self.append_section(
                    doc_id=action.payload["doc_id"],
                    heading=action.payload["heading"],
                    body=action.payload.get("body", ""),
                )
                return self._success(
                    action,
                    dry_run=dry_run,
                    external_id=doc["id"],
                    external_url=doc.get("url"),
                    message=f"Appended Doc section ({self._mode_label()}).",
                )
        except KeyError as exc:
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="google_docs",
                status="failed",
                error=f"missing payload field: {exc}",
                message="Docs adapter rejected payload.",
            )
        except _MissingDocumentIdError as exc:
            # Review issue 3 — upstream contract violation: Docs
            # returned 200 without ``documentId``. Surface the named
            # diagnostic; do NOT echo response body (str(exc) is
            # adapter-authored so safe).
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="google_docs",
                status="failed",
                error=str(exc),
                message="Docs response shape violated contract.",
            )
        except httpx.HTTPStatusError as exc:
            # M8 — never echo the raw response body. Captive portals
            # and corporate proxies often interpose HTML 4xx / 5xx pages
            # on Google endpoints; safe_error_summary structures the
            # output and caps it at 200 chars.
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="google_docs",
                status="failed",
                error=safe_error_summary(exc.response, target="docs"),
                message="Docs REST returned an error status.",
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # Round-2 M8 + round-3 M4 — drop ``str(exc)`` body; the
            # exception message embeds the request URL which carries
            # user content (doc title, batchUpdate body). InvalidURL is
            # its own exception class outside HTTPError, so the
            # tuple-catch is necessary to keep the same leak surface
            # closed across both classes.
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="google_docs",
                status="failed",
                error=safe_network_summary(exc, target="docs"),
                message="Docs network error.",
            )

        return ExternalActionResult(
            action_id=action.action_id,
            target_system="google_docs",
            status="failed",
            error=f"unknown action type {action.type!r}",
            message="Docs adapter has no handler for this action type.",
        )

    def _dry_run_preview(self, action: ExternalAction) -> ExternalActionResult:
        """Synthetic preview for dry-run real-mode. Mirrors Plane / Drive.

        external_id is deterministic over the action so a PM previewing
        twice sees the same synthetic id. external_url is None — no
        real doc has been created. Audit consumers render "preview
        only" UI off the dry_run status.

        Review issue 5 — fall back to ``action.action_id`` when neither
        ``title`` (create) nor ``doc_id`` (append) is in the payload.
        Hashing the empty string yields the same synthetic id for every
        empty-payload preview, breaking the deterministic-per-action
        guarantee.
        """
        if action.type in _CREATE_TYPES:
            key = action.payload.get("title") or action.action_id
        else:
            key = action.payload.get("doc_id") or action.action_id
        synthetic_id = f"dry_run_{_hash(str(key))}"
        return ExternalActionResult(
            action_id=action.action_id,
            target_system="google_docs",
            status="dry_run",
            external_id=synthetic_id,
            external_url=None,
            message=f"Dry-run Docs preview ({self._mode_label()}).",
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


def _build_section_insert_requests(sections: list[str]) -> list[dict[str, Any]]:
    """Build a list of Docs ``insertText`` requests, one per section heading.

    Each heading is inserted at the document's ``endOfSegmentLocation``
    with a trailing newline so subsequent headings start on a fresh
    paragraph. A follow-up can attach ``updateParagraphStyle(HEADING_2)``
    so the headings render bold; v1 keeps the request shape minimal to
    reduce failure modes.
    """
    return [
        {
            "insertText": {
                "endOfSegmentLocation": {},
                "text": f"{section}\n",
            }
        }
        for section in sections
    ]
