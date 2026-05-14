"""Round-2 M7 — Settings URL validators.

Before this commit, ``plane_base_url`` and ``google_drive_api_base``
were plain ``str`` fields. An operator typo like
``https//www.googleapis.com`` (missing colon) would land directly in
the adapter's URL builder, where httpx would surface it as an opaque
``ConnectError`` at the first API call — not at startup, where it
belongs.

The fix is a ``@field_validator`` on each URL field that requires
``http://`` or ``https://`` scheme + non-empty netloc, with a clear
error message. Keeps the field type as ``str`` so existing callers
that do ``s.plane_base_url.rstrip("/")`` etc. continue to work.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from competitionops.config import Settings


# ---------------------------------------------------------------------------
# google_drive_api_base — has a default (https://www.googleapis.com)
# ---------------------------------------------------------------------------


def test_drive_api_base_default_is_accepted() -> None:
    """The shipped default must pass its own validator (regression
    guard against accidentally typing the default itself wrong)."""
    s = Settings()
    assert s.google_drive_api_base == "https://www.googleapis.com"


def test_drive_api_base_accepts_well_formed_https_url() -> None:
    s = Settings(google_drive_api_base="https://drive-test.example.invalid")
    assert s.google_drive_api_base == "https://drive-test.example.invalid"


def test_drive_api_base_accepts_http_for_local_shims() -> None:
    """Self-hosted Drive shims used in dev / kind often live behind
    plain http on a cluster-internal address. The validator must
    accept ``http://`` (we only forbid missing-scheme typos)."""
    s = Settings(google_drive_api_base="http://drive-shim.internal:8080")
    assert s.google_drive_api_base == "http://drive-shim.internal:8080"


def test_drive_api_base_rejects_missing_scheme_colon() -> None:
    """``https//www.googleapis.com`` (missing colon) is the canonical
    M7 motivating typo. Must surface as a startup ValidationError,
    not as an opaque ConnectError at first API call."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(google_drive_api_base="https//www.googleapis.com")
    msg = str(exc_info.value)
    assert "google_drive_api_base" in msg
    # Operator-friendly hint must name the expected schemes.
    assert "http" in msg.lower()


def test_drive_api_base_rejects_misspelled_scheme() -> None:
    with pytest.raises(ValidationError):
        Settings(google_drive_api_base="htts://www.googleapis.com")


def test_drive_api_base_rejects_scheme_only() -> None:
    """``https://`` alone (forgot to paste the host) must fail."""
    with pytest.raises(ValidationError):
        Settings(google_drive_api_base="https://")


def test_drive_api_base_rejects_relative_path() -> None:
    with pytest.raises(ValidationError):
        Settings(google_drive_api_base="/api/drive")


def test_drive_api_base_rejects_empty_string() -> None:
    """Empty string is a common copy-paste artefact when an operator
    means "use the default". Force them to omit the env var instead."""
    with pytest.raises(ValidationError):
        Settings(google_drive_api_base="")


def test_drive_api_base_strips_trailing_slash() -> None:
    """Canonicalise so adapter code's ``base.rstrip("/")`` is
    idempotent — operators who set the env with or without a slash
    get the same behaviour."""
    s = Settings(google_drive_api_base="https://drive.example.invalid/")
    assert s.google_drive_api_base == "https://drive.example.invalid"


# ---------------------------------------------------------------------------
# plane_base_url — Optional (default None)
# ---------------------------------------------------------------------------


def test_plane_base_url_none_is_accepted() -> None:
    """Mock-mode operates without ``plane_base_url``; ``None`` must
    pass the validator."""
    s = Settings(plane_base_url=None)
    assert s.plane_base_url is None


def test_plane_base_url_default_is_none() -> None:
    s = Settings()
    assert s.plane_base_url is None


def test_plane_base_url_accepts_well_formed_https() -> None:
    s = Settings(plane_base_url="https://plane.acme.example.invalid")
    assert s.plane_base_url == "https://plane.acme.example.invalid"


def test_plane_base_url_rejects_typo_scheme() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(plane_base_url="htts://plane.example.invalid")
    msg = str(exc_info.value)
    assert "plane_base_url" in msg


def test_plane_base_url_rejects_missing_scheme_colon() -> None:
    with pytest.raises(ValidationError):
        Settings(plane_base_url="https//plane.example.invalid")


def test_plane_base_url_rejects_empty_string() -> None:
    with pytest.raises(ValidationError):
        Settings(plane_base_url="")


def test_plane_base_url_strips_trailing_slash() -> None:
    s = Settings(plane_base_url="https://plane.example.invalid/")
    assert s.plane_base_url == "https://plane.example.invalid"
