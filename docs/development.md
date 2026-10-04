# Development — Phase 0

## Prerequisites

- Ubuntu host (tested on 24.04)
- Docker Engine
- Docker Compose v2
- `openssl` (for `scripts/generate_dev_secrets.sh`)
- `python3` + `pip` + `venv` (for local backend testing)
- `node` + `npm` (for local frontend building)

## First-time setup

```bash
git clone <repo-url> ai-cloud-cost-detective
cd ai-cloud-cost-detective

# Generate a fresh .env with strong random secrets (mode 600).
make secrets

# Build images and start the stack.
make build
make up

# Verify end-to-end.
make verify
```

`.env` is git-ignored and must never be committed.

## Useful commands

| Command         | What it does                                          |
| --------------- | ----------------------------------------------------- |
| `make up`       | Start all services in the background                  |
| `make down`     | Stop the stack (named volume `pgdata` is preserved)   |
| `make restart`  | Restart all services                                  |
| `make build`    | Build all custom images                               |
| `make logs`     | Tail logs from all services                           |
| `make ps`       | Show running services                                 |
| `make test`     | Run backend pytest suite inside the backend container |
| `make verify`   | Run `scripts/phase0_verify.sh` end-to-end             |
| `make clean`    | Stop AND remove all volumes (5-second grace period)   |

## Local development — backend

```bash
cd backend
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

Configuration is loaded via `pydantic-settings` from environment variables
(or `.env` if mounted). See `backend/app/core/config.py`.

## Local development — frontend

```bash
cd frontend
npm install
npm run dev    # dev server on http://localhost:5173, proxies /api to backend
npm run build  # production build into dist/
```

The dev server proxies `/api/*` to the backend container (in Compose) or
`http://localhost:8000` (when running locally outside Compose). The dev proxy
is dev-only — production is served by Nginx from the built `dist/` directory.

## Adding a new dependency

1. Backend: append to `backend/requirements.txt` with a pinned version.
   Rebuild the backend image: `make build backend`.
2. Frontend: append to `frontend/package.json` with a pinned version.
   Rebuild the frontend image: `make build frontend`.

## Stopping the environment

```bash
make down      # stop containers, keep data
make clean     # stop AND delete the pgdata volume (explicit only)
```

`make clean` has a 5-second grace period so you can abort with Ctrl-C.

## Re-generating secrets

`make secrets` overwrites `.env` with fresh random values. After rotating
secrets you must restart the affected services:

```bash
make secrets
docker compose up -d
```
