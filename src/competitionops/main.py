import ipaddress
import os
import re
import socket

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel, field_validator

from competitionops.adapters.google_drive import GoogleDriveAdapter

from competitionops.adapters.registry import AdapterRegistry
from competitionops.adapters.token_provider_google import TokenRefreshError
from competitionops.config import Settings, get_settings
from competitionops.ports import (
    AuditLogPort,
    PdfIngestionPort,
    PlanRepository,
    WebIngestionPort,
)
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
from competitionops.runtime import (  # noqa: E402
    _audit_log,
    _pdf_adapter,
    _plan_repo,
    _registry,
    _web_adapter,
)


def get_plan_repo() -> PlanRepository:
    return _plan_repo()


def get_audit_log() -> AuditLogPort:
    return _audit_log()


def get_registry() -> AdapterRegistry:
    return _registry()


def get_pdf_adapter() -> PdfIngestionPort:
    """FastAPI dependency that resolves the PDF parser engine via
    ``runtime._pdf_adapter()`` (deep-review M6). Test fixtures inject
    stubs by overriding this in ``app.dependency_overrides``."""
    return _pdf_adapter()


def get_web_adapter() -> WebIngestionPort:
    """FastAPI dependency that resolves the web ingestion adapter via
    ``runtime._web_adapter()``. Test fixtures inject stubs by
    overriding in ``app.dependency_overrides`` — symmetric to
    ``get_pdf_adapter``."""
    return _web_adapter()


def get_drive_adapter() -> GoogleDriveAdapter:
    """FastAPI dependency for the ``/briefs/extract/drive`` read path
    (P2-005 Sprint 5).

    Constructs a fresh ``GoogleDriveAdapter`` (reads ``get_settings()``
    internally for the bearer-only ``real_mode`` switch). There's no
    runtime singleton factory like ``_pdf_adapter`` / ``_web_adapter``
    because Drive has no env-driven engine choice — mock-vs-real is
    decided by the presence of the OAuth token, not a ``*_ADAPTER``
    env. Tests inject a pre-seeded adapter via
    ``app.dependency_overrides``."""
    return GoogleDriveAdapter()


def _eager_validate_runtime_config() -> None:
    """Round-3 M1 — fail fast at module import on invalid runtime
    config so the pod never reaches a state where ``/health`` is green
    but a request-path adapter would explode.

    Concretely: call ``_pdf_adapter()`` and ``_web_adapter()`` once.
    Each factory raises ``ValueError`` on unknown values (typo'd
    ``PDF_ADAPTER`` / ``WEB_ADAPTER``) and ``ImportError`` / ``RuntimeError``
    when a value names a real backend whose deps aren't installed.
    Surfacing both at module import means uvicorn aborts before
    binding the port → CrashLoopBackoff with the offending value in
    the log, instead of a silent "first request 503s" hours later.

    Idempotent (lru_cache(1) on each factory) and side-effect-free
    for the default ``mock`` adapters. Safe to keep at module top level.
    """
    _pdf_adapter()
    _web_adapter()


_eager_validate_runtime_config()


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


def _assert_pdf_within_cap(contents: bytes, *, source: str) -> None:
    """Raise 413 if a fully-materialised PDF blob exceeds the size cap.

    For the post-download case (Drive ingestion). The multipart upload
    endpoint deliberately does NOT use this — it caps mid-stream while
    reading (deep-review M5) so it never materialises an over-cap blob
    in the first place. ``source`` is a human label for the message
    (e.g. ``"Drive file"``)."""
    if len(contents) > _PDF_UPLOAD_MAX_BYTES:
        raise HTTPException(
            status_code=413,  # Content Too Large
            detail=(
                f"{source} exceeds the {_PDF_UPLOAD_MAX_BYTES}-byte limit "
                f"({len(contents)} bytes)."
            ),
        )


def _assert_pdf_magic(contents: bytes, *, source: str) -> None:
    """Raise 422 if the blob does not start with the ``%PDF-`` magic.

    Shared by every PDF-ingestion endpoint (multipart upload + Drive)
    so a non-PDF never reaches the extractor. ``source`` is a human
    label for the message (e.g. ``"uploaded file"`` / ``"Drive file"``)."""
    if not contents.startswith(_PDF_MAGIC_BYTES):
        raise HTTPException(
            status_code=422,  # Unprocessable Content
            detail=(
                f"{source} does not start with the PDF magic bytes "
                f"({_PDF_MAGIC_BYTES.decode('ascii')!r})"
            ),
        )


@app.post("/briefs/extract/pdf", response_model=CompetitionBrief)
async def extract_brief_from_pdf(
    file: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
    pdf_port: PdfIngestionPort = Depends(get_pdf_adapter),
) -> CompetitionBrief:
    """Upload a PDF, extract a structured ``CompetitionBrief``.

    The PDF parser engine is resolved through ``Depends(get_pdf_adapter)``
    (Sprint 3 / deep-review M6). Default is the Sprint 0 ``MockPdfAdapter``
    (zero deps, decodes bytes as UTF-8 — fine for synthetic briefs).
    Setting ``PDF_ADAPTER=docling`` switches to layout-aware extraction
    via Docling (requires ``uv sync --extra ocr``).

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

    # Size is already capped mid-stream above (M5); only the magic
    # check is shared with the Drive ingestion path.
    _assert_pdf_magic(contents, source="uploaded file")

    extractor = BriefExtractor(settings=settings, pdf_port=pdf_port)
    # Round-2 H1 — ``pdf_port.extract`` is sync. With ``PDF_ADAPTER=docling``
    # it can take 10-60 s of ML inference on a real PDF, which would
    # block this worker's event loop for that entire duration. H2 still
    # pins prod to ``replicas: 1``, so a blocked worker is a cluster-wide
    # stall. ``run_in_threadpool`` offloads to a worker thread; FastAPI
    # awaits while the thread runs, so other concurrent requests on this
    # worker keep progressing. The wrap covers the WHOLE
    # ``BriefExtractor.extract_from_pdf`` call, not just the port call,
    # because the regex extraction in the service is also (lightly) CPU-
    # bound and the call site shouldn't peer into the implementation.
    return await run_in_threadpool(extractor.extract_from_pdf, contents)


# ----------------------------------------------------------------------
# Web ingestion (P1-006)
# ----------------------------------------------------------------------


_IpAddr = ipaddress.IPv4Address | ipaddress.IPv6Address


def _resolve_host_to_addresses(host: str) -> list[_IpAddr]:
    """Return all IP addresses the host resolves to, or ``[]`` if
    resolution fails.

    Two paths:
    - IP literal (``127.0.0.1`` / ``::1``): parsed directly via
      ``ipaddress.ip_address`` — no DNS lookup.
    - Hostname: ``socket.getaddrinfo`` is the canonical resolver,
      returns IPv4 + IPv6 records. ``gaierror`` → return ``[]``
      (lenient: unresolvable hosts can't hit internal infra anyway,
      and this keeps RFC-6761 ``.invalid`` / ``.test`` URLs usable
      as offline test fixtures).

    Module-level so tests can monkey-patch ``socket.getaddrinfo``.
    """
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass  # not an IP literal — fall through to DNS

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []

    addresses: list[_IpAddr] = []
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        # IPv4: (host, port). IPv6: (host, port, flowinfo, scope_id).
        # Both have a str at index 0 — cast for mypy.
        ip_str = str(sockaddr[0])
        # Strip IPv6 zone identifier if a resolver returns one.
        if "%" in ip_str:
            ip_str = ip_str.split("%", 1)[0]
        try:
            addresses.append(ipaddress.ip_address(ip_str))
        except ValueError:
            continue
    return addresses


def _is_non_routable_address(addr: _IpAddr) -> bool:
    """SSRF filter predicate. True if the address falls in any range
    that should not be reachable from PM-supplied URLs:

    - ``is_loopback`` — 127.0/8, ::1/128
    - ``is_private``  — RFC-1918 v4 (10/8, 172.16/12, 192.168/16) +
      RFC-4193 v6 ULA (fc00::/7)
    - ``is_link_local`` — 169.254/16 (incl. cloud metadata
      169.254.169.254 — AWS / GCP / Azure all expose secrets here)
      + IPv6 fe80::/10
    - ``is_reserved`` — RFC-6890 future-use / IETF protocol assignments
    - ``is_multicast`` — 224.0.0.0/4 + ff00::/8 (not a typical SSRF
      target but no legitimate use as a brief URL)
    - ``is_unspecified`` — 0.0.0.0 / ``::`` (could map to all
      interfaces on some implementations)

    These categories overlap (loopback is also private on v4 etc.)
    but together cover the SSRF target set.
    """
    return bool(
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


class _UrlIngestRequest(BaseModel):
    """Request body for ``POST /briefs/extract/url``.

    ``url`` is validated for scheme at the Pydantic layer (only
    ``http(s)://`` accepted) so file://, javascript:, data:, ftp:
    etc. never reach the web adapter. Defence-in-depth for when
    Sprint 2 wires Crawl4AI — its browser engine can be coaxed into
    reading local files via ``file://`` URLs, which would be a
    sandbox-escape vector if exposed to PM-controlled input.

    **KNOWN GAP — SSRF SURFACE (Sprint 2 responsibility)**. The scheme
    allow-list is necessary but NOT sufficient. Even with http(s)
    enforced, PM-controlled URLs can still target internal infrastructure:

    - ``http://localhost:8080/admin`` — local services on the API host
    - ``http://10.0.0.0/8`` / ``172.16.0.0/12`` / ``192.168.0.0/16`` —
      RFC-1918 private networks the API can reach
    - ``http://169.254.169.254/latest/meta-data/`` — cloud instance
      metadata endpoints (AWS / GCP / Azure all expose secrets here)
    - ``http://[::1]/`` / ``http://[fe80::]/`` — IPv6 loopback / link-local

    Sprint 0's ``MockWebAdapter`` cannot actually exfiltrate, but
    Sprint 2's browser-backed adapter WILL follow these. **Before
    Sprint 2 ships, this validator MUST grow IP-level filtering**:
    resolve hostname → reject any IP in private / loopback / link-local
    / metadata ranges. Alternatives: enforce egress through a proxy
    that does the filtering, or run the adapter in a network namespace
    that can only reach the public internet.

    Tracked as "P1-006 Sprint 1: SSRF filtering" — must land before
    Sprint 2's real adapter.
    """

    url: str

    @field_validator("url")
    @classmethod
    def _validate_url_safety(cls, v: str) -> str:
        from urllib.parse import urlparse

        if not v:
            raise ValueError("url must not be empty")
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"url scheme must be http or https; got {parsed.scheme!r}"
            )
        if not parsed.netloc:
            raise ValueError("url must include a host component")

        # P1-006 Sprint 1 — SSRF filter. The scheme allow-list above
        # blocks file:// / javascript: / data: / ftp:, but any http(s)
        # URL can still target internal infrastructure. Resolve the
        # host (IP literal OR DNS) and reject if ANY resulting address
        # falls in a non-routable range.
        host = parsed.hostname
        if host is None:
            raise ValueError("url must include a host component")
        # Strip IPv6 zone identifier (e.g. ``fe80::1%eth0``) — Python's
        # ipaddress parser rejects zones, but link-local addresses with
        # zones are still a banned shape.
        if "%" in host:
            host = host.split("%", 1)[0]

        addresses = _resolve_host_to_addresses(host)
        for addr in addresses:
            if _is_non_routable_address(addr):
                raise ValueError(
                    f"url resolves to a non-routable address ({addr}); "
                    "loopback / RFC-1918 private / link-local (incl. "
                    "cloud-metadata 169.254.169.254) / IPv6 ULA / "
                    "reserved / unspecified ranges are blocked to "
                    "prevent SSRF"
                )
        return v


@app.post("/briefs/extract/url", response_model=CompetitionBrief)
async def extract_brief_from_url(
    payload: _UrlIngestRequest,
    settings: Settings = Depends(get_settings),
    web_port: WebIngestionPort = Depends(get_web_adapter),
) -> CompetitionBrief:
    """Fetch a URL via the web ingestion port and return a structured
    ``CompetitionBrief``.

    Sprint 0: the default ``MockWebAdapter`` returns canned content —
    use ``app.dependency_overrides[get_web_adapter]`` in tests to
    inject pre-registered fixtures. Sprint 2 swaps in a Crawl4AI /
    Playwright-backed adapter behind ``WEB_ADAPTER=crawl4ai`` (gated
    by the ``[web]`` extra).

    The adapter's canonical URL (post-redirect) becomes the brief's
    ``source_uri`` — may differ from the request URL when the page
    redirects.
    """
    result = await web_port.fetch(payload.url)
    extractor = BriefExtractor(settings=settings)
    return extractor.extract_from_text(content=result.text, source_uri=result.url)


# ----------------------------------------------------------------------
# Drive PDF ingestion (P2-005 Sprint 5)
# ----------------------------------------------------------------------


# Google Drive file ids are base64url-ish — ``[A-Za-z0-9_-]``, ~33
# chars. The endpoint interpolates ``file_id`` raw into the Drive API
# URL path, so the charset MUST be constrained at the Pydantic layer:
# an unconstrained id permits query-param injection (e.g.
# ``?acknowledgeAbuse=true`` forces download of an abuse-flagged file)
# and path traversal within googleapis.com (``../../oauth2/v3/...``
# fired with the operator's Bearer token). The 256-char ceiling is a
# generous upper bound — real ids are far shorter — that also caps
# pathological inputs.
_DRIVE_FILE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,256}")


class _DriveIngestRequest(BaseModel):
    """Request body for ``POST /briefs/extract/drive`` — a Google Drive
    file id whose PDF content is pulled and run through the brief
    extractor."""

    file_id: str

    @field_validator("file_id")
    @classmethod
    def _valid_drive_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("file_id must not be empty")
        if not _DRIVE_FILE_ID_RE.fullmatch(v):
            raise ValueError(
                "file_id must be a Google Drive file id — characters "
                "[A-Za-z0-9_-] only, max 256. Reject query / path / "
                "fragment characters so a crafted id cannot inject "
                "params or traverse the Drive API URL."
            )
        return v


@app.post("/briefs/extract/drive", response_model=CompetitionBrief)
async def extract_brief_from_drive(
    payload: _DriveIngestRequest,
    settings: Settings = Depends(get_settings),
    pdf_port: PdfIngestionPort = Depends(get_pdf_adapter),
    drive_adapter: GoogleDriveAdapter = Depends(get_drive_adapter),
) -> CompetitionBrief:
    """Pull a PDF from a Google Drive file id and return a structured
    ``CompetitionBrief``.

    Reuses the ``/briefs/extract/pdf`` pipeline — same PDF adapter
    (``PDF_ADAPTER`` env), same 10 MiB cap, same ``%PDF-`` magic gate —
    and stamps ``source_uri = drive://<file_id>`` for audit provenance.

    Reading a Drive file is a low-risk action (CLAUDE.md rule #4's
    approval gate covers move / delete / permission changes, not
    reads), so this path has no dry_run machinery.

    Known limitation: the size cap is enforced AFTER the full download
    (the bytes are already in memory). The multipart endpoint streams +
    caps mid-upload because a client controls the size; here the file
    id is operator-chosen and a competition brief PDF is small, so a
    post-download check is the pragmatic v1. A streaming variant is a
    follow-up if huge-file abuse becomes a concern.
    """
    try:
        pdf_bytes = await drive_adapter.download_file(file_id=payload.file_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail=f"Drive file not found: {payload.file_id!r}",
            )
        raise HTTPException(
            status_code=502,
            detail=(
                f"Drive returned status {exc.response.status_code} for the "
                "file download"
            ),
        )
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # M8 / M4 redaction — class name only. ``str(exc)`` embeds the
        # request URL (which carries the file id, potentially PM-pasted
        # content); never echo it.
        raise HTTPException(
            status_code=502,
            detail=f"Drive download failed: {type(exc).__name__}",
        )
    except TokenRefreshError as exc:
        # The TokenProvider could not mint an access token (refresh token
        # expired / revoked, OAuth endpoint down). ``exc`` carries only a
        # pre-redacted summary — safe to surface in the response detail.
        raise HTTPException(
            status_code=502,
            detail=f"Drive auth failed: {exc}",
        )

    # Post-download cap + magic gate — shared helpers, same checks the
    # multipart endpoint applies (it caps mid-stream, then both share
    # the magic check).
    _assert_pdf_within_cap(pdf_bytes, source="Drive file")
    _assert_pdf_magic(pdf_bytes, source="Drive file")

    extractor = BriefExtractor(settings=settings, pdf_port=pdf_port)
    # Round-2 H1 — offload the sync (potentially ML-heavy with Docling)
    # extraction to a worker thread; see extract_brief_from_pdf.
    return await run_in_threadpool(
        extractor.extract_from_pdf, pdf_bytes, f"drive://{payload.file_id}"
    )


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
