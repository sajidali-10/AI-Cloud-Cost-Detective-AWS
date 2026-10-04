#!/usr/bin/env bash
# postgres/init/create-databases.sh
# Idempotent initialization: create the two logical databases and their roles.
# Runs once on the first start of the postgres container (docker-entrypoint-initdb.d).
# Source values from the environment variables defined in docker-compose.yml.

set -euo pipefail

: "${POSTGRES_USER:?POSTGRES_USER must be set}"
: "${COST_DETECTIVE_DB:?COST_DETECTIVE_DB must be set}"
: "${COST_DETECTIVE_DB_USER:?COST_DETECTIVE_DB_USER must be set}"
: "${COST_DETECTIVE_DB_PASSWORD:?COST_DETECTIVE_DB_PASSWORD must be set}"
: "${LITELLM_DB:?LITELLM_DB must be set}"
: "${LITELLM_DB_USER:?LITELLM_DB_USER must be set}"
: "${LITELLM_DB_PASSWORD:?LITELLM_DB_PASSWORD must be set}"

# The postgres container's official entrypoint already initialized a DB
# named $POSTGRES_DB using $POSTGRES_USER/$POSTGRES_PASSWORD. We use that
# superuser connection to create additional roles and databases.

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<EOSQL
-- Application database
DO \$\$
BEGIN
   IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${COST_DETECTIVE_DB_USER}') THEN
      CREATE ROLE ${COST_DETECTIVE_DB_USER} WITH LOGIN PASSWORD '${COST_DETECTIVE_DB_PASSWORD}';
   END IF;
END
\$\$;

SELECT 'CREATE DATABASE ${COST_DETECTIVE_DB} OWNER ${COST_DETECTIVE_DB_USER}'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${COST_DETECTIVE_DB}')\gexec

GRANT ALL PRIVILEGES ON DATABASE ${COST_DETECTIVE_DB} TO ${COST_DETECTIVE_DB_USER};

-- LiteLLM database
DO \$\$
BEGIN
   IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${LITELLM_DB_USER}') THEN
      CREATE ROLE ${LITELLM_DB_USER} WITH LOGIN PASSWORD '${LITELLM_DB_PASSWORD}';
   END IF;
END
\$\$;

SELECT 'CREATE DATABASE ${LITELLM_DB} OWNER ${LITELLM_DB_USER}'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${LITELLM_DB}')\gexec

GRANT ALL PRIVILEGES ON DATABASE ${LITELLM_DB} TO ${LITELLM_DB_USER};
EOSQL
