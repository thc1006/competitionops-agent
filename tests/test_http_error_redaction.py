"""M8 — ``safe_error_summary`` helper redacts HTTP error responses.

Before this commit, both ``PlaneAdapter`` and ``GoogleDriveAdapter``
echoed ``exc.response.text[:200]`` verbatim into the audit log's
``error`` field on an ``httpx.HTTPStatusError``. For a self-hosted
Plane or Drive shim that responds with an HTML 500 page (stack trace,
internal hostnames, file paths, occasionally secret fragments), the
200-char window was wide enough to leak operational infrastructure
into PM-visible audit records.

The helper enforces a "structured fields only, plain text never" rule:

- Body parses as JSON and is a ``dict`` with a string under one of
  ``error`` / ``detail`` / ``message`` (the standard DRF / Plane
  shapes) → that string is extracted, truncated, and appended to the
  status line.
- Anything else (HTML, plain text, empty, JSON array, nested object
  without a string field) → ONLY the ``"<status_code> <reason>"``
  line is returned. The raw body is never echoed.

Total output is hard-capped at 200 chars so an attacker-controlled
long JSON string also can't bloat audit log entries.
"""

from __future__ import annotations

import httpx
import pytest

from competitionops.adapters._http_errors import (
    safe_error_summary,
    safe_network_summary,
)


def _response(
    status_code: int,
    *,
    json_body: object | None = None,
    text_body: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Build an httpx.Response with the given body and headers."""
    if json_body is not None and text_body is not None:
        raise ValueError("provide json_body OR text_body, not both")
    if json_body is not None:
        return httpx.Response(status_code, json=json_body, headers=headers)
    return httpx.Response(status_code, content=text_body or b"", headers=headers)


# ---------------------------------------------------------------------------
# Structured JSON errors — extracted from known fields
# ---------------------------------------------------------------------------


def test_extracts_error_field_from_json_body() -> None:
    resp = _response(401, json_body={"error": "invalid api key"})
    summary = safe_error_summary(resp)
    assert "401" in summary
    assert "invalid api key" in summary


def test_extracts_detail_field_from_json_body() -> None:
    """DRF (django-rest-framework) default uses ``detail``."""
    resp = _response(403, json_body={"detail": "Authentication credentials were not provided."})
    summary = safe_error_summary(resp)
    assert "403" in summary
    assert "Authentication credentials" in summary


def test_extracts_message_field_from_json_body() -> None:
    """Some Plane self-hosted variants use ``message``."""
    resp = _response(422, json_body={"message": "name is required"})
    summary = safe_error_summary(resp)
    assert "422" in summary
    assert "name is required" in summary


def test_error_field_wins_over_detail_when_both_present() -> None:
    """Deterministic ordering so the audit message is stable."""
    resp = _response(
        400, json_body={"error": "first", "detail": "second", "message": "third"}
    )
    summary = safe_error_summary(resp)
    assert "first" in summary
    assert "second" not in summary
    assert "third" not in summary


# ---------------------------------------------------------------------------
# Non-string / unknown JSON shapes — body is NOT echoed
# ---------------------------------------------------------------------------


def test_html_body_is_never_echoed_only_status_line_returned() -> None:
    """The M8 motivating case: a 500 HTML page leaks stack traces /
    hostnames. Helper must collapse to the status line."""
    html = (
        "<html><head><title>500 Internal Server Error</title></head>"
        "<body><h1>Stack trace</h1>"
        "<pre>File '/srv/plane/db/secrets.py', line 42, "
        "in get_db_password: return os.environ['DB_PASS']\n"
        "InternalServerError: connection to db-internal.prod.svc.cluster.local refused"
        "</pre></body></html>"
    )
    resp = _response(500, text_body=html)
    summary = safe_error_summary(resp)
    assert "500" in summary
    # None of the leaky tokens make it into the summary.
    assert "secrets.py" not in summary
    assert "DB_PASS" not in summary
    assert "db-internal" not in summary
    assert "<html>" not in summary
    assert "stack trace" not in summary.lower()


def test_plaintext_non_json_body_is_not_echoed() -> None:
    """A plain text body (e.g., a CDN's stock '503 Service Unavailable'
    page rendered as text) is also opaque to us — collapse to status."""
    resp = _response(503, text_body="Service Unavailable. Origin returned a 502.")
    summary = safe_error_summary(resp)
    assert "503" in summary
    assert "Origin" not in summary
    assert "502" not in summary


def test_empty_body_returns_status_line_only() -> None:
    resp = _response(502)
    summary = safe_error_summary(resp)
    assert "502" in summary
    assert summary.strip()  # non-empty


def test_json_array_body_is_not_echoed() -> None:
    """Plane sometimes returns an array of field errors for validation
    failures (e.g., ``[{"field": "name", "code": "blank"}]``). We don't
    try to render these — the structure is too speculative and may
    nest arbitrary data."""
    resp = _response(
        400,
        json_body=[
            {"field": "name", "error": "required"},
            {"field": "owner", "error": "invalid"},
        ],
    )
    summary = safe_error_summary(resp)
    assert "400" in summary
    assert "name" not in summary
    assert "required" not in summary


def test_nested_object_without_string_error_field_is_not_echoed() -> None:
    """``{"errors": [{"detail": "blah"}]}`` is too deeply structured —
    we'd risk leaking arbitrary nested keys."""
    resp = _response(
        500, json_body={"errors": [{"detail": "blah"}], "trace_id": "x123"}
    )
    summary = safe_error_summary(resp)
    assert "500" in summary
    assert "blah" not in summary
    assert "trace_id" not in summary
    assert "x123" not in summary


def test_json_error_field_with_non_string_value_is_not_echoed() -> None:
    """``{"error": {"code": 42, "msg": "..."}}`` — error field exists
    but is itself a dict. We accept ONLY string values to avoid having
    to think about nested redaction."""
    resp = _response(400, json_body={"error": {"code": 42, "msg": "no"}})
    summary = safe_error_summary(resp)
    assert "400" in summary
    assert "no" not in summary
    assert "42" not in summary


# ---------------------------------------------------------------------------
# Length cap
# ---------------------------------------------------------------------------


def test_long_json_error_string_is_truncated() -> None:
    """A 10 KiB JSON ``error`` field (attacker-controlled in a
    self-hosted Plane) must not bloat the audit log entry."""
    huge = "x" * 10_000
    resp = _response(500, json_body={"error": huge})
    summary = safe_error_summary(resp)
    assert len(summary) <= 200
    assert "500" in summary


def test_short_summary_stays_unchanged() -> None:
    """No truncation when the result is already short."""
    resp = _response(400, json_body={"error": "bad"})
    summary = safe_error_summary(resp)
    assert summary.endswith("bad")
    assert len(summary) < 50  # comfortable below cap


# ---------------------------------------------------------------------------
# Prefix label support — caller's adapter name lives in the result
# ---------------------------------------------------------------------------


def test_prefix_label_is_prepended_when_supplied() -> None:
    """Callers (PlaneAdapter, GoogleDriveAdapter) pass a label so the
    audit log entry identifies which integration produced the error."""
    resp = _response(401, json_body={"error": "invalid api key"})
    summary = safe_error_summary(resp, target="plane")
    assert summary.startswith("plane ")
    assert "401" in summary
    assert "invalid api key" in summary


def test_prefix_label_optional_defaults_to_no_prefix() -> None:
    resp = _response(401, json_body={"error": "x"})
    summary = safe_error_summary(resp)
    assert not summary.startswith("plane")
    assert not summary.startswith("drive")
    assert "401" in summary


# ---------------------------------------------------------------------------
# Smoke tests through pytest parametrize for the most common shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [400, 401, 403, 404, 409, 422, 429, 500, 502, 503, 504],
)
def test_status_code_always_appears_in_summary(status: int) -> None:
    resp = _response(status, json_body={"error": "x"})
    summary = safe_error_summary(resp)
    assert str(status) in summary


# ---------------------------------------------------------------------------
# Round-2 M8 — ``safe_network_summary`` for ``httpx.HTTPError`` failures
# that are NOT status errors (ConnectError / ReadTimeout / WriteError /
# etc.). The round-1 ``safe_error_summary`` only covers HTTPStatusError.
# For other httpx failures we used to render ``str(exc)`` which often
# embeds the request URL — and Drive's search URL embeds the q-param
# (folder name = user content); Plane's URL embeds search= (issue
# title = user content). User content could contain copy-pasted
# secrets, hence the redaction.
# ---------------------------------------------------------------------------


def test_safe_network_summary_drops_str_exc_and_keeps_type_name() -> None:
    """The exception's ``__str__`` is the leak surface (httpx puts the
    request URL in there). Only the exception class name + caller
    target make it through."""
    request = httpx.Request(
        "GET",
        "https://api.example.invalid/files?q=name='SECRET-TOKEN-AS-FOLDER-NAME'",
    )
    exc = httpx.ConnectError(
        "All connection attempts failed", request=request
    )

    summary = safe_network_summary(exc, target="drive")

    assert "drive" in summary
    assert "ConnectError" in summary
    # Critical — none of the leaky URL tokens make it through.
    assert "SECRET-TOKEN" not in summary
    assert "q=" not in summary
    assert "api.example.invalid" not in summary
    assert "All connection attempts" not in summary


def test_safe_network_summary_without_target_omits_prefix() -> None:
    exc = httpx.ConnectTimeout("dns failure")
    summary = safe_network_summary(exc)
    # No leading "drive " / "plane " prefix when target is omitted.
    assert not summary.startswith("drive")
    assert not summary.startswith("plane")
    assert "ConnectTimeout" in summary


@pytest.mark.parametrize(
    "exception_cls",
    [
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteError,
        httpx.RemoteProtocolError,
        httpx.PoolTimeout,
    ],
)
def test_safe_network_summary_handles_common_httpx_errors(
    exception_cls: type[httpx.HTTPError],
) -> None:
    """Smoke sweep across the httpx error hierarchy. Any constructor
    quirks (some take request= kwarg, some don't) would surface here."""
    try:
        exc = exception_cls("synthetic failure message")
    except TypeError:
        request = httpx.Request("GET", "https://example.invalid/")
        exc = exception_cls("synthetic failure message", request=request)

    summary = safe_network_summary(exc, target="plane")

    assert "plane" in summary
    assert exception_cls.__name__ in summary
    assert "synthetic failure" not in summary  # exception body dropped


def test_safe_network_summary_output_capped_at_short_length() -> None:
    """Cap is structural: ``<target> network error: <ClassName>``
    never exceeds ~80 chars even for the longest httpx class names."""
    exc = httpx.RemoteProtocolError("anything")
    summary = safe_network_summary(exc, target="drive")
    assert len(summary) <= 80
