"""Tier 0 #1 — SSRF allow-list validators.

Covers cloud-metadata blocking, private/loopback IP ranges, scheme
allow-list, drive URI shape rules, and the model-validator wired into
``BriefExtractRequest`` (Pydantic 422 on unsafe input).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from competitionops.schemas import BriefExtractRequest
from competitionops.security import (
    UnsafeSourceURIError,
    assert_safe_drive_uri,
    assert_safe_url,
)


# ---------------------------------------------------------------------------
# URL allow-list
# ---------------------------------------------------------------------------


def test_assert_safe_url_accepts_https_external_host() -> None:
    assert (
        assert_safe_url("https://example.com/brief.pdf")
        == "https://example.com/brief.pdf"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/brief.pdf",   # http not https
        "file:///etc/passwd",              # local file
        "ftp://example.com/brief.pdf",     # ftp
        "data:text/plain;base64,SGVsbG8=", # data url
        "javascript:alert(1)",             # js
        "gopher://example.com/x",          # gopher
    ],
)
def test_assert_safe_url_rejects_non_https_schemes(url: str) -> None:
    with pytest.raises(UnsafeSourceURIError):
        assert_safe_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/x",
        "https://localhost.localdomain/x",
        "https://foo.localhost/x",
        "https://169.254.169.254/latest/meta-data",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://foo.metadata.google.internal/x",
        "https://broadcasthost/x",
    ],
)
def test_assert_safe_url_rejects_metadata_and_loopback_aliases(url: str) -> None:
    with pytest.raises(UnsafeSourceURIError):
        assert_safe_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://10.0.0.1/x",         # private RFC1918
        "https://192.168.1.1/x",      # private RFC1918
        "https://172.16.0.1/x",       # private RFC1918
        "https://127.0.0.1/x",        # loopback
        "https://169.254.0.5/x",      # link-local IPv4
        "https://0.0.0.0/x",          # unspecified
        "https://224.0.0.1/x",        # multicast
        "https://[::1]/x",            # IPv6 loopback
        "https://[fe80::1]/x",        # IPv6 link-local
        "https://[fc00::1]/x",        # IPv6 unique local
    ],
)
def test_assert_safe_url_rejects_private_and_reserved_ip_literals(url: str) -> None:
    with pytest.raises(UnsafeSourceURIError):
        assert_safe_url(url)


def test_assert_safe_url_rejects_empty_and_no_host() -> None:
    with pytest.raises(UnsafeSourceURIError):
        assert_safe_url("")
    with pytest.raises(UnsafeSourceURIError):
        assert_safe_url("https://")


def test_assert_safe_url_does_not_block_legitimate_lookalike_hosts() -> None:
    """A host that *contains* a forbidden token but isn't a suffix passes."""
    # ``localhost.example.com`` is on the public internet despite the prefix
    assert assert_safe_url("https://localhost.example.com/brief.pdf")
    # ``mymetadata.example.com`` is similarly fine
    assert assert_safe_url("https://mymetadata.example.com/x")


# ---------------------------------------------------------------------------
# Drive URI shape
# ---------------------------------------------------------------------------


def test_assert_safe_drive_uri_accepts_well_formed_id() -> None:
    uri = "drive://1AbCdEfGhIjKlMnO_-pqrstuvwxyz12345"
    assert assert_safe_drive_uri(uri) == uri


@pytest.mark.parametrize(
    "uri",
    [
        "",                                       # empty
        "https://drive.google.com/file/d/abc",    # wrong scheme
        "drive://",                                # missing id
        "drive://short",                           # too short
        "drive://../etc/passwd",                   # traversal
        "drive://contains/slash",                  # slash in id
        "drive://contains spaces",                 # space
        "drive://has.dots",                        # dots
    ],
)
def test_assert_safe_drive_uri_rejects_malformed(uri: str) -> None:
    with pytest.raises(UnsafeSourceURIError):
        assert_safe_drive_uri(uri)


# ---------------------------------------------------------------------------
# BriefExtractRequest integration
# ---------------------------------------------------------------------------


def test_brief_extract_request_text_source_unchanged() -> None:
    """Existing ``text`` callers must not break."""
    request = BriefExtractRequest(
        source_type="text", content="RunSpace\nDeadline: 2026-09-30\n"
    )
    assert request.source_type == "text"
    assert request.content


def test_brief_extract_request_rejects_unsafe_url() -> None:
    with pytest.raises(ValidationError):
        BriefExtractRequest(
            source_type="url",
            source_uri="https://169.254.169.254/latest/meta-data",
        )


def test_brief_extract_request_accepts_safe_url() -> None:
    request = BriefExtractRequest(
        source_type="url",
        source_uri="https://example.com/brief.pdf",
        content="",
    )
    assert request.source_type == "url"
    assert request.source_uri == "https://example.com/brief.pdf"


def test_brief_extract_request_requires_source_uri_for_non_text() -> None:
    with pytest.raises(ValidationError):
        BriefExtractRequest(source_type="url", source_uri=None)
    with pytest.raises(ValidationError):
        BriefExtractRequest(source_type="drive", source_uri=None)


def test_brief_extract_request_rejects_malformed_drive_uri() -> None:
    with pytest.raises(ValidationError):
        BriefExtractRequest(
            source_type="drive",
            source_uri="drive://../etc/passwd",
        )


def test_brief_extract_request_accepts_well_formed_drive_uri() -> None:
    request = BriefExtractRequest(
        source_type="drive",
        source_uri="drive://1AbCdEfGhIjKlMnOpQrStUvWxYz12345",
        content="",
    )
    assert request.source_type == "drive"
