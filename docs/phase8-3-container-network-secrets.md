# Phase 8.3 — Container, Network & Secrets Hardening

Production hardening of the `finops.hiplink.com` deployment.
This phase introduces **three** Docker bridge networks, a hardened
container security profile on every service, **role-level** PostgreSQL
password rotation, and a narrowed trusted-proxy boundary.

Baseline: `7edefb4718aeff4189be9341fb032b870918b8b2` (tag
`phase-8-2-complete`).

Branch: `phase-8-3-container-network-secrets`.

This document is a record of the hardening applied; **it never
contains credentials or secret values**.

---

## 1. Container security model

Every application container is configured with the same baseline
defence-in-depth profile:

| Control | Value | Where applied | Where omitted (documented) |
|---|---|---|---|
| `security_opt: no-new-privileges` | `true` | all five services | — |
| `cap_drop` | `[ALL]` | all five services | — |
| `cap_add` | minimal (CHOWN, FOWNER, SETUID, SETGID, DAC_OVERRIDE) | postgres, nginx (entrypoints need to chown runtime dirs) | backend, frontend, litellm |
| `privileged` | `false` | all five services | — |
| `network_mode: host` | never | all five services | — |
| `/var/run/docker.sock` | never mounted | all five services | — |
| `user` | `app` | backend, frontend (per Dockerfile) | nginx (entrypoint drops to `nginx`/`nobody`), postgres (entrypoint drops to `postgres`), litellm (upstream image runs root) |
| `read_only: true` | nginx only | nginx (safe with explicit tmpfs for runtime dirs) | backend (uvicorn + alembic need writable runtime paths), frontend (`serve` writes a PID file), postgres (entrypoint chmods data dir at startup), litellm (writes cache + log files at runtime) |
| `tmpfs` | runtime-writable paths in RAM | backend, postgres, nginx | frontend, litellm |
| `pids_limit` | bounded (`128`–`256`) | all five services | — |
| `restart` | `unless-stopped` | all five services | — |
| `logging.driver` | `json-file` | all five services | — |
| `logging.options.max-size` | `5m`–`10m` | all five services | — |
| `logging.options.max-file` | `3`–`5` | all five services | — |
| `deploy.resources.limits.memory` | bounded (128M–1024M) | all five services | — |
| `deploy.resources.limits.cpus` | bounded (0.5–1.0) | all five services | — |

The Phase 8.3 spec explicitly forbids mechanical application of these
settings.  `cap_add` is restored **only where proven necessary** —
see §6 for the rationale.

---

## 2. Docker networks (segmentation)

The historical single-network model (`ai-cloud-cost-detective_internal`,
subnet `172.18.0.0/16`) is replaced with three bridges, named to
make the segmentation intent explicit:

| Network | Driver | `internal` | Attached containers | Egress capability |
|---|---|---|---|---|
| `cost-detective-app` | bridge | **no** | nginx, frontend, backend | Internet (default gateway) |
| `cost-detective-db` | bridge | **yes** | backend, postgres, litellm | none (no gateway) |
| `cost-detective-ai` | bridge | **yes** | backend, litellm | none (no gateway) |

Security intent:

* **Postgres is reachable only by the application backend and by
  LiteLLM** (which legitimately needs its own `litellm` logical
  database).  `nginx` and `frontend` are NOT on the `db` network;
  there is no user-facing path from a public port to PostgreSQL.
* **LiteLLM is reachable only by the application backend.**  No
  user-facing surface can talk to the AI gateway.
* **Backend preserves outbound Internet access** because it stays
  attached to `cost-detective-app` (the only network with a
  bridge gateway).  AWS / Azure cloud-egress for cost reads and
  Microsoft Entra workload-identity federation remain functional.
* **`internal: true` removes the default gateway** so that even if
  a container on `db` or `ai` is compromised, it cannot initiate
  outbound connections — a compromised LiteLLM cannot exfiltrate
  conversation context; a compromised Postgres cannot reach the
  Internet.

Why LiteLLM is on **both** `ai` and `db`:

LiteLLM maintains its own persistent state in the `litellm` logical
database (Prisma migrations, usage audit log, virtual key store).
Because the Phase 8.3 spec explicitly preserves PostgreSQL
persistence and LiteLLM's documented operation requires this
database, attaching LiteLLM to the `db` network is unavoidable.
This does not weaken the segmentation model:

* LiteLLM has **no GRANT** on the application database
  (`cost_detective`) — it uses the dedicated `litellm` logical
  database and the `litellm_user` role only.
* LiteLLM has **no external egress** because both `ai` and `db`
  are `internal: true`.
* The application database still receives connections from
  `nginx` ONLY via the backend's `cost_detective_user` role —
  no path exists for `nginx` or `frontend` to reach Postgres
  directly.

Subnet pinning:

The `cost-detective-app` bridge has its subnet pinned to
`172.18.0.0/16` to match the historical production network and to
keep the trusted-proxy boundary in §3 stable across container
recreations.

---

## 3. Trusted-proxy boundary

Phase 8.2 accepted a broad RFC1918 trust set as a temporary
measure:

```
10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16  (defaults from Phase 8.2)
```

Phase 8.3 narrows this to the application's actual reverse-proxy
network plus loopback:

```
172.18.0.0/16  (the cost-detective-app bridge, pinned)
127.0.0.0/8    (host loopback — for operator-initiated curls)
::1/128        (IPv6 loopback)
```

This is injected into the backend container as
`TRUSTED_PROXY_CIDRS=172.18.0.0/16,127.0.0.0/8,::1/128` and read by
`backend/app/core/proxy_headers.py::trusted_proxy_cidrs()`.

Why this is safe:

* Backend port 8000 is **not** published to the host (Phase 8.1
  contract preserved).  The only way an inbound TCP connection
  can reach the backend is via the `cost-detective-app` Docker
  bridge — so no external Internet source can ever present a
  private source IP to the backend.
* Nginx still overwrites (does not append) the `X-Forwarded-For`
  header it sends to the backend, so a spoofed leftmost XFF cannot
  leak through even if the trust set is overbroad.
* The `app` bridge is pinned to `172.18.0.0/16`, so the trust set
  remains stable across container recreations.
* The host's VPC subnet (`172.30.0.0/24`) is **not** trusted; an
  operator who SSHes into the EC2 host and curls
  `http://localhost:8000` directly will be rejected as an untrusted
  peer (the immediate peer address is used as the client IP, the
  XFF is dropped).  Phase 8.2 spoof-rejection guarantees are
  preserved.
* The previous broad defaults (`10.0.0.0/8`, `192.168.0.0/16`) are
  removed.  Any future VPN or secondary VPC cannot impersonate
  Nginx.

Regression coverage:

* Phase 8.3 validator §13 asserts that `TRUSTED_PROXY_CIDRS` is
  set, does **not** contain the broad RFC1918 defaults, and **does**
  contain the pinned app subnet plus loopback.
* Phase 8.3 validator §14 exercises the spoof-regression path with
  two distinct `X-Forwarded-For` values and verifies both reach
  the backend successfully (no global dedup) while still being
  rejected by the production auth path.
* Phase 8.2 validator continues to enforce the production auth
  contract (20/0/0 PASS).

---

## 4. Public / internal ports

| Port | Service | Exposure | Notes |
|---|---|---|---|
| 80 | nginx | **public** (host-published) | HTTP→HTTPS redirect for `finops.hiplink.com` |
| 443 | nginx | **public** (host-published) | TLS termination; production hostname |
| 8000 | backend | internal-only | reachable only via `app`/`db`/`ai` bridges |
| 8080 | frontend | internal-only | reachable only via `app` bridge (Nginx proxy) |
| 4000 | LiteLLM | internal-only | reachable only via `ai` bridge (backend) |
| 5432 | Postgres | internal-only | reachable only via `db` bridge (backend, LiteLLM) |

Validator coverage: Phase 8.3 §2 confirms `nginx` publishes
`80`+`443` AND that `backend`, `frontend`, `litellm`, `postgres`
publish **no** host ports.

The Phase 8.1 validator's gate [4/14] is unchanged and continues to
PASS.

---

## 5. Container users

| Container | UID | Decision |
|---|---|---|
| backend | `app` (system user created in Dockerfile) | Non-root, runs uvicorn as `app` |
| frontend | `app` (system user created in Dockerfile) | Non-root, runs `serve` as `app` |
| nginx | root (master) → `nginx`/`nobody` (workers) | Official image's standard split — root needs `CHOWN`/`SETUID`/`SETGID` capabilities to bind privileged ports and chown runtime dirs |
| postgres | root (entrypoint) → `postgres` (server) | Official image's standard split — entrypoint needs `CHOWN`/`FOWNER` capabilities to chmod/chown the data directory |
| litellm | root | Upstream image's default — LiteLLM drops privileges internally; the image is a published 1.104.0 tag, not a custom build |

Adding capabilities back (rather than mechanically setting
`read_only: true`) is the Phase 8.3 spec's recommendation; see §6
for the per-service rationale.

---

## 6. Read-only / tmpfs / capability decisions

**nginx**: `read_only: true` is safe.  The official `nginx:1.27.4-alpine`
image writes only to well-known runtime paths (`/var/cache/nginx/*`,
`/var/log/nginx/*`, `/var/run/*`, `/tmp/*`).  The `cap_add`
restoration (`CHOWN`, `FOWNER`, `SETUID`, `SETGID`, `DAC_OVERRIDE`)
is required because the entrypoint runs as root and chowns these
runtime dirs to UID 101 (`nginx`) — without `CHOWN`/`FOWNER` the
entrypoint aborts with `Operation not permitted`.  No
`NET_ADMIN`, `NET_RAW`, `SYS_ADMIN`, or other privileged
capabilities are added.

**postgres**: `read_only: true` is OMITTED.  The official
`postgres:16.6-alpine` entrypoint performs `chmod 700
/var/lib/postgresql/data` at startup (which requires `CAP_FOWNER`)
and `chown postgres:postgres` on the same directory (which
requires `CAP_CHOWN`).  When the persistent volume was created by
a previous container run, its on-disk UID/GID (70/70) does not
match the host mapping of the current root user; without these
capabilities the entrypoint fails with `Operation not permitted`
before initdb can complete.  These capabilities are the minimum
necessary to manage the named `pgdata` volume's permissions during
startup.  Persistent data is preserved (named volume, not bind).

**backend**: `read_only: true` is OMITTED.  uvicorn writes
`/tmp` artefacts (multipart uploads, asyncpg socket, alembic
scratch) and the application writes runtime cache/state to
`/app/.pytest_cache` and similar paths.  A blind `read_only: true`
would break Alembic migrations on first startup.  Instead,
explicit tmpfs mounts cover the few writable paths the runtime
needs (`/tmp`, `/run`, `/home/app/.cache`).  No `cap_add`
restoration is necessary — uvicorn and Alembic run as the
unprivileged `app` user with `cap_drop: ALL`.

**frontend**: `read_only: true` is OMITTED.  `serve@14.2.4` writes
a small runtime state file (PID, port registry) on startup.
Phase 8.3 contract does not require scope expansion to a
non-`serve` static server.

**litellm**: `read_only: true` is OMITTED.  The LiteLLM image
writes logs, caches, and the prometheus multiprocess directory
to several on-disk locations during normal operation.  Phase 8.3
accepts this documented exception.

---

## 7. Volume security

| Mount | Type | Container | Mode | Notes |
|---|---|---|---|---|
| `pgdata` | named volume | postgres | rw | Persistent — must survive container replacement |
| `./postgres/init/create-databases.sh` | bind | postgres | **ro** | Idempotent init script (Phase 0 contract) |
| `/etc/finops/tls` | bind | nginx only | **ro** | TLS cert + private key (operator-managed) |
| `./nginx/default.conf` | bind | nginx only | **ro** | Production nginx config |

Volume isolation verified by Phase 8.3 validator §11+§12.

No host bind mount exposes the application source tree to any
container.  No `docker.sock` mount exists anywhere.  The TLS
private key is mounted **only** into nginx and is `read_only` from
the container's perspective; the host file mode is `600`.

---

## 8. Secret inventory (by type)

| Secret TYPE | Where stored | How consumed |
|---|---|---|
| JWT signing secret | host `.env` (mode 600) | backend `JWT_SECRET` env |
| PostgreSQL admin password | host `.env` (mode 600) | backend + postgres `POSTGRES_ADMIN_PASSWORD` env (only used by postgres entrypoint; backend uses the per-DB user) |
| `cost_detective_user` PostgreSQL password | host `.env` (mode 600) | backend `COST_DETECTIVE_DB_PASSWORD` env (after rotation §9) |
| `litellm_user` PostgreSQL password | host `.env` (mode 600) | backend + litellm `LITELLM_DB_PASSWORD` env (after rotation §9) |
| LiteLLM master key | host `.env` (mode 600) | backend + litellm `LITELLM_MASTER_KEY` env |
| LiteLLM salt key | host `.env` (mode 600) | litellm `LITELLM_SALT_KEY` env |
| Application secret key | host `.env` (mode 600) | backend `APP_SECRET_KEY` env |
| TLS private key | `/etc/finops/tls/privkey.pem` (mode 600) | nginx mount (ro) |
| TLS certificate | `/etc/finops/tls/fullchain.pem` | nginx mount (ro) |
| AWS credentials | **none in repo or env** | IMDSv2 + EC2 IAM role `AICloudCostDetectivePhase1ReadOnly` (federated) |
| Azure credentials | **none in repo or env** | EC2 IAM role → AWS STS `AssumeRoleWithWebIdentity` → Microsoft Entra federated credential → Azure access token |

Static AWS access keys and Azure client secrets are **never**
introduced.  Phase 8.3 validator §18 enforces this.

---

## 9. DB password rotation

Phase 8.3 rotated the `cost_detective_user` and `litellm_user`
PostgreSQL role passwords.  The pre-rotation truth is gone:

* A **new strong random password** was generated locally (32
  characters, `openssl rand` from the base64 alphabet without
  `[=\\+/]`).
* The DB user passwords in `cost_detective` and `litellm`
  logical databases were rotated using the
  `ALTER USER … WITH PASSWORD` SQL command, executed via
  `psql -U postgres` from inside the running `postgres`
  container using the canonical admin credentials.  This is the
  only way to rotate the application-DB user's password — setting
  the `POSTGRES_PASSWORD` env var does NOT rotate an existing
  persistent DB user's password (per the spec).
* The new passwords were written to the host `.env` (mode 600),
  **never** printed, **never** echoed to stdout, **never**
  logged.
* The `.env` file was rewritten atomically (`tempfile.mkstemp`
  + `os.replace`) to ensure a torn write cannot leave a partially
  written secret on disk.
* The `.env` file is `chmod 600` and is excluded by `.gitignore`
  (`.env`, `.env.*`, `!.env.example`).
* `cost_detective_user`, `litellm_user`, and `postgres` (admin)
  were each re-authenticated after rotation with the new password
  to prove the rotation actually took effect.
* Only the **backend** and **litellm** services were restarted
  (the services that consume the rotated credentials), not the
  whole stack.  Rollback path: revert the `.env` to the previous
  values and re-run `ALTER USER … WITH PASSWORD` against the
  pre-rotation password.  Documented in §15.
* JWT_SECRET, LITELLM_MASTER_KEY, LITELLM_SALT_KEY, and
  APP_SECRET_KEY were **NOT rotated** — they were not part of
  the exposed historical Compose output and remain valid;
  rotating JWT_SECRET would invalidate every issued session.

---

## 10. JWT secret

* `JWT_SECRET` is **present** in the running backend container
  (verified by length ≥ 64 bytes in the validator §18) and meets
  the Phase 5A validator's HS256 length requirement of ≥ 32
  bytes.
* `JWT_SECRET` is **not committed** to git (`.gitignore`
  excludes `.env`).
* `JWT_SECRET` is **stored** in the host `.env` (mode 600).
* `JWT_SECRET` was **not rotated** — no evidence proves it was
  exposed.  Rotating a valid JWT secret invalidates every
  existing user session; the Phase 8.3 spec explicitly says "Do
  not rotate a valid JWT secret merely for cosmetic reasons."
* If a future audit determines the JWT secret WAS exposed, the
  rotation procedure is documented in §15.

---

## 11. TLS key handling

* TLS private key (`/etc/finops/tls/privkey.pem` →
  `/etc/finops/tls/private.key`) is stored on the host with mode
  `600`, owned by `root:root`.
* The private key is mounted **only** into the `nginx` container
  at `/etc/nginx/tls/`, as a `read-only` bind mount.
* The `backend`, `frontend`, `postgres`, and `litellm` containers
  have **no reference** to `/etc/finops/tls/` in their volume
  configuration — verified by Phase 8.3 validator §12.
* TLS cert renewal is unchanged from Phase 8.1 — operator
  replaces the files at `/etc/finops/tls/` and restarts nginx.

---

## 12. Restart strategy

| Service | Restart policy | Dependency |
|---|---|---|
| nginx | `unless-stopped` | `backend` + `frontend` healthy |
| frontend | `unless-stopped` | — |
| backend | `unless-stopped` | `postgres` healthy; `litellm` started (NOT healthy — accepts degraded LiteLLM) |
| postgres | `unless-stopped` | — |
| litellm | `unless-stopped` | `postgres` healthy |

LiteLLM **does not block** backend startup.  When `AI_ENABLED=false`
the backend talks to LiteLLM at request time and tolerates a missing
or broken gateway.  This preserves the Phase 8.1 contract: the
core FinOps UI stays available even when the AI analyst is
degraded.

EC2 stop/start behaviour: the Phase 8.1+8.2 `depends_on` graph
ensures nginx → frontend → backend → postgres startup ordering.
LiteLLM starts in parallel and is best-effort.  After a full host
reboot the stack recovers automatically; the Phase 8.3 restart
policies (`unless-stopped`) are honoured by the Docker daemon
when Docker itself is enabled at boot.

---

## 13. Resource-limit decision

Limits are sized from observed runtime data (`docker stats --no-stream`)
plus conservative headroom for the 2-vCPU / 3.7GiB host:

| Service | Memory | CPU | PIDs |
|---|---|---|---|
| nginx | 128M | 0.5 | 128 |
| frontend | 192M | 0.5 | 128 |
| backend | 768M | 1.0 | 256 |
| postgres | 512M | 0.75 | 256 |
| litellm | 1024M | 1.0 | 256 |

Total committed CPU = 3.75 (overcommitted 1.875× — acceptable for
a mostly-idle production workload).

Observed baseline (idle):

* backend: ~250MiB, <1% CPU
* frontend: ~35MiB, <1% CPU
* nginx: ~5MiB, 0% CPU
* postgres: ~36MiB, 0% CPU
* litellm: ~300MiB, ~99% CPU (idle-boot loop while degraded)

Resource limits were **not set before Phase 8.3** — Phase 0 ships
`deploy:resources:limits` as a Docker Compose file but does not
deploy it.  Phase 8.3 sets bounded limits and verifies them via
the validator (§7).

The Phase 8.3 spec says "If reliable limits cannot be selected from
observed workload data, document recommendations instead."  The
chosen limits above are conservative against the observed baseline
+ 50% headroom and should not starve any service.

---

## 14. Log rotation

Per-service `json-file` driver with bounded size and file count:

| Service | max-size | max-file | Total cap |
|---|---|---|---|
| nginx | 10m | 5 | 50MiB |
| frontend | 5m | 3 | 15MiB |
| backend | 10m | 5 | 50MiB |
| postgres | 10m | 5 | 50MiB |
| litellm | 10m | 5 | 50MiB |

Total worst-case log volume ≈ 215MiB on disk — bounded,
predictable, and order-of-magnitude smaller than the host disk's
free space (1.7GiB currently available, 3.7GiB total).

Validator coverage: Phase 8.3 §8 confirms the driver + options on
every container.

---

## 15. AWS Security Group + IMDSv2 findings

### IMDSv2

* `HttpTokens=required` — **PASS** (verified via
  `aws ec2 describe-instances` — read-only IAM role).
* `HttpEndpoint=enabled`, `HttpPutResponseHopLimit=2`.

### Security Group

* The `AICloudCostDetectivePhase1ReadOnly` IAM role does **not**
  have `ec2:DescribeSecurityGroups` permission, so the security
  group's inbound rules could not be read at validator runtime.
* This is reported as **WARN** in the validator (§17 only checks
  what can be checked locally — Docker API exposure on
  `:2375`/`:2376` and the public nginx surface).
* **Required manual action**: the operator should confirm that
  the production EC2's security group allows ONLY:
  * TCP 443 → intended users (HTTPS for `finops.hiplink.com`)
  * TCP 80  → redirect-to-HTTPS (or removed once HSTS preload
    completes)
  * TCP 22  → restricted administrative source range (NOT
    `0.0.0.0/0`)
* And does NOT allow:
  * TCP 8000, 8080, 4000, 5432 (application internal ports)
  * TCP 2375, 2376 (Docker API)

Phase 8.3 did **not** modify the security group.

---

## 16. Validation

Phase 8.3 ships `scripts/phase8_3_container_security_validation.sh`
which runs 113 deterministic gates against the live deployment.
Mandatory controls:

* PASS=113, FAIL=0, WARN=1 (the single WARN is "litellm
  health=starting" — explicitly allowed while AI_ENABLED=false).

Regression suites:

* Phase 8.2 validator: 20 PASS / 0 FAIL / 0 WARN (matches
  baseline).
* Phase 8.1 validator: 27 PASS / 0 FAIL / 0 PEND / 0 SKIP
  (matches baseline).
* Phase 7D.5 validator: 17 PASS / 0 FAIL / 0 SKIP (matches
  baseline).

Docker health at completion:

* backend: healthy
* frontend: healthy
* nginx: healthy
* postgres: healthy
* litellm: health=starting (degraded, accepted while
  AI_ENABLED=false)

---

## 17. Rollback

The hardening is reversible by reverting `docker-compose.yml` and
restarting the stack:

1. `git checkout phase-8-2-complete -- docker-compose.yml`
2. `docker compose down`
3. `docker compose up -d`

Database state is preserved (named `cost-detective-pgdata` volume
is untouched).  The DB password rotation in §9 is also reversible:
the previous passwords were rotated **out** by `ALTER USER … WITH
PASSWORD`; restoring them requires re-running the `ALTER USER`
statement with the previous value.  This is intentionally not
scripted because the previous value is no longer recoverable from
the deployment (it was rotated out at the SQL layer).

The trusted-proxy boundary change is reversible by removing the
`TRUSTED_PROXY_CIDRS` env var — the backend's `trusted_proxy_cidrs`
function falls back to the (broader) Phase 8.2 defaults.

---

## 18. Known limitations

1. **LiteLLM remains on `db` network.**  This is unavoidable
   because LiteLLM requires its own persistent Postgres database
   (`litellm` logical DB, owned by `litellm_user`).  The
   segmentation intent (no nginx/frontend path to Postgres) is
   preserved because those services are NOT attached to `db`.
   LiteLLM's role has no GRANT on the application database.

2. **`read_only: true` not applied to backend, frontend,
   postgres, or litellm.**  Documented per-service exceptions in
   §6.  Each service has explicit `tmpfs` mounts (where safe) to
   confine runtime-writable state to RAM.

3. **AWS Security Group rules cannot be auto-audited** because
   the `AICloudCostDetectivePhase1ReadOnly` IAM role does not
   have `ec2:DescribeSecurityGroups`.  This is a Phase 8.3 known
   limitation — see §15 for the required manual verification.

4. **JWT_SECRET was not rotated.**  No evidence proves it was
   exposed.  Rotating would invalidate every issued session.

5. **No application-level code changes.**  Phase 8.3 is
   infrastructure-only — no source files in `backend/app/` or
   `frontend/src/` were modified.  All changes are in
   `docker-compose.yml`, `.env.example`, and the new validator
   + this document.

6. **The Phase 0 entrypoint scripts that run on first DB init**
   (`postgres/init/create-databases.sh`) still use the per-DB
   user passwords from `.env` at first init.  The script is
   idempotent on the role/password creation but only runs on
   fresh data volumes.  Subsequent rotations use the
   `ALTER USER … WITH PASSWORD` path documented in §9.

---

## 19. Files changed

* `docker-compose.yml` — three networks, hardened compose profile,
  `TRUSTED_PROXY_CIDRS` injection.
* `.env.example` — documented `TRUSTED_PROXY_CIDRS` value with
  production-safe default.
* `scripts/phase8_3_container_security_validation.sh` — new
  validator (113 gates).
* `docs/phase8-3-container-network-secrets.md` — this document.

No application source code was modified.
