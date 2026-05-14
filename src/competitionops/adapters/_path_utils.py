"""Shared filename-sanitisation helper for file-backed adapters.

Round-2 review L3 — ``file_audit.py`` and ``file_plan_store.py`` both
defined ``_sanitise`` with identical logic (and slightly drifting
docstrings). Lifted here as the single source of truth so adding a
sixth file-backed adapter doesn't add a third copy.

This module is internal to ``adapters/`` (leading underscore). The
helper has no Pydantic, no httpx, no Settings dependency, so it can be
imported by any adapter that derives a filesystem path from an
attacker-controlled identifier.
"""

from __future__ import annotations


def sanitise_filename_segment(value: str) -> str:
    """Map an arbitrary identifier to a safe filename segment.

    Hash-based plan_ids and k8s pod names only contain
    ``[A-Za-z0-9_-]``, so this is a no-op for every legitimate value
    the system generates. Anything outside that set (``/``, ``.``,
    ``\\``, whitespace, control chars) folds to ``_``.

    Crucially, ``.`` is NOT in the allowed set — that's the pattern
    we're defending against. ``..`` as a filename component would let
    a malformed identifier escape its base directory via path
    traversal (e.g., ``../../etc/passwd`` → without sanitisation, the
    file lookup would resolve outside the intended store).

    An empty (or fully-fold-to-underscore) value falls back to
    ``"_"`` so callers always get a non-empty path segment.
    """
    cleaned = "".join(
        char if char.isalnum() or char in "-_" else "_" for char in value
    )
    return cleaned or "_"
