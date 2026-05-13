# 07 — Backlog

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

### P2-005 — Local OCR / layout parsing with GPU
