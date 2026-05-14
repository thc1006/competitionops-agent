"""Round-3 H1 — the documented onboarding command (``uv sync && uv run
pytest``) must collect the test suite cleanly on a fresh checkout.

Before this fix, ``langgraph`` was only listed under
``[project.optional-dependencies].langgraph`` so ``uv sync`` (no flags)
didn't install it. ``tests/test_workflow_graph.py`` then imported it
eagerly at module load, aborting collection with
``ModuleNotFoundError`` 1 second into the suite. Every "400 passed"
verification we logged across 21 PRs only succeeded because the dev
venv carried a leaked ``--extra langgraph`` install from earlier
sessions.

The fix puts ``langgraph`` into ``[dependency-groups].dev`` (PEP 735),
which ``uv sync`` installs by default. Production images that don't
want the workflow runtime can still opt out via ``uv sync --no-dev``.
The runtime-side entry point at
``[project.optional-dependencies].langgraph`` is kept for parity with
the operator-facing ``pip install .[langgraph]`` pattern.

This file is structural — it inspects ``pyproject.toml`` directly so
the regression surfaces in CI even without a fresh-checkout
``uv sync`` step.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def _load_pyproject() -> dict:
    with _PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def _flatten_dep_strings(deps: list[str]) -> list[str]:
    """Lowercase the package-name portion of each requirement string so
    ``langgraph>=0.5.0`` matches a check for ``"langgraph"``."""
    flat: list[str] = []
    for raw in deps:
        name = raw.split(";")[0].split("[")[0].split(">=")[0].split("==")[0]
        name = name.split(">")[0].split("<")[0].split("~=")[0].split("!=")[0]
        flat.append(name.strip().lower())
    return flat


def test_dev_dependency_group_includes_langgraph() -> None:
    """``test_workflow_graph.py`` imports ``langgraph.graph`` at module
    top-level — it must be installed by the default ``uv sync``.
    PEP 735 ``[dependency-groups].dev`` is the canonical place; uv
    auto-installs it unless ``--no-dev`` is passed."""
    py = _load_pyproject()
    dev = py.get("dependency-groups", {}).get("dev", [])
    names = _flatten_dep_strings(dev)
    assert "langgraph" in names, (
        "``langgraph`` must be listed in ``[dependency-groups].dev`` "
        "so the documented ``uv sync && uv run pytest`` flow doesn't "
        "abort collection on ``ModuleNotFoundError: No module named "
        "'langgraph'``. Found dev group: "
        f"{names!r}"
    )


def test_langgraph_checkpoint_pinned_alongside_langgraph_in_dev_group() -> None:
    """``workflows/graph.py`` does
    ``from langgraph.checkpoint.memory import MemorySaver`` — the
    checkpoint package is published separately from ``langgraph`` so
    it must be in the dev group too. Otherwise tests fail with a
    less obvious ``ModuleNotFoundError: langgraph.checkpoint`` after
    H1 is fixed for the parent package alone."""
    py = _load_pyproject()
    dev = py.get("dependency-groups", {}).get("dev", [])
    names = _flatten_dep_strings(dev)
    assert "langgraph-checkpoint" in names, (
        "``langgraph-checkpoint`` must accompany ``langgraph`` in "
        "``[dependency-groups].dev``. Found dev group: "
        f"{names!r}"
    )


def test_optional_dependencies_langgraph_kept_for_production_opt_in() -> None:
    """The ``[project.optional-dependencies].langgraph`` extra stays
    so ``pip install .[langgraph]`` (or ``uv sync --extra langgraph``)
    keeps working — distros that omit dev deps but want the workflow
    runtime use this path. Round-3 fix did NOT move langgraph; it
    DUPLICATED the listing into the dev group."""
    py = _load_pyproject()
    extras = py.get("project", {}).get("optional-dependencies", {})
    assert "langgraph" in extras, (
        "``[project.optional-dependencies].langgraph`` must stay so "
        "the operator-facing ``pip install .[langgraph]`` path keeps "
        "working independently of the dev tooling layer."
    )


def test_workflow_graph_test_module_collects_without_extra_flags() -> None:
    """End-to-end regression: ``tests/test_workflow_graph.py`` imports
    must succeed in the CURRENT venv. We can't easily re-run
    ``uv sync`` from inside pytest, but we CAN import the test module
    fresh and assert it doesn't blow up — which catches the case
    where langgraph is missing AND the test file imports it eagerly.
    """
    import importlib
    import sys

    # If this test runs at all the venv has langgraph (else we'd have
    # aborted at collection). But re-import is the cheapest sanity
    # check that the module-level imports of ``test_workflow_graph``
    # haven't grown new optional deps.
    sys.modules.pop("tests.test_workflow_graph", None)
    spec = importlib.util.spec_from_file_location(
        "tests.test_workflow_graph",
        _REPO_ROOT / "tests" / "test_workflow_graph.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # raises if any top-level import is missing
