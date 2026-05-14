"""Sprint 0 — OpenTelemetry bootstrap.

These tests lock in two contracts for the telemetry module:

1. ``setup_tracer_provider()`` returns an SDK-backed ``TracerProvider`` and
   that provider is the one OTel returns through ``trace.get_tracer_provider()``.
2. ``setup_tracer_provider()`` is idempotent — re-calling it never installs
   a new provider, so unit tests can call it freely without polluting the
   OTel global state across test cases.

Later sprints (1+) build on this bootstrap to add manual spans, FastAPI
instrumentation, and metric counters.
"""

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider

from competitionops.telemetry import setup_meter_provider, setup_tracer_provider
from competitionops.telemetry.setup import OtelInstallOrderError


def test_setup_creates_tracer_provider() -> None:
    provider = setup_tracer_provider()
    assert isinstance(provider, TracerProvider)
    assert trace.get_tracer_provider() is provider


def test_setup_idempotent() -> None:
    first = setup_tracer_provider()
    second = setup_tracer_provider()
    assert first is second
    assert trace.get_tracer_provider() is first


# ---------------------------------------------------------------------------
# M1 — silent reader drop on MeterProvider re-install
#
# OTel's MeterProvider takes its ``metric_readers`` at construction time
# and there is NO public API to add more after the fact. So if a
# MeterProvider is already installed when ``setup_meter_provider`` is
# called with new readers, the old implementation returned the existing
# provider and silently dropped the requested readers on the floor. A
# production deployment that depended on those readers being wired up
# (e.g. OTLP) would emit zero metrics while every existing test still
# said "no crash, all good".
#
# Fix: surface the install-order conflict via ``OtelInstallOrderError``.
# The operator either installs readers on the first call, or accepts a
# no-op by calling without ``readers=``.
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolated_meter_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pretend no MeterProvider is installed yet. We can't actually
    uninstall a global MeterProvider (OTel forbids it) so we patch the
    accessor to report the noop default during this test only."""
    from opentelemetry.metrics._internal import _ProxyMeterProvider

    proxy = _ProxyMeterProvider()
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: proxy)
    monkeypatch.setattr(metrics, "set_meter_provider", lambda _provider: None)


def test_setup_meter_provider_installs_readers_on_first_call(
    _isolated_meter_provider: None,
) -> None:
    """Happy path: when no SDK MeterProvider exists yet,
    ``setup_meter_provider`` installs one with the requested readers."""
    reader = InMemoryMetricReader()
    provider = setup_meter_provider(readers=[reader])
    assert isinstance(provider, MeterProvider)


def test_setup_meter_provider_returns_existing_when_no_readers_requested() -> None:
    """Backward-compatible behaviour: calling without ``readers=`` is
    explicitly a no-op accept of whatever provider is installed.
    Existing call sites that just want the global handle still work."""
    provider = setup_meter_provider()
    # Either an SDK MeterProvider (if some test installed one already)
    # or a proxy provider (if no test has touched it yet). Both are
    # acceptable return shapes for ``readers=None``.
    assert provider is not None


def test_setup_meter_provider_raises_when_readers_requested_but_provider_already_set() -> None:
    """M1 regression guard. If a MeterProvider is already installed and
    the caller passes new ``readers``, the function must raise rather
    than silently drop the readers on the floor.

    Concrete scenario: ``tests/test_metrics.py`` installs a session-
    scoped MeterProvider with an InMemoryMetricReader. Later, the
    FastAPI module-init runs ``_wire_otel_exporters`` with OTLP env set
    and calls ``setup_meter_provider(readers=[OTLPMetricExporter()])``.
    Before this fix, that call silently dropped the OTLP reader —
    production telemetry would be missing in any deployment whose
    import order happened to hit this case. After this fix, it raises
    so the operator finds out before shipping.
    """
    # Force a MeterProvider to exist. ``setup_meter_provider`` with no
    # readers is idempotent and safe.
    setup_meter_provider()
    # The session may already have an SDK provider from test_metrics,
    # OR a proxy. Either way, asking for readers must raise.
    current = metrics.get_meter_provider()
    if not isinstance(current, MeterProvider):
        # No SDK provider yet — install one with empty readers so the
        # next call has something to clash with.
        metrics.set_meter_provider(MeterProvider(metric_readers=[]))

    with pytest.raises(OtelInstallOrderError) as exc_info:
        setup_meter_provider(readers=[InMemoryMetricReader()])

    msg = str(exc_info.value)
    assert "MeterProvider" in msg
    assert "reader" in msg.lower()
    # Operator guidance: the message should tell the operator HOW to fix.
    assert "first" in msg.lower() or "no-op" in msg.lower()


# ---------------------------------------------------------------------------
# M2 — TracerProvider double-call collapse
#
# Before this fix, ``competitionops.main`` called
# ``setup_tracer_provider()`` twice: once unconditionally at module init
# (so FastAPI auto-instrumentation has a real SDK provider) and once
# inside ``_wire_otel_exporters`` to fetch the provider for
# ``add_span_processor``. The second call was redundant. More
# importantly, it muddied the "who owns the provider?" question for
# embedders: if an embedder installed their own SDK TracerProvider
# before main loaded, both calls would return that foreign provider
# and our exporters would attach to it without consent.
#
# Fix: ``_wire_otel_exporters`` now uses ``trace.get_tracer_provider()``
# with an explicit isinstance check — failing loudly if the global
# provider is not an SDK provider. The module-init call is still the
# single source of truth for installing one.
# ---------------------------------------------------------------------------


def test_wire_otel_exporters_uses_trace_get_tracer_provider_not_setup() -> None:
    """Structural guard: M2 collapse means the wiring function no
    longer calls ``setup_tracer_provider`` twice. ``main`` module-init
    is the only legitimate caller during normal startup; the wiring
    path observes via ``trace.get_tracer_provider()``.

    Checks executable lines only (comments / docstrings are stripped
    by ``ast.unparse`` of the function body), so an explanatory comment
    that mentions the old call name doesn't trigger a false positive.
    """
    import ast
    import inspect

    from competitionops import main as main_module

    source = inspect.getsource(main_module._wire_otel_exporters)
    tree = ast.parse(source)
    setup_calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "setup_tracer_provider":
            setup_calls.append(ast.unparse(node))
        elif isinstance(func, ast.Attribute) and func.attr == "setup_tracer_provider":
            setup_calls.append(ast.unparse(node))

    assert not setup_calls, (
        "M2 regression: ``_wire_otel_exporters`` calls "
        "``setup_tracer_provider()`` itself. The module-init at the "
        "bottom of main.py is the only place that should install the "
        "provider; the wiring path should use "
        "``trace.get_tracer_provider()`` with an isinstance check. "
        f"Offending calls: {setup_calls!r}"
    )
    # Positive assertion: it DOES look up the global provider.
    assert "get_tracer_provider" in source, (
        "``_wire_otel_exporters`` should fetch the global "
        "TracerProvider via ``trace.get_tracer_provider()`` so it can "
        "attach span processors to whatever module-init installed."
    )


def test_wire_otel_exporters_raises_when_tracer_provider_is_not_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M2 — if for any reason the global TracerProvider is NOT an SDK
    instance when ``_wire_otel_exporters`` runs (e.g., main-init was
    bypassed or an embedder installed a Proxy provider), we must fail
    loudly rather than silently dropping the BatchSpanProcessor."""
    from opentelemetry import trace

    from competitionops import main as main_module

    # Replace the global lookup to return a non-SDK object so the
    # isinstance check fails. We don't actually swap the real global
    # provider — that would leak across tests.
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: object())
    monkeypatch.setenv("COMPETITIONOPS_OTEL_CONSOLE", "1")

    with pytest.raises(OtelInstallOrderError) as exc_info:
        main_module._wire_otel_exporters()
    assert "TracerProvider" in str(exc_info.value)
