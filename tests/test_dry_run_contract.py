"""Round-2 I2 — ``_dry_run_preview`` contract test.

Plane (C1) and Drive (P1-005) both ship a ``_dry_run_preview`` method
on their real-mode adapter. Same contract:

- Short-circuits BEFORE any HTTP call when ``dry_run=True`` AND real
  mode is active.
- Returns ``status="dry_run"``.
- ``external_id`` is a deterministic ``dry_run_<sha1_prefix>`` so
  re-running the same approval surfaces a stable preview id.
- ``external_url`` is None (no real resource exists yet).
- ``message`` ends with ``(<mode>, dry_run).``.

This file enumerates every adapter that should follow the contract
and asserts the behaviour. The test fails loudly if a real adapter
forgets the dry-run gate or drifts from the format — exactly the C1
leak shape.

Round-2 I2's framing was "extract the pattern into adapters/__init__.py
docstring or a contract test." This file is the contract test.

TODO — extend the parametrize block below when these real adapters
land. Each must be added as ``(build_adapter, payload, action_type,
target_system)`` so the dry-run gate is enforced before merge:

- ``P1-001`` ``GoogleDocsAdapter`` (action_type ``google.docs.create_doc``)
- ``P1-002`` ``GoogleSheetsAdapter`` (action_type ``google.sheets.append_rows``)
- ``P1-003`` ``GoogleCalendarAdapter`` (action_type ``google.calendar.create_event``)

Each adapter's own ``_dry_run_preview`` docstring cross-references
back to this file so the dependency is bidirectional.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from competitionops.adapters.google_drive import GoogleDriveAdapter
from competitionops.adapters.plane import PlaneAdapter
from competitionops.config import Settings
from competitionops.schemas import ExternalAction, RiskLevel


def _plane_real_settings() -> Settings:
    return Settings(
        plane_base_url="https://plane.example.invalid",
        plane_api_key=SecretStr("test-key"),
        plane_workspace_slug="acme",
        plane_project_id="00000000-0000-0000-0000-000000000abc",
    )


def _drive_real_settings() -> Settings:
    return Settings(
        google_oauth_access_token=SecretStr("ya29.test"),
        google_drive_api_base="https://drive-test.example.invalid",
    )


class _NoHTTPGuard:
    """MockTransport handler that BOTH raises on call AND tracks the
    call count. Belt-and-braces: a future real adapter that wraps
    ``_dry_run_preview`` in ``try/except Exception:`` would swallow the
    raised AssertionError and pass the rest of the contract via
    result-shape alone. The counter exposes the real fire count so the
    test can assert it explicitly after the execute returns."""

    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        raise AssertionError(
            f"HTTP {request.method} {request.url} fired during a "
            "dry_run execute — contract violation."
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "build_adapter,action_payload,action_type,target_system",
    [
        (
            lambda client: PlaneAdapter(
                settings=_plane_real_settings(), client=client
            ),
            {"title": "Pitch deck"},
            "plane.create_issue",
            "plane",
        ),
        (
            lambda client: GoogleDriveAdapter(
                settings=_drive_real_settings(), client=client
            ),
            {"folder_name": "RunSpace"},
            "google.drive.create_competition_folder",
            "google_drive",
        ),
    ],
    ids=["plane", "drive"],
)
async def test_real_adapter_dry_run_preview_contract(
    build_adapter: Any,
    action_payload: dict[str, Any],
    action_type: str,
    target_system: str,
) -> None:
    """Every real adapter MUST honor the dry-run contract identically.

    The four invariants checked together are exactly the C1 + P1-005
    fix: no HTTP, stable id, None URL, ``dry_run`` mode label in
    message. A future Docs / Sheets / Calendar real adapter that
    forgets any of them surfaces here, not at production-time.
    """
    guard = _NoHTTPGuard()
    transport = httpx.MockTransport(guard)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = build_adapter(client)
        action = ExternalAction(
            action_id="act_contract",
            type=action_type,
            target_system=target_system,
            payload=action_payload,
            requires_approval=True,
            risk_level=RiskLevel.medium,
        )
        result = await adapter.execute(action, dry_run=True)

    # 1. No HTTP fired. The guard's raise gets the call instantly,
    # but we ALSO assert the counter for defense against a future
    # ``try/except Exception`` swallow inside the adapter.
    assert guard.call_count == 0, (
        f"{target_system}: dry_run path made {guard.call_count} HTTP "
        "call(s) — the guard exception may have been swallowed. "
        "Real adapters must short-circuit BEFORE any HTTP."
    )
    # 2. Status is "dry_run".
    assert result.status == "dry_run", (
        f"{target_system}: dry_run=True must produce status=dry_run, "
        f"got {result.status!r}"
    )
    # 3. external_id is deterministic + prefixed.
    assert result.external_id is not None
    assert result.external_id.startswith("dry_run_"), (
        f"{target_system}: external_id must start with ``dry_run_`` "
        f"prefix so audit consumers can filter preview vs real; "
        f"got {result.external_id!r}"
    )
    # The hash component must be reasonably sized — not a literal
    # input echo or full sha1. Plane / Drive both pick [:8].
    suffix = result.external_id.removeprefix("dry_run_")
    assert 6 <= len(suffix) <= 16
    # 4. external_url is None — no real resource exists yet.
    assert result.external_url is None, (
        f"{target_system}: external_url must be None during dry_run "
        f"(no resource was actually created); got {result.external_url!r}"
    )
    # 5. message ends with the dry_run mode marker so audit-log
    # consumers can grep for it without parsing fields.
    msg = result.message or ""
    assert msg.endswith("(real, dry_run)."), (
        f"{target_system}: message must end with ``(real, dry_run).`` "
        f"to mark the preview path explicitly; got {msg!r}"
    )


@pytest.mark.asyncio
async def test_dry_run_preview_id_stable_across_repeated_calls() -> None:
    """The Plane + Drive _dry_run_preview ids are sha1 hashes of the
    input — the same input should produce the same id every time so
    approval-UI re-renders show a stable preview identifier."""

    guard = _NoHTTPGuard()
    transport = httpx.MockTransport(guard)
    async with httpx.AsyncClient(transport=transport) as client:
        plane = PlaneAdapter(settings=_plane_real_settings(), client=client)
        plane_a = await plane.execute(
            ExternalAction(
                action_id="a1",
                type="plane.create_issue",
                target_system="plane",
                payload={"title": "Stable Title"},
                requires_approval=True,
                risk_level=RiskLevel.medium,
            ),
            dry_run=True,
        )
        plane_b = await plane.execute(
            ExternalAction(
                action_id="a2",  # different action_id, same title
                type="plane.create_issue",
                target_system="plane",
                payload={"title": "Stable Title"},
                requires_approval=True,
                risk_level=RiskLevel.medium,
            ),
            dry_run=True,
        )

        drive = GoogleDriveAdapter(
            settings=_drive_real_settings(), client=client
        )
        drive_a = await drive.execute(
            ExternalAction(
                action_id="b1",
                type="google.drive.create_competition_folder",
                target_system="google_drive",
                payload={"folder_name": "Stable Folder"},
                requires_approval=True,
                risk_level=RiskLevel.medium,
            ),
            dry_run=True,
        )
        drive_b = await drive.execute(
            ExternalAction(
                action_id="b2",  # different action_id, same folder name
                type="google.drive.create_competition_folder",
                target_system="google_drive",
                payload={"folder_name": "Stable Folder"},
                requires_approval=True,
                risk_level=RiskLevel.medium,
            ),
            dry_run=True,
        )

    assert plane_a.external_id == plane_b.external_id
    assert drive_a.external_id == drive_b.external_id
    # And the two adapters give DIFFERENT ids — they hash different
    # inputs (title vs folder_name + parent_id), so cross-adapter
    # collisions on identical strings are coincidence, not contract.
    assert plane_a.external_id != drive_a.external_id
    # Final defence: zero HTTP across all 4 dry_run executes above.
    assert guard.call_count == 0
