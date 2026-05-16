"""TokenProvider failures must surface as clean, attributed results.

Deep-review finding A/B for the P2-006 TokenProvider port: after the
rewire, fetching an access token went from a non-raising attribute
read to ``await provider.get_access_token()`` — which raises
``TokenRefreshError`` when the refresh token is expired / revoked or
the OAuth endpoint is unreachable.

- Finding A: each Google adapter's ``execute()`` must convert that into
  a ``status="failed"`` ``ExternalActionResult`` attributed to the
  adapter, not let it escape to ``execution.py``'s generic handler.
- Finding B: the ``/briefs/extract/drive`` endpoint must convert it
  into a clean HTTP 502, not an unhandled 500.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from competitionops.adapters.google_calendar import GoogleCalendarAdapter
from competitionops.adapters.google_docs import GoogleDocsAdapter
from competitionops.adapters.google_drive import GoogleDriveAdapter
from competitionops.adapters.google_sheets import GoogleSheetsAdapter
from competitionops.adapters.token_provider_google import TokenRefreshError
from competitionops.main import app, get_drive_adapter
from competitionops.schemas import ExternalAction, RiskLevel

_REDACTED_MESSAGE = "oauth 401 Unauthorized: invalid_grant"


class _RaisingTokenProvider:
    """A TokenProvider whose refresh always fails — mimics an expired /
    revoked refresh token. Structurally satisfies the TokenProvider port."""

    async def get_access_token(self) -> str:
        raise TokenRefreshError(_REDACTED_MESSAGE)


def _action(action_type: str, target: str, payload: dict) -> ExternalAction:
    return ExternalAction(
        action_id="act_tok",
        type=action_type,
        target_system=target,
        payload=payload,
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )


# Finding A — each adapter, with a minimal valid payload so execute()
# reaches the real-mode token fetch (not a KeyError on payload first).
_ADAPTER_CASES = [
    (
        GoogleDriveAdapter,
        "google_drive",
        "google.drive.create_competition_folder",
        {"name": "Test Competition"},
    ),
    (
        GoogleDocsAdapter,
        "google_docs",
        "google.docs.create_doc",
        {"title": "Test Doc"},
    ),
    (
        GoogleSheetsAdapter,
        "google_sheets",
        "google.sheets.append_rows",
        {"rows": [{"col": "value"}]},
    ),
    (
        GoogleCalendarAdapter,
        "google_calendar",
        "google.calendar.create_event",
        {
            "title": "Kickoff",
            "start": "2026-06-01T00:00:00+08:00",
            "end": "2026-06-01T01:00:00+08:00",
        },
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter_cls,target,action_type,payload", _ADAPTER_CASES
)
async def test_execute_converts_token_refresh_error_to_failed_result(
    adapter_cls: type,
    target: str,
    action_type: str,
    payload: dict,
) -> None:
    adapter = adapter_cls(token_provider=_RaisingTokenProvider())
    assert adapter.real_mode is True  # a provider is wired -> real mode
    action = _action(action_type, target, payload)

    # dry_run=False -> real path -> token fetch raises TokenRefreshError.
    result = await adapter.execute(action, dry_run=False)

    assert result.status == "failed"
    assert result.target_system == target
    assert result.action_id == "act_tok"
    assert "access token" in result.message
    # TokenRefreshError carries a pre-redacted summary — surfaced verbatim.
    assert _REDACTED_MESSAGE in (result.error or "")


# Finding B — /briefs/extract/drive endpoint.


class _RaisingDriveAdapter:
    """Drive adapter stub whose read path fails with TokenRefreshError."""

    async def download_file(self, *, file_id: str) -> bytes:
        raise TokenRefreshError(_REDACTED_MESSAGE)


def test_drive_ingest_endpoint_handles_token_refresh_error() -> None:
    app.dependency_overrides[get_drive_adapter] = lambda: _RaisingDriveAdapter()
    try:
        client = TestClient(app)
        response = client.post("/briefs/extract/drive", json={"file_id": "abc123"})
    finally:
        app.dependency_overrides.pop(get_drive_adapter, None)

    # Clean 502 — not an unhandled 500.
    assert response.status_code == 502
    assert "auth failed" in response.json()["detail"].lower()
