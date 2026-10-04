"""Application configuration loaded from environment variables.

This module is the single source of truth for backend settings. It uses
pydantic-settings to read from the environment (or .env if mounted) and
exposes a cached `get_settings()` accessor.

No AWS credentials or paid AI provider keys are required in Phase 0.
"""
from functools import lru_cache
from typing import List

from pydantic import Field
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

    @property
    def litellm_base_url(self) -> str:
        return f"http://{self.litellm_host}:{self.litellm_port}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
