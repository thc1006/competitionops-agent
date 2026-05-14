"""Windmill script — approve a subset of plan actions and run them.

Inputs:
    plan_id: the plan to act on (from ``generate_plan`` output).
        Required.
    approved_action_ids: action_ids the PM chose to approve. Pass as
        an empty list to reject everything. Required.
    approved_by: PM identifier (typically an email) — written to the
        audit log. Required, non-empty.

Output: ``ApprovalResponse`` JSON dict from ``POST
{WINDMILL_API_BASE}/plans/{plan_id}/approve`` (the legacy combined
approve+execute endpoint — one Windmill step instead of two for
operational simplicity).
"""

from __future__ import annotations

import os
from typing import Any

import httpx


def main(
    plan_id: str,
    approved_action_ids: list[str],
    approved_by: str,
) -> dict[str, Any]:
    if not plan_id:
        raise ValueError("plan_id must be a non-empty string")
    if not approved_by:
        raise ValueError("approved_by must be a non-empty string")
    if approved_action_ids is None:
        raise ValueError("approved_action_ids must be a list, not None")
    if not isinstance(approved_action_ids, list):
        raise ValueError("approved_action_ids must be a list of strings")

    api_base = os.environ.get("WINDMILL_API_BASE", "http://localhost:8000")
    body: dict[str, Any] = {
        "approved_action_ids": approved_action_ids,
        "approved_by": approved_by,
    }

    with httpx.Client(base_url=api_base, timeout=60.0) as client:
        response = client.post(f"/plans/{plan_id}/approve", json=body)
        response.raise_for_status()
        return response.json()
