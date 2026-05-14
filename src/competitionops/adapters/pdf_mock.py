"""Mock-first PDF ingestion adapter (P2-005 Sprint 0).

Designed for tests and CI: treats the bytes following the standard
``%PDF-1.x`` header as plain UTF-8 text. Real PDF layout parsing is
deferred to a Docling-backed adapter in Sprint 3 — that one ships in a
separate ``--extra ocr`` group so dev clones stay lean.

The bytes-in / text-out signature matches ``ports.PdfIngestionPort`` so
the brief extractor never needs to know which engine produced the text.
"""

from __future__ import annotations

from typing import Final

_PDF_MAGIC: Final[bytes] = b"%PDF-"


class MockPdfAdapter:
    """Stateless mock — strips the PDF header line and decodes the rest as UTF-8.

    Useful for synthetic test PDFs that embed plain text after the
    ``%PDF-1.4\\n`` magic. Production replaces this with the Docling
    adapter via the ``Settings.pdf_adapter`` switch (Sprint 3).
    """

    def extract(self, pdf_bytes: bytes) -> str:
        if not pdf_bytes:
            return ""
        body = pdf_bytes
        # If a PDF magic header is present, advance past the first
        # newline so the version line itself never leaks into the
        # extracted text.
        if body.startswith(_PDF_MAGIC):
            newline_index = body.find(b"\n")
            if newline_index >= 0:
                body = body[newline_index + 1 :]
        return body.decode("utf-8", errors="ignore")
