import os

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, status
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from competitionops.adapters.pdf_mock import MockPdfAdapter
from competitionops.adapters.registry import AdapterRegistry
from competitionops.config import Settings, get_settings
from competitionops.ports import AuditLogPort, PlanRepository
from competitionops.schemas import (
    ActionPlan,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalResponse,
    BriefExtractRequest,
    CompetitionBrief,
    ExecutionRequest,
    PlanGenerateRequest,
)
from competitionops.services.brief_extractor import BriefExtractor
from competitionops.services.execution import ExecutionService, PlanNotFoundError
from competitionops.services.planner import CompetitionPlanner
from competitionops.telemetry import (
    OtelInstallOrderError,
    setup_meter_provider,
    setup_tracer_provider,
)


# ----------------------------------------------------------------------
# Sprint 6 — opt-in OTLP / console exporter wiring
# ----------------------------------------------------------------------


def _otel_exporters_enabled() -> bool:
    """True when any production telemetry exporter has been requested."""
    return (
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") is not None
        or os.environ.get("COMPETITIONOPS_OTEL_CONSOLE") == "1"
    )


def _wire_otel_exporters() -> None:
    """Attach span processors + meter readers based on env.

    Two opt-in switches:

    - ``OTEL_EXPORTER_OTLP_ENDPOINT=https://otel-collector:4317`` —
      attaches OTLP gRPC exporters for traces + metrics. Requires
      ``uv sync --extra otel`` so ``opentelemetry-exporter-otlp`` is
      installed. The exporter package picks up the standard OTel env
      vars (``OTEL_EXPORTER_OTLP_HEADERS``, ``OTEL_SERVICE_NAME``, etc.)
      without further configuration.

    - ``COMPETITIONOPS_OTEL_CONSOLE=1`` — attaches console exporters
      (traces + metrics) for local dev. Only requires the ``sdk`` base
      dep so no extra install needed.

    Both flags can be set simultaneously; processors stack.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.metrics.export import (
        MetricReader,
        PeriodicExportingMetricReader,
    )
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # M2 — Module-init below already installed the SDK TracerProvider
    # for FastAPI auto-instrumentation. We look it up via the global
    # accessor (NOT a second ``setup_tracer_provider()`` call) so the
    # ownership chain stays a single root: one install, one consumer.
    # If the global is somehow not an SDK provider here (module-init
    # bypassed, embedder swapped it), fail loudly — attaching span
    # processors to a Proxy / NoOp provider is silent data loss.
    tracer_provider = trace.get_tracer_provider()
    if not isinstance(tracer_provider, TracerProvider):
        raise OtelInstallOrderError(
            "TracerProvider is not an SDK ``TracerProvider`` "
            f"({type(tracer_provider).__name__}). Module-init at the "
            "bottom of main.py must install one before this wiring "
            "function runs. If an embedder replaced the provider, "
            "they need to also call setup_tracer_provider() before "
            "loading competitionops.main."
        )
    metric_readers: list[MetricReader] = []

    if os.environ.get("COMPETITIONOPS_OTEL_CONSOLE") == "1":
        from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        tracer_provider.add_span_processor(
            BatchSpanProcessor(ConsoleSpanExporter())
        )
        metric_readers.append(
            PeriodicExportingMetricReader(
                ConsoleMetricExporter(),
                export_interval_millis=60000,
            )
        )

    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        # Lazy import: only the production deployment path needs the
        # gRPC exporter package, which is gated behind the ``otel`` extra.
        # Dual ignore code: ``import-not-found`` covers dev venvs without
        # ``--extra otel``; ``unused-ignore`` keeps mypy silent when the
        # extra IS installed.
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (  # type: ignore[import-not-found, unused-ignore]
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore[import-not-found, unused-ignore]
            OTLPSpanExporter,
        )

        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter())
        )
        metric_readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(),
                export_interval_millis=60000,
            )
        )

    if metric_readers:
        setup_meter_provider(readers=metric_readers)


# Always install the SDK TracerProvider so the FastAPI auto-instrumentation
# middleware can emit real (not NonRecording) spans even in dev. Idempotent
# under pytest reimports. The MeterProvider stays as ProxyMeterProvider
# unless an exporter is opted in (see _wire_otel_exporters), so test files
# can still attach their own InMemoryMetricReader during the test session.
setup_tracer_provider()

if _otel_exporters_enabled():
    _wire_otel_exporters()


app = FastAPI(title="CompetitionOps Agent", version="0.1.0")

# Attach OTel auto-instrumentation. ``instrument_app`` adds an ASGI
# middleware that creates a SERVER-kind span per request with attributes
# ``http.route / http.method / http.status_code``. Sprint 4 P2-004.
FastAPIInstrumentor.instrument_app(app)


# M4 — singletons live in ``competitionops.runtime`` so the workflow
# package (and any future worker process) can reach them without
# importing FastAPI. ``main._plan_repo is runtime._plan_repo`` — same
# function object, same lru_cache, so existing test fixtures that do
# ``main_module._plan_repo.cache_clear()`` keep targeting the canonical
# cache and don't accidentally clear a parallel singleton.
from competitionops.runtime import _audit_log, _plan_repo, _registry  # noqa: E402


def get_plan_repo() -> PlanRepository:
    return _plan_repo()


def get_audit_log() -> AuditLogPort:
    return _audit_log()


def get_registry() -> AdapterRegistry:
    return _registry()


def get_execution_service(
    plan_repo: PlanRepository = Depends(get_plan_repo),
    registry: AdapterRegistry = Depends(get_registry),
    audit: AuditLogPort = Depends(get_audit_log),
    settings: Settings = Depends(get_settings),
) -> ExecutionService:
    return ExecutionService(
        plan_repo=plan_repo,
        registry=registry,
        audit_log=audit,
        settings=settings,
    )


# ----------------------------------------------------------------------
# Health
# ----------------------------------------------------------------------


@app.get("/healthz")
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ----------------------------------------------------------------------
# Brief extraction
# ----------------------------------------------------------------------


@app.post("/briefs/extract", response_model=CompetitionBrief)
async def extract_brief(
    payload: BriefExtractRequest,
    settings: Settings = Depends(get_settings),
) -> CompetitionBrief:
    # Tier 0 #1: source_type=url|drive passes the SSRF allow-list at
    # validation time, but the real fetch implementation lands in P1-006.
    # Refuse explicitly with 501 so callers don't think the call silently
    # succeeded.
    if payload.source_type != "text":
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                f"source_type={payload.source_type!r} is not implemented in "
                f"the MVP; URL / Drive ingestion lands in P1-006. The SSRF "
                f"allow-list has already validated {payload.source_uri!r}."
            ),
        )
    extractor = BriefExtractor(settings=settings)
    return extractor.extract_from_text(
        content=payload.content, source_uri=payload.source_uri
    )


# ----------------------------------------------------------------------
# P2-005 — PDF upload extraction
# ----------------------------------------------------------------------

_PDF_UPLOAD_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB hard cap, prevents pdf bombs
_PDF_UPLOAD_CHUNK_BYTES = 1 * 1024 * 1024  # 1 MiB read step
_PDF_MAGIC_BYTES = b"%PDF-"


@app.post("/briefs/extract/pdf", response_model=CompetitionBrief)
async def extract_brief_from_pdf(
    file: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
) -> CompetitionBrief:
    """Upload a PDF, extract a structured ``CompetitionBrief``.

    P2-005 Sprint 0-2 ships the mock PDF adapter — it treats the bytes
    after the ``%PDF-`` header as plain UTF-8 text. The Docling-backed
    real adapter lands in Sprint 3 under ``--extra ocr`` and will be
    selected here via a future ``Settings.pdf_adapter`` switch.

    Validations:
    - size ≤ 10 MiB (413 Request Entity Too Large otherwise)
    - file content must start with ``%PDF-`` magic (422 otherwise)

    Deep-review M5: the body is consumed in ``_PDF_UPLOAD_CHUNK_BYTES``
    chunks and the 413 is raised the moment accumulated bytes overshoot
    the cap. This keeps the largest in-process allocation bounded by
    ``limit + chunk`` instead of growing to whatever the client decided
    to send.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_PDF_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > _PDF_UPLOAD_MAX_BYTES:
            raise HTTPException(
                status_code=413,  # Content Too Large
                detail=(
                    f"PDF size exceeds the {_PDF_UPLOAD_MAX_BYTES}-byte "
                    "limit (refused mid-stream after reading "
                    f"{total} bytes)."
                ),
            )
        chunks.append(chunk)
    contents = b"".join(chunks)

    if not contents.startswith(_PDF_MAGIC_BYTES):
        raise HTTPException(
            status_code=422,  # Unprocessable Content
            detail=(
                "uploaded file does not start with the PDF magic bytes "
                f"({_PDF_MAGIC_BYTES.decode('ascii')!r})"
            ),
        )

    pdf_port = MockPdfAdapter()
    extractor = BriefExtractor(settings=settings, pdf_port=pdf_port)
    return extractor.extract_from_pdf(contents)


# ----------------------------------------------------------------------
# Plan generation
# ----------------------------------------------------------------------


@app.post("/plans/generate", response_model=ActionPlan)
async def generate_plan(
    payload: PlanGenerateRequest,
    settings: Settings = Depends(get_settings),
    plan_repo: PlanRepository = Depends(get_plan_repo),
) -> ActionPlan:
    planner = CompetitionPlanner(settings=settings)
    plan = planner.generate(
        competition=payload.competition,
        team_capacity=payload.team_capacity,
    )
    plan.requires_approval = (
        payload.preferences.pm_approval_required or plan.requires_approval
    )
    plan_repo.save(plan)
    return plan


# ----------------------------------------------------------------------
# Combined approve + execute (legacy, kept for backward compatibility)
# ----------------------------------------------------------------------


@app.post("/plans/{plan_id}/approve", response_model=ApprovalResponse)
async def approve_and_execute_plan(
    plan_id: str,
    payload: ApprovalRequest,
    service: ExecutionService = Depends(get_execution_service),
) -> ApprovalResponse:
    try:
        return await service.approve_and_execute(
            plan_id=plan_id,
            approved_action_ids=payload.approved_action_ids,
            approved_by=payload.approved_by,
            allow_reexecute=payload.allow_reexecute,
        )
    except PlanNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


# ----------------------------------------------------------------------
# Two-phase API: approve-only and run-only
# ----------------------------------------------------------------------


@app.post("/approvals/{plan_id}/approve", response_model=ApprovalDecision)
async def approve_plan(
    plan_id: str,
    payload: ApprovalRequest,
    service: ExecutionService = Depends(get_execution_service),
) -> ApprovalDecision:
    try:
        return service.approve_actions(
            plan_id=plan_id,
            approved_action_ids=payload.approved_action_ids,
            approved_by=payload.approved_by,
        )
    except PlanNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


@app.post("/executions/{plan_id}/run", response_model=ApprovalResponse)
async def run_plan_executions(
    plan_id: str,
    payload: ExecutionRequest,
    service: ExecutionService = Depends(get_execution_service),
) -> ApprovalResponse:
    try:
        return await service.run_approved(
            plan_id=plan_id,
            executed_by=payload.executed_by,
            action_ids=payload.action_ids,
            allow_reexecute=payload.allow_reexecute,
        )
    except PlanNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
