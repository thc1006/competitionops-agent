"""P2-005 Sprint 3 + deep-review M6 — ``runtime._pdf_adapter`` factory.

Two contracts in one file:

1. ``Settings.pdf_adapter`` chooses the PDF backend. ``None`` /
   ``"mock"`` → ``MockPdfAdapter`` (the Sprint 0 default). ``"docling"``
   → ``DoclingPdfAdapter`` (Sprint 3, requires ``--extra ocr``).
2. The FastAPI endpoint ``POST /briefs/extract/pdf`` resolves the
   adapter through ``Depends(get_pdf_adapter)`` so tests can inject a
   stub via ``app.dependency_overrides``. Before this PR the endpoint
   instantiated ``MockPdfAdapter`` directly, which is the M6
   deep-review finding.

The Docling integration path is exercised via
``pytest.importorskip("docling")`` so this file stays green on the
default CI install (no ``--extra ocr``). Operators verify the real
Docling path after ``uv sync --extra ocr``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from competitionops import config as config_module
from competitionops import main as main_module
from competitionops.adapters.pdf_mock import MockPdfAdapter
from competitionops.ports import PdfIngestionPort

_FAKE_PDF = b"%PDF-1.4\nRunSpace Challenge\nSubmission deadline: 2026-09-30\n"


def _reset_runtime() -> None:
    config_module.get_settings.cache_clear()
    from competitionops import runtime

    runtime._plan_repo.cache_clear()
    runtime._audit_log.cache_clear()
    runtime._registry.cache_clear()
    runtime._pdf_adapter.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_runtime_caches() -> Any:
    yield
    _reset_runtime()


# ---------------------------------------------------------------------------
# Settings field
# ---------------------------------------------------------------------------


def test_settings_pdf_adapter_defaults_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PDF_ADAPTER", raising=False)
    _reset_runtime()
    settings = config_module.get_settings()
    assert settings.pdf_adapter is None


def test_settings_pdf_adapter_reads_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PDF_ADAPTER", "docling")
    _reset_runtime()
    settings = config_module.get_settings()
    assert settings.pdf_adapter == "docling"


# ---------------------------------------------------------------------------
# runtime._pdf_adapter factory (M6 — registry pattern mirroring _plan_repo)
# ---------------------------------------------------------------------------


def test_runtime_pdf_adapter_factory_defaults_to_mock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PDF_ADAPTER", raising=False)
    _reset_runtime()
    from competitionops import runtime

    adapter = runtime._pdf_adapter()
    assert isinstance(adapter, MockPdfAdapter)


def test_runtime_pdf_adapter_factory_mock_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PDF_ADAPTER=mock`` and the unset case must produce the same
    adapter — explicit form is just for operator clarity."""
    monkeypatch.setenv("PDF_ADAPTER", "mock")
    _reset_runtime()
    from competitionops import runtime

    adapter = runtime._pdf_adapter()
    assert isinstance(adapter, MockPdfAdapter)


def test_runtime_pdf_adapter_factory_raises_on_unknown_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator typo (e.g. ``PDF_ADAPTER=Docling`` mixed-case) must
    fail startup loudly, NOT silently fall back to mock. A silent
    fallback would let a prod deployment that thinks it has Docling
    actually use the mock for weeks before anyone notices."""
    monkeypatch.setenv("PDF_ADAPTER", "TotallyMadeUpEngine")
    _reset_runtime()
    from competitionops import runtime

    with pytest.raises(ValueError) as exc_info:
        runtime._pdf_adapter()
    assert "PDF_ADAPTER" in str(exc_info.value) or "pdf_adapter" in str(exc_info.value)
    assert "TotallyMadeUpEngine" in str(exc_info.value)


def test_runtime_pdf_adapter_factory_singleton_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``@lru_cache`` is what keeps test fixtures' ``cache_clear`` usable."""
    monkeypatch.delenv("PDF_ADAPTER", raising=False)
    _reset_runtime()
    from competitionops import runtime

    first = runtime._pdf_adapter()
    second = runtime._pdf_adapter()
    assert first is second


def test_runtime_pdf_adapter_satisfies_port() -> None:
    """Structural typing — runtime returns something that satisfies
    ``PdfIngestionPort`` so downstream code (BriefExtractor) doesn't
    have to type-cast."""
    _reset_runtime()
    from competitionops import runtime

    adapter: PdfIngestionPort = runtime._pdf_adapter()
    assert adapter.extract(_FAKE_PDF)


# ---------------------------------------------------------------------------
# M6 — FastAPI endpoint resolves adapter via Depends, NOT a hard-coded
# ``MockPdfAdapter()`` instantiation
# ---------------------------------------------------------------------------


def test_pdf_upload_endpoint_uses_depends_injectable_adapter() -> None:
    """``app.dependency_overrides[get_pdf_adapter]`` must redirect the
    handler to a stub. Before M6 the endpoint did
    ``pdf_port = MockPdfAdapter()`` inline, so the only way to inject
    a stub was monkey-patching the symbol — fragile and noisy."""

    class _SpyAdapter:
        def __init__(self) -> None:
            self.called_with: list[bytes] = []

        def extract(self, pdf_bytes: bytes) -> str:
            self.called_with.append(pdf_bytes)
            return "RunSpace Spy\nSubmission deadline: 2026-09-30\n"

    spy = _SpyAdapter()
    main_module.app.dependency_overrides[main_module.get_pdf_adapter] = lambda: spy
    try:
        _reset_runtime()
        client = TestClient(main_module.app)
        response = client.post(
            "/briefs/extract/pdf",
            files={"file": ("brief.pdf", _FAKE_PDF, "application/pdf")},
        )
    finally:
        main_module.app.dependency_overrides.pop(
            main_module.get_pdf_adapter, None
        )

    assert response.status_code == 200, response.text
    assert spy.called_with, "stub adapter was never invoked"
    assert spy.called_with[0].startswith(b"%PDF-")


def test_pdf_upload_endpoint_does_not_hard_code_mock_pdf_adapter() -> None:
    """Structural guard: M6 regression. The handler source must not
    contain the literal ``MockPdfAdapter(`` constructor call —
    that's the exact pattern the deep review flagged."""
    import ast
    import inspect

    source = inspect.getsource(main_module.extract_brief_from_pdf)
    tree = ast.parse(source)
    direct_constructions: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id if isinstance(func, ast.Name)
            else (func.attr if isinstance(func, ast.Attribute) else "")
        )
        if name == "MockPdfAdapter":
            direct_constructions.append(ast.unparse(node))
    assert not direct_constructions, (
        "M6 regression — ``extract_brief_from_pdf`` constructs "
        f"``MockPdfAdapter`` inline: {direct_constructions!r}. Resolve "
        "the adapter via ``Depends(get_pdf_adapter)`` so tests can "
        "inject stubs and operators can switch engines via env."
    )


# ---------------------------------------------------------------------------
# Docling adapter — Sprint 3 wiring. Skipped on standard CI where the
# heavy ``docling`` dep isn't installed. Operators run after
# ``uv sync --extra ocr``.
# ---------------------------------------------------------------------------


def test_docling_adapter_module_imports_when_extra_installed() -> None:
    """``DoclingPdfAdapter`` lives at ``competitionops.adapters.pdf_docling``
    and is importable iff ``docling`` is installed (i.e., ``--extra ocr``).
    Standard CI skips this test."""
    pytest.importorskip(
        "docling",
        reason="requires `uv sync --extra ocr` for the Docling parser",
    )
    from competitionops.adapters.pdf_docling import DoclingPdfAdapter

    assert callable(DoclingPdfAdapter)


def test_runtime_pdf_adapter_factory_returns_docling_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: env ``PDF_ADAPTER=docling`` → factory imports the
    Docling adapter lazily and returns an instance. Lazy import means
    operators without ``--extra ocr`` never pay the docling import
    cost; they get a clean ImportError only when they explicitly opt
    in via the env var."""
    pytest.importorskip("docling", reason="requires `uv sync --extra ocr`")
    monkeypatch.setenv("PDF_ADAPTER", "docling")
    _reset_runtime()
    from competitionops import runtime
    from competitionops.adapters.pdf_docling import DoclingPdfAdapter

    adapter = runtime._pdf_adapter()
    assert isinstance(adapter, DoclingPdfAdapter)


def test_runtime_pdf_adapter_raises_helpful_error_when_docling_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the operator sets ``PDF_ADAPTER=docling`` but never ran
    ``uv sync --extra ocr``, the factory should surface an actionable
    error pointing at the install path — not a raw ``ModuleNotFoundError``
    that they have to dig through."""
    monkeypatch.setenv("PDF_ADAPTER", "docling")
    _reset_runtime()

    # Force the lazy import to fail by hiding the docling module from
    # sys.modules for the duration of this test.
    import builtins
    import importlib
    import sys

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "competitionops.adapters.pdf_docling" or name.startswith("docling"):
            raise ModuleNotFoundError("No module named 'docling'")
        return real_import(name, *args, **kwargs)

    # Also strip any cached versions so the import path re-runs.
    monkeypatch.setattr(builtins, "__import__", fake_import)
    sys.modules.pop("competitionops.adapters.pdf_docling", None)
    importlib.invalidate_caches()

    from competitionops import runtime

    with pytest.raises(RuntimeError) as exc_info:
        runtime._pdf_adapter()
    msg = str(exc_info.value)
    assert "docling" in msg.lower()
    assert "ocr" in msg.lower() or "--extra" in msg


def test_docling_adapter_extract_returns_text_from_real_pdf() -> None:
    """Integration: feed a tiny real-PDF byte stream through Docling
    and assert the extracted text contains the expected token.

    Requires ``--extra ocr`` AND the test fixture builds a tiny PDF in
    memory via ``reportlab``. Skipped when either is missing — the
    cost of pulling reportlab into the dev base just to test Docling
    isn't worth it; operators verify with their own competition PDFs."""
    pytest.importorskip("docling", reason="requires `uv sync --extra ocr`")
    pytest.importorskip(
        "reportlab",
        reason="optional dep for synthesising a tiny test PDF",
    )
    from io import BytesIO

    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    c = canvas.Canvas(buffer)
    c.drawString(100, 800, "RunSpace Innovation Challenge 2026")
    c.drawString(100, 780, "Submission deadline: 2026-09-30")
    c.save()
    pdf_bytes = buffer.getvalue()
    assert pdf_bytes.startswith(b"%PDF-")

    from competitionops.adapters.pdf_docling import DoclingPdfAdapter

    text = DoclingPdfAdapter().extract(pdf_bytes)
    assert "RunSpace" in text
    assert "2026-09-30" in text
