# 07 — Backlog

> **Status snapshot (2026-05-14):** P0-001 through P0-006 are implemented,
> tested (84 passed in 1.37s), and committed. P1 not started. P2-004 in
> progress (Sprint 0+2+3 done). See `docs/10_p2_roadmap.md` for the active
> P2 sprint sequence.

## P0

### P0-001 — Define core schemas

Status: TODO

Acceptance:
- `CompetitionBrief`
- `Deliverable`
- `TaskDraft`
- `CalendarEventDraft`
- `ActionPlan`
- `ExternalAction`
- `ExternalActionResult`

### P0-002 — Brief extraction dry-run API

Status: TODO

Endpoint:
- `POST /briefs/extract`

Acceptance:
- Accepts text source.
- Returns valid `CompetitionBrief`.
- No external write.

### P0-003 — Generate ActionPlan

Status: TODO

Endpoint:
- `POST /plans/generate`

Acceptance:
- Converts CompetitionBrief into task/calendar/docs/sheets drafts.
- `dry_run=true`.
- `requires_approval=true` for writes.

### P0-004 — Approval Gate

Status: TODO

Endpoint:
- `POST /plans/{plan_id}/approve`

Acceptance:
- Approves selected action IDs.
- Does not execute unapproved actions.
- Returns audit records.

### P0-005 — MCP local server

Status: TODO

Tools:
- `extract_competition_brief`
- `generate_action_plan`
- `preview_external_actions`
- `approve_action_plan`

### P0-006 — Mock Google adapters

Status: TODO

Acceptance:
- Fake Drive / Docs / Sheets / Calendar adapters pass tests.
- Real adapters can be added later without changing domain logic.

## P1

### P1-001 — Google Docs real adapter

### P1-002 — Google Sheets real adapter

### P1-003 — Google Calendar real adapter

### P1-004 — Plane REST adapter

### P1-005 — Drive folder creation / move files

### P1-006 — Web ingestion through Playwright / Crawl4AI

## P2

### P2-001 — LangGraph workflow with human-in-the-loop

### P2-002 — Windmill workflow scripts

### P2-003 — Kubernetes deployment

### P2-004 — Observability with OpenTelemetry

Status: **In Progress** — Sprint 0 (tracer bootstrap) ✅, Sprint 2
(ExecutionService root + adapter_call spans) ✅, Sprint 3 (root-span
plan_id/actor attributes, adapter_call plan_id, result.status=failed →
span STATUS=ERROR mapping, M1 OTel auto-exception coverage) ✅,
Sprint 4 (FastAPI auto-instrumentation via FastAPIInstrumentor +
six MCP tool spans `mcp.tool.*` with attribute coverage; shared
decorators extracted to `telemetry/decorators.py`) ✅,
Sprint 5 (Counter `competitionops.actions.total` per lifecycle state,
Counter `competitionops.audit.records.total` per AuditRecord, Histogram
`competitionops.action.execution.duration_seconds` per adapter dispatch;
MeterProvider bootstrap via `setup_meter_provider(readers=...)`) ✅.

Next: Sprint 6 — (optional) console-exporter dev mode + OTLP exporter
wiring under the `otel` extra for production. See
`docs/10_p2_roadmap.md` for the full sprint sequence.

### P2-005 — Local OCR / layout parsing with GPU
