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

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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


@lru_cache
def get_settings() -> Settings:
    return Settings()
