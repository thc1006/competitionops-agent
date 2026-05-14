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
hot path).** **M7 (2026-05-14) closed — ``_search_existing_issue`` now
(a) caps the ``search`` query parameter at 512 chars so a 10 KiB title
cannot push the GET URL past typical proxy / origin limits and silently
lose idempotency via 414, and (b) raises immediately on 401 / 403
instead of degrading to POST, so auth misconfiguration surfaces at the
real failure point. Non-auth 4xx / 5xx / network / malformed JSON all
keep degrading to POST (self-hosted Plane with search disabled still
creates issues).** **M8 (2026-05-14) closed — HTTPStatusError audit
field now goes through ``adapters/_http_errors.py::safe_error_summary``
which extracts ONLY string values from JSON ``error``/``detail``/
``message`` fields and falls back to ``<status> <reason>`` for HTML /
opaque / nested bodies. Previously a self-hosted Plane 5xx HTML stack
trace could leak internal hostnames, file paths, and occasional env
fragments into PM-visible audit records via the 200-char raw body
echo. Output now hard-capped at 200 chars regardless of input size.
Same helper wired into ``google_drive.py`` for symmetry — captive
portals / corporate proxies also interpose HTML on Drive endpoints.**

### P1-005 — Drive folder creation / move files

Status: **In Progress (2026-05-14)** — Drive adapter upgraded from Stage 4
stateful mock to mock-first + real-mode httpx-backed REST. Real mode
activates when both ``Settings.google_oauth_access_token`` (SecretStr)
and ``Settings.google_drive_api_base`` are set; partial config falls
back to deterministic mock. ``create_folder`` does GET-by-search
(Drive ``files.list`` with name + mimeType + parent + trashed=false)
before POST, returning the existing folder on match (Tier 0 #5
idempotency). Search-step failures (4xx/5xx, network, malformed JSON)
degrade to POST so self-hosted Drive shims with broken search still
create folders. Deep-review C1 honored: real mode short-circuits
``dry_run=True`` to a synthetic ``dry_run_<hash>`` preview WITHOUT any
HTTP call, so ``Settings.dry_run_default=True`` can never accidentally
write to Drive. Tests use ``httpx.MockTransport`` so the suite stays
offline. ``move_file`` / ``search_files`` stay mock until a follow-up
sprint. The Stage 4 "no httpx in adapter source" guard relaxed to
allow generic HTTP in ``google_drive`` only (Docs / Sheets / Calendar
remain pure mocks until P1-001~003).

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

**M3 (2026-05-14) closed** — Accumulative state fields now declare
``Annotated[list[dict[str, Any]], operator.add]`` so LangGraph appends
parallel writes instead of raising ``InvalidUpdateError`` (which is
what default last-value-wins channels do on concurrent updates).
Locked-in fields: ``executed`` / ``skipped`` / ``failed`` /
``blocked`` (execute outputs) and ``audit_records``. Single-writer
fields (``brief`` / ``plan`` / ``rejected_action_ids`` / caller
inputs) deliberately stay unannotated so graph replay doesn't
accumulate duplicates. The current graph is linear so behaviour for
the happy path is identical, but the contract is now safe for a
future ``Send``-based fan-out (e.g. one execute task per action). 3
new tests cover: structural Annotated metadata for accumulative
fields, defence against over-application on single-writer fields,
and an end-to-end ``Send`` API parallel-writers proof (used to raise
``InvalidUpdateError`` before this PR; now both writers' records
survive in the final state).

**M4 (2026-05-14) closed** — Process-level singletons
(``_plan_repo`` / ``_audit_log`` / ``_registry``) moved out of
``competitionops.main`` (and the duplicate set in
``competitionops_mcp.server``) into a new neutral
``src/competitionops/runtime.py``. ``main`` / ``mcp_server`` /
``workflows.nodes`` all import from there. The workflow no longer
needs the local ``from competitionops import main as main_module``
hack to dodge a circular dependency. A future worker process
(Windmill executor / Celery / dedicated k8s Deployment) can run the
LangGraph workflow without pulling in FastAPI by importing
``competitionops.runtime`` directly. Test fixtures unchanged —
``main._plan_repo is runtime._plan_repo`` (same function object), so
existing ``main_module._plan_repo.cache_clear()`` calls still target
the canonical lru_cache. 9 new tests in ``tests/test_runtime.py``:
runtime module surface, env-driven plan_repo / audit_log switches,
``main`` and ``mcp_server`` factories are runtime factories
by-identity, structural guard that workflows/nodes contains no
``from competitionops import main`` import, and an end-to-end check
that ``audit_node`` runs without first instantiating the FastAPI app.

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

Status: **Done (2026-05-14)** — Kustomize base (deployment + service +
configmap + secret.template + pvc) plus three overlays (dev / staging /
prod), each shipping its own ``namespace.yaml``. Hardened pod posture:
distroless ``python3-debian12:nonroot`` image, uid 65532, dropped ALL
caps, read-only root fs, automountServiceAccountToken=false,
RuntimeDefault seccomp, readinessProbe on ``/health`` + livenessProbe
on ``/healthz``. PVC ``competitionops-audit`` (5Gi RWX) backs Tier 0
#4's ``AUDIT_LOG_DIR``; dev overlay swaps PVC for emptyDir for
minikube / kind without RWX. Prod overlay: 1 replica (H2-pinned, see below) +
podAntiAffinity + nginx ingress with cert-manager letsencrypt-prod +
20rps rate limit. Staging: same shape with letsencrypt-staging.
``secret.template.yaml`` ships with seven empty key placeholders —
real values flow via external-secrets / sealed-secrets / kubectl.
Multi-stage Dockerfile builds with uv into a distroless runtime. 31
manifest tests parse YAML directly (no kustomize CLI dep), 32nd smoke
test calls ``kustomize build`` per overlay if the binary is available.

**H1 (2026-05-14) closed** — Each overlay now ships its own
``namespace.yaml`` declaring ``Namespace/competitionops-{env}``. Base
no longer ships a Namespace resource or pins a default namespace.
Previously the base's ``Namespace/competitionops`` was the ONLY
Namespace rendered into every overlay (because kustomize's
``namespace:`` field cannot rename a Namespace kind — it only rewrites
``metadata.namespace`` on namespaced resources), so
``kubectl apply -k overlays/dev/`` would create
``Namespace/competitionops`` and then fail to place the Deployment
into ``competitionops-dev`` which never got created.

**H2 (2026-05-14) closed** — Prod overlay re-pinned to ``replicas: 1``.
``_plan_repo()`` is a process-bound singleton over
``InMemoryPlanRepository``; with >1 pod a plan created on pod A is
invisible to pod B and ``POST /plans/{plan_id}/approve`` returns 404
whenever the LB lands the approval on a different pod. The
``podAntiAffinity`` block stays in the patch so the spread intent
survives until a shared ``PlanRepository`` adapter (SQLite-on-PVC,
Postgres, or Redis) lands — at which point replicas can climb back.
Audit-log RWX PVC stays provisioned for the same future scale-up.
Inline comments at ``infra/k8s/overlays/prod/deployment-patch.yaml`` +
``src/competitionops/main.py::_plan_repo`` cross-reference the
dependency so it can't get silently bumped.

**H2 follow-up (2026-05-14, capability shipped)** —
``FilePlanRepository`` adapter lands under
``src/competitionops/adapters/file_plan_store.py``: one JSON file per
``plan_id``, atomic-rename save (``os.replace``) so multi-pod readers
on a shared volume see either the old complete file or the new
complete file — never a partial. ``_plan_repo()`` in both
``main.py`` and ``competitionops_mcp/server.py`` honors
``Settings.plan_repo_dir`` (env ``PLAN_REPO_DIR``), mirroring how the
audit log honors ``AUDIT_LOG_DIR`` (Tier 0 #4). 16 new tests cover
round-trip / overwrite / list_all / atomic-rename / path-traversal
sanitisation / two-instance cross-pod simulation / FastAPI full
lifecycle with simulated pod restart. **Pin stays at replicas=1** —
lifting it also requires the H3 audit-log multi-writer fix.

**H3 (2026-05-14) closed** — ``FileAuditLog`` now writes one file per
``(plan_id, writer_id)`` instead of a single shared
``<plan_id>.jsonl``. Filename format becomes
``<plan_id>.<writer_id>.jsonl`` where ``writer_id`` defaults to
``socket.gethostname()`` — in a k8s pod that's ``metadata.name``, so
each pod automatically owns its own file with zero extra wiring.
Because writers no longer share a file, the multi-writer torn-write
race the deep review flagged is impossible *by construction*
regardless of RWX filesystem semantics (no reliance on ``fcntl.flock``
which has unreliable behaviour on NFS / Azure Files / EFS).
``list_for_plan`` globs ``<plan_id>.*.jsonl`` and merges across
writers, plus picks up the legacy ``<plan_id>.jsonl`` form for
in-place upgrades. 8 new tests cover: writer_id defaults to hostname /
explicit writer_id in filename / two writers two files / merge across
writers / no leak across plan_ids / 300-record multi-writer volume
test / writer_id path-traversal sanitisation / legacy single-file
backward compat. Pin stays at replicas=1 as deployment-policy default
— lifting it is now a one-line manifest change once operators wire
``PLAN_REPO_DIR``.

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

**M1 + M2 (2026-05-15) closed** — Two install-order footguns in the
OTel bootstrap surfaced for the first time. M1: when a MeterProvider
was already installed (e.g. ``tests/test_metrics.py``'s session
fixture had run), ``setup_meter_provider(readers=[...])`` silently
returned the existing provider and dropped the requested readers on
the floor — OTel SDK has no API to add readers post-construction.
``test_wire_otel_exporters_*_runs_without_error`` would pass while
the OTLP / Console exporters were never wired. After the fix, that
silent-drop branch raises ``OtelInstallOrderError`` with operator
guidance. M2: ``main.py`` previously called ``setup_tracer_provider``
twice (once at module-init, once inside ``_wire_otel_exporters``).
The wiring path now uses ``trace.get_tracer_provider()`` with an
isinstance check, so module-init is the single root of TracerProvider
installation; if module-init was bypassed (or an embedder swapped in a
non-SDK provider) the wiring raises rather than attaching span
processors to a Proxy. 5 new tests cover: happy path + readers-on-
empty-provider + readers-with-already-installed (raises) + AST
structural guard that the wiring function no longer calls
``setup_tracer_provider`` + isinstance failure raises. The two
existing ``runs_without_error`` smoke tests upgraded to also assert
the MeterProvider was actually installed (was passing for the wrong
reason).

### P2-005 — Local OCR / layout parsing with GPU

Status: **Sprint 0-3 Done (2026-05-15)** — ``PdfIngestionPort``
Protocol (``extract(pdf_bytes) -> str``) + ``MockPdfAdapter`` (strips
``%PDF-`` header, decodes the rest as UTF-8 — Sprint 0).
``BriefExtractor.extract_from_pdf(pdf_bytes)`` glues the port to the
existing text extractor and computes
``source_uri = "pdf://" + sha1(bytes)[:16]`` for audit-linkable
provenance (Sprint 1). ``POST /briefs/extract/pdf`` multipart endpoint
accepts ``UploadFile`` with a 10 MiB hard cap (413) + ``%PDF-`` magic
check (422); the Stage 5 OpenAPI guard now skips multipart bodies
since they don't carry a JSON schema (Sprint 2). Sprint 3 (2026-05-15)
lands ``DoclingPdfAdapter`` for real layout-aware extraction —
opt-in via ``PDF_ADAPTER=docling`` and ``uv sync --extra ocr`` (heavy
ML deps so default install stays light). ``runtime._pdf_adapter()``
factory switches on ``Settings.pdf_adapter`` with deterministic
``ValueError`` on unknown values (prevents silent fallback on operator
typo) and a friendlier ``RuntimeError`` pointing at the install path
when ``docling`` is missing. Sprints 4 (GPU) and 5 (Drive path)
deferred — they need either ``--extra ocr-gpu`` (future) or P1-005
real Drive adapter.

**M6 (2026-05-15) closed** — ``POST /briefs/extract/pdf`` now resolves
the adapter through ``Depends(get_pdf_adapter)`` instead of
constructing ``MockPdfAdapter()`` inline. Tests inject stubs via
``app.dependency_overrides`` without monkey-patching anything. A
structural AST guard (``test_pdf_upload_endpoint_does_not_hard_code_mock_pdf_adapter``)
walks the handler source and fails if a ``MockPdfAdapter(`` Call node
ever reappears. 10 new tests cover Settings field, runtime factory
defaults / explicit-mock / unknown-value-raises / singleton / port
satisfaction, endpoint DI replacement, AST structural guard, lazy
ImportError-when-missing path; 3 additional integration tests behind
``pytest.importorskip("docling")`` exercise the real engine after
``uv sync --extra ocr``.

**Round-2 H1 + H2 (2026-05-15) closed** — round-2 deep review flagged
two related issues in the Docling adoption path. **H1**: the
``async def extract_brief_from_pdf`` handler called
``extractor.extract_from_pdf(contents)`` directly. ``pdf_port.extract``
is sync; with ``PDF_ADAPTER=docling`` it runs 10-60s of ML inference
on a real PDF — blocking the worker's event loop for that duration,
which (combined with prod's H2 ``replicas: 1`` pin) is a cluster-wide
stall. Fix: wrap the call in ``fastapi.concurrency.run_in_threadpool``.
**H2**: ``DoclingPdfAdapter.extract`` bound ``tmp_path = Path(handle.name)``
AFTER ``handle.write(pdf_bytes)`` inside the ``with NamedTemporaryFile``
block. An OSError on write (disk full / quota) propagated past the
assignment, leaving ``tmp_path`` unbound and bypassing the outer
``try/finally`` cleanup — orphan ``.pdf`` files accumulated under
``$TMPDIR``. Fix: capture ``tmp_path`` first, move ``write_bytes`` into
the ``try`` body. 4 new tests: behavioural event-loop probe (stub
adapter checks ``asyncio.get_running_loop()`` raises iff offloaded),
AST structural guard that the handler invokes ``run_in_threadpool``,
AST ordering guard that ``tmp_path`` binds before any failable op in
``DoclingPdfAdapter.extract``, AST guard that the cleanup ``unlink``
lives inside a ``finally:`` block. The AST tests don't import
docling so they run on every CI without ``--extra ocr``.

**Round-2 M7 + M8 (2026-05-15) closed** — defence-in-depth against two
adapter information-leak surfaces. **M7**: ``plane_base_url`` and
``google_drive_api_base`` were plain ``str`` fields. An operator
typo like ``https//www.googleapis.com`` (missing colon) flowed into
the adapter URL builder and surfaced as an opaque httpx
``ConnectError`` at the first API call — not at startup. Fix: a
shared ``_validate_http_url`` helper called by ``@field_validator``
on both fields. Requires ``http://`` or ``https://`` scheme +
non-empty host; strips trailing slash; rejects empty string and
mid-scheme typos. **M8 round-2**: the ``except httpx.HTTPError``
non-status branch in Plane/Drive still rendered ``str(exc)``, which
httpx populates with the request URL — and our search URLs embed
user content (Drive's ``q=name='<folder>'``, Plane's
``search=<title>``). Token-like substrings in folder names / issue
titles would leak via that branch. Fix: new
``adapters/_http_errors.py::safe_network_summary`` returns
``"{target} network error: {ExceptionClassName}"`` and drops the
body entirely. Wired into both adapters' ``HTTPError`` branches
mirroring the round-1 ``safe_error_summary`` pattern. 25 new tests:
16 Settings validator (well-formed accept / typo reject / empty
reject / slash strip / None for optional / default for required) +
9 helper unit (drop-exc-body / target-prefix / parametrized sweep
of 6 httpx error classes / length cap). The two existing adapter
network-error tests upgraded — previously asserted ``"synthetic"
in error`` (the exact leak surface); now assert the class name IS
in the error AND the leak token is NOT.

**Round-2 M3 + M4 (2026-05-15) closed** — operational gaps closing
the H2-lift and Docling-deploy paths so operators don't discover
missing wiring at first request. **M3**: ``infra/k8s/base/configmap.yaml``
now ships commented ``PLAN_REPO_DIR`` (pointing at
``/var/lib/competitionops/audit/plans``, a subdir of the existing
audit PVC mount — zero infra change to lift the H2 pin) and
``PDF_ADAPTER`` placeholders. The H2 operator checklist in
``infra/k8s/README.md`` rewritten to name the configmap step and
the H3-build verification step explicitly. **M4**:
``infra/docker/Dockerfile`` exposes an ``INCLUDE_OCR`` build-arg
(``${INCLUDE_OCR:+--extra ocr}`` shell expansion adds Docling only
when non-empty). Default build stays slim; operators flipping
``PDF_ADAPTER=docling`` build with ``docker build --build-arg
INCLUDE_OCR=1 ...``. New README section "Enabling Docling" documents
the build-arg + configmap pair. 7 new tests: 3 configmap placeholder
guards (key in raw text, key NOT in active ``cm.data``, default path
references the audit subdir), 2 Dockerfile structural guards
(INCLUDE_OCR arg declared, default empty so slim build is preserved),
2 README content guards (H2 checklist references configmap +
INCLUDE_OCR pair).

**M5 (2026-05-14) closed** — PDF upload handler now reads the body in
1 MiB chunks and raises 413 the moment accumulated bytes overshoot
the 10 MiB cap. Before this fix, ``contents = await file.read()`` with
no size argument materialised the entire upload into a single Python
``bytes`` object before the size check ran, so a Content-Length: 10 GiB
client could OOM the pod even though the request was clearly
oversized. Three regression tests guard the fix: a spy verifies the
handler never calls ``read()`` without a positive size argument; a
behavioural test sends a 15 MiB body and asserts the total bytes read
stay under ``limit + 2 MiB`` chunk-overshoot allowance; a structural
test greps the handler source for ``file.read()`` to catch silent
reverts.
