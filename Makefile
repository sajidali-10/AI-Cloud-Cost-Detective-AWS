# Makefile — AI Cloud Cost Detective (Phase 0)
# Wraps Docker Compose and project validation cleanly.

REPO_ROOT := $(shell pwd)
COMPOSE   ?= docker compose

.PHONY: help up down restart build logs ps test verify clean secrets secrets-self-check migrate migrate

help:
	@echo "Targets:"
	@echo "  up               Start the full stack"
	@echo "  down             Stop the stack (keeps volumes)"
	@echo "  restart          Restart the stack"
	@echo "  build            Build all images"
	@echo "  logs             Tail logs from all services"
	@echo "  ps               Show running services"
	@echo "  test             Run backend pytest suite"
	@echo "  verify           Run scripts/phase0_verify.sh end-to-end"
	@echo "  verify-phase1    Run scripts/phase1_verify.sh end-to-end"
	@echo "  verify-phase2    Run scripts/phase2_verify.sh end-to-end"
	@echo "  secrets          Generate .env from .env.example (overwrites .env)"
	@echo "  migrate          Apply Alembic migrations (Phase 2 cost_cache)"
	@echo "  clean            Stop stack and REMOVE volumes (destructive; explicit only)"

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart

build:
	$(COMPOSE) build

logs:
	$(COMPOSE) logs -f --tail=200

ps:
	$(COMPOSE) ps

test:
	$(COMPOSE) run --rm --entrypoint=pytest backend -q

verify:
	bash scripts/phase0_verify.sh

verify-phase1:
	bash scripts/phase1_verify.sh

verify-phase2:
	bash scripts/phase2_verify.sh

verify-phase3:
	bash scripts/phase3_verify.sh

verify-phase4:
	bash scripts/phase4_verify.sh

verify-phase5a:
	bash scripts/phase5a_verify.sh

create-admin:
	$(COMPOSE) exec backend python scripts/create_admin.py

secrets:
	bash scripts/generate_dev_secrets.sh

secrets-self-check:
	bash -n scripts/generate_dev_secrets.sh && bash -c 'set -e; test -f scripts/generate_dev_secrets.sh; bash -n scripts/generate_dev_secrets.sh; ! grep -E "AKIA|sk-[A-Za-z0-9]{20,}" .env.example; echo "secrets script self-check ok"'

migrate:
	$(COMPOSE) run --rm --entrypoint=alembic backend upgrade head

clean:
	@echo "This will REMOVE all named volumes (including the PostgreSQL data volume)."
	@echo "Press Ctrl-C within 5 seconds to abort..."
	@sleep 5
	$(COMPOSE) down -v
