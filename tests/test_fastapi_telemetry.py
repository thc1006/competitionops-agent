"""Sprint 4 — FastAPI auto-instrumentation contract.

Verifies that the OTel ``FastAPIInstrumentor`` middleware bolted onto
``competitionops.main.app`` emits one SERVER-kind span per HTTP request
with the canonical ``http.route`` / ``http.method`` / ``http.status_code``
attributes.

Test isolation note: the global TracerProvider already has an
InMemorySpanExporter attached if ``test_execution_telemetry.py`` ran
first. We attach our own exporter module-scoped here too and clear it
between tests. Multiple SpanProcessors coexist cleanly because each owns
its exporter.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind

from competitionops import main as main_module
from competitionops.main import app
from competitionops.telemetry import setup_tracer_provider


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


def _reset_state() -> None:
    main_module._plan_repo.cache_clear()
    main_module._audit_log.cache_clear()
    main_module._registry.cache_clear()


def _server_spans(exporter: InMemorySpanExporter) -> list[Any]:
    return [
        span
        for span in exporter.get_finished_spans()
        if span.kind == SpanKind.SERVER
    ]


def test_http_get_health_creates_server_span(
    span_exporter: InMemorySpanExporter,
) -> None:
    _reset_state()
    response = TestClient(app).get("/health")
    assert response.status_code == 200

    servers = _server_spans(span_exporter)
    assert len(servers) == 1
    span = servers[0]
    assert span.attributes is not None
    route = span.attributes.get("http.route")
    method = span.attributes.get("http.request.method") or span.attributes.get(
        "http.method"
    )
    status_code = span.attributes.get(
        "http.response.status_code"
    ) or span.attributes.get("http.status_code")
    assert route == "/health"
    assert method == "GET"
    assert status_code == 200


def test_http_post_briefs_extract_creates_server_span(
    span_exporter: InMemorySpanExporter,
) -> None:
    _reset_state()
    response = TestClient(app).post(
        "/briefs/extract",
        json={
            "source_type": "text",
            "content": "Telemetry Cup\nSubmission deadline: 2026-09-30\n",
        },
    )
    assert response.status_code == 200

    servers = _server_spans(span_exporter)
    matching = [
        span
        for span in servers
        if span.attributes is not None
        and span.attributes.get("http.route") == "/briefs/extract"
    ]
    assert len(matching) == 1
    span = matching[0]
    assert span.attributes is not None
    method = span.attributes.get("http.request.method") or span.attributes.get(
        "http.method"
    )
    assert method == "POST"


def test_http_approve_then_execute_emits_nested_server_and_execution_spans(
    span_exporter: InMemorySpanExporter,
) -> None:
    """End-to-end span coverage: the /approvals and /executions HTTP
    requests each produce one SERVER span; the underlying ExecutionService
    methods produce ``execution.*`` INTERNAL spans inside the same trace.
    """
    _reset_state()
    client = TestClient(app)

    brief_resp = client.post(
        "/briefs/extract",
        json={
            "source_type": "text",
            "content": (
                "Pipeline Cup\nSubmission deadline: 2026-09-30\n"
                "Required: pitch deck.\n"
            ),
        },
    )
    brief = brief_resp.json()
    for deliverable in brief["deliverables"]:
        deliverable["owner_role"] = "business"
    plan_resp = client.post(
        "/plans/generate",
        json={
            "competition": brief,
            "team_capacity": [
                {
                    "member_id": "m1",
                    "name": "Alice",
                    "role": "business",
                    "weekly_capacity_hours": 20,
                }
            ],
            "preferences": {"pm_approval_required": True},
        },
    )
    plan = plan_resp.json()
    span_exporter.clear()  # focus assertions on approve+execute path

    target = plan["actions"][0]["action_id"]
    client.post(
        f"/approvals/{plan['plan_id']}/approve",
        json={"approved_action_ids": [target], "approved_by": "pm@example.com"},
    )
    client.post(
        f"/executions/{plan['plan_id']}/run",
        json={"executed_by": "pm@example.com", "action_ids": [target]},
    )

    finished = span_exporter.get_finished_spans()
    routes_seen = {
        span.attributes.get("http.route")
        for span in finished
        if span.kind == SpanKind.SERVER and span.attributes is not None
    }
    assert "/approvals/{plan_id}/approve" in routes_seen
    assert "/executions/{plan_id}/run" in routes_seen

    internal_names = {
        span.name
        for span in finished
        if span.kind == SpanKind.INTERNAL
    }
    assert "execution.approve_actions" in internal_names
    assert "execution.run_approved" in internal_names
    assert "execution.adapter_call" in internal_names
