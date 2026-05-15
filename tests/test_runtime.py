"""M4 — ``competitionops.runtime`` is the neutral home for the three
process-level singletons (``_plan_repo`` / ``_audit_log`` /
``_registry``).

Before this commit, those factories lived in ``competitionops.main``
and ``competitionops_mcp.server``. ``workflows/nodes.py`` had to do a
local-import dance to dodge the circular dependency they created.

After this commit:
- All three factories live in ``competitionops.runtime``.
- ``main.py`` and ``mcp/server.py`` re-import the SAME function
  objects from runtime, so existing test fixtures that call
  ``main_module._plan_repo.cache_clear()`` keep working — the
  reference is identical.
- ``workflows/nodes.py`` imports from runtime directly with no
  local-import hack and no circular dependency.

This unlocks the deep-review M4 follow-up of splitting workflows into
a separate worker process: a worker process loads
``competitionops.runtime`` without pulling in FastAPI, gets its own
lru_cache for the singletons, and runs the LangGraph workflow
independently.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest


# Round-2 M6 — per-test teardown lives in ``tests/conftest.py`` as an
# autouse fixture covering all five runtime singletons. This file's
# tests that need a mid-body reset import the helper directly from
# conftest (pytest adds the conftest's directory to sys.path).
from conftest import reset_runtime_caches  # noqa: E402, I001


# ---------------------------------------------------------------------------
# Runtime module surface
# ---------------------------------------------------------------------------


def test_runtime_module_exposes_three_factories() -> None:
    from competitionops import runtime

    for name in ("_plan_repo", "_audit_log", "_registry"):
        factory = getattr(runtime, name, None)
        assert callable(factory), f"runtime.{name} must be callable"
        # @lru_cache wraps with a wrapper that exposes ``cache_clear``.
        assert hasattr(factory, "cache_clear"), (
            f"runtime.{name} must be lru_cached so tests can reset between "
            "invocations (mirrors main / mcp_server convention)."
        )


def test_runtime_plan_repo_factory_returns_in_memory_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLAN_REPO_DIR", raising=False)
    reset_runtime_caches()
    from competitionops import runtime
    from competitionops.adapters.memory_plan_store import InMemoryPlanRepository

    repo = runtime._plan_repo()
    assert isinstance(repo, InMemoryPlanRepository)


def test_runtime_plan_repo_factory_returns_file_backed_when_dir_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLAN_REPO_DIR", str(tmp_path))
    reset_runtime_caches()
    from competitionops import runtime
    from competitionops.adapters.file_plan_store import FilePlanRepository

    repo = runtime._plan_repo()
    assert isinstance(repo, FilePlanRepository)
    assert repo.base_dir == tmp_path


def test_runtime_audit_log_factory_returns_in_memory_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUDIT_LOG_DIR", raising=False)
    reset_runtime_caches()
    from competitionops import runtime
    from competitionops.adapters.memory_audit import InMemoryAuditLog

    log = runtime._audit_log()
    assert isinstance(log, InMemoryAuditLog)


def test_runtime_audit_log_factory_returns_file_backed_when_dir_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUDIT_LOG_DIR", str(tmp_path))
    reset_runtime_caches()
    from competitionops import runtime
    from competitionops.adapters.file_audit import FileAuditLog

    log = runtime._audit_log()
    assert isinstance(log, FileAuditLog)
    assert log.base_dir == tmp_path


# ---------------------------------------------------------------------------
# Single source of truth — main / mcp_server / workflows all share the
# SAME function objects (so cache_clear etc behave consistently)
# ---------------------------------------------------------------------------


def test_main_reuses_runtime_factories_by_identity() -> None:
    """``main._plan_repo is runtime._plan_repo`` — same function object.
    Means existing test fixtures that do
    ``main_module._plan_repo.cache_clear()`` still target the canonical
    cache and don't accidentally clear a separate, parallel singleton."""
    from competitionops import main, runtime

    assert main._plan_repo is runtime._plan_repo
    assert main._audit_log is runtime._audit_log
    assert main._registry is runtime._registry


def test_mcp_server_reuses_runtime_factories_by_identity() -> None:
    """Same invariant for the MCP server module — both processes load
    the runtime singletons by reference, so a stray test that clears
    one cache clears them all (which is fine: there's only one
    logical singleton per process)."""
    from competitionops_mcp import server as mcp_server
    from competitionops import runtime

    assert mcp_server._plan_repo is runtime._plan_repo
    assert mcp_server._audit_log is runtime._audit_log
    assert mcp_server._registry is runtime._registry


# ---------------------------------------------------------------------------
# Structural guard — workflows must NOT depend on main / mcp_server
# ---------------------------------------------------------------------------


def test_workflow_nodes_do_not_import_main_module() -> None:
    """M4 — ``workflows/nodes.py`` previously did
    ``from competitionops import main as main_module`` inside execute /
    audit nodes. That coupled the workflow package to the FastAPI app
    and stopped the workflow from running in a separate worker process
    (e.g., Windmill, Celery, a dedicated k8s Deployment). The fix
    moves the shared singletons to ``competitionops.runtime`` so the
    workflow imports there instead. This test pins the contract."""
    from competitionops.workflows import nodes

    source = inspect.getsource(nodes)
    forbidden_imports = (
        "from competitionops import main",
        "from competitionops.main",
        "import competitionops.main",
    )
    for needle in forbidden_imports:
        assert needle not in source, (
            f"workflows/nodes.py contains {needle!r}, which re-introduces "
            "the FastAPI / workflow circular dependency M4 closed. "
            "Use ``competitionops.runtime`` instead."
        )

    # Positive assertion: nodes DO import runtime (otherwise they have
    # no singletons at all, which would mean the node bodies broke).
    assert "competitionops.runtime" in source or "from competitionops import runtime" in source, (
        "workflows/nodes.py must import ``competitionops.runtime`` "
        "for shared singleton access."
    )


def test_workflow_nodes_can_run_without_importing_main_first() -> None:
    """Direct unit invocation of ``audit_node`` works without first
    touching ``competitionops.main``. This is the property a separate
    worker process needs — load runtime, load workflow, run nodes,
    never instantiate FastAPI.
    """
    # If nodes.py still imported main at the top, this test couldn't
    # avoid triggering the FastAPI app construction. With the M4 fix
    # the import is purely lazy via ``competitionops.runtime``.
    from competitionops.workflows.nodes import audit_node

    result = audit_node({})  # type: ignore[arg-type]
    assert result == {"audit_records": []}


# ---------------------------------------------------------------------------
# Round-3 M1 — eager validation of runtime config at process boot
# ---------------------------------------------------------------------------
#
# Background. ``runtime._pdf_adapter`` already raises ``ValueError`` on an
# unknown ``PDF_ADAPTER`` value — but only when **called**. In the current
# wiring nothing calls it at process start, so a typo like ``PDF_ADAPTER=tika``
# lets the pod pass ``/health`` and only blows up on the first
# ``/briefs/extract/pdf`` request — sometimes hours into the deployment.
# The M1 contract: ``main.py`` invokes the validator at module import time
# so uvicorn aborts before binding the port → CrashLoopBackoff with the
# bad config name in the log → operator notices immediately.


def test_eager_validate_runtime_config_raises_on_unknown_pdf_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_eager_validate_runtime_config()`` must surface unknown
    ``PDF_ADAPTER`` values as a ``ValueError`` whose message names the
    offending value. The conftest autouse teardown clears the cached
    Settings between tests, so this set is local to the function."""
    from competitionops import main as main_module

    reset_runtime_caches()
    monkeypatch.setenv("PDF_ADAPTER", "tika")

    with pytest.raises(ValueError, match="tika"):
        main_module._eager_validate_runtime_config()


def test_eager_validate_runtime_config_succeeds_on_default_mock_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default ``PDF_ADAPTER=`` (unset → ``"mock"``) must not raise.
    The validator is wired at module import so it would crash every
    fresh pod if it raised on the default path."""
    from competitionops import main as main_module

    reset_runtime_caches()
    monkeypatch.delenv("PDF_ADAPTER", raising=False)

    main_module._eager_validate_runtime_config()  # must not raise


def test_eager_validate_runtime_config_raises_on_unknown_web_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-4 PR A (High#3) — symmetric behavioural guard for
    ``WEB_ADAPTER``. PDF already has this test; web only had an AST
    guard that the factory is *called* — which a future
    ``try: ... except ValueError: pass`` refactor would silently
    defeat while still passing the AST check.

    ``_eager_validate_runtime_config()`` must surface an unknown
    ``WEB_ADAPTER`` value as a ``ValueError`` naming the offending
    value, so a typo crashes uvicorn at module import (round-3 M1)."""
    from competitionops import main as main_module

    reset_runtime_caches()
    monkeypatch.setenv("WEB_ADAPTER", "scrapy-2.11")

    with pytest.raises(ValueError, match="scrapy-2.11"):
        main_module._eager_validate_runtime_config()


def test_main_module_invokes_eager_validate_runtime_config_at_init() -> None:
    """Structural guard. ``_eager_validate_runtime_config()`` must be
    called at module top level in ``main.py`` — not behind a function,
    not on first-request, not inside ``lifespan``. Otherwise the
    contract degrades back to "fails on first /briefs/extract/pdf"
    that M1 was filed against.
    """
    import ast
    from pathlib import Path

    source = Path("src/competitionops/main.py").read_text(encoding="utf-8")
    module = ast.parse(source)

    top_level_calls = [
        node.value.func.id
        for node in module.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    ]
    assert "_eager_validate_runtime_config" in top_level_calls, (
        "main.py must call _eager_validate_runtime_config() at module "
        "import time (top-level statement). Putting it behind a "
        "function or FastAPI lifespan defeats round-3 M1 — invalid "
        "PDF_ADAPTER values must crash uvicorn import, not the first "
        "PDF request."
    )
