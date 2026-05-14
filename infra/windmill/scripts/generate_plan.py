"""Windmill script — generate a dry-run ActionPlan from a brief.

Inputs:
    competition: ``CompetitionBrief`` dict (typically the output of
        the ``extract_brief`` script piped via Windmill's result
        passing). Required.
    team_capacity: list of ``TeamMember`` dicts. Optional; defaults
        to ``[]``.
    pm_approval_required: whether the resulting plan must be approved
        before any execution. Defaults to True (production posture).

Output: ``ActionPlan`` JSON dict from ``POST
{WINDMILL_API_BASE}/plans/generate``.
"""

from __future__ import annotations

import os
from typing import Any

import httpx


def main(
    competition: dict[str, Any],
    team_capacity: list[dict[str, Any]] | None = None,
    pm_approval_required: bool = True,
) -> dict[str, Any]:
    if not isinstance(competition, dict):
        raise ValueError("competition must be a dict (CompetitionBrief shape)")
    if not competition.get("name"):
        raise ValueError("competition.name is required")

    api_base = os.environ.get("WINDMILL_API_BASE", "http://localhost:8000")
    body = {
        "competition": competition,
        "team_capacity": team_capacity or [],
        "preferences": {"pm_approval_required": pm_approval_required},
    }

    with httpx.Client(base_url=api_base, timeout=30.0) as client:
        response = client.post("/plans/generate", json=body)
        response.raise_for_status()
        return response.json()
