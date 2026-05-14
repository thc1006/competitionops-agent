"""Windmill script — extract a structured competition brief from raw text.

Inputs:
    content: raw brief text. Required, non-empty.
    source_uri: provenance label (e.g., 'drive://abcdef'). Optional.

Output: ``CompetitionBrief`` JSON dict from
``POST {WINDMILL_API_BASE}/briefs/extract``.

The script reads ``WINDMILL_API_BASE`` from env (defaults to
``http://localhost:8000``) so the same script file can target dev,
staging, and prod by env switch alone — no code change needed.
"""

from __future__ import annotations

import os
from typing import Any

import httpx


def main(content: str, source_uri: str | None = None) -> dict[str, Any]:
    if not content:
        raise ValueError("content must be a non-empty string")

    api_base = os.environ.get("WINDMILL_API_BASE", "http://localhost:8000")
    body: dict[str, Any] = {"source_type": "text", "content": content}
    if source_uri is not None:
        body["source_uri"] = source_uri

    with httpx.Client(base_url=api_base, timeout=30.0) as client:
        response = client.post("/briefs/extract", json=body)
        response.raise_for_status()
        return response.json()
