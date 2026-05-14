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

Status: **Done (2026-05-14)** — Plane adapter upgraded from Stage 0 stub
to mock-first + real-mode httpx-backed REST. Real mode activated when
all four Settings fields are present (``plane_base_url``,
``plane_api_key`` (SecretStr), ``plane_workspace_slug``,
``plane_project_id``); otherwise falls back to deterministic mock. Tests
use ``httpx.MockTransport`` so the suite stays offline. Tier 0 #3
closed — every executed audit record now surfaces ``target_external_id``
including Plane. Tier 0 #5 closed — real mode does GET-by-search before
POST, returning the existing issue on match (idempotent across repeated
approvals). Search-step failures degrade to plain POST so self-hosted
instances with search disabled still work. **C1 (2026-05-14) closed —
real mode now honors ``dry_run=True`` by short-circuiting BEFORE any
HTTP call and returning a synthetic ``dry_run_<sha1(title)[:8]>``
preview. Previously a preview against a fully-configured Plane would
silently create a real issue (Settings.dry_run_default=True is the
hot path).**

### P1-005 — Drive folder creation / move files

### P1-006 — Web ingestion through Playwright / Crawl4AI

## P2

### P2-001 — LangGraph workflow with human-in-the-loop

Status: **Done (2026-05-14)** — Five-node ``StateGraph`` (extract → plan →
approve → execute → audit) compiled with ``interrupt_before=["approve"]``
and a ``MemorySaver`` checkpointer. Caller invokes the graph, gets a
paused state after ``plan``, supplies ``approved_action_ids`` via
``graph.update_state``, then resumes — at which point the existing
``ExecutionService`` runs the approved actions through the mock-first
adapter registry and ``audit_node`` snapshots the resulting audit
records into final state. 11 tests cover state round-trip, each node's
invariant, the interrupt behavior (zero adapter calls before resume),
the post-approval execute + audit path, and ``MemorySaver`` persistence
across graph reconstruction (same ``thread_id`` survives a "process
restart"). Production deployments can swap the saver for
``SqliteSaver`` / ``PostgresSaver`` without changing the graph shape.

### P2-002 — Windmill workflow scripts

Status: **Done (2026-05-14)** — Three Windmill rawscripts under
``infra/windmill/scripts/`` (extract_brief / generate_plan /
approve_and_execute), each ``def main(...) -> dict`` reading
``WINDMILL_API_BASE`` from env. A flow YAML under
``infra/windmill/flows/competition_pipeline.yaml`` chains them with a
``suspend`` step where the PM picks ``approved_action_ids`` (7-day
timeout). README walks through importing the flow into a local
Windmill instance. 8 tests use ``httpx.MockTransport`` + ``TestClient``
so pytest never opens a real socket and all three scripts are
exercised both individually and as a composed pipeline.

### P2-003 — Kubernetes deployment

Status: **Done (2026-05-14)** — Kustomize base (namespace + deployment
+ service + configmap + secret.template + pvc) plus three overlays
(dev / staging / prod). Hardened pod posture: distroless
``python3-debian12:nonroot`` image, uid 65532, dropped ALL caps,
read-only root fs, automountServiceAccountToken=false, RuntimeDefault
seccomp, readinessProbe on ``/health`` + livenessProbe on
``/healthz``. PVC ``competitionops-audit`` (5Gi RWX) backs Tier 0 #4's
``AUDIT_LOG_DIR``; dev overlay swaps PVC for emptyDir for minikube /
kind without RWX. Prod overlay: 3 replicas + podAntiAffinity + nginx
ingress with cert-manager letsencrypt-prod + 20rps rate limit.
Staging: same shape with letsencrypt-staging. ``secret.template.yaml``
ships with seven empty key placeholders — real values flow via
external-secrets / sealed-secrets / kubectl. Multi-stage Dockerfile
builds with uv into a distroless runtime. 28 manifest tests parse YAML
directly (no kustomize CLI dep), 29th smoke test calls
``kustomize build`` per overlay if the binary is available.

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

Sprint 6 — opt-in OTLP / console exporter wiring driven by env
(``OTEL_EXPORTER_OTLP_ENDPOINT`` for OTLP gRPC, requires ``uv sync
--extra otel``; ``COMPETITIONOPS_OTEL_CONSOLE=1`` for console dev mode,
no extra needed). Default behavior unchanged — exporters stay off unless
explicitly opted in. ✅

P2-004 main track complete. Remaining: optional polish (Sprint 6+
metric attribute review, custom resource attributes for service.name).

### P2-005 — Local OCR / layout parsing with GPU
