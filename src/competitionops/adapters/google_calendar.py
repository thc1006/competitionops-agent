"""Google Calendar adapter — mock-first with real-mode switch (P1-003).

Sprint-0 stateful mock survives unchanged for unit tests and
dry-run previews. P1-003 layers real httpx-backed REST behind the
same ``GoogleCalendarAdapter`` interface. Bearer-only ``real_mode``
(issue-1 pattern; AST guard pins the structure).

Real-mode operations:

- ``google.calendar.create_event`` → ``POST {base}/calendar/v3/calendars/{calendarId}/events``.
  Default calendarId is ``"primary"`` (auth'd user's primary
  calendar). Payload may override via ``calendar_id``. Body:
  ``{"summary": ..., "start": {"dateTime": ISO}, "end": {"dateTime":
  ISO}, "attendees": [{"email": ...}]}``.
- ``google.calendar.create_checkpoint_series`` → N create_event calls
  (one per offset; default ``(30, 14, 7, 1)`` days). Partial-failure
  surface (Docs issue-2 pattern): if some succeed before one fails,
  the created event IDs are preserved in the result so the operator
  can clean them up. Dispatcher surfaces ``status=failed`` with the
  IDs in the error message.

Safety properties (Plane / Drive / Docs / Sheets shape):

- Deep-review C1 — ``dry_run=True`` short-circuits BEFORE any HTTP
  call. Synthetic ``dry_run_<sha1(key)[:8]>``. CLAUDE.md rule #3.
- M8 + round-3 M4 — HTTPStatusError → ``safe_error_summary``;
  HTTPError + InvalidURL → ``safe_network_summary``. Event titles,
  attendee emails, calendarId all carry user content.

Out of scope:

- RRULE / recurrence — single events only.
- Conference data (Meet / Hangouts link autocreation).
- Reminder overrides — uses calendar defaults.
- Timezone normalisation — caller-supplied ISO strings must carry
  tzinfo. Naive datetimes are passed as-is; Calendar uses the
  calendar's primary timezone.
- ISO 8601 well-formedness validation. ``_iso`` passes string input
  through verbatim; a malformed string lands directly in the
  Calendar API body and surfaces as 400 via ``safe_error_summary``.
  Trust the planner to emit valid timestamps.
- Past-dated checkpoint events. ``create_checkpoint_series`` emits
  one event per offset relative to the deadline; if the deadline is
  closer than the largest offset (e.g., deadline in 5 days with
  default T-30d offset), the resulting event is in the past. Calendar
  accepts past events; the planner should validate offset feasibility
  upstream rather than expecting this layer to filter.
- 429 backoff / retry.
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, AsyncIterator

import httpx

from competitionops.adapters._http_errors import (
    HTTP_TIMEOUT_SECONDS,
    safe_error_summary,
    safe_network_summary,
)
from competitionops.config import Settings, get_settings
from competitionops.schemas import ExternalAction, ExternalActionResult

_DEFAULT_OFFSETS_DAYS: tuple[int, ...] = (30, 14, 7, 1)
_DEFAULT_CALENDAR_ID = "primary"
_CREATE_EVENT_TYPE = "google.calendar.create_event"
_CHECKPOINT_SERIES_TYPE = "google.calendar.create_checkpoint_series"


def _hash(text: str, length: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def _coerce_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _iso(value: datetime | str) -> str:
    """Render a datetime / ISO string back to a Calendar-API-friendly
    ISO 8601 string. Already-string input is passed through verbatim
    (operator-supplied tzinfo is preserved); datetime input is
    serialised via ``.isoformat()``."""
    if isinstance(value, str):
        return value
    return value.isoformat()


class GoogleCalendarAdapter:
    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings if settings is not None else get_settings()
        self._injected_client = client
        # Mock-mode state — used in tests, dry-run previews, and audit trail.
        self.events: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []

    @property
    def real_mode(self) -> bool:
        """Real REST mode is enabled iff ``google_oauth_access_token`` is set.

        ``google_calendar_api_base`` defaults to the prod URL and is
        non-empty at Settings construction (Pydantic's ``str`` type
        check rejects ``None``; the URL validator raises on ``""``),
        so it is never falsy at runtime — it is a configuration knob,
        not a gate. Pinned structurally by
        ``test_real_mode_property_does_not_reference_api_base_attribute``.
        """
        return bool(self.settings.google_oauth_access_token)

    # ---- high-level operations --------------------------------------

    async def create_event(
        self,
        *,
        title: str,
        start: datetime | str,
        end: datetime | str,
        attendees: list[str] | None = None,
        calendar_id: str | None = None,
    ) -> dict[str, Any]:
        if self.real_mode:
            return await self._real_create_event(
                title=title,
                start=start,
                end=end,
                attendees=attendees,
                calendar_id=calendar_id or _DEFAULT_CALENDAR_ID,
            )
        return await self._mock_create_event(
            title=title, start=start, end=end, attendees=attendees
        )

    async def create_checkpoint_series(
        self,
        *,
        competition_name: str,
        deadline: datetime | str,
        offsets_days: tuple[int, ...] | list[int] | None = None,
        calendar_id: str | None = None,
    ) -> dict[str, Any]:
        deadline_dt = _coerce_datetime(deadline)
        offsets = tuple(offsets_days or _DEFAULT_OFFSETS_DAYS)
        created: list[dict[str, Any]] = []
        partial_failure: str | None = None
        for offset in offsets:
            start = deadline_dt - timedelta(days=offset)
            end = start + timedelta(hours=1)
            title = f"{competition_name}: T-{offset}d"
            try:
                event = await self.create_event(
                    title=title,
                    start=start,
                    end=end,
                    calendar_id=calendar_id,
                )
            except httpx.HTTPStatusError as exc:
                partial_failure = (
                    f"checkpoint T-{offset}d failed: "
                    + safe_error_summary(exc.response, target="calendar")
                )
                break
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                partial_failure = (
                    f"checkpoint T-{offset}d failed: "
                    + safe_network_summary(exc, target="calendar")
                )
                break
            created.append(event)
        return {
            "competition_name": competition_name,
            "events": created,
            "partial_failure": partial_failure,
        }

    # ---- mock path ---------------------------------------------------

    async def _mock_create_event(
        self,
        *,
        title: str,
        start: datetime | str,
        end: datetime | str,
        attendees: list[str] | None,
    ) -> dict[str, Any]:
        start_dt = _coerce_datetime(start)
        end_dt = _coerce_datetime(end)
        event_id = f"mock_event_{_hash(f'{title}|{start_dt.isoformat()}')}"
        if event_id not in self.events:
            self.events[event_id] = {
                "id": event_id,
                "title": title,
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "attendees": list(attendees or []),
                "url": f"https://calendar.example.invalid/event?eid={event_id}",
            }
        return self.events[event_id]

    # ---- real path ---------------------------------------------------

    async def _real_create_event(
        self,
        *,
        title: str,
        start: datetime | str,
        end: datetime | str,
        attendees: list[str] | None,
        calendar_id: str,
    ) -> dict[str, Any]:
        """Real-mode event creation.

        Returns ``{"id": ..., "url": ...}`` — a strict subset of the
        mock's return shape (the mock also carries ``title``, ``start``,
        ``end``, ``attendees`` from the local store). The dispatcher
        reads only ``id`` + ``url`` so the audit path is mode-agnostic;
        direct callers inspecting ``["attendees"]`` work on mock and
        ``KeyError`` on real. Same issue-4 surface as Docs P1-001.
        """
        s = self.settings
        assert s.google_oauth_access_token is not None  # for mypy
        token = s.google_oauth_access_token.get_secret_value()
        events_url = (
            s.google_calendar_api_base.rstrip("/")
            + f"/calendar/v3/calendars/{calendar_id}/events"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        body: dict[str, Any] = {
            "summary": title,
            "start": {"dateTime": _iso(start)},
            "end": {"dateTime": _iso(end)},
        }
        if attendees:
            body["attendees"] = [{"email": email} for email in attendees]

        async with self._client_session() as client:
            response = await client.post(
                events_url,
                json=body,
                headers=headers,
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()

        return {
            "id": data.get("id", ""),
            "url": data.get("htmlLink"),
        }

    # ---- ExternalActionExecutor dispatch ----------------------------

    async def execute(
        self, action: ExternalAction, dry_run: bool = True
    ) -> ExternalActionResult:
        self.calls.append(
            {"action_id": action.action_id, "type": action.type, "dry_run": dry_run}
        )
        # Deep-review C1 — real-mode must honor dry_run before touching
        # the network. Mock has no side effects.
        if dry_run and self.real_mode and action.type in (
            _CREATE_EVENT_TYPE, _CHECKPOINT_SERIES_TYPE
        ):
            return self._dry_run_preview(action)
        try:
            if action.type == _CREATE_EVENT_TYPE:
                event = await self.create_event(
                    title=action.payload["title"],
                    start=action.payload["start"],
                    end=action.payload["end"],
                    attendees=action.payload.get("attendees"),
                    calendar_id=action.payload.get("calendar_id"),
                )
                return self._success(
                    action,
                    dry_run=dry_run,
                    external_id=event["id"],
                    external_url=event.get("url"),
                    message=f"Created Calendar event ({self._mode_label()}).",
                )
            if action.type == _CHECKPOINT_SERIES_TYPE:
                result = await self.create_checkpoint_series(
                    competition_name=action.payload["competition_name"],
                    deadline=action.payload["deadline"],
                    offsets_days=action.payload.get("offsets_days"),
                    calendar_id=action.payload.get("calendar_id"),
                )
                created_events = result["events"]
                # Partial-failure surface (issue-2 pattern) — surface
                # ``failed`` but keep the created event IDs in the
                # error message so the operator can clean up. When the
                # FIRST event fails, ``created_events`` is empty: the
                # message must not promise stray events that don't
                # exist (review follow-up #1).
                if result.get("partial_failure"):
                    created_ids = ", ".join(
                        str(e.get("id", "")) for e in created_events
                    )
                    if created_events:
                        msg = (
                            "Checkpoint series partially created — operator "
                            "must delete the stray events or retry."
                        )
                    else:
                        msg = (
                            "Checkpoint series failed on first event — "
                            "no cleanup needed; retry the action."
                        )
                    return ExternalActionResult(
                        action_id=action.action_id,
                        target_system="google_calendar",
                        status="failed",
                        external_id=(
                            f"series_{_hash(action.payload['competition_name'])}"
                        ),
                        external_url=(
                            created_events[0].get("url")
                            if created_events else None
                        ),
                        error=(
                            f"{result['partial_failure']} | "
                            f"created event ids: [{created_ids}]"
                        ),
                        message=msg,
                    )
                external_id = (
                    f"series_{_hash(action.payload['competition_name'])}"
                )
                return self._success(
                    action,
                    dry_run=dry_run,
                    external_id=external_id,
                    external_url=(
                        created_events[0].get("url") if created_events else None
                    ),
                    message=(
                        f"Created checkpoint series of {len(created_events)} "
                        f"events ({self._mode_label()})."
                    ),
                )
        except KeyError as exc:
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="google_calendar",
                status="failed",
                error=f"missing payload field: {exc}",
                message="Calendar adapter rejected payload.",
            )
        except httpx.HTTPStatusError as exc:
            # M8 — never echo the raw response body. Captive portals
            # and corporate proxies often interpose HTML 4xx / 5xx
            # pages on Google endpoints; safe_error_summary structures
            # the output and caps it at 200 chars.
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="google_calendar",
                status="failed",
                error=safe_error_summary(exc.response, target="calendar"),
                message="Calendar REST returned an error status.",
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # Round-2 M8 + round-3 M4 — drop ``str(exc)`` body.
            # Exception messages embed the request URL which carries
            # calendarId; bodies carry event titles + attendee emails.
            # Both are user content.
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="google_calendar",
                status="failed",
                error=safe_network_summary(exc, target="calendar"),
                message="Calendar network error.",
            )

        return ExternalActionResult(
            action_id=action.action_id,
            target_system="google_calendar",
            status="failed",
            error=f"unknown action type {action.type!r}",
            message="Calendar adapter has no handler for this action type.",
        )

    def _dry_run_preview(self, action: ExternalAction) -> ExternalActionResult:
        """Synthetic preview for dry-run real-mode. Mirrors Plane / Drive
        / Docs / Sheets. Deterministic over the most identifying field
        (title for events, competition_name for series); falls back to
        ``action.action_id`` when neither is present (issue-5 pattern).
        external_url is None — no real event was created."""
        if action.type == _CREATE_EVENT_TYPE:
            key = action.payload.get("title") or action.action_id
        else:
            key = action.payload.get("competition_name") or action.action_id
        synthetic_id = f"dry_run_{_hash(str(key))}"
        return ExternalActionResult(
            action_id=action.action_id,
            target_system="google_calendar",
            status="dry_run",
            external_id=synthetic_id,
            external_url=None,
            message=f"Dry-run Calendar preview ({self._mode_label()}).",
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
            target_system="google_calendar",
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
