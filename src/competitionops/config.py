"""Settings loader (Tier 0 #2: secret fields masked via SecretStr).

The three sensitive credentials — ``anthropic_api_key``,
``google_oauth_client_secret``, ``plane_api_key`` — are typed as
``pydantic.SecretStr`` so that ``repr``, ``str``, ``model_dump``, and
``model_dump_json`` never reveal the raw value. Adapters that actually
need to authenticate must call ``.get_secret_value()`` explicitly,
which makes any leak a deliberate code change rather than an accidental
side effect of structured logging or error responses.

Non-sensitive companion fields (client_id, redirect_uri, base_url) stay
plain strings on purpose — they need to be loggable for debugging.
"""

from functools import lru_cache
from urllib.parse import urlparse

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _validate_http_url(
    value: str | None, *, field_name: str, treat_empty_as_none: bool = False
) -> str | None:
    """Round-2 M7 — validate that a Settings URL field is well-formed.

    Operator typos like ``https//www.googleapis.com`` (missing colon)
    used to flow into the adapter URL builders and only surface at
    the first API call as an opaque httpx ``ConnectError``. This
    validator surfaces them at Settings construction time with a
    clear, actionable error message.

    Args:
        value: Raw env / kwarg input.
        field_name: For error messages.
        treat_empty_as_none: Round-3 H2 — when True, an empty string
            resolves to ``None`` instead of raising. Set this for
            Optional URL fields whose k8s secret template ships an
            empty placeholder (``infra/k8s/base/secret.template.yaml``);
            applying that template unmodified would otherwise
            CrashLoopBackoff the pod. Keep False for fields that have
            a non-empty default and where empty IS a typo
            (``google_drive_api_base``).

    For non-None values (and non-empty when ``treat_empty_as_none``),
    requires:

    - non-empty
    - parses to an ``http`` or ``https`` scheme (so ``https//...`` is
      caught — its ``urlparse().scheme`` is empty)
    - has a netloc (so ``https://`` alone is caught)

    Returns the value with a stripped trailing slash, so adapter
    code's ``base.rstrip("/")`` is idempotent for operators who set
    the env with or without a trailing slash.
    """
    if value is None:
        return None
    if not value:
        if treat_empty_as_none:
            return None
        raise ValueError(
            f"{field_name} cannot be empty — omit the env var to fall "
            "back to the default, OR set a full ``http://`` or "
            "``https://`` URL."
        )
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"{field_name}={value!r} is not a valid URL — must start "
            "with ``http://`` or ``https://`` (check for missing "
            "``://``, e.g. ``https//...``, or a misspelled scheme)."
        )
    if not parsed.netloc:
        raise ValueError(
            f"{field_name}={value!r} has no host — the URL must "
            "include a hostname after the scheme."
        )
    return value.rstrip("/")


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "sqlite:///./competitionops.db"
    approval_required: bool = True
    dry_run_default: bool = True

    # Tier 0 #4 — when set (e.g., via env AUDIT_LOG_DIR=/var/lib/competitionops/audit)
    # the audit log factory in main.py / MCP server returns a FileAuditLog
    # writing to this directory, so audit records survive process restart.
    # Leaving this None keeps the in-memory adapter (dev / unit-test default).
    audit_log_dir: str | None = None
    # H2 follow-up — when set (e.g., env PLAN_REPO_DIR=/var/lib/competitionops/plans)
    # the plan repository factory returns a FilePlanRepository instead of the
    # process-bound InMemoryPlanRepository. Plans survive pod restart; each
    # plan_id is its own JSON file with atomic-rename save so multi-pod reads
    # are safe. (Lifting the prod replicas=1 pin still requires the H3 audit-
    # log multi-writer fix in addition to this.)
    plan_repo_dir: str | None = None

    anthropic_api_key: SecretStr | None = None

    google_oauth_client_id: str | None = None
    google_oauth_client_secret: SecretStr | None = None
    google_oauth_redirect_uri: str = "http://localhost:8080/callback"
    # P1-005 — short-lived bearer used by the Drive real adapter. Operators
    # wire this from their own OAuth refresh loop (a TokenProvider port is a
    # later step). SecretStr keeps it out of logs / model_dump output.
    google_oauth_access_token: SecretStr | None = None
    # Override the Drive API base URL for staging/self-hosted Drive shims.
    # Production uses the default ``https://www.googleapis.com``.
    google_drive_api_base: str = "https://www.googleapis.com"
    # P1-001 — Google Docs API base. Default is the prod Docs URL so an
    # operator providing only a bearer flips Docs into real mode.
    # Staging against a Docs-emulator overrides this. Validated like
    # the Drive base so an invalid value crashes at Settings
    # construction, not at the first ``documents.create``.
    google_docs_api_base: str = "https://docs.googleapis.com"

    plane_base_url: str | None = None
    plane_api_key: SecretStr | None = None
    # P1-004 — workspace_slug and project_id are part of the issue-creation
    # path. Not secrets (visible in user-facing URLs) so they stay plain str.
    plane_workspace_slug: str | None = None
    plane_project_id: str | None = None

    # P2-005 Sprint 3 — PDF parser backend. ``None`` / ``"mock"`` keeps
    # the Sprint 0 byte-decode mock (zero deps, fine for synthetic
    # briefs); ``"docling"`` loads the IBM Research Docling parser
    # (real layout-aware PDF extraction; requires
    # ``uv sync --extra ocr``). Unknown values fail loudly at the
    # ``runtime._pdf_adapter`` factory rather than silently falling
    # back to mock — operator typos surface at startup.
    pdf_adapter: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Round-2 M7 — validate URL fields at Settings construction so
    # operator typos surface loudly at startup, not as opaque httpx
    # ConnectError at the first adapter call.
    @field_validator("google_drive_api_base")
    @classmethod
    def _validate_drive_api_base(cls, v: str) -> str:
        # The default is non-None so the cast is safe; the helper
        # still handles ``None`` for symmetry with optional fields.
        result = _validate_http_url(v, field_name="google_drive_api_base")
        assert result is not None  # for mypy — required field, never None
        return result

    @field_validator("google_docs_api_base")
    @classmethod
    def _validate_docs_api_base(cls, v: str) -> str:
        result = _validate_http_url(v, field_name="google_docs_api_base")
        assert result is not None  # for mypy — required field, never None
        return result

    @field_validator("plane_base_url")
    @classmethod
    def _validate_plane_base_url(cls, v: str | None) -> str | None:
        # Round-3 H2 — ``treat_empty_as_none=True`` because
        # ``infra/k8s/base/secret.template.yaml:22`` ships
        # ``PLANE_BASE_URL: ""`` as a placeholder for the operator to
        # fill in. Without this flag the unmodified template
        # CrashLoopBackoffs the pod at uvicorn import time. Empty
        # semantically means "no Plane wired" → mock mode.
        return _validate_http_url(
            v, field_name="plane_base_url", treat_empty_as_none=True
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
