#!/usr/bin/env bash
# generate_dev_secrets.sh
# Safely generate strong local development secrets for the AI Cloud Cost Detective
# Phase 0 platform. Writes a fresh `.env` file at the repo root from `.env.example`
# using cryptographically strong randomness (openssl rand).
#
# This script is idempotent at the file level: it OVERWRITES .env each run.
# It never echoes secret values to stdout. It must NEVER be committed.
#
# Usage:   bash scripts/generate_dev_secrets.sh
# Result:  ./.env (mode 600) with strong random values

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
ENV_EXAMPLE="${REPO_ROOT}/.env.example"

if [[ ! -f "${ENV_EXAMPLE}" ]]; then
  echo "ERROR: .env.example not found at ${ENV_EXAMPLE}" >&2
  exit 1
fi

# --- 1. Generate strong random secrets (never printed) ---
POSTGRES_ADMIN_PASSWORD="$(openssl rand -base64 36 | tr -d '\n=/+' | cut -c1-32)"
COST_DETECTIVE_DB_PASSWORD="$(openssl rand -base64 36 | tr -d '\n=/+' | cut -c1-32)"
LITELLM_DB_PASSWORD="$(openssl rand -base64 36 | tr -d '\n=/+' | cut -c1-32)"

# LITELLM_MASTER_KEY must start with sk- (LiteLLM requirement).
LITELLM_MASTER_KEY="sk-$(openssl rand -base64 48 | tr -d '\n=/+' | cut -c1-48)"

# Salt key (any strong random string).
LITELLM_SALT_KEY="$(openssl rand -base64 32 | tr -d '\n=/+' | cut -c1-32)"

# Backend application secret.
APP_SECRET_KEY="$(openssl rand -base64 48 | tr -d '\n=/+' | cut -c1-48)"

# --- 2. Build .env by substituting the placeholder values ---
TMP_FILE="$(mktemp)"
trap 'rm -f "${TMP_FILE}"' EXIT

# Copy .env.example line-by-line and replace CHANGE_ME for the secret keys.
# Non-secret keys are taken verbatim from .env.example.
while IFS= read -r line || [[ -n "${line}" ]]; do
  case "${line}" in
    POSTGRES_ADMIN_PASSWORD=*)
      printf 'POSTGRES_ADMIN_PASSWORD=%s\n' "${POSTGRES_ADMIN_PASSWORD}" >> "${TMP_FILE}"
      ;;
    COST_DETECTIVE_DB_PASSWORD=*)
      printf 'COST_DETECTIVE_DB_PASSWORD=%s\n' "${COST_DETECTIVE_DB_PASSWORD}" >> "${TMP_FILE}"
      ;;
    LITELLM_DB_PASSWORD=*)
      printf 'LITELLM_DB_PASSWORD=%s\n' "${LITELLM_DB_PASSWORD}" >> "${TMP_FILE}"
      ;;
    LITELLM_MASTER_KEY=*)
      printf 'LITELLM_MASTER_KEY=%s\n' "${LITELLM_MASTER_KEY}" >> "${TMP_FILE}"
      ;;
    LITELLM_SALT_KEY=*)
      printf 'LITELLM_SALT_KEY=%s\n' "${LITELLM_SALT_KEY}" >> "${TMP_FILE}"
      ;;
    APP_SECRET_KEY=*)
      printf 'APP_SECRET_KEY=%s\n' "${APP_SECRET_KEY}" >> "${TMP_FILE}"
      ;;
    ""|"#"*)
      printf '%s\n' "${line}" >> "${TMP_FILE}"
      ;;
    *)
      printf '%s\n' "${line}" >> "${TMP_FILE}"
      ;;
  esac
done < "${ENV_EXAMPLE}"

# --- 3. Move into place with restrictive permissions ---
umask 077
mv -f "${TMP_FILE}" "${ENV_FILE}"
chmod 600 "${ENV_FILE}"

echo "Wrote ${ENV_FILE} (mode 600). Do NOT commit it." >&2
