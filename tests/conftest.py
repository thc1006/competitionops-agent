"""Shared pytest fixtures for the CompetitionOps test suite.

Two cleanup primitives lifted here from per-file duplication
(round-2 deep review finding M6 + cross-PR observations 3 & 4):

1. ``reset_runtime_caches`` + autouse teardown that calls it. Clears
   the four ``competitionops.runtime`` lru_cache singletons plus
   ``config.get_settings``. Before this conftest, three of the four
   test files that needed this duplicated 5-7 lines of cache_clear
   calls AND each version missed at least one cache — most recently
   ``_pdf_adapter`` (added by PR #14) was only cleared in the file
   that introduced it. The autouse fixture means future runtime
   singletons get reset everywhere by default.

2. ``isolated_meter_provider`` fixture. OTel's
   ``metrics.set_meter_provider`` is once-only at process scope, so
   tests that want to exercise a fresh MeterProvider install path
   have to monkeypatch ``metrics.get_meter_provider`` / ``set_meter_provider``
   to a stub. That stub pattern was inlined three times across
   ``test_telemetry_setup.py`` and ``test_otel_wiring.py`` before this
   conftest pulled it together.

This file is the canonical place to add new cross-file fixtures.
Keep file-local fixtures inside the file that owns them.
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest


def reset_runtime_caches() -> None:
    """Clear every ``@lru_cache`` singleton in ``competitionops.runtime``
    plus ``config.get_settings``.

    Tests that monkeypatch env vars (``PLAN_REPO_DIR`` / ``AUDIT_LOG_DIR`` /
    ``PDF_ADAPTER``) rely on the next ``runtime._*()`` call to construct
    a fresh adapter against the new env. Without this teardown, env
    leaks into later tests via the cached Settings instance.

    Imports are lazy so ``conftest.py`` doesn't force-load the runtime
    module at collection time (tests like ``test_workflow_graph`` that
    don't touch runtime should still collect even if a runtime import
    side-effect breaks).
    """
    from competitionops import config as config_module
    from competitionops import runtime

    config_module.get_settings.cache_clear()
    runtime._plan_repo.cache_clear()
    runtime._audit_log.cache_clear()
    runtime._registry.cache_clear()
    runtime._pdf_adapter.cache_clear()
    runtime._web_adapter.cache_clear()
    runtime._token_provider.cache_clear()


@pytest.fixture(autouse=True)
def _runtime_cache_teardown() -> Iterator[None]:
    """Autouse cleanup — runs ``reset_runtime_caches`` after every test.

    Setup-side: nothing (we don't pre-reset because most tests start
    with a clean cache anyway from the previous test's teardown).
    Teardown-side: clear everything so the NEXT test starts clean.

    The cost per test is ~5 dict lookups + 5 ``.cache_clear()`` calls
    — microseconds. The benefit is that adding a sixth runtime
    singleton in the future automatically gets reset everywhere by
    bumping a single line in ``reset_runtime_caches``.
    """
    yield
    reset_runtime_caches()


@pytest.fixture
def isolated_meter_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Pretend no MeterProvider is installed yet.

    OTel's ``metrics.set_meter_provider`` is once-only at the SDK
    level, so we can't actually uninstall the global. Instead we
    patch ``metrics.get_meter_provider`` to report a fresh
    ``_ProxyMeterProvider`` and ``metrics.set_meter_provider`` to
    swallow the install. Tests asserting "this code path installs a
    MeterProvider" can monkeypatch ``set_meter_provider`` further to
    capture the instance.

    Opt in by requesting the fixture name in the test signature —
    this is NOT autouse because most tests don't touch MeterProvider
    and applying it universally would mask real install-order
    interactions.
    """
    from opentelemetry import metrics
    from opentelemetry.metrics._internal import _ProxyMeterProvider

    proxy = _ProxyMeterProvider()
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: proxy)
    monkeypatch.setattr(metrics, "set_meter_provider", lambda _provider: None)
    yield


@pytest.fixture
def meter_provider_install_captor(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, Any]]:
    """Like ``isolated_meter_provider`` but ALSO captures whatever
    provider the code-under-test tries to install. Use in tests that
    need to assert ``metrics.set_meter_provider`` was actually called
    (M1's silent-drop regression guard).
    """
    from opentelemetry import metrics
    from opentelemetry.metrics._internal import _ProxyMeterProvider

    proxy = _ProxyMeterProvider()
    installed: dict[str, Any] = {}
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: proxy)
    monkeypatch.setattr(
        metrics,
        "set_meter_provider",
        lambda provider: installed.setdefault("provider", provider),
    )
    yield installed


@pytest.fixture
def isolated_tracer_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Any]:
    """Yield a fresh SDK ``TracerProvider`` that tests can wire span
    processors / exporters onto without leaking into the process-wide
    global.

    Why this exists (round-3 M2). ``test_otel_wiring.py`` exercises
    ``_wire_otel_exporters`` which calls
    ``trace.get_tracer_provider().add_span_processor(...)``. Before
    this fixture the processor (and its ConsoleSpanExporter /
    BatchSpanProcessor worker thread) attached to the **real** global
    set by ``setup_tracer_provider`` at module import. The exporter
    thread kept holding ``sys.stderr`` after pytest closed its
    captured fd, so background flushes during teardown raised
    ``ValueError: I/O operation on closed file`` — visible in
    ``scripts/verify.sh`` even though tests pass.

    Mechanics. ``trace.set_tracer_provider`` is once-only at SDK
    level, so we don't try to install. We monkeypatch
    ``trace.get_tracer_provider`` (the lookup the wiring code uses)
    to return a per-test SDK provider, and on teardown shut the
    per-test provider down — this stops its BatchSpanProcessor's
    worker thread BEFORE pytest closes the captured stderr fd.

    Symmetric to ``isolated_meter_provider`` / ``meter_provider_install_captor``
    above. Opt in by name — most tests don't touch TracerProvider so
    autouse would mask real install-order interactions.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider()
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    try:
        yield provider
    finally:
        provider.shutdown()
