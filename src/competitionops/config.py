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

    anthropic_api_key: SecretStr | None = None

    google_oauth_client_id: str | None = None
    google_oauth_client_secret: SecretStr | None = None
    google_oauth_redirect_uri: str = "http://localhost:8080/callback"

    plane_base_url: str | None = None
    plane_api_key: SecretStr | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
