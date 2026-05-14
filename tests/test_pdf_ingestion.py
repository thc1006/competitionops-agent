"""P2-005 — PDF ingestion contract.

Covers Sprints 0-2 of the docs/10 plan:

- Sprint 0: ``PdfIngestionPort`` Protocol + ``MockPdfAdapter`` byte-to-text
- Sprint 1: ``BriefExtractor.extract_from_pdf`` glues port to extractor,
  setting ``source_uri = pdf://<sha1>``
- Sprint 2: ``POST /briefs/extract/pdf`` multipart upload, with size +
  magic-bytes guards

Sprints 3 (Docling real engine), 4 (GPU) and 5 (Drive path) are
intentionally deferred — they require either ``--extra ocr`` or
P1-005 real Drive adapter, neither of which lands in this commit.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from competitionops import main as main_module
from competitionops.adapters.pdf_mock import MockPdfAdapter
from competitionops.config import Settings
from competitionops.main import app
from competitionops.ports import PdfIngestionPort
from competitionops.services.brief_extractor import BriefExtractor

_FAKE_PDF = (
    b"%PDF-1.4\n"
    b"RunSpace Innovation Challenge 2026\n"
    b"Organizer: NYCU Startup Hub (synthetic)\n"
    b"Submission deadline: 2026-09-30\n"
    b"Required deliverables: pitch deck, demo video.\n"
)


# ---------------------------------------------------------------------------
# Sprint 0 — MockPdfAdapter
# ---------------------------------------------------------------------------


def test_mock_pdf_adapter_returns_text_after_magic_header() -> None:
    text = MockPdfAdapter().extract(_FAKE_PDF)
    assert "RunSpace Innovation Challenge" in text
    assert not text.startswith("%PDF-")


def test_mock_pdf_adapter_handles_empty_bytes() -> None:
    assert MockPdfAdapter().extract(b"") == ""


def test_mock_pdf_adapter_handles_payload_without_magic_header() -> None:
    """Non-PDF bytes still decode cleanly — useful for tests that pass
    plain UTF-8."""
    text = MockPdfAdapter().extract(b"plain text without magic")
    assert text == "plain text without magic"


def test_mock_pdf_adapter_implements_pdf_ingestion_port() -> None:
    port: PdfIngestionPort = MockPdfAdapter()
    assert port.extract(_FAKE_PDF)


# ---------------------------------------------------------------------------
# Sprint 1 — BriefExtractor.extract_from_pdf
# ---------------------------------------------------------------------------


def test_brief_extractor_pdf_path_requires_pdf_port() -> None:
    extractor = BriefExtractor(settings=Settings())  # no pdf_port
    with pytest.raises(RuntimeError, match="pdf_port is not configured"):
        extractor.extract_from_pdf(_FAKE_PDF)


def test_brief_extractor_extract_from_pdf_returns_structured_brief() -> None:
    extractor = BriefExtractor(settings=Settings(), pdf_port=MockPdfAdapter())
    brief = extractor.extract_from_pdf(_FAKE_PDF)
    assert brief.name.startswith("RunSpace")
    assert brief.submission_deadline is not None
    assert brief.submission_deadline.strftime("%Y-%m-%d") == "2026-09-30"
    assert brief.deliverables


def test_brief_extractor_pdf_default_source_uri_is_content_hash() -> None:
    extractor = BriefExtractor(settings=Settings(), pdf_port=MockPdfAdapter())
    brief = extractor.extract_from_pdf(_FAKE_PDF)
    expected_digest = hashlib.sha1(_FAKE_PDF).hexdigest()[:16]
    assert brief.source_uri == f"pdf://{expected_digest}"


def test_brief_extractor_pdf_honors_explicit_source_uri() -> None:
    extractor = BriefExtractor(settings=Settings(), pdf_port=MockPdfAdapter())
    brief = extractor.extract_from_pdf(
        _FAKE_PDF, source_uri="drive://workspace-file-abc"
    )
    assert brief.source_uri == "drive://workspace-file-abc"


# ---------------------------------------------------------------------------
# Sprint 2 — POST /briefs/extract/pdf
# ---------------------------------------------------------------------------


def _fresh_client() -> TestClient:
    main_module._plan_repo.cache_clear()
    main_module._audit_log.cache_clear()
    main_module._registry.cache_clear()
    return TestClient(app)


def test_api_pdf_upload_returns_structured_brief_with_pdf_source_uri() -> None:
    client = _fresh_client()
    response = client.post(
        "/briefs/extract/pdf",
        files={"file": ("runspace.pdf", _FAKE_PDF, "application/pdf")},
    )
    assert response.status_code == 200, response.text
    brief = response.json()
    assert brief["name"].startswith("RunSpace")
    assert brief["submission_deadline"].startswith("2026-09-30")
    assert brief["source_uri"].startswith("pdf://")


def test_api_pdf_upload_rejects_oversized_payload() -> None:
    client = _fresh_client()
    # 10 MiB + 1 byte — one over the limit.
    payload = b"%PDF-1.4\n" + (b"x" * (10 * 1024 * 1024 - 9 + 1))
    response = client.post(
        "/briefs/extract/pdf",
        files={"file": ("big.pdf", payload, "application/pdf")},
    )
    assert response.status_code == 413


def test_api_pdf_upload_rejects_missing_pdf_magic_bytes() -> None:
    client = _fresh_client()
    response = client.post(
        "/briefs/extract/pdf",
        files={"file": ("notpdf.txt", b"this is not a pdf", "application/pdf")},
    )
    assert response.status_code == 422
    assert "magic" in response.json()["detail"].lower()


def test_api_pdf_upload_rejects_empty_file() -> None:
    client = _fresh_client()
    response = client.post(
        "/briefs/extract/pdf",
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )
    # An empty file fails the magic-bytes check before anything else.
    assert response.status_code == 422


def test_api_pdf_upload_does_not_pollute_text_endpoint() -> None:
    """The existing JSON endpoint must still reject empty content via
    its own validator — PDF upload is a separate route, not a fallback."""
    client = _fresh_client()
    response = client.post(
        "/briefs/extract",
        json={"source_type": "text", "content": ""},
    )
    assert response.status_code == 422
