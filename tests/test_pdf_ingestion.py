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


# ---------------------------------------------------------------------------
# M5 — chunked read defense against OOM via oversized upload
#
# Background: the original Sprint 0-2 implementation did
# ``contents = await file.read()`` with no chunk size, then checked the
# 10 MiB limit on the returned bytes. A client posting a Content-Length:
# 10GB upload would force the handler to allocate a 10 GiB Python
# ``bytes`` object BEFORE the 413 check could fire — OOM-killing the pod
# even though the request was clearly oversized. (Starlette
# ``SpooledTemporaryFile`` spills the body to disk past 1 MiB, but
# ``read()`` with no argument materialises the whole thing back into
# RAM.)
#
# The fix reads in fixed-size chunks and refuses with 413 the moment
# accumulated bytes exceed the limit, so the largest in-process
# allocation is bounded by ``limit + chunk_size``.
# ---------------------------------------------------------------------------


def test_pdf_upload_handler_reads_in_bounded_chunks_not_unbounded() -> None:
    """M5 regression guard. Spy on ``UploadFile.read`` to verify the
    handler never calls it without a positive size argument. An
    unbounded read is what created the OOM window in the first place."""
    # Patch the base ``starlette.datastructures.UploadFile`` so spies
    # fire whether FastAPI hands us a starlette instance or its own
    # subclass. (The two are distinct classes at runtime; patching the
    # fastapi subclass alone misses reads on starlette-created
    # instances that flow through the parser.)
    from starlette.datastructures import UploadFile as _UploadFile

    sizes_seen: list[int | None] = []
    original_read = _UploadFile.read

    async def spy(self: _UploadFile, size: int = -1) -> bytes:
        sizes_seen.append(size)
        return await original_read(self, size)

    _UploadFile.read = spy  # type: ignore[method-assign]
    try:
        client = _fresh_client()
        response = client.post(
            "/briefs/extract/pdf",
            files={"file": ("ok.pdf", _FAKE_PDF, "application/pdf")},
        )
    finally:
        _UploadFile.read = original_read  # type: ignore[method-assign]

    assert response.status_code == 200, response.text
    assert sizes_seen, "handler must read the upload at least once"
    unbounded = [s for s in sizes_seen if s is None or s < 0]
    assert not unbounded, (
        f"handler called UploadFile.read with unbounded size {unbounded!r}; "
        "M5 requires chunked reads so an oversized upload cannot allocate "
        "an unbounded bytes object before the 413 check fires."
    )


def test_pdf_upload_short_circuits_oversized_payload_within_a_chunk_of_the_limit() -> None:
    """M5 regression guard — bytes-read budget.

    For an oversized payload, the handler must stop reading shortly
    after crossing the 10 MiB limit. With a 1 MiB chunk size the worst
    case is ``limit + chunk_size = 11 MiB``. We send a body just one
    byte past the limit and verify the cumulative read budget stays
    inside that window. A regressed implementation that buffers
    everything via ``await file.read()`` would read the full body in
    one unbounded call (10 MiB + 1 bytes), exceeding the bound by far.
    """
    from starlette.datastructures import UploadFile as _UploadFile

    bytes_read_total = 0
    sizes_seen: list[int] = []
    original_read = _UploadFile.read

    async def spy(self: _UploadFile, size: int = -1) -> bytes:
        nonlocal bytes_read_total
        sizes_seen.append(size)
        data = await original_read(self, size)
        bytes_read_total += len(data)
        return data

    # 15 MiB — well past the 10 MiB cap. Picking a payload comfortably
    # bigger than (limit + a generous chunk-size allowance) is what lets
    # this test distinguish a buggy unbounded read (which would read
    # all 15 MiB) from a chunked early-exit (which stops at ~11 MiB).
    payload = b"%PDF-1.4\n" + (b"x" * (15 * 1024 * 1024 - 9))
    assert len(payload) == 15 * 1024 * 1024

    _UploadFile.read = spy  # type: ignore[method-assign]
    try:
        client = _fresh_client()
        response = client.post(
            "/briefs/extract/pdf",
            files={"file": ("big.pdf", payload, "application/pdf")},
        )
    finally:
        _UploadFile.read = original_read  # type: ignore[method-assign]

    assert response.status_code == 413, response.text
    # Bound = limit + 2 MiB allowance for chunk overshoot. The fixed
    # implementation (1 MiB chunk) reads ~11 MiB before refusing. The
    # buggy unbounded implementation reads the full 15 MiB and trips
    # the assertion below.
    upper_bound = 10 * 1024 * 1024 + 2 * 1024 * 1024
    assert bytes_read_total <= upper_bound, (
        f"handler read {bytes_read_total} bytes for a 15 MiB payload; "
        f"M5 requires bounded reads (~limit + one chunk = "
        f"<= {upper_bound} bytes). Largest chunk requested: "
        f"max(sizes_seen)={max(sizes_seen) if sizes_seen else 0}."
    )
    # And: refusing past the limit should require more than one read
    # call. A single read of the entire body is exactly the bug.
    assert len(sizes_seen) > 1, (
        f"handler only invoked read() {len(sizes_seen)} time(s); M5 "
        "expects multiple chunked reads before refusing."
    )


def test_pdf_upload_handler_source_does_not_call_unbounded_read() -> None:
    """Structural guard against silent reversion: grep the handler
    source for ``file.read()`` with no args. Cheap and deterministic —
    fires the moment someone restores the original one-liner."""
    import inspect
    import re

    from competitionops import main as main_module

    source = inspect.getsource(main_module.extract_brief_from_pdf)
    forbidden = re.search(r"\bfile\.read\(\s*\)", source)
    assert forbidden is None, (
        "M5 regression — the PDF upload handler calls ``file.read()`` "
        "with no size argument, which buffers the entire upload before "
        "the 413 check can run. Use a chunked loop with an explicit "
        "chunk size instead."
    )
