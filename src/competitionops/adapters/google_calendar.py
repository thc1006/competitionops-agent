"""Mock-first Google Calendar adapter — no network, no credentials."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from competitionops.schemas import ExternalAction, ExternalActionResult

_DEFAULT_OFFSETS_DAYS: tuple[int, ...] = (30, 14, 7, 1)


def _hash(text: str, length: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def _coerce_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


class GoogleCalendarAdapter:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []

    # ---- high-level operations --------------------------------------

    async def create_event(
        self,
        *,
        title: str,
        start: datetime | str,
        end: datetime | str,
        attendees: list[str] | None = None,
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

    async def create_checkpoint_series(
        self,
        *,
        competition_name: str,
        deadline: datetime | str,
        offsets_days: tuple[int, ...] | list[int] | None = None,
    ) -> list[dict[str, Any]]:
        deadline_dt = _coerce_datetime(deadline)
        offsets = tuple(offsets_days or _DEFAULT_OFFSETS_DAYS)
        series: list[dict[str, Any]] = []
        for offset in offsets:
            start = deadline_dt - timedelta(days=offset)
            end = start + timedelta(hours=1)
            event = await self.create_event(
                title=f"{competition_name}: T-{offset}d",
                start=start,
                end=end,
            )
            series.append(event)
        return series

    # ---- dispatch ----------------------------------------------------

    async def execute(
        self, action: ExternalAction, dry_run: bool = True
    ) -> ExternalActionResult:
        self.calls.append(
            {"action_id": action.action_id, "type": action.type, "dry_run": dry_run}
        )
        try:
            if action.type == "google.calendar.create_event":
                event = await self.create_event(
                    title=action.payload["title"],
                    start=action.payload["start"],
                    end=action.payload["end"],
                    attendees=action.payload.get("attendees"),
                )
                return self._success(
                    action,
                    dry_run=dry_run,
                    external_id=event["id"],
                    external_url=event["url"],
                    message="Created Calendar event (mock).",
                )
            if action.type == "google.calendar.create_checkpoint_series":
                series = await self.create_checkpoint_series(
                    competition_name=action.payload["competition_name"],
                    deadline=action.payload["deadline"],
                    offsets_days=action.payload.get("offsets_days"),
                )
                external_id = (
                    f"series_{_hash(action.payload['competition_name'])}"
                )
                return self._success(
                    action,
                    dry_run=dry_run,
                    external_id=external_id,
                    external_url=series[0]["url"] if series else None,
                    message=f"Created checkpoint series of {len(series)} events (mock).",
                )
        except KeyError as exc:
            return ExternalActionResult(
                action_id=action.action_id,
                target_system="google_calendar",
                status="failed",
                error=f"missing payload field: {exc}",
                message="Calendar adapter rejected payload.",
            )

        return ExternalActionResult(
            action_id=action.action_id,
            target_system="google_calendar",
            status="failed",
            error=f"unknown action type {action.type!r}",
            message="Calendar adapter has no handler for this action type.",
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
