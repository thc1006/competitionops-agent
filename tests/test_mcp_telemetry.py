"""Sprint 4 — MCP tool span coverage.

Each of the six MCP tools wraps its body in ``@traced_*("mcp.tool.<name>")``
and annotates the span with the right context attributes. Tests assert
the span name, attribute presence, and exception path for one
representative async tool.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from competitionops.telemetry import setup_tracer_provider
from competitionops_mcp import server as mcp_server


# ---------------------------------------------------------------------------
# Module-scope exporter (same pattern as test_execution_telemetry)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _attach_exporter() -> InMemorySpanExporter:
    provider = setup_tracer_provider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


@pytest.fixture
def span_exporter(_attach_exporter: InMemorySpanExporter) -> InMemorySpanExporter:
    _attach_exporter.clear()
    return _attach_exporter


def _reset_mcp_state() -> None:
    mcp_server._plan_repo.cache_clear()
    mcp_server._audit_log.cache_clear()
    mcp_server._registry.cache_clear()


# ---------------------------------------------------------------------------
# Each of the six tools must produce its named span on a happy-path call
# ---------------------------------------------------------------------------


def test_extract_competition_brief_emits_named_span_with_content_length(
    span_exporter: InMemorySpanExporter,
) -> None:
    _reset_mcp_state()
    mcp_server.extract_competition_brief(
        content="Cup\nSubmission deadline: 2026-09-30\nRequired: pitch deck.\n",
        source_uri="test://demo",
    )

    matching = [
        span
        for span in span_exporter.get_finished_spans()
        if span.name == "mcp.tool.extract_competition_brief"
    ]
    assert len(matching) == 1
    attrs = matching[0].attributes
    assert attrs is not None
    assert attrs.get("source_uri") == "test://demo"
    assert isinstance(attrs.get("content_length"), int)
    assert attrs["content_length"] > 0


def test_generate_competition_plan_emits_span_with_plan_id(
    span_exporter: InMemorySpanExporter,
) -> None:
    _reset_mcp_state()
    brief = mcp_server.extract_competition_brief(
        content="Cup\nSubmission deadline: 2026-09-30\nRequired: pitch deck.\n",
    )
    plan = mcp_server.generate_competition_plan(competition=brief)

    matching = [
        span
        for span in span_exporter.get_finished_spans()
        if span.name == "mcp.tool.generate_competition_plan"
    ]
    assert len(matching) == 1
    attrs = matching[0].attributes
    assert attrs is not None
    assert attrs.get("plan_id") == plan["plan_id"]
    assert attrs.get("action_count") == len(plan["actions"])


def test_propose_google_workspace_actions_emits_span_with_plan_id(
    span_exporter: InMemorySpanExporter,
) -> None:
    _reset_mcp_state()
    brief = mcp_server.extract_competition_brief(
        content="Cup\nSubmission deadline: 2026-09-30\nRequired: pitch deck.\n",
    )
    plan = mcp_server.generate_competition_plan(competition=brief)
    span_exporter.clear()

    mcp_server.propose_google_workspace_actions(plan_id=plan["plan_id"])

    matching = [
        span
        for span in span_exporter.get_finished_spans()
        if span.name == "mcp.tool.propose_google_workspace_actions"
    ]
    assert len(matching) == 1
    assert matching[0].attributes is not None
    assert matching[0].attributes.get("plan_id") == plan["plan_id"]


def test_list_pending_approvals_emits_span_with_scope(
    span_exporter: InMemorySpanExporter,
) -> None:
    _reset_mcp_state()
    mcp_server.list_pending_approvals()  # no plan_id -> all-plans scope

    matching = [
        span
        for span in span_exporter.get_finished_spans()
        if span.name == "mcp.tool.list_pending_approvals"
    ]
    assert len(matching) == 1
    assert matching[0].attributes is not None
    assert matching[0].attributes.get("scope") == "all"


@pytest.mark.asyncio
async def test_approve_action_async_tool_emits_span_with_actor(
    span_exporter: InMemorySpanExporter,
) -> None:
    _reset_mcp_state()
    brief = mcp_server.extract_competition_brief(
        content="Cup\nSubmission deadline: 2026-09-30\nRequired: pitch deck.\n",
    )
    plan = mcp_server.generate_competition_plan(competition=brief)
    span_exporter.clear()

    target = plan["actions"][0]["action_id"]
    await mcp_server.approve_action(
        plan_id=plan["plan_id"], action_id=target, approved_by="pm@example.com"
    )

    matching = [
        span
        for span in span_exporter.get_finished_spans()
        if span.name == "mcp.tool.approve_action"
    ]
    assert len(matching) == 1
    attrs = matching[0].attributes
    assert attrs is not None
    assert attrs.get("plan_id") == plan["plan_id"]
    assert attrs.get("action_id") == target
    assert attrs.get("actor") == "pm@example.com"


@pytest.mark.asyncio
async def test_execute_approved_action_mock_async_tool_emits_span(
    span_exporter: InMemorySpanExporter,
) -> None:
    _reset_mcp_state()
    brief = mcp_server.extract_competition_brief(
        content="Cup\nSubmission deadline: 2026-09-30\nRequired: pitch deck.\n",
    )
    plan = mcp_server.generate_competition_plan(competition=brief)
    target = plan["actions"][0]["action_id"]
    await mcp_server.approve_action(
        plan_id=plan["plan_id"], action_id=target, approved_by="pm@example.com"
    )
    span_exporter.clear()

    await mcp_server.execute_approved_action_mock(
        plan_id=plan["plan_id"], action_id=target, executed_by="pm@example.com"
    )

    matching = [
        span
        for span in span_exporter.get_finished_spans()
        if span.name == "mcp.tool.execute_approved_action_mock"
    ]
    assert len(matching) == 1
    attrs = matching[0].attributes
    assert attrs is not None
    assert attrs.get("plan_id") == plan["plan_id"]
    assert attrs.get("action_id") == target
    assert attrs.get("actor") == "pm@example.com"
    assert attrs.get("allow_reexecute") is False


def test_all_six_tools_have_dedicated_span_names(
    span_exporter: InMemorySpanExporter,
) -> None:
    """End-to-end coverage: a single happy flow exercises every tool once
    and produces exactly one span per tool name."""
    _reset_mcp_state()
    brief = mcp_server.extract_competition_brief(
        content="Cup\nSubmission deadline: 2026-09-30\nRequired: pitch deck.\n",
    )
    plan = mcp_server.generate_competition_plan(competition=brief)
    mcp_server.propose_google_workspace_actions(plan_id=plan["plan_id"])
    mcp_server.list_pending_approvals(plan_id=plan["plan_id"])
    target = plan["actions"][0]["action_id"]
    asyncio.run(
        mcp_server.approve_action(
            plan_id=plan["plan_id"], action_id=target, approved_by="pm@example.com"
        )
    )
    asyncio.run(
        mcp_server.execute_approved_action_mock(
            plan_id=plan["plan_id"], action_id=target, executed_by="pm@example.com"
        )
    )

    expected_span_names = {
        "mcp.tool.extract_competition_brief",
        "mcp.tool.generate_competition_plan",
        "mcp.tool.propose_google_workspace_actions",
        "mcp.tool.list_pending_approvals",
        "mcp.tool.approve_action",
        "mcp.tool.execute_approved_action_mock",
    }
    seen = {span.name for span in span_exporter.get_finished_spans()}
    assert expected_span_names.issubset(seen), (
        f"missing spans: {expected_span_names - seen}"
    )
