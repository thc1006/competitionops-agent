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
from competitionops.adapters.registry import AdapterRegistry, build_default_registry
from competitionops.config import get_settings
from competitionops.ports import AuditLogPort, PlanRepository


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
def _registry() -> AdapterRegistry:
    """Adapter registry singleton — the same mock-first + real-mode
    set every FastAPI request / MCP tool / workflow execute step uses.
    """
    return build_default_registry()
