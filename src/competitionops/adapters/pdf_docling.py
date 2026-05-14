"""Docling-backed ``PdfIngestionPort`` adapter (P2-005 Sprint 3).

The Sprint 0 ``MockPdfAdapter`` decodes raw bytes as UTF-8 after the
``%PDF-`` header — fine for synthetic test briefs but unable to read
an actual PDF where text lives inside compressed streams. Docling
(IBM Research's open-source PDF parser) lifts the floor to "real
layout-aware extraction with optional OCR for scanned pages".

Why a separate adapter (not a flag on the mock):

- Docling pulls in heavy ML / CV deps (``easyocr`` → ``torch``,
  ``pypdfium2``, ``huggingface-hub``). Operators who don't need real
  PDF parsing should NOT pay that import cost. Keeping the engine in
  its own module + a lazy import from ``runtime._pdf_adapter`` means
  ``import competitionops`` stays light.
- Future engines (Tika, pymupdf, Marker) plug into the same
  ``PdfIngestionPort`` Protocol without touching this file.

Install:
    uv sync --extra ocr

The ``ocr`` extra in ``pyproject.toml`` pins ``docling``. Sprint 4
will gate GPU acceleration (``--extra ocr-gpu`` or similar) through
the same adapter without API changes.

Why bytes → tempfile → path:

Docling's primary input is a filesystem path because the underlying
PDFium engine memory-maps the file. A ``BytesIO`` workaround exists
in some Docling versions but isn't stable across releases. Writing
the upload bytes to a tempfile is the canonical API, and
``NamedTemporaryFile`` cleans up on context-manager exit even on
exception.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

# Docling is the heavy lazy dep. The ``ocr`` extra in pyproject.toml
# pins it. ``runtime._pdf_adapter`` imports THIS module lazily so a
# default install never pays the cost; ``ImportError`` surfaces only
# when the operator opts in via ``PDF_ADAPTER=docling``.
#
# Dual ignore: ``import-not-found`` covers dev / CI installs without
# ``--extra ocr`` (mypy run against a default venv); ``unused-ignore``
# keeps mypy silent on machines that DO have the extra. Same pattern
# the OTLP lazy import in main.py uses.
from docling.document_converter import (  # type: ignore[import-not-found, unused-ignore]
    DocumentConverter,
)


class DoclingPdfAdapter:
    """Real PDF extraction via Docling's ``DocumentConverter``.

    The converter loads ML models on first use, so we keep one per
    adapter instance (matched to the ``@lru_cache`` ``_pdf_adapter``
    factory in ``runtime.py``).
    """

    def __init__(self) -> None:
        self._converter = DocumentConverter()

    def extract(self, pdf_bytes: bytes) -> str:
        """Return Docling's markdown rendering of the PDF.

        Markdown over plain text on purpose: it preserves headings,
        tables, and list structure, which the regex-based
        ``BriefExtractor`` can selectively ignore but the future
        LLM-prompted extractor (Sprint 5+) will actually use.
        """
        # Round-2 H2 — capture ``tmp_path`` BEFORE any failable op so the
        # outer ``try/finally`` always runs. The previous shape did
        # ``handle.write(pdf_bytes); tmp_path = Path(handle.name)`` inside
        # the ``with`` block; an OSError on write (disk full / quota)
        # propagated past the assignment, so ``tmp_path`` was never bound
        # and the outer finally never reached — orphan ``.pdf`` files
        # accumulated under ``$TMPDIR``.
        #
        # ``NamedTemporaryFile(delete=False)`` so Docling can open the
        # file by path after we've closed our handle; the GC closing our
        # handle while Docling reads has crashed on some platforms. The
        # explicit ``unlink`` in the ``finally`` is our cleanup.
        with tempfile.NamedTemporaryFile(
            suffix=".pdf", delete=False
        ) as handle:
            tmp_path = Path(handle.name)
        try:
            tmp_path.write_bytes(pdf_bytes)
            result = self._converter.convert(tmp_path)
            # Docling has no published type stubs; cast to str so mypy
            # doesn't complain about returning Any from this method.
            return str(result.document.export_to_markdown())
        finally:
            tmp_path.unlink(missing_ok=True)
