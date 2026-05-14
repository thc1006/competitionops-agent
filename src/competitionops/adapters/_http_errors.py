"""Shared HTTP-error redaction helper for real adapters (M8).

Background: before this helper, ``PlaneAdapter`` and
``GoogleDriveAdapter`` both echoed ``exc.response.text[:200]`` directly
into the audit log's ``error`` field on an ``httpx.HTTPStatusError``.
That was fine for legitimate JSON error bodies but leaked HTML stack
traces, internal hostnames, and file paths from self-hosted Plane / Drive
shims that respond with HTML 500 pages.

The helper enforces a "structured fields only, plain text never" rule:

- If the body parses as JSON AND is a ``dict`` AND contains a STRING
  value under one of ``error`` / ``detail`` / ``message`` (the standard
  DRF / Plane / Google shapes), that string is appended to the status
  line.
- Anything else (HTML, plain text, JSON array, nested object without
  a string error field) → only the ``"<status_code> <reason>"`` line
  is returned. The raw body is never echoed.

Output is hard-capped at ``_SUMMARY_MAX_CHARS`` so an attacker-controlled
long JSON string field can't bloat audit log entries either.

This module is internal to ``adapters/`` — note the leading underscore.
It has no Settings dependency, no Pydantic, no httpx-specific assumptions
beyond ``response.status_code`` / ``response.reason_phrase`` /
``response.json()``, so it can be lifted into a different package later
if more adapters need it.
"""

from __future__ import annotations

import httpx

_SUMMARY_MAX_CHARS = 200
_STRING_ERROR_FIELDS: tuple[str, ...] = ("error", "detail", "message")


def safe_error_summary(
    response: httpx.Response, *, target: str | None = None
) -> str:
    """Return a redacted, audit-safe summary of an error response.

    Always includes the status code (so audit log searches by status
    still work). Includes a structured error field only when the body
    is a JSON ``dict`` with a string value under one of
    ``error`` / ``detail`` / ``message``. Never echoes raw bytes / HTML
    / text bodies.

    Args:
        response: The httpx response carrying the error.
        target: Optional adapter label (e.g. ``"plane"`` / ``"drive"``)
            prepended to the summary so audit consumers can tell which
            integration produced the failure without parsing the rest.

    Returns:
        A string capped at ``_SUMMARY_MAX_CHARS``.
    """
    reason = response.reason_phrase or "unknown"
    status_line = f"{response.status_code} {reason}".strip()
    prefix = f"{target} " if target else ""
    summary = f"{prefix}{status_line}"

    extracted = _extract_structured_message(response)
    if extracted:
        # Leave room for the suffix ": <message>" within the cap.
        suffix_overhead = len(": ")
        remaining = _SUMMARY_MAX_CHARS - len(summary) - suffix_overhead
        if remaining > 0:
            summary = f"{summary}: {extracted[:remaining]}"

    return summary[:_SUMMARY_MAX_CHARS]


def safe_network_summary(
    exc: httpx.HTTPError | httpx.InvalidURL, *, target: str | None = None
) -> str:
    """Audit-safe summary of a network-layer httpx exception.

    Accepts both ``httpx.HTTPError`` (parent of ``ConnectError``,
    ``ReadTimeout``, ``WriteError``, ``RemoteProtocolError``, …) AND
    ``httpx.InvalidURL`` — which sits OUTSIDE the ``HTTPError``
    hierarchy in httpx (parent is plain ``Exception``). Round-3 M4
    closed the gap where adapters caught only ``HTTPError`` and let
    ``InvalidURL`` propagate uncaught, leaking the bad-URL fragment
    via the FastAPI 500 traceback. The leak surface is the same as
    M8's: ``str(httpx.InvalidURL)`` typically echoes the URL chunk
    that broke parsing, and that chunk embeds user content (folder
    names / issue titles / workspace_slug).

    Returns ``"{target} network error: {ExceptionClassName}"`` and
    DELIBERATELY drops ``str(exc)``. Round-2 M8 — httpx's exception
    ``__str__`` typically embeds the request URL, and adapter URLs
    embed user-controlled query strings: Drive's
    ``q=name='<folder_name>'`` and Plane's ``search=<issue_title>``.
    A folder name or issue title that happened to contain a token-
    like substring (copy-pasted credential, secret label) would leak
    into the audit log via that branch.

    Args:
        exc: The httpx exception caught at the adapter boundary.
        target: Optional adapter label (e.g. ``"plane"`` / ``"drive"``)
            prepended so audit consumers can tell which integration
            produced the failure.

    Returns:
        A short, redacted string. Class names are bounded so the
        output is naturally short — no explicit length cap needed.
    """
    prefix = f"{target} " if target else ""
    return f"{prefix}network error: {type(exc).__name__}"


def _extract_structured_message(response: httpx.Response) -> str | None:
    """Return the first string-valued ``error`` / ``detail`` / ``message``
    field in a JSON dict body, or None for anything else.

    Conservative by design: arrays, nested objects, non-string values
    all collapse to None so the caller falls back to the bare status
    line. We never speculate about deeper structure.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    for field in _STRING_ERROR_FIELDS:
        value = body.get(field)
        if isinstance(value, str) and value:
            return value
    return None
