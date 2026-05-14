"""P1-004 — Plane adapter mock + real-mode contract.

The adapter operates in two modes:
- **Mock mode** (default): stateful in-process, deterministic ids.
- **Real mode**: httpx-backed REST when all four Plane Settings fields
  are configured. Tests inject an ``httpx.MockTransport`` so no real
  network call ever leaves the process — pytest stays offline.

Tier 0 #3 progress: this commit lands the real adapter so the audit log
finally surfaces ``target_external_id`` for plane actions. The Stage 8
e2e exception is removed in the same commit (see test_e2e_dry_run.py
diff for "GOOGLE_TARGETS" removal).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from competitionops.adapters.plane import PlaneAdapter
from competitionops.config import Settings
from competitionops.schemas import ExternalAction, RiskLevel

_REAL_BASE_URL = "https://plane.example.invalid"
_REAL_API_KEY = "test-api-key-xyz-secret"
_REAL_WORKSPACE = "acme-workspace"
_REAL_PROJECT = "00000000-0000-0000-0000-000000000abc"


def _settings_real() -> Settings:
    return Settings(
        plane_base_url=_REAL_BASE_URL,
        plane_api_key=_REAL_API_KEY,
        plane_workspace_slug=_REAL_WORKSPACE,
        plane_project_id=_REAL_PROJECT,
    )


def _settings_mock() -> Settings:
    return Settings()  # all plane_* fields default to None → mock mode


def _make_action(title: str = "Pitch deck draft", **overrides: Any) -> ExternalAction:
    payload: dict[str, Any] = {"title": title}
    payload.update(overrides)
    return ExternalAction(
        action_id="act_plane_test",
        type="plane.create_issue",
        target_system="plane",
        payload=payload,
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )


# ---------------------------------------------------------------------------
# Mock-mode behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_create_issue_returns_deterministic_id() -> None:
    adapter = PlaneAdapter(settings=_settings_mock())
    first = await adapter.create_issue(title="Deterministic title")
    assert first["id"].startswith("mock_issue_")
    assert first["url"].startswith("https://plane.example.invalid/")


@pytest.mark.asyncio
async def test_mock_create_issue_idempotent_for_same_title() -> None:
    adapter = PlaneAdapter(settings=_settings_mock())
    a = await adapter.create_issue(title="Same")
    b = await adapter.create_issue(title="Same")
    assert a["id"] == b["id"]


@pytest.mark.asyncio
async def test_mock_create_issue_different_titles_produce_different_ids() -> None:
    adapter = PlaneAdapter(settings=_settings_mock())
    a = await adapter.create_issue(title="Title A")
    b = await adapter.create_issue(title="Title B")
    assert a["id"] != b["id"]


@pytest.mark.asyncio
async def test_mock_execute_dispatches_plane_create_issue() -> None:
    adapter = PlaneAdapter(settings=_settings_mock())
    result = await adapter.execute(_make_action("Demo video"), dry_run=True)

    assert result.status == "dry_run"
    assert result.target_system == "plane"
    assert result.external_id is not None
    assert result.external_id.startswith("mock_issue_")
    assert result.external_url is not None
    assert "plane.example.invalid" in result.external_url
    assert "mock" in (result.message or "").lower()


@pytest.mark.asyncio
async def test_mock_execute_handles_unknown_action_type() -> None:
    adapter = PlaneAdapter(settings=_settings_mock())
    action = ExternalAction(
        action_id="act_unknown",
        type="plane.does_not_exist",
        target_system="plane",
        payload={},
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )
    result = await adapter.execute(action, dry_run=True)
    assert result.status == "failed"
    assert "unknown action type" in (result.error or "")


@pytest.mark.asyncio
async def test_mock_execute_handles_missing_payload_field() -> None:
    adapter = PlaneAdapter(settings=_settings_mock())
    action = ExternalAction(
        action_id="act_missing",
        type="plane.create_issue",
        target_system="plane",
        payload={},  # no 'title'
        requires_approval=True,
        risk_level=RiskLevel.medium,
    )
    result = await adapter.execute(action, dry_run=True)
    assert result.status == "failed"
    assert "title" in (result.error or "")


# ---------------------------------------------------------------------------
# Real-mode switching
# ---------------------------------------------------------------------------


def test_real_mode_requires_all_four_settings_fields() -> None:
    # Full config → real
    assert PlaneAdapter(settings=_settings_real()).real_mode is True
    # Missing key → mock
    s = Settings(
        plane_base_url=_REAL_BASE_URL,
        plane_workspace_slug=_REAL_WORKSPACE,
        plane_project_id=_REAL_PROJECT,
    )
    assert PlaneAdapter(settings=s).real_mode is False
    # Missing url → mock
    s = Settings(
        plane_api_key=_REAL_API_KEY,
        plane_workspace_slug=_REAL_WORKSPACE,
        plane_project_id=_REAL_PROJECT,
    )
    assert PlaneAdapter(settings=s).real_mode is False
    # No config at all → mock
    assert PlaneAdapter(settings=_settings_mock()).real_mode is False


# ---------------------------------------------------------------------------
# Real-mode REST calls (no network — httpx.MockTransport)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_create_issue_posts_to_plane_workspace_endpoint() -> None:
    """After the Tier 0 #5 idempotency upgrade the real adapter issues
    a GET (search) before the POST (create). Verify both go to the
    same workspace+project endpoint and that the POST carries the
    expected payload."""
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[])  # search returns empty
        return httpx.Response(
            201,
            json={
                "id": "real-issue-uuid-123",
                "name": "Pitch deck",
                "description_html": "",
                "project": _REAL_PROJECT,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        issue = await adapter.create_issue(title="Pitch deck")

    assert issue["id"] == "real-issue-uuid-123"
    assert len(captured_requests) == 2
    methods = [req.method for req in captured_requests]
    assert methods == ["GET", "POST"]
    expected_url_suffix = (
        f"/api/v1/workspaces/{_REAL_WORKSPACE}"
        f"/projects/{_REAL_PROJECT}/issues/"
    )
    for req in captured_requests:
        assert expected_url_suffix in str(req.url)


@pytest.mark.asyncio
async def test_real_create_issue_sends_x_api_key_header() -> None:
    captured_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.append(request.headers)
        return httpx.Response(201, json={"id": "ok", "name": "x"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        await adapter.create_issue(title="x")

    assert captured_headers[0].get("X-API-Key") == _REAL_API_KEY
    assert "application/json" in (captured_headers[0].get("Accept") or "")


@pytest.mark.asyncio
async def test_real_create_issue_body_contains_title_and_description() -> None:
    """The POST body (not the GET search params) carries the create payload."""
    captured_post_bodies: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            import json as _json

            captured_post_bodies.append(_json.loads(request.content.decode()))
            return httpx.Response(201, json={"id": "real-1", "name": "The Pitch"})
        # GET search: nothing existing → fall through to POST
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        await adapter.create_issue(
            title="The Pitch", description="10 pages", owner_role="business"
        )

    assert len(captured_post_bodies) == 1
    body = captured_post_bodies[0]
    assert body["name"] == "The Pitch"
    assert "10 pages" in body["description_html"]
    assert "business" in body["description_html"]  # owner_role merged in


@pytest.mark.asyncio
async def test_real_execute_surfaces_external_id_and_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "id": "real-uuid-from-api",
                "name": "Demo task",
                "project": _REAL_PROJECT,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        result = await adapter.execute(_make_action("Demo task"), dry_run=False)

    assert result.status == "executed"
    assert result.external_id == "real-uuid-from-api"
    assert result.external_url is not None
    assert _REAL_WORKSPACE in result.external_url
    assert _REAL_PROJECT in result.external_url
    assert "real" in (result.message or "").lower()


@pytest.mark.asyncio
async def test_real_execute_maps_http_status_error_to_failed_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        result = await adapter.execute(_make_action(), dry_run=False)

    assert result.status == "failed"
    assert "401" in (result.error or "")


@pytest.mark.asyncio
async def test_real_execute_maps_network_error_to_failed_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic network failure")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        result = await adapter.execute(_make_action(), dry_run=False)

    assert result.status == "failed"
    assert "network" in (result.message or "").lower()
    assert "synthetic" in (result.error or "")


# ---------------------------------------------------------------------------
# C1 — dry_run safety in real mode
#
# Background: ``Settings.dry_run_default=True`` and the ExecutionService
# always threads ``dry_run`` into ``adapter.execute``. Before this fix,
# real mode performed the GET + POST against Plane regardless of the
# flag, which meant the very first preview against a fully-configured
# Plane would silently create a real issue. Violates CLAUDE.md absolute
# rule #3 ("所有 Google / Plane / Calendar 寫入動作都先做 dry-run").
#
# Contract after the fix: real mode + dry_run=True ⇒ zero HTTP calls,
# return a synthetic ``dry_run_<sha1[:8]>`` external_id so the audit
# trail and preview UI still have something to display. Mock mode is
# unchanged — it has no side effects so running it through is safe.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_execute_dry_run_makes_no_http_call() -> None:
    """The defining test for C1: real mode must NOT touch the network
    when dry_run=True."""
    seen_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_methods.append(request.method)
        return httpx.Response(500, text="should not have reached Plane")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        result = await adapter.execute(
            _make_action("Real-mode dry-run target"), dry_run=True
        )

    assert seen_methods == [], (
        f"real mode must not touch the network during dry_run; saw {seen_methods!r}"
    )
    assert result.status == "dry_run"
    assert result.target_system == "plane"
    assert result.external_id is not None
    assert result.external_id.startswith("dry_run_")
    # message should make the mode explicit so PMs reading the audit
    # log can tell preview-vs-executed apart.
    assert "real" in (result.message or "").lower()
    assert "dry" in (result.message or "").lower()


@pytest.mark.asyncio
async def test_real_execute_dry_run_preview_id_is_deterministic() -> None:
    """Preview ids are stable across re-runs so the approval UI can
    show the same identifier the second time the PM looks at it."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Defensive: any HTTP call here is a regression of C1.
        raise AssertionError("dry_run must not issue HTTP requests")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        first = await adapter.execute(_make_action("Stable title"), dry_run=True)
        second = await adapter.execute(_make_action("Stable title"), dry_run=True)
        different = await adapter.execute(_make_action("Other title"), dry_run=True)

    assert first.external_id == second.external_id
    assert first.external_id != different.external_id


@pytest.mark.asyncio
async def test_real_execute_dry_run_false_still_reaches_plane() -> None:
    """Sanity: dry_run=False keeps the existing real-mode behavior. The
    C1 guard is targeted — it must not also block legitimate executes."""
    captured_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(
            201,
            json={"id": "real-issue-after-fix", "name": "Title that lands"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        result = await adapter.execute(
            _make_action("Title that lands"), dry_run=False
        )

    assert captured_methods == ["GET", "POST"]
    assert result.status == "executed"
    assert result.external_id == "real-issue-after-fix"


@pytest.mark.asyncio
async def test_mock_mode_dry_run_keeps_passing_through_to_mock_create() -> None:
    """The C1 short-circuit must NOT swallow mock-mode behaviour — mock
    has no side effects, and the existing tests / audit fixtures rely
    on dry_run mock executes producing ``mock_issue_<sha1>`` ids."""
    adapter = PlaneAdapter(settings=_settings_mock())
    result = await adapter.execute(_make_action("Mock dry"), dry_run=True)

    assert result.status == "dry_run"
    assert result.external_id is not None
    assert result.external_id.startswith("mock_issue_")
    assert not result.external_id.startswith("dry_run_")


@pytest.mark.asyncio
async def test_adapter_source_does_not_reference_real_plane_hosts() -> None:
    """Defense in depth: the adapter source code must never hardcode a
    real Plane production URL. Configuration must come from Settings."""
    import inspect

    from competitionops.adapters import plane as plane_mod

    source = inspect.getsource(plane_mod)
    forbidden = ("app.plane.so", "https://api.plane.so")
    for needle in forbidden:
        assert needle not in source, (
            f"Plane adapter source must not hardcode {needle!r}; configure via Settings"
        )


# ---------------------------------------------------------------------------
# Tier 0 #5 — query-then-create idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_create_issue_returns_existing_when_search_finds_match() -> None:
    """If GET search returns an issue with name == title, the adapter must
    surface that existing issue and never POST."""
    captured_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {"id": "wrong-name-id", "name": "Other issue"},
                    {"id": "existing-id-abc", "name": "Pitch deck"},
                ],
            )
        # POST must never run for this case.
        return httpx.Response(500, text="should not be called")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        issue = await adapter.create_issue(title="Pitch deck")

    assert captured_methods == ["GET"]  # POST not called
    assert issue["id"] == "existing-id-abc"
    assert "url" in issue


@pytest.mark.asyncio
async def test_real_create_issue_handles_pagination_results_wrapper() -> None:
    """Some Plane installations wrap the issue list under ``results``."""
    captured_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "count": 1,
                    "results": [{"id": "wrapped-id", "name": "Wrapped task"}],
                },
            )
        return httpx.Response(500, text="should not be called")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        issue = await adapter.create_issue(title="Wrapped task")

    assert captured_methods == ["GET"]
    assert issue["id"] == "wrapped-id"


@pytest.mark.asyncio
async def test_real_create_issue_is_idempotent_across_repeated_calls() -> None:
    """Stateful handler: first call posts, second call's search finds the
    just-posted issue and returns it without a second POST. Mirrors the
    re-execute-with-allow_reexecute flow at the adapter layer."""
    plane_db: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            # search ignores query and returns full plane_db; the
            # adapter filters by exact name match itself.
            return httpx.Response(200, json=list(plane_db))
        # POST
        import json as _json

        body = _json.loads(request.content.decode())
        new = {"id": f"real-id-{len(plane_db) + 1}", "name": body["name"]}
        plane_db.append(new)
        return httpx.Response(201, json=new)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        first = await adapter.create_issue(title="Same task")
        second = await adapter.create_issue(title="Same task")

    # Only one POST happened — plane_db has exactly one row.
    assert len(plane_db) == 1
    # Both calls return the same issue.
    assert first["id"] == second["id"]
    assert first["id"] == "real-id-1"


@pytest.mark.asyncio
async def test_real_create_issue_falls_through_when_search_returns_5xx() -> None:
    """If Plane's search endpoint errors (e.g., disabled on a self-hosted
    instance), the adapter must still create the issue rather than fail
    hard. Idempotency is forfeit for that call but the workflow proceeds."""
    captured_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(500, text="search disabled")
        return httpx.Response(
            201, json={"id": "post-after-search-fail", "name": "x"}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        issue = await adapter.create_issue(title="x")

    assert captured_methods == ["GET", "POST"]
    assert issue["id"] == "post-after-search-fail"


@pytest.mark.asyncio
async def test_real_create_issue_falls_through_when_search_raises_network() -> None:
    """Network failures on the search step also degrade to plain POST,
    matching the 5xx fall-through semantics."""
    captured_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_methods.append(request.method)
        if request.method == "GET":
            raise httpx.ConnectError("synthetic search-step network failure")
        return httpx.Response(
            201, json={"id": "post-after-network-fail", "name": "x"}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        issue = await adapter.create_issue(title="x")

    assert captured_methods == ["GET", "POST"]
    assert issue["id"] == "post-after-network-fail"


# ---------------------------------------------------------------------------
# M7 — search-step hardening
#
# Background: ``_search_existing_issue`` previously swallowed ALL 4xx /
# 5xx responses and degraded to POST. That's the right call for
# "search disabled on this self-hosted Plane" (5xx, 404, 4xx other than
# auth) — we'd rather lose idempotency than block the create. It is
# NOT the right call for auth failures: 401 / 403 from search means
# the credentials are misconfigured, and falling through to POST will
# just produce the same 401 / 403 from the POST with a less-specific
# error message. Failing fast on auth surfaces the actual problem.
#
# Separately, the raw title was passed as the ``search`` query param
# with no length cap. A 10 KiB competition brief title used as an
# issue name produced a URL longer than typical proxy / origin limits
# (~8 KiB), yielding 414 URI Too Long from the search step — which
# under the old fall-through code silently lost idempotency on every
# retry. The fix truncates the search query to a conservative byte
# budget while keeping the full title in the POST body and in the
# exact-match comparison on the search response.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_search_401_raises_and_skips_post() -> None:
    """M7 — auth failure on search must short-circuit, NOT fall
    through to POST. Falling through would surface the same 401 from
    a different verb and waste a round trip."""
    captured_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(401, json={"error": "invalid api key"})
        return httpx.Response(201, json={"id": "should-not-post", "name": "x"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        result = await adapter.execute(_make_action(), dry_run=False)

    assert captured_methods == ["GET"], (
        f"M7: POST must NOT be sent after a 401 from search; "
        f"saw {captured_methods!r}"
    )
    assert result.status == "failed"
    assert "401" in (result.error or "")


@pytest.mark.asyncio
async def test_real_search_403_raises_and_skips_post() -> None:
    """M7 — 403 (authenticated but not authorised for this workspace
    or project) is also a hard auth failure. Same behaviour as 401."""
    captured_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(403, json={"error": "forbidden"})
        return httpx.Response(201, json={"id": "should-not-post", "name": "x"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        result = await adapter.execute(_make_action(), dry_run=False)

    assert captured_methods == ["GET"]
    assert result.status == "failed"
    assert "403" in (result.error or "")


@pytest.mark.asyncio
async def test_real_search_404_still_falls_through_to_post() -> None:
    """Regression guard: only auth failures (401/403) short-circuit.
    A 404 from search means "endpoint not implemented" (some
    self-hosted Plane variants); fall through to POST stays correct."""
    captured_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(404, text="search not implemented")
        return httpx.Response(
            201, json={"id": "post-after-404-search", "name": "x"}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        issue = await adapter.create_issue(title="x")

    assert captured_methods == ["GET", "POST"]
    assert issue["id"] == "post-after-404-search"


@pytest.mark.asyncio
async def test_real_search_414_uri_too_long_still_falls_through() -> None:
    """If a misconfigured proxy still rejects a (post-truncation)
    search URL with 414, the create should still succeed. Same
    contract as 5xx — search is best-effort idempotency."""
    captured_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(414, text="URI Too Long")
        return httpx.Response(
            201, json={"id": "post-after-414-search", "name": "x"}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        issue = await adapter.create_issue(title="x")

    assert captured_methods == ["GET", "POST"]
    assert issue["id"] == "post-after-414-search"


@pytest.mark.asyncio
async def test_real_search_query_param_truncated_for_long_titles() -> None:
    """M7 — a title longer than the search-query cap is truncated for
    the GET ``search=`` param but the FULL title still lands in the
    POST body. The truncation cap keeps the search URL well under
    typical 8 KiB URL limits even after URL-encoding."""
    captured_get_query: dict[str, str] = {}
    captured_post_body: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            captured_get_query["raw"] = str(request.url)
            captured_get_query["search"] = request.url.params.get("search", "")
            return httpx.Response(200, json=[])  # no match → fall through to POST
        body = json.loads(request.content.decode("utf-8"))
        captured_post_body["name"] = body["name"]
        return httpx.Response(201, json={"id": "long-title-issue", "name": body["name"]})

    long_title = "RunSpace Innovation Challenge " * 500  # ~15 KiB raw
    assert len(long_title) > 10_000

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        issue = await adapter.create_issue(title=long_title)

    # Search param is bounded (well under typical 8 KiB URL limit).
    assert len(captured_get_query["search"]) <= 1024, (
        f"M7 search query was {len(captured_get_query['search'])} chars; "
        "must be capped to keep the URL under proxy / origin limits"
    )
    # The full URL (after URL-encoding) also stays well under 8 KiB.
    assert len(captured_get_query["raw"]) < 8 * 1024
    # Crucially, the POST body still carries the FULL title — the
    # truncation is for search idempotency only, not for the stored
    # issue name.
    assert captured_post_body["name"] == long_title
    assert issue["id"] == "long-title-issue"


@pytest.mark.asyncio
async def test_real_create_issue_truncated_search_still_dedupes_exact_match() -> None:
    """The exact-match filter on the search response uses the FULL
    title, not the truncated search query. So if Plane returns a hit
    that has the same long title, idempotency holds. (If Plane returns
    a different title that happens to share the first N chars, the
    exact-match comparison correctly rejects it and we POST.)"""
    long_title = "X" * 2000  # bigger than the truncation cap
    captured_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_methods.append(request.method)
        if request.method == "GET":
            # Plane returns a hit whose name matches the FULL long title.
            return httpx.Response(
                200,
                json=[{"id": "already-exists", "name": long_title}],
            )
        # If POST fires, the test failed.
        return httpx.Response(500, text="POST should not have fired on exact match")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = PlaneAdapter(settings=_settings_real(), client=client)
        issue = await adapter.create_issue(title=long_title)

    assert captured_methods == ["GET"], (
        "exact-match on full title must short-circuit before POST"
    )
    assert issue["id"] == "already-exists"
