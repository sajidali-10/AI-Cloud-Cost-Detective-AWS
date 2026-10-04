"""Shared pytest fixtures: isolate tests from real env vars / secrets."""
import os

# Ensure required settings exist BEFORE app imports.
os.environ.setdefault("APP_SECRET_KEY", "test-secret")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_ADMIN_USER", "postgres")
os.environ.setdefault("POSTGRES_ADMIN_PASSWORD", "test")
os.environ.setdefault("COST_DETECTIVE_DB", "cost_detective")
os.environ.setdefault("COST_DETECTIVE_DB_USER", "cost_detective_user")
os.environ.setdefault("COST_DETECTIVE_DB_PASSWORD", "test")
os.environ.setdefault("LITELLM_DB", "litellm")
os.environ.setdefault("LITELLM_DB_USER", "litellm_user")
os.environ.setdefault("LITELLM_DB_PASSWORD", "test")
os.environ.setdefault("LITELLM_HOST", "localhost")
os.environ.setdefault("LITELLM_PORT", "4000")
os.environ.setdefault("LITELLM_MASTER_KEY", "sk-test")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
