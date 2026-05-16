"""Process-level singletons shared by FastAPI / MCP / workflow.

Closes deep-review M4. Before this module existed, the three
singletons ``_plan_repo`` / ``_audit_log`` / ``_registry`` lived in
``competitionops.main`` (the FastAPI app module), with a duplicate set
in ``competitionops_mcp.server``. ``workflows/nodes.py`` reached into
``competitionops.main`` to fetch them, which:

1. Created a circular dependency papered over by a local ``import
   competitionops.main`` inside each node body.
2. Coupled the workflow package to FastAPI — running the workflow
   from a separate worker process (Windmill, Celery, a dedicated k8s
   Deployment) required loading the entire HTTP app.
3. Forced every workflow test to call ``main._plan_repo.cache_clear()``
   to reset state — touching a private name of an unrelated module.

After this module: ``main`` / ``mcp_server`` / ``workflows`` all
import these factories from here. The factories themselves are
unchanged — same ``lru_cache``, same env-driven switches as before —
so a worker process that imports ``competitionops.runtime`` (and
nothing FastAPI-related) gets a working PlanRepository / AuditLogPort
/ AdapterRegistry stack.

Test fixtures still work without modification because ``main`` and
``mcp_server`` re-import the SAME function objects from here:
``main._plan_repo is runtime._plan_repo``. Calling
``main._plan_repo.cache_clear()`` clears the canonical cache.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from competitionops.adapters.file_audit import FileAuditLog
from competitionops.adapters.file_plan_store import FilePlanRepository
from competitionops.adapters.memory_audit import InMemoryAuditLog
from competitionops.adapters.memory_plan_store import InMemoryPlanRepository
from competitionops.adapters.pdf_mock import MockPdfAdapter
from competitionops.adapters.registry import AdapterRegistry, build_default_registry
from competitionops.adapters.token_provider_google import GoogleOAuthTokenProvider
from competitionops.adapters.token_provider_static import StaticTokenProvider
from competitionops.config import get_settings
from competitionops.ports import (
    AuditLogPort,
    PdfIngestionPort,
    PlanRepository,
    TokenProvider,
    WebIngestionPort,
)

_KNOWN_PDF_ADAPTERS: tuple[str, ...] = ("mock", "docling")
_KNOWN_WEB_ADAPTERS: tuple[str, ...] = ("mock", "crawl4ai")


@lru_cache(maxsize=1)
def _plan_repo() -> PlanRepository:
    """Plan repository singleton.

    H2 — When ``Settings.plan_repo_dir`` is set (typically via
    ``PLAN_REPO_DIR=/var/lib/competitionops/plans``), plans persist to
    one ``<plan_id>.json`` file per plan under that directory.
    Otherwise the process-bound in-memory adapter is used (dev / unit
    tests). Mirrors the ``_audit_log`` switch from Tier 0 #4.

    Setting this env var alone does NOT make multi-replica prod safe —
    the audit log multi-writer fix (H3) must also be on. The prod
    ``replicas: 1`` pin in
    ``infra/k8s/overlays/prod/deployment-patch.yaml`` should only be
    lifted after both halves are in place; see the inline operator
    checklist in ``infra/k8s/README.md``.
    """
    plan_dir = get_settings().plan_repo_dir
    if plan_dir:
        return FilePlanRepository(base_dir=Path(plan_dir))
    return InMemoryPlanRepository()


@lru_cache(maxsize=1)
def _audit_log() -> AuditLogPort:
    """Audit log singleton.

    When ``Settings.audit_log_dir`` is set (typically via
    ``AUDIT_LOG_DIR=/var/lib/competitionops/audit``) records persist
    into per-(plan_id, writer_id) JSONL files there (Tier 0 #4 + H3).
    Otherwise the in-memory adapter is used — fine for dev / unit
    tests but loses records on process restart.
    """
    audit_dir = get_settings().audit_log_dir
    if audit_dir:
        return FileAuditLog(base_dir=Path(audit_dir))
    return InMemoryAuditLog()


@lru_cache(maxsize=1)
def _token_provider() -> TokenProvider | None:
    """OAuth access-token provider singleton for the Google adapters.

    Selection, highest-capability first:

    - Refresh-token trio set (``GOOGLE_OAUTH_REFRESH_TOKEN`` +
      ``GOOGLE_OAUTH_CLIENT_ID`` + ``GOOGLE_OAUTH_CLIENT_SECRET``) →
      ``GoogleOAuthTokenProvider``: access tokens are minted and
      refreshed automatically, so a PM never re-pastes an hourly bearer.
    - Static bearer only (``GOOGLE_OAUTH_ACCESS_TOKEN``) →
      ``StaticTokenProvider``: the operator re-supplies the token when
      it expires (the pre-refresh-port behaviour).
    - Neither → ``None``: the Google adapters stay in mock mode.

    Shared by all four Google adapters via ``_registry`` →
    ``build_default_registry``; ``GoogleOAuthTokenProvider`` holds an
    ``asyncio.Lock`` so the shared instance is concurrency-safe.
    """
    s = get_settings()
    if (
        s.google_oauth_refresh_token is not None
        and s.google_oauth_client_id
        and s.google_oauth_client_secret is not None
    ):
        return GoogleOAuthTokenProvider(
            client_id=s.google_oauth_client_id,
            client_secret=s.google_oauth_client_secret.get_secret_value(),
            refresh_token=s.google_oauth_refresh_token.get_secret_value(),
        )
    if s.google_oauth_access_token is not None:
        return StaticTokenProvider(s.google_oauth_access_token.get_secret_value())
    return None


@lru_cache(maxsize=1)
def _registry() -> AdapterRegistry:
    """Adapter registry singleton — the same mock-first + real-mode
    set every FastAPI request / MCP tool / workflow execute step uses.
    """
    return build_default_registry(token_provider=_token_provider())


@lru_cache(maxsize=1)
def _pdf_adapter() -> PdfIngestionPort:
    """PDF ingestion adapter singleton (P2-005 Sprint 3 + deep-review M6).

    Switches on ``Settings.pdf_adapter`` (env ``PDF_ADAPTER``):

    - ``None`` / ``"mock"`` (default): Sprint 0's ``MockPdfAdapter`` —
      strips ``%PDF-`` header and decodes the rest as UTF-8. Zero deps,
      fine for synthetic briefs and CI.
    - ``"docling"``: real layout-aware extraction via
      ``DoclingPdfAdapter``. Requires ``uv sync --extra ocr`` (Docling
      pulls in heavy ML / CV deps). Imported lazily inside this
      factory so the default install never pays the import cost.

    Unknown values raise ``ValueError`` rather than silently falling
    back to mock — operator typos must surface at startup.

    Wired through ``main.get_pdf_adapter`` (FastAPI ``Depends``) so
    tests can inject stubs via ``app.dependency_overrides`` without
    monkey-patching anything (deep-review M6).
    """
    raw = get_settings().pdf_adapter or "mock"
    choice = raw.lower()
    if choice == "mock":
        return MockPdfAdapter()
    if choice == "docling":
        # Lazy import — Docling pulls in torch / easyocr / pypdfium2.
        # Only operators who explicitly opted in (PDF_ADAPTER=docling)
        # pay this cost.
        #
        # Round-2 L5 — NO ``# type: ignore[import-not-found]`` here
        # (unlike ``main.py`` for OTLP and ``pdf_docling.py`` itself
        # for the Docling SDK). This import is a FIRST-PARTY module
        # path (``competitionops.adapters.pdf_docling``) that always
        # exists in the source tree; only its transitive ``docling``
        # SDK import may be missing. The ``except ModuleNotFoundError``
        # turns that missing SDK into a clear ``RuntimeError`` with
        # operator guidance — strictly better than mypy silence.
        try:
            from competitionops.adapters.pdf_docling import DoclingPdfAdapter
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "PDF_ADAPTER=docling requires the optional ``ocr`` extra. "
                "Run ``uv sync --extra ocr`` to install Docling, or unset "
                "PDF_ADAPTER to fall back to the mock adapter."
            ) from exc
        return DoclingPdfAdapter()
    raise ValueError(
        f"Unknown pdf_adapter / PDF_ADAPTER value: {raw!r} "
        f"(normalized to {choice!r}). Expected one of "
        f"{_KNOWN_PDF_ADAPTERS!r} (case-insensitive)."
    )


@lru_cache(maxsize=1)
def _web_adapter() -> WebIngestionPort:
    """Web ingestion adapter singleton (P1-006).

    Switches on ``Settings.web_adapter`` (env ``WEB_ADAPTER``):

    - ``None`` / ``"mock"`` (default): Sprint 0's ``MockWebAdapter`` —
      returns registered fixtures or deterministic synthetic content.
      No network. Fine for tests + CI + dry-run previews.
    - ``"crawl4ai"`` (Sprint 2): real Crawl4AI-backed browser scraping.
      Construction is side-effect-free (lazy import inside ``fetch``);
      the actual ``crawl4ai`` package is imported on first fetch.
      Missing ``[web]`` extra surfaces as ``RuntimeError`` with
      operator guidance on the first fetch, NOT at module import — so
      operators can flip ``WEB_ADAPTER=crawl4ai`` ahead of installing
      the extra without breaking pod startup.

    Unknown values raise ``ValueError`` — mirrors the round-3 M1
    pattern for PDF_ADAPTER. Wired through ``main.get_web_adapter``.
    """
    from competitionops.adapters.web_mock import MockWebAdapter

    raw = get_settings().web_adapter or "mock"
    choice = raw.lower()
    if choice == "mock":
        return MockWebAdapter()
    if choice == "crawl4ai":
        # Sprint 2 — real adapter. Construction is side-effect-free
        # (lazy import inside ``fetch``); the heavy ``crawl4ai``
        # package only loads on the first request. If the ``[web]``
        # extra is missing, ``fetch`` raises a clear RuntimeError
        # pointing at ``uv sync --extra web``.
        from competitionops.adapters.web_crawl4ai import Crawl4AIWebAdapter
        return Crawl4AIWebAdapter()
    raise ValueError(
        f"Unknown web_adapter / WEB_ADAPTER value: {raw!r} "
        f"(normalized to {choice!r}). Expected one of "
        f"{_KNOWN_WEB_ADAPTERS!r} (case-insensitive)."
    )
