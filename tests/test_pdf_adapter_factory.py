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


# ---------------------------------------------------------------------------
# Round 2 deep review — H1: ``pdf_port.extract`` must NOT block the
# FastAPI event loop. Real Docling inference is 10-60s on a single PDF;
# H2 still pins prod to ``replicas=1``, so a single blocked worker
# means cluster-wide stall. The fix wraps the call in
# ``run_in_threadpool`` (FastAPI's standard offload primitive).
#
# The probe stub below distinguishes "ran on the event loop" from "ran
# on a worker thread" by checking whether ``asyncio.get_running_loop()``
# raises — it does in a non-event-loop thread, and returns the loop on
# the event loop. Deterministic, no timing dependency.
# ---------------------------------------------------------------------------


def test_pdf_endpoint_runs_extract_off_event_loop() -> None:
    """H1 regression guard: the FastAPI handler must offload
    ``pdf_port.extract`` so heavy sync engines (Docling) don't block
    every concurrent request on the worker."""
    import asyncio

    class _LoopProbeAdapter:
        def __init__(self) -> None:
            self.was_offloaded: bool | None = None

        def extract(self, pdf_bytes: bytes) -> str:
            try:
                asyncio.get_running_loop()
                # Running loop accessible → we're on the event loop (BAD).
                self.was_offloaded = False
            except RuntimeError:
                # No running loop → we're on a worker thread (GOOD).
                self.was_offloaded = True
            return "Probe Cup\nSubmission deadline: 2026-09-30\n"

    probe = _LoopProbeAdapter()
    main_module.app.dependency_overrides[main_module.get_pdf_adapter] = (
        lambda: probe
    )
    try:
        _reset_runtime()
        client = TestClient(main_module.app)
        response = client.post(
            "/briefs/extract/pdf",
            files={
                "file": (
                    "probe.pdf",
                    b"%PDF-1.4\nProbe Cup\nSubmission deadline: 2026-09-30\n",
                    "application/pdf",
                ),
            },
        )
    finally:
        main_module.app.dependency_overrides.pop(
            main_module.get_pdf_adapter, None
        )

    assert response.status_code == 200, response.text
    assert probe.was_offloaded is True, (
        "H1 regression: ``pdf_port.extract`` ran on the FastAPI worker's "
        "event loop. Heavy sync engines (real Docling = 10-60s) block "
        "every other request on this worker. Wrap the call in "
        "``fastapi.concurrency.run_in_threadpool`` (or "
        "``anyio.to_thread.run_sync``)."
    )


def test_pdf_endpoint_handler_invokes_run_in_threadpool() -> None:
    """Structural guard: the handler source must contain a call to
    ``run_in_threadpool``. Belt-and-braces with the behavioural test
    above — guarantees a clean diff signal if the offload disappears.
    AST walk so a comment that names the function doesn't false-positive."""
    import ast
    import inspect

    source = inspect.getsource(main_module.extract_brief_from_pdf)
    tree = ast.parse(source)
    threadpool_calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "run_in_threadpool":
            threadpool_calls.append(ast.unparse(node))
        elif (
            isinstance(func, ast.Attribute) and func.attr == "run_in_threadpool"
        ):
            threadpool_calls.append(ast.unparse(node))

    assert threadpool_calls, (
        "H1: ``extract_brief_from_pdf`` must call ``run_in_threadpool`` "
        "to offload the sync extractor."
    )


# ---------------------------------------------------------------------------
# Round 2 deep review — H2: ``DoclingPdfAdapter.extract`` previously
# assigned ``tmp_path = Path(handle.name)`` AFTER ``handle.write(...)``.
# If write raised (disk full / quota), ``tmp_path`` was never bound AND
# the outer ``try/finally`` never ran — orphan ``.pdf`` files
# accumulated under ``$TMPDIR``.
#
# Tested via AST so docling does NOT need to be installed.
# ---------------------------------------------------------------------------


def test_pdf_docling_extract_captures_tmp_path_before_failable_ops() -> None:
    """H2 structural regression guard. Walks ``pdf_docling.py`` source
    via AST (no docling import) and asserts the line that binds
    ``tmp_path`` precedes any ``.write`` / ``.write_bytes`` / ``.convert``
    call inside ``extract``. A regression would put the bind line after
    a failable op, leaking tempfiles."""
    import ast
    from pathlib import Path as _Path

    src_path = (
        _Path(__file__).resolve().parents[1]
        / "src"
        / "competitionops"
        / "adapters"
        / "pdf_docling.py"
    )
    tree = ast.parse(src_path.read_text(encoding="utf-8"))

    extract_method: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "extract":
            extract_method = node
            break
    assert extract_method is not None, "extract method not found in pdf_docling.py"

    tmp_path_bind_line: int | None = None
    first_failable_op_line: int | None = None
    for node in ast.walk(extract_method):
        # Find: tmp_path = ...
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "tmp_path":
                    if (
                        tmp_path_bind_line is None
                        or node.lineno < tmp_path_bind_line
                    ):
                        tmp_path_bind_line = node.lineno
        # Find first call to .write / .write_bytes / .convert
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("write", "write_bytes", "convert"):
                if (
                    first_failable_op_line is None
                    or node.lineno < first_failable_op_line
                ):
                    first_failable_op_line = node.lineno

    assert tmp_path_bind_line is not None, (
        "extract method must bind a local named ``tmp_path`` — the H2 "
        "fix anchors cleanup on this variable."
    )
    assert first_failable_op_line is not None, (
        "extract method should contain a failable file / engine call "
        "(write / write_bytes / convert)."
    )
    assert tmp_path_bind_line <= first_failable_op_line, (
        f"H2 regression: ``tmp_path`` bound at line {tmp_path_bind_line} "
        f"AFTER first failable op at line {first_failable_op_line}. "
        "If that op raises before tmp_path is captured, the outer "
        "``try/finally`` never runs and the tempfile leaks."
    )


def test_pdf_docling_extract_unlinks_tempfile_in_finally() -> None:
    """H2 structural — cleanup must live inside a ``finally:`` block so
    it runs on any in-try exception. Belt-and-braces with the ordering
    test above."""
    import ast
    from pathlib import Path as _Path

    src_path = (
        _Path(__file__).resolve().parents[1]
        / "src"
        / "competitionops"
        / "adapters"
        / "pdf_docling.py"
    )
    tree = ast.parse(src_path.read_text(encoding="utf-8"))

    extract_method: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "extract":
            extract_method = node
            break
    assert extract_method is not None

    found_unlink_in_finally = False
    for try_node in ast.walk(extract_method):
        if not isinstance(try_node, ast.Try):
            continue
        for stmt in try_node.finalbody:
            for inner in ast.walk(stmt):
                if not isinstance(inner, ast.Call):
                    continue
                func = inner.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "unlink"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "tmp_path"
                ):
                    found_unlink_in_finally = True
                    break

    assert found_unlink_in_finally, (
        "H2 regression: ``tmp_path.unlink(...)`` not found inside a "
        "``finally:`` block. Cleanup must be in finally so it runs "
        "whether the engine succeeds or raises."
    )
