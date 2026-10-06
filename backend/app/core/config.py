"""Application configuration loaded from environment variables.

This module is the single source of truth for backend settings. It uses
pydantic-settings to read from the environment (or .env if mounted) and
exposes a cached `get_settings()` accessor.

No AWS credentials or paid AI provider keys are required in Phase 0.
"""
from functools import lru_cache
from typing import List

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = Field(default="development")
    app_secret_key: str = Field(default="dev-only-change-me")

    # --- PostgreSQL ---
    postgres_host: str = Field(default="postgres")
    postgres_port: int = Field(default=5432)
    postgres_admin_user: str = Field(default="postgres")
    postgres_admin_password: str = Field(default="")

    cost_detective_db: str = Field(default="cost_detective")
    cost_detective_db_user: str = Field(default="cost_detective_user")
    cost_detective_db_password: str = Field(default="")

    litellm_db: str = Field(default="litellm")
    litellm_db_user: str = Field(default="litellm_user")
    litellm_db_password: str = Field(default="")

    # --- LiteLLM ---
    litellm_host: str = Field(default="litellm")
    litellm_port: int = Field(default=4000)
    litellm_master_key: str = Field(default="")

    # --- CORS (env-driven, never wildcard with credentials) ---
    # Stored as a raw string and split below to avoid pydantic-settings 2.7+
    # JSON-decoding it from the env source.
    cors_allowed_origins: str = Field(default="")

    # --- AWS region (no AWS keys in Phase 0) ---
    aws_default_region: str = Field(default="us-east-1")

    # --- Phase 4: AI Cost Analyst (LiteLLM Gateway) ---
    # Master switch. When False, every /api/ai/* generation endpoint
    # returns a controlled "AI_DISABLED" response without contacting
    # any provider. The status endpoint still reports the disabled
    # state so operators can confirm the configuration.
    ai_enabled: bool = Field(default=False)
    # Base URL for the LiteLLM gateway. When LITELLM_BASE_URL is unset
    # we fall back to http://<litellm_host>:<litellm_port> below.
    litellm_base_url: str = Field(default="")
    litellm_model: str = Field(default="cost-detective-free")
    litellm_api_key: str = Field(default="")
    ai_request_timeout_seconds: int = Field(default=30, ge=1, le=120)
    ai_max_output_tokens: int = Field(default=800, ge=1, le=4000)
    ai_max_context_recommendations: int = Field(default=20, ge=1, le=100)
    ai_max_context_services: int = Field(default=15, ge=1, le=50)
    ai_max_context_regions: int = Field(default=10, ge=1, le=50)
    ai_max_question_length: int = Field(default=2000, ge=1, le=10000)

    # --- Phase 5A: Authentication & RBAC ---
    # Master switch. When False, business APIs behave exactly like
    # Phase 0-4: anonymous access is permitted (a synthetic ADMIN
    # user is injected for backward compatibility). When True, every
    # business route requires a valid bearer token.
    auth_enabled: bool = Field(default=False)
    # Symmetric HMAC secret for JWT signing. Required (length >= 32)
    # when ``auth_enabled=True``. MUST be supplied via environment
    # variables in every non-test deployment; the validator below
    # refuses to start with placeholder or short values.
    jwt_secret: str = Field(default="")
    jwt_algorithm: str = Field(default="HS256")
    jwt_access_token_minutes: int = Field(default=60, ge=1, le=24 * 60)
    jwt_issuer: str = Field(default="ai-cloud-cost-detective")
    jwt_audience: str = Field(default="ai-cloud-cost-detective-api")

    @model_validator(mode="after")
    def _resolve_litellm_base_url(self) -> "Settings":
        """Fill ``litellm_base_url`` from host+port when unset.

        Keeps the existing Phase 0/1/2/3 internal Docker URL the
        default, while letting operators override via LITELLM_BASE_URL
        for tests or alternate deployments.
        """
        if not self.litellm_base_url:
            object.__setattr__(
                self,
                "litellm_base_url",
                f"http://{self.litellm_host}:{self.litellm_port}",
            )
        return self

    @model_validator(mode="after")
    def _validate_auth_secret(self) -> "Settings":
        """Fail closed on missing/unsafe JWT secrets.

        When ``AUTH_ENABLED=true``, ``JWT_SECRET`` must be set, must be
        at least 32 characters long, and must not match any of the
        documented placeholders. The validator raises ``ValueError``
        with a SAFE message (never the secret value).

        When ``AUTH_ENABLED=false``, this is a no-op so existing
        Phase 0-4 development flows keep working unchanged.
        """
        if not self.auth_enabled:
            return self
        secret = (self.jwt_secret or "").strip()
        forbidden = {
            "",
            "change-me",
            "change_me",
            "changeme",
            "dev-only-change-me",
            "dev_only_change_me",
            "your-secret",
            "your_secret",
            "yoursecret",
            "test",
            "test-secret",
            "test_secret",
        }
        if secret.lower() in forbidden:
            raise ValueError(
                "AUTH_ENABLED=true but JWT_SECRET is empty or set to a "
                "documented placeholder. Provide a unique JWT_SECRET of "
                "at least 32 characters via the environment."
            )
        if len(secret) < 32:
            raise ValueError(
                "AUTH_ENABLED=true but JWT_SECRET is shorter than 32 "
                "characters. Provide a longer unique secret via the "
                "environment."
            )
        return self

    @property
    def cors_allowed_origins_list(self) -> List[str]:
        if not self.cors_allowed_origins:
            return []
        return [item.strip() for item in self.cors_allowed_origins.split(",") if item.strip()]

    @property
    def database_url(self) -> str:
        """SQLAlchemy URL for the application database (cost_detective)."""
        return (
            f"postgresql+psycopg://{self.cost_detective_db_user}:"
            f"{self.cost_detective_db_password}@{self.postgres_host}:"
            f"{self.postgres_port}/{self.cost_detective_db}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
