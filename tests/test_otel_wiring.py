"""Sprint 6 — OTLP / console exporter wiring contract.

The main.py module performs two distinct OTel setup steps at import time:

1. ``setup_tracer_provider()`` is **unconditional** — pytest already
   exercises this through Sprint 0+4 tests.
2. ``_wire_otel_exporters()`` is **opt-in** via env vars. These tests
   cover the env-detection logic and the import-path soundness of the
   wiring helper. They deliberately do NOT exercise real OTLP transport
   (that's OTel's own integration tests).
"""

from __future__ import annotations

import pytest

from competitionops import main as main_module


@pytest.fixture(autouse=True)
def _clean_otel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip OTel-related env vars at the start of every test so the
    detection helper sees a clean slate. monkeypatch restores them
    automatically at teardown.
    """
    for var in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_SERVICE_NAME",
        "COMPETITIONOPS_OTEL_CONSOLE",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# env detection
# ---------------------------------------------------------------------------


def test_otel_exporters_enabled_returns_false_without_env() -> None:
    assert main_module._otel_exporters_enabled() is False


def test_otel_exporters_enabled_returns_true_with_otlp_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
    assert main_module._otel_exporters_enabled() is True


def test_otel_exporters_enabled_returns_true_with_console_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPETITIONOPS_OTEL_CONSOLE", "1")
    assert main_module._otel_exporters_enabled() is True


def test_otel_exporters_enabled_treats_empty_console_value_as_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``COMPETITIONOPS_OTEL_CONSOLE=0`` or empty must not enable console."""
    monkeypatch.setenv("COMPETITIONOPS_OTEL_CONSOLE", "0")
    assert main_module._otel_exporters_enabled() is False
    monkeypatch.setenv("COMPETITIONOPS_OTEL_CONSOLE", "")
    assert main_module._otel_exporters_enabled() is False


def test_otel_exporters_enabled_otlp_endpoint_empty_string_still_truthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OTel SDK treats ``OTEL_EXPORTER_OTLP_ENDPOINT=""`` as "set"
    (unsetting requires actually unsetting the var). We match that
    convention so an operator can't half-configure the wiring.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    assert main_module._otel_exporters_enabled() is True


# ---------------------------------------------------------------------------
# Console wiring — runs against opentelemetry-sdk (already a base dep)
# ---------------------------------------------------------------------------


def test_wire_otel_exporters_console_mode_runs_without_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The console branch must complete cleanly; it attaches a
    BatchSpanProcessor + ConsoleSpanExporter to the global TracerProvider
    and a PeriodicExportingMetricReader + ConsoleMetricExporter to the
    MeterProvider. We don't assert internal SDK state because that's
    OTel's contract, not ours; we only assert "no crash".
    """
    monkeypatch.setenv("COMPETITIONOPS_OTEL_CONSOLE", "1")
    # Should not raise.
    main_module._wire_otel_exporters()


def test_wire_otel_exporters_no_env_runs_but_attaches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even called directly without env, the helper must be safe — it just
    skips both branches. Defensive guard so callers don't need to check
    env themselves."""
    main_module._wire_otel_exporters()
    # No side effects measured here; the assertion is "did not raise".


# ---------------------------------------------------------------------------
# OTLP wiring — only meaningful when ``--extra otel`` is installed
# ---------------------------------------------------------------------------


def test_wire_otel_exporters_otlp_mode_runs_without_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies the lazy-import path for OTLP works when the exporter
    package is installed. Skipped automatically on stripped-down clones
    (``uv sync`` without ``--extra otel``).
    """
    pytest.importorskip(
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
        reason="requires `uv sync --extra otel` for opentelemetry-exporter-otlp",
    )
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector.example.invalid:4317"
    )
    # Should not raise; OTLP exporter constructs lazily — actual gRPC
    # connection happens on first export, not at instantiation.
    main_module._wire_otel_exporters()
