"""P1-003 — GoogleCalendarAdapter real-mode contract.

Mirrors P1-005 / P1-001 / P1-002:

- ``real_mode`` flips on iff ``google_oauth_access_token`` is set.
  ``google_calendar_api_base`` has a non-empty prod-URL default + URL
  validator — configuration knob, not a gate (issue-1 / AST guard).
- Real-mode operations:
  - ``google.calendar.create_event`` →
    ``POST {base}/calendar/v3/calendars/{calendarId}/events``. Body:
    ``{"summary": ..., "start": {"dateTime": ISO}, "end": {"dateTime": ISO},
    "attendees": [{"email": "..."}]}``. Default calendarId is
    ``"primary"`` (the auth'd user's primary calendar).
  - ``google.calendar.create_checkpoint_series`` → N create_event calls
    (one per offset). Partial-failure surface: if some succeed before
    one fails, preserve the IDs of created events in ``partial_failure``
    so the operator can clean up. Same issue-2 pattern Docs adopted.
- Deep-review C1 — ``dry_run=True`` short-circuits BEFORE any HTTP.
  Synthetic ``dry_run_<sha1(key)[:8]>``.
- M8 + R3-M4 redaction.

Out of scope: recurrence rules (RRULE), conference data (Meet links),
reminders override, timezone normalisation (we trust caller's ISO
strings to carry tzinfo), 429 backoff.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from competitionops.adapters.google_calendar import GoogleCalendarAdapter
from competitionops.config import Settings
from competitionops.schemas import ExternalAction, RiskLevel


def _real_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "google_oauth_access_token": SecretStr("ya29.test-bearer"),
        "google_calendar_api_base": "https://cal-test.example.invalid",
    }
    base.update(overrides)
    return Settings(**base)


def _mock_transport(handler: Any) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


def _make_event_action(
    *, title: str = "Pitch rehearsal",
    start: str = "2026-09-30T15:00:00+08:00",
    end: str = "2026-09-30T16:00:00+08:00",
    attendees: list[str] | None = None,
    calendar_id: str | None = None,
    action_id: str = "act_cal_event",
) -> ExternalAction:
    payload: dict[str, Any] = {"title": title, "start": start, "end": end}
    if attendees is not None:
        payload["attendees"] = attendees
    if calendar_id is not None:
        payload["calendar_id"] = calendar_id
    return ExternalAction(
        action_id=action_id,
        type="google.calendar.create_event",
        target_system="google_calendar",
        payload=payload,
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )


def _make_series_action(
    *, competition_name: str = "RunSpace",
    deadline: str = "2026-09-30T23:59:00+08:00",
    offsets_days: list[int] | None = None,
) -> ExternalAction:
    payload: dict[str, Any] = {
        "competition_name": competition_name,
        "deadline": deadline,
    }
    if offsets_days is not None:
        payload["offsets_days"] = offsets_days
    return ExternalAction(
        action_id="act_cal_series",
        type="google.calendar.create_checkpoint_series",
        target_system="google_calendar",
        payload=payload,
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )


# ---------------------------------------------------------------------------
# real_mode toggle — bearer-only (issue-1 pattern)
# ---------------------------------------------------------------------------


def test_real_mode_off_by_default() -> None:
    adapter = GoogleCalendarAdapter(settings=Settings())
    assert adapter.real_mode is False


def test_real_mode_off_when_access_token_missing() -> None:
    settings = Settings(google_calendar_api_base="https://cal-test.example.invalid")
    adapter = GoogleCalendarAdapter(settings=settings)
    assert adapter.real_mode is False


def test_real_mode_on_with_access_token_alone() -> None:
    """Issue-1 contract: bearer alone flips real mode on. The
    structural AST guard tuple is extended in this PR to cover
    calendar_mod, so this property is also enforced structurally."""
    settings = Settings(
        google_oauth_access_token=SecretStr("ya29.token-default-base"),
    )
    adapter = GoogleCalendarAdapter(settings=settings)
    assert adapter.real_mode is True


# ---------------------------------------------------------------------------
# create_event — POST events with bearer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_create_event_posts_to_events_endpoint() -> None:
    """Endpoint ``POST /calendar/v3/calendars/primary/events`` by
    default. Bearer in Authorization header. Body shape includes
    ``summary``, ``start.dateTime``, ``end.dateTime``."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "real-event-1",
                "htmlLink": "https://calendar.google.com/event?eid=Z123",
            },
        )

    async with _mock_transport(handler) as client:
        adapter = GoogleCalendarAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(_make_event_action(), dry_run=False)

    assert captured["method"] == "POST"
    assert captured["path"] == "/calendar/v3/calendars/primary/events"
    assert captured["auth"] == "Bearer ya29.test-bearer"
    body = captured["body"]
    assert body["summary"] == "Pitch rehearsal"
    assert body["start"]["dateTime"] == "2026-09-30T15:00:00+08:00"
    assert body["end"]["dateTime"] == "2026-09-30T16:00:00+08:00"
    assert result.status == "executed"
    assert result.external_id == "real-event-1"


@pytest.mark.asyncio
async def test_real_create_event_uses_explicit_calendar_id() -> None:
    """When payload supplies ``calendar_id``, the adapter targets that
    calendar instead of ``primary``."""
    captured_path: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_path.append(request.url.path)
        return httpx.Response(200, json={"id": "e", "htmlLink": "https://x"})

    async with _mock_transport(handler) as client:
        adapter = GoogleCalendarAdapter(settings=_real_settings(), client=client)
        await adapter.execute(
            _make_event_action(calendar_id="team-cal@group.calendar.google.com"),
            dry_run=False,
        )

    assert "team-cal" in captured_path[0]
    assert captured_path[0].endswith("/events")


@pytest.mark.asyncio
async def test_real_create_event_passes_attendees_as_email_objects() -> None:
    """Google Calendar API expects attendees as
    ``[{"email": "..."}]`` objects, not bare strings. The adapter
    must convert."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"id": "e", "htmlLink": "https://x"})

    async with _mock_transport(handler) as client:
        adapter = GoogleCalendarAdapter(settings=_real_settings(), client=client)
        await adapter.execute(
            _make_event_action(
                attendees=["alice@example.com", "bob@example.com"]
            ),
            dry_run=False,
        )

    attendees = captured.get("attendees")
    assert attendees == [
        {"email": "alice@example.com"},
        {"email": "bob@example.com"},
    ]


@pytest.mark.asyncio
async def test_real_create_event_returns_htmlLink_as_external_url() -> None:
    """The user-facing URL must be the ``htmlLink`` Google returns —
    that's the click-through URL operators use to open the event in
    Google Calendar."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "evt-7",
                "htmlLink": "https://calendar.google.com/calendar/u/0/r/eventedit/Z7",
            },
        )

    async with _mock_transport(handler) as client:
        adapter = GoogleCalendarAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(_make_event_action(), dry_run=False)

    assert result.external_url == (
        "https://calendar.google.com/calendar/u/0/r/eventedit/Z7"
    )


# ---------------------------------------------------------------------------
# create_checkpoint_series — N events; partial-failure preserves created IDs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_create_checkpoint_series_posts_one_per_offset() -> None:
    """Default offsets are (30, 14, 7, 1) — four events. Each is a
    distinct POST to the events endpoint."""
    posts_seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts_seen.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "id": f"checkpoint-{len(posts_seen)}",
                "htmlLink": f"https://calendar.google.com/e/{len(posts_seen)}",
            },
        )

    async with _mock_transport(handler) as client:
        adapter = GoogleCalendarAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(
            _make_series_action(competition_name="RunSpace"), dry_run=False
        )

    assert len(posts_seen) == 4
    # All four event titles must include the competition name + T-Nd marker.
    titles = [p["summary"] for p in posts_seen]
    assert all("RunSpace" in t and "T-" in t for t in titles)
    assert result.status == "executed"


@pytest.mark.asyncio
async def test_real_create_checkpoint_series_partial_failure_preserves_created_ids() -> None:
    """Issue-2 pattern. First two checkpoints succeed; third 500s.
    The dispatcher must surface ``status=failed`` AND keep the
    created event IDs in the error message so the operator can
    delete the stray events."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] <= 2:
            return httpx.Response(
                200,
                json={
                    "id": f"created-{call_count['n']}",
                    "htmlLink": f"https://x/{call_count['n']}",
                },
            )
        return httpx.Response(500, json={"error": "internal"})

    async with _mock_transport(handler) as client:
        adapter = GoogleCalendarAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(
            _make_series_action(offsets_days=[30, 14, 7, 1]), dry_run=False
        )

    assert result.status == "failed"
    err = result.error or ""
    # The two created IDs must appear so the operator can clean them up.
    assert "created-1" in err
    assert "created-2" in err
    # The status line of the failing call should also surface.
    assert "500" in err or "internal" in err


@pytest.mark.asyncio
async def test_real_create_checkpoint_series_failure_on_first_event() -> None:
    """Review follow-up — when the very first checkpoint fails, NO
    stray events exist on Google's side. The dispatcher message must
    reflect this (don't tell the operator to "delete stray events"
    when there are none) AND the created-ids list in the error must
    be empty, not "[]"-with-garbage.

    Catches a class of regressions where the partial-failure branch
    is taken unconditionally even when ``created`` is empty.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    async with _mock_transport(handler) as client:
        adapter = GoogleCalendarAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(
            _make_series_action(offsets_days=[30, 14, 7]), dry_run=False
        )

    assert result.status == "failed"
    # The message must NOT promise stray events when none exist.
    msg = result.message or ""
    assert "stray" not in msg.lower(), (
        f"Message advises cleanup when zero events were created: {msg!r}"
    )
    # And the audit error must surface ZERO created IDs (not a comma
    # mess or "[]"-with-garbage). Operator-readable.
    err = result.error or ""
    assert "created event ids: []" in err or "no events" in err.lower()


@pytest.mark.asyncio
async def test_real_create_checkpoint_series_respects_explicit_offsets() -> None:
    posts_seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts_seen.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200, json={"id": f"e{len(posts_seen)}", "htmlLink": "https://x"}
        )

    async with _mock_transport(handler) as client:
        adapter = GoogleCalendarAdapter(settings=_real_settings(), client=client)
        await adapter.execute(
            _make_series_action(offsets_days=[60, 7]), dry_run=False
        )

    assert len(posts_seen) == 2
    titles = [p["summary"] for p in posts_seen]
    assert any("T-60d" in t for t in titles)
    assert any("T-7d" in t for t in titles)


# ---------------------------------------------------------------------------
# Dry-run safety (C1 / CLAUDE.md rule #3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_create_event_honors_dry_run_and_makes_no_http_call() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(500, text="must not reach")

    async with _mock_transport(handler) as client:
        adapter = GoogleCalendarAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(_make_event_action(), dry_run=True)

    assert seen == []
    assert result.status == "dry_run"
    assert result.external_id is not None
    assert result.external_id.startswith("dry_run_")


@pytest.mark.asyncio
async def test_real_create_checkpoint_series_honors_dry_run() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(500, text="must not reach")

    async with _mock_transport(handler) as client:
        adapter = GoogleCalendarAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(_make_series_action(), dry_run=True)

    assert seen == []
    assert result.status == "dry_run"


# ---------------------------------------------------------------------------
# Error surfaces — auth, network, InvalidURL (M8 + R3-M4 redaction)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_create_event_surfaces_401_as_failed_action() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    async with _mock_transport(handler) as client:
        adapter = GoogleCalendarAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(_make_event_action(), dry_run=False)

    assert result.status == "failed"
    assert "401" in (result.error or "")


@pytest.mark.asyncio
async def test_real_create_event_surfaces_network_error_as_failed_action() -> None:
    """Calendar URLs embed calendarId in the path (can be a user-
    supplied calendar email); event title goes into the body. Both
    are leak surfaces if ``str(exc)`` were echoed."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "synthetic SECRET-NET-MSG", request=request
        )

    async with _mock_transport(handler) as client:
        adapter = GoogleCalendarAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(
            _make_event_action(
                title="SECRET-TITLE",
                calendar_id="SECRET-CALENDAR@group.calendar.google.com",
            ),
            dry_run=False,
        )

    assert result.status == "failed"
    err = result.error or ""
    assert "ConnectError" in err
    assert "SECRET-NET-MSG" not in err
    assert "SECRET-TITLE" not in err
    assert "SECRET-CALENDAR" not in err


@pytest.mark.asyncio
async def test_real_create_event_surfaces_invalid_url_as_failed_action() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.InvalidURL("bad url: SECRET-URL")

    async with _mock_transport(handler) as client:
        adapter = GoogleCalendarAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(_make_event_action(), dry_run=False)

    assert result.status == "failed"
    err = result.error or ""
    assert "InvalidURL" in err
    assert "SECRET-URL" not in err


@pytest.mark.asyncio
async def test_real_create_event_fails_loudly_when_response_missing_id() -> None:
    """Round-4 PR A (Medium#4) — a Calendar API 200 response without an
    ``id`` field is an upstream contract violation. Previously the
    adapter did ``data.get("id", "")`` → surfaced ``external_id=""``
    with ``status="executed"`` and a broken click-through URL. That
    hides the bug exactly like the pre-issue-3 Docs ``documentId``
    case did.

    Calendar now adopts the Docs ``_MissingDocumentIdError`` pattern:
    raise a named ``_MissingEventIdError`` inside ``_real_create_event``,
    catch in ``execute()``, surface as ``status=failed`` with a clear
    diagnostic. The dispatcher still promises to always return an
    ``ExternalActionResult`` (no raise escapes ``execute``)."""
    def handler(request: httpx.Request) -> httpx.Response:
        # 200 OK but no ``id`` — contract violation.
        return httpx.Response(200, json={"htmlLink": "https://calendar.google.com/x"})

    async with _mock_transport(handler) as client:
        adapter = GoogleCalendarAdapter(settings=_real_settings(), client=client)
        result = await adapter.execute(_make_event_action(), dry_run=False)

    assert result.status == "failed", result
    err = (result.error or "").lower()
    assert "id" in err, (
        "Failure message must name the missing field so operators can "
        "diagnose without digging into the raw response."
    )
    # external_id must NOT be the empty string masquerading as a real id.
    assert result.external_id in (None, ""), result
