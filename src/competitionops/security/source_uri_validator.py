"""SSRF allow-list for ``BriefExtractRequest.source_uri`` (Tier 0 #1).

When P1-006 enables ``source_type="url"`` and ``source_type="drive"``
ingestion, the brief extractor will fetch the referenced resource. To
keep that fetch from being weaponised into a server-side request forgery
against cloud metadata services or internal infrastructure, the URI
must pass these validators **before** any HTTP client is constructed.

Currently the validators are wired into ``BriefExtractRequest`` and
``POST /briefs/extract`` returns 501 for non-text sources — but the
allow-list itself is fully active so a future P1-006 PR only needs to
add the fetch implementation, not the safety check.

Blocks:
- non-HTTPS schemes (no ``http://``, ``file://``, ``ftp://``, ``data:``,
  ``javascript:``, ``gopher://``, …)
- cloud metadata endpoints (GCP ``metadata.google.internal``, AWS
  ``169.254.169.254`` IMDS, common loopback aliases)
- private / loopback / link-local / reserved IPv4 and IPv6 ranges when
  the host is an IP literal
- empty host or empty URI

Out of scope (document, do not silently mitigate):
- DNS rebinding (host resolves to an external IP at validation time
  but a private IP at fetch time) — must be handled at fetch via a
  pinned-IP HTTP client when P1-006 lands.
- TOCTOU between validation and fetch — same mitigation.
- Drive ID ACL — Drive API's own permission checks handle that.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

_ALLOWED_URL_SCHEMES = frozenset({"https"})

# Drive file IDs from Google Drive are typically 28-44 chars of base64-ish
# alphabet plus ``-`` and ``_``. We accept a slightly wider range so legitimate
# Workspace ids never get falsely rejected, but never accept slashes or dots
# which would enable path traversal.
_DRIVE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,256}$")

# Hostnames or host suffixes that must never be reachable as a source URI.
# Suffix match (``host == forbidden`` or ``host.endswith("." + forbidden)``)
# to defend against subdomain-style escapes (``metadata.google.internal.evil.com``
# would still NOT match — only true suffixes do).
_FORBIDDEN_HOST_SUFFIXES: frozenset[str] = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "169.254.169.254",
        "metadata",  # docker / k8s internal common alias
        "localhost",
        "localhost.localdomain",  # default Linux /etc/hosts loopback alias
        "ip6-localhost",
        "ip6-loopback",
        "broadcasthost",
    }
)


class UnsafeSourceURIError(ValueError):
    """Raised when a source URI fails the SSRF allow-list."""


def assert_safe_url(url: str) -> str:
    """Validate that ``url`` is safe for a future P1-006 fetch.

    Returns the URL on success; raises ``UnsafeSourceURIError`` otherwise.
    """
    if not url:
        raise UnsafeSourceURIError("source url is empty")

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise UnsafeSourceURIError(
            f"scheme {scheme!r} not allowed; only {sorted(_ALLOWED_URL_SCHEMES)} permitted"
        )

    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsafeSourceURIError("source url has no host component")

    # Suffix-match against the forbidden list. Suffix not substring so a
    # legit ``localhost.example.com`` is not blocked but ``foo.localhost``
    # is. Bare hostname equality also covered.
    for forbidden in _FORBIDDEN_HOST_SUFFIXES:
        if host == forbidden or host.endswith("." + forbidden):
            raise UnsafeSourceURIError(
                f"host {host!r} matches forbidden suffix {forbidden!r}"
            )

    # When the host is an IP literal, refuse private / loopback / link-local
    # / reserved / multicast ranges. Non-IP hostnames pass — DNS rebinding
    # is documented as out of scope (must be re-checked at fetch time).
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return url

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise UnsafeSourceURIError(
            f"host IP {host!r} is in a reserved / private range "
            f"(private={ip.is_private}, loopback={ip.is_loopback}, "
            f"link_local={ip.is_link_local})"
        )
    return url


def assert_safe_drive_uri(uri: str) -> str:
    """Validate a ``drive://<file_id>`` URI.

    Drive ingestion goes through the (future) Google Drive adapter, which
    performs ACL checks server-side, so the only thing this validator must
    enforce is the URI shape — no slashes, no relative-traversal sequences.
    """
    if not uri:
        raise UnsafeSourceURIError("source drive uri is empty")
    if not uri.startswith("drive://"):
        raise UnsafeSourceURIError(
            f"drive uri must start with 'drive://', got {uri!r}"
        )
    file_id = uri[len("drive://") :]
    if not _DRIVE_ID_PATTERN.match(file_id):
        raise UnsafeSourceURIError(
            f"drive file id {file_id!r} does not match {_DRIVE_ID_PATTERN.pattern}"
        )
    return uri
