# Phase 8.4 — Final Security Validation & Production Release

Production security closure for the `finops.hiplink.com` deployment.

This document records the consolidated security posture at the
Phase 8.4 release cut.  It is the single source of truth for
operations and compliance, superseding the per-phase notes for any
control that appears in multiple phases.

**Baseline:** `411867d9c90f3a329dcd3877f7546959c454cb4c`
(tag `phase-8-3-complete`).

**Branch:** `phase-8-4-security-release`.

This document **never contains credentials, JWTs, signing secrets,
or private-key material**.  References to secrets are by name only.

---

## 1. Final architecture

Single-host Docker Compose on one EC2 instance.

```
                            Internet
                                │
                          AWS Security Group
                          (operator-managed)
                                │
                          ┌─────────┐
                          │   :80   │   HTTP → HTTPS 301 redirect
                          │  :443   │   TLS termination
                          └────┬────┘
                          ┌────┴──────────────┐
                          │ cost-detective-app │   bridge, gateway
                          │     172.18.0.0/16 │
                          └┬─────────┬─────────┘
              ┌────────────┘         │
    ┌─────────┴────────┐    ┌────────┴────────┐
    │  cost-detective- │    │  cost-detective-│
    │      frontend    │    │     backend     │  uvicorn, app=non-root
    │ (nginx:1.27.4-   │    │ (cost-detective │
    │  alpine serving  │    │  -backend:phase0)│
    │   static SPA)    │    │                 │
    └──────────────────┘    └─┬───────────────┘
                              │       │
                  ┌───────────┘       └──────────────┐
                  │                                  │
        ┌─────────┴──────────┐           ┌───────────┴────────┐
        │  cost-detective-db │           │  cost-detective-ai │
        │   internal=true    │           │   internal=true    │
        │  (backend,         │           │ (backend, litellm) │
        │   postgres,        │           │   no gateway       │
        │   litellm)         │           │                    │
        └─────────┬──────────┘           └─────────┬──────────┘
                  │                                │
        ┌─────────┴─────────┐            ┌────────┴────────┐
        │  postgres:16.6-   │            │   litellm:       │
        │  alpine           │            │  v1.104.0        │
        │  named vol        │            │  (degraded)      │
        │  cost-detective-  │            │                  │
        │  pgdata           │            │                  │
        └───────────────────┘            └──────────────────┘
```

Three bridges, not one.  Backend is the only container on
all three networks and the only one with external egress
(required for AWS/Azure APIs and Microsoft Entra federation).

---

## 2. Public attack surface

| Port | Process | Source | Notes |
|---|---|---|---|
| 80  | docker-proxy → cost-detective-nginx | 0.0.0.0 | HTTP 301 → HTTPS |
| 443 | docker-proxy → cost-detective-nginx | 0.0.0.0 | TLS, served by nginx |
| 22  | sshd (host) | restricted | Not modified in Phase 8.4 |

**Not exposed to the host:**

| Port | Service | Bound to |
|---|---|---|
| 8000 | backend (uvicorn) | `cost-detective-app` only |
| 8080 | frontend (static) | `cost-detective-app` only |
| 4000 | LiteLLM gateway | `cost-detective-ai` only |
| 5432 | PostgreSQL | `cost-detective-db` only |
| 2375 / 2376 | Docker API | not listening on TCP |

`ss -lntp` confirms the only host listeners are
docker-proxy on 80/443 and sshd on 22.

---

## 3. HTTPS / TLS posture

* **Hostname**: `finops.hiplink.com` (resolves to `98.85.39.207`).
* **Cert subject**: `CN=*.hiplink.com`, **SAN**: `DNS:*.hiplink.com,
  DNS:hiplink.com` (wildcard covers apex).
* **Issuer**: `C=GB, O=Sectigo Limited, CN=Sectigo Public Server
  Authentication CA DV R36` (publicly trusted).
* **Expiry**: `Jan 20 23:59:59 2027 GMT` — **91 days remaining**
  at the Phase 8.4 cut (renewal reminder §16).
* **TLS 1.2**: enabled (cipher ECDHE-RSA-AES256-GCM-SHA384).
* **TLS 1.3**: enabled (cipher TLS_AES_256_GCM_SHA384).
* **TLS 1.0 / 1.1**: rejected (handshake closed without selecting
  a protocol — `openssl s_client -no_tls1_2 -no_tls1_3 -tls1_1`
  returns no negotiated cipher).
* **HTTP → HTTPS**: 301 with `Location: https://finops.hiplink.com/`.
* **HSTS**: `Strict-Transport-Security: max-age=31536000`
  (one year, **no includeSubDomains**, **no preload** — added
  only after the live chain validation proved the cert is
  publicly trusted; preload would lock in any future
  subdomain-coverage mistake).
* **Other security headers**: `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`,
  `Server: nginx` (version suppressed).
* **Private key**: `/etc/finops/tls/privkey.pem` (mode 600,
  owned `root:root`), mounted read-only into the `nginx`
  container only.  Backend, frontend, postgres, and litellm have
  **no reference** to `/etc/finops/tls/` in their volumes —
  verified by Phase 8.4 validator gate 12.

Live chain validation (Phase 8.1 §13 / Phase 8.4 gate 2) connects
to `finops.hiplink.com:443` with `openssl s_client
-verify_return_error` and asserts `Verify return code: 0 (ok)`
against the system trust store.  PASS.

---

## 4. Authentication / RBAC

* `AUTH_ENABLED=true` is reported by `/api/auth/info` on the
  live endpoint (Phase 8.4 gate 4, Phase 8.2 gate 9).
* JWT is HS256, issuer/audience checked on every verify.
  `JWT_SECRET` length is 64 bytes (Phase 8.4 gate 13).
* **Anonymous protected request** — `/api/aws/identity` returns
  **401** (Phase 8.4 gate 5, Phase 8.2 gate 2).
* **Invalid JWT** — forgered HS256 token returns **401**
  (Phase 8.2 gate 3).
* **Expired JWT** — Python-forged `exp=2` token returns **401**
  (Phase 8.2 gate 4).
* **RBAC** — `/api/conversations` (Phase 5B canonical route)
  returns **401** for anonymous (Phase 8.2 gate 5).
* **Dev-token bypass endpoints** — `/api/auth/dev-token`,
  `/api/auth/debug-login`, `/api/auth/mint-test-token`,
  `/api/dev/bypass-auth` all return **404** in production
  (Phase 8.2 gate 6, Phase 8.4 manual re-check).
* **JWT_SECRET** was **NOT rotated** in Phase 8.3 or 8.4 —
  no evidence proves it was exposed, and rotating would
  invalidate every issued session.

---

## 5. CORS / CSRF decision

* **CORS allowlist** is env-driven
  (`CORS_ALLOWED_ORIGINS`) and pinned to
  `https://finops.hiplink.com` for production.  No wildcard.
* **Production origin** — preflight
  `Origin: https://finops.hiplink.com` returns
  `Access-Control-Allow-Origin: https://finops.hiplink.com`
  with `Access-Control-Allow-Credentials: true`
  (Phase 8.4 gate 6, Phase 8.2 gate 7).
* **Foreign origin** — preflight
  `Origin: https://attacker.example` returns no
  `Access-Control-Allow-Origin` header (Phase 8.4 gate 6,
  Phase 8.2 gate 7).
* **CSRF**: the API is JSON-only with bearer-token
  authentication; `Authorization: Bearer …` is not auto-attached
  by browsers.  CORS preflight is the only cross-origin gate.
  No CSRF token machinery is required for this design, and the
  CORS allowlist is the explicit security boundary.

---

## 6. Rate limiting

* `/api/auth/login` enforces a per-client-IP rate limit.
* In Phase 8.4 probing the 11th attempt returned
  **HTTP 429** with a non-empty `Retry-After` header
  (Phase 8.4 gate 7, Phase 8.2 gate 8).
* The limiter buckets by effective client IP.  Two distinct
  `X-Forwarded-For` values from the production domain reach
  the backend independently and are both rejected at the auth
  layer — there is no global dedup and no shared bypass
  (Phase 8.4 gate 8, Phase 8.3 gate 14).

---

## 7. Trusted proxy model

`TRUSTED_PROXY_CIDRS=172.18.0.0/16,127.0.0.0/8,::1/128` — set on
the backend container (Phase 8.3 §13, Phase 8.4 gate 13 — see
`backend/app/core/proxy_headers.py::trusted_proxy_cidrs()`).

| CIDR | Why trusted |
|---|---|
| `172.18.0.0/16` | pinned `cost-detective-app` Docker bridge — the only path to backend |
| `127.0.0.0/8` | host loopback for operator-initiated curls |
| `::1/128` | IPv6 loopback |

The previous broad RFC1918 defaults (`10.0.0.0/8`,
`172.16.0.0/12`, `192.168.0.0/16`) are **removed** (Phase 8.3 §3,
Phase 8.4 validator checks negative-contains).

* Backend port 8000 is **not** host-published; only the
  `app` bridge can deliver TCP to it.
* Nginx overwrites (does not append) `X-Forwarded-For`, so a
  leftmost spoof cannot leak through.
* The Phase 8.4 spoof-regression gate (Phase 8.4 gate 8) sends
  two distinct `X-Forwarded-For` values and verifies both
  reach the backend independently (no global dedup) while
  still being rejected by the production auth path.

---

## 8. Container security

Hardening baseline on every service (Phase 8.3 §1):

| Control | nginx | frontend | backend | postgres | litellm |
|---|---|---|---|---|---|
| `privileged` | false | false | false | false | false |
| `network_mode` | bridge | bridge | bridge | bridge | bridge |
| `security_opt` | no-new-privileges | no-new-privileges | no-new-privileges | no-new-privileges | no-new-privileges |
| `cap_drop` | ALL | ALL | ALL | ALL | ALL |
| `cap_add` | CHOWN/FOWNER/SETUID/SETGID/DAC_OVERRIDE (entrypoint needs) | — | — | CHOWN/FOWNER/SETUID/SETGID/DAC_OVERRIDE (entrypoint needs) | — |
| `read_only` | **true** | false (documented) | false (documented) | false (documented) | false (documented) |
| `tmpfs` | /var/cache/nginx, /var/log/nginx, /var/run, /tmp, /run | — | /tmp, /run, /home/app/.cache | /tmp, /run/postgresql | — |
| `pids_limit` | 128 | 128 | 256 | 256 | 256 |
| `restart` | unless-stopped | unless-stopped | unless-stopped | unless-stopped | unless-stopped |
| log driver | json-file 10m×5 | json-file 5m×3 | json-file 10m×5 | json-file 10m×5 | json-file 10m×5 |
| `User` | root (entrypoint drops) | app | app | root (entrypoint drops) | root |
| `/var/run/docker.sock` mount | absent | absent | absent | absent | absent |

The `cap_add` restoration is the documented minimum
required by the official `nginx:1.27.4-alpine` and
`postgres:16.6-alpine` entrypoints (chown on first boot).
No `NET_ADMIN`, `NET_RAW`, `SYS_ADMIN` or other privileged
capabilities are added.

`read_only: true` is applied where safe (nginx only).  For
backend/frontend/postgres/litellm, the upstream entrypoints
require writable paths; explicit tmpfs mounts confine
runtime-writable state to RAM.

Phase 8.4 gate 11 verifies all five containers are non-privileged,
non-host-network, no-new-privileges, no docker.sock.
Phase 8.4 gate 19 verifies restart policy and bounded logs.

---

## 9. Network segmentation

| Network | Driver | Internal | Members |
|---|---|---|---|
| `cost-detective-app` | bridge | **no** | nginx, frontend, backend |
| `cost-detective-db`  | bridge | **yes** | backend, postgres, litellm |
| `cost-detective-ai`  | bridge | **yes** | backend, litellm |

| Property | Verified |
|---|---|
| nginx NOT on `cost-detective-db` | PASS (Phase 8.4 gate 10) |
| frontend NOT on `cost-detective-db` | PASS (Phase 8.4 gate 10) |
| postgres NOT on `cost-detective-app` | PASS (Phase 8.4 gate 10) |
| `cost-detective-db` is `internal: true` | PASS (Phase 8.4 gate 10) |
| `cost-detective-ai` is `internal: true` | PASS (Phase 8.4 gate 10) |
| `cost-detective-app` subnet pinned `172.18.0.0/16` | PASS (Phase 8.3 §1) |

LiteLLM is on **both** `ai` and `db` because it owns its own
`litellm` logical database (Prisma migrations, virtual key store).
The segmentation intent — no user-facing path from any public
port to PostgreSQL — is preserved because nginx and frontend are
NOT on `db`.  LiteLLM's role (`litellm_user`) has **no GRANT**
on the application database (`cost_detective`).

Backend preserves AWS/Azure egress because it stays on the
`cost-detective-app` bridge (the only one with a default
gateway).  AWS outbound identity federation into Microsoft
Entra continues to work (verified by the Azure canonical
endpoints returning HTTP 200 in Phase 7D.5 §6 and re-probed
live during Phase 8.4 §11 below).

---

## 10. Secrets model

| Secret type | Storage | Consumed by |
|---|---|---|
| `JWT_SECRET` | host `/home/ubuntu/ai-cloud-cost-detective/.env` (mode 600) | backend |
| `APP_SECRET_KEY` | host `.env` (mode 600) | backend |
| `POSTGRES_ADMIN_PASSWORD` | host `.env` (mode 600) | postgres entrypoint + backend |
| `COST_DETECTIVE_DB_PASSWORD` | host `.env` (mode 600) | backend (post-Phase 8.3 rotation) |
| `LITELLM_DB_PASSWORD` | host `.env` (mode 600) | backend + litellm (post-Phase 8.3 rotation) |
| `LITELLM_MASTER_KEY` | host `.env` (mode 600) | backend + litellm |
| `LITELLM_SALT_KEY` | host `.env` (mode 600) | litellm |
| TLS private key | `/etc/finops/tls/privkey.pem` (mode 600) | nginx only (ro mount) |
| TLS certificate | `/etc/finops/tls/fullchain.pem` | nginx only (ro mount) |
| AWS credentials | **none** | IMDSv2 + IAM role `AICloudCostDetectivePhase1ReadOnly` |
| Azure credentials | **none** | AWS STS AssumeRoleWithWebIdentity → Microsoft Entra federated credential |

| Property | Verified |
|---|---|
| `.env` not tracked by git | PASS (Phase 8.4 gate 13) |
| `.env` mode = 600 | PASS (Phase 8.4 gate 13) |
| `JWT_SECRET` length ≥ 32 | PASS (length 64) |
| `AWS_ACCESS_KEY_ID` in `.env` | absent (Phase 8.4 gate 14) |
| `AWS_SECRET_ACCESS_KEY` in `.env` | absent (Phase 8.4 gate 14) |
| `AWS_SESSION_TOKEN` in `.env` | absent (Phase 8.4 gate 14) |
| `AZURE_CLIENT_SECRET` in `.env` | absent (Phase 8.4 gate 15) |
| `AZURE_PASSWORD` in `.env` | absent (Phase 8.4 gate 15) |
| `AZURE_TENANT_SECRET` in `.env` | absent (Phase 8.4 gate 15) |
| `RACKSPACE_*` in `.env` | absent (Phase 8.4 gate 15) |
| Tracked tree contains static AWS/Azure secret names | absent (Phase 8.4 gates 14–15) |
| TLS private key mounted into non-nginx container | absent (Phase 8.4 gate 12) |

---

## 11. DB password rotation confirmation (Phase 8.3)

* `cost_detective_user` and `litellm_user` PostgreSQL role
  passwords were rotated in Phase 8.3 (see
  `docs/phase8-3-container-network-secrets.md` §9).  The
  pre-rotation truth is gone.
* Each rotated role was re-authenticated after rotation with
  the new password to prove the rotation actually took effect.
* `.env` was rewritten atomically (`tempfile.mkstemp` +
  `os.replace`).
* Backend and LiteLLM were restarted to consume the new
  credentials; the whole stack was **not** restarted.
* The persistent volume `cost-detective-pgdata` is **untouched**
  by the rotation (named volume, not bind).
* Phase 8.4 **does not** rotate credentials again.
* `/api/health/ready` returns **HTTP 200** (Phase 8.4 gate 17),
  proving the new credentials authenticate the backend against
  the application database.

---

## 12. AWS identity

* **IAM role**: `arn:aws:iam::974053642038:instance-profile/
  AICloudCostDetectivePhase1ReadOnly` (Phase 8.4 gate 16,
  Phase 7C.2A — `aws ec2 describe-instances` shows the role
  attached).
* **Account ID**: `974053642038` (live `/api/aws/identity`
  response, `is_management: true`).
* **Region**: `us-east-1`.
* **No static AWS credentials** in `.env`, in the running
  container environment, or in the tracked repository
  (Phase 8.4 gates 14, 15).
* AWS read paths (`/api/cloud/costs?provider=AWS`,
  `/api/cloud/resources?provider=AWS`,
  `/api/cloud/recommendations?provider=AWS`) return HTTP 200
  with valid bearer token (Phase 7D.5 gate 5).
* IAM/RBAC is **not** modified in Phase 8.4.

---

## 13. Azure federation

* Authentication is **secretless** via AWS outbound identity
  federation into Microsoft Entra (AWS STS
  `AssumeRoleWithWebIdentity` → federated JWT → Entra
  federated credential → Azure access token).
* `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`,
  `AZURE_SUBSCRIPTION_1_ID`, `AZURE_SUBSCRIPTION_2_ID` are
  non-secret identifiers.  `AZURE_CLIENT_SECRET` is **absent**
  (Phase 8.4 gate 15).
* Two Azure subscriptions are discoverable via
  `/api/cloud/accounts/selectable/AZURE` (Phase 7D.5 gate 4):
  - `Microsoft Azure Sponsorship`
  - `MCPP Subscription`
* Azure read paths (`costs`, `resources`, `recommendations`)
  return HTTP 200 with valid bearer token (Phase 7D.5 gate 6).

---

## 14. IMDSv2

* `HttpTokens=required` (Phase 8.3 §15).
* **IMDSv2** token PUT succeeds:
  `PUT /latest/api/token` returns a 20-minute token.
* **IMDSv1** is rejected: `GET /latest/meta-data/iam/info`
  without a token header returns **HTTP 401**
  (Phase 8.4 gate 16).
* IAM info reachable via the v2 token reports the expected
  instance profile.

---

## 15. Logging

* **Driver**: `json-file` on every service
  (Phase 8.4 gate 19, Phase 8.3 §14).
* **Bounded**: 5–10 MB per file, 3–5 rotations
  (≈ 215 MiB worst-case on disk across the stack).
* **Secret-leak scan** (Phase 8.4 §15): bounded sample of
  recent backend / nginx / postgres logs (last 100–500 lines
  per service) shows **zero** matches for:
  - `Bearer <JWT>`
  - `JWT_SECRET`
  - `AWS_SECRET_ACCESS_KEY`
  - `AZURE_CLIENT_SECRET` / `ENTRA_TOKEN`
  - `sk-[A-Za-z0-9]{12,}` (LiteLLM / OpenAI key prefix)
  - `-----BEGIN (RSA|EC|OPENSSH|)PRIVATE KEY-----`
  - password / connection-string credentials
* The application intentionally does **not** log bearer tokens
  or signing secrets — no instrumentation has been added in
  Phase 8.4 that would change that.

---

## 16. Backup / recovery readiness

**What is backed up:**

* **PostgreSQL data**: named volume `cost-detective-pgdata`
  mounted at `/var/lib/docker/volumes/cost-detective-pgdata/_data`
  on the host (driver: `local`).  Not part of the repository.
* **Host `.env`** (mode 600) — must be backed up **out of band**
  (operator workstation or encrypted secret store).  Not in
  git, not in the image, not in any container.
* **TLS material** at `/etc/finops/tls/` (`fullchain.pem`,
  `privkey.pem`, mode 600) — must be backed up out of band.
* **Application repository**: `git clone` of
  `sajidali-10/AI-Cloud-Cost-Detective-AWS` and checkout of
  the `phase-8-4-security-release` branch (or the equivalent
  release tag at merge time).
* **DNS / Elastic IP**: not in our control; the operator
  ensures the route 53 record (or equivalent) for
  `finops.hiplink.com` points to the EC2's Elastic IP.

**Container rebuild procedure:**

1. Restore the application repository at the release tag.
2. Restore `/etc/finops/tls/fullchain.pem` and `privkey.pem`.
3. Restore `/home/ubuntu/ai-cloud-cost-detective/.env`.
4. `docker compose pull` (uses the pinned tags in
   `docker-compose.yml`).
5. `docker compose up -d`.

**Status (this cut):**

| Component | Status |
|---|---|
| Backup configuration verified | YES (the `cost-detective-pgdata` named volume exists; .env and TLS files are present with restrictive permissions) |
| Restore procedure documented | YES (above) |
| Actual PostgreSQL restore test | **NOT performed** in Phase 8.4 (no destructive operations) |
| Actual EC2 reboot | **NOT performed** in Phase 8.4 (deterministic-only verification, §13) |

**Certificate renewal reminder:** cert notAfter is
`Jan 20 23:59:59 2027 GMT`.  **91 days remaining** at the
Phase 8.4 cut.  Renewal should be initiated **at least 30 days
before expiry** — i.e. by **Dec 20 2026** — to allow for
Sectigo validation and operator swap.

---

## 17. Boot / restart readiness

Deterministic verification only.  **No EC2 reboot was
performed during Phase 8.4.**

| Property | Status |
|---|---|
| `docker.service` enabled at boot | YES (`systemctl is-enabled docker` → `enabled`) |
| Host `nginx.service` disabled at boot | YES (`disabled`) |
| All 5 compose services restart=`unless-stopped` | YES (Phase 8.4 gate 19) |
| Dependency graph permits core recovery | YES — `nginx` waits on `backend` + `frontend` (both healthy); `backend` waits on `postgres` healthy and `litellm` started (NOT healthy — accepts degraded gateway); `litellm` waits on `postgres` healthy.  When `AI_ENABLED=false` LiteLLM failure does not block backend/UI startup. |
| LiteLLM failure cannot block backend/UI when `AI_ENABLED=false` | YES — `depends_on: { litellm: { condition: service_started } }` (Phase 8.3 §12, Phase 8.4 gate 19, observed live: `litellm health=starting` while core surfaces remain 200) |

**No EC2 reboot was performed during Phase 8.4.**  This
section is a deterministic analysis, not a live recovery test.

---

## 18. Image / dependency posture

Pinned images in production `docker-compose.yml`:

| Service | Image | Tag |
|---|---|---|
| postgres | `postgres` | `16.6-alpine` |
| litellm | `ghcr.io/berriai/litellm` | `v1.104.0` |
| backend | `cost-detective-backend` | `phase0` (locally built) |
| frontend | `cost-detective-frontend` | `phase0` (locally built) |
| nginx | `nginx` | `1.27.4-alpine` |

* **No floating `:latest` tags** — every image is pinned
  (Phase 8.4 §14 grep).
* **No unexpected unpinned images**.
* **No package upgrades** performed in Phase 8.4.
* **No new vulnerability scanner installed** — only the
  read-only checks the existing validators already provide.

**Deferred patch recommendations** (NOT release blockers):

* Track LiteLLM upstream releases for a future minor
  upgrade (`v1.104.0` → a newer patch when available).
* Track `nginx:1.27.x-alpine` patch updates.
* Track `postgres:16.6-alpine` patch updates.

These are tracked separately and do not block the Phase 8.4
release.

---

## 19. Outstanding accepted risks

These are explicitly NOT release blockers and are documented
here for transparency:

1. **LiteLLM provider/model not configured.**  `AI_ENABLED=false`
   (`.env`, Phase 8.4 §18 — verified live).  LiteLLM is
   degraded (`health=starting`); the application tolerates this
   gracefully.  This is the **same accepted warning** as
   Phase 8.3.
2. **LiteLLM may remain degraded.**  Expected; the canonical AI
   surfaces return `status=DISABLED` cleanly
   (Phase 7D.5 gate 9).
3. **Host `.env` remains the secret store** for this single-EC2
   Compose deployment.  A future move to AWS Secrets Manager
   or HashiCorp Vault is out of scope for Phase 8.4.
4. **No refresh-token architecture.**  Phase 5A ships
   stateless access tokens only; refresh tokens are deferred.
5. **Actual EC2 reboot not performed** in Phase 8.4.
   Boot-recovery posture is verified deterministically (§17)
   but not by live reboot.
6. **Actual PostgreSQL restore test not performed** in Phase 8.4
   (§16).
7. **AWS Security Group confirmation performed manually** by
   the operator — the `AICloudCostDetectivePhase1ReadOnly` IAM
   role does not have `ec2:DescribeSecurityGroups`.  Required
   manual verification (per Phase 8.3 §15):
   - **Allowed**: TCP 22 (restricted source), TCP 80, TCP 443
     on the EC2's primary interface.
   - **Not allowed**: TCP 8000, 8080, 4000, 5432, 2375, 2376.
   The operator has confirmed this manually (recorded here at
   the operator's request).  No additional IAM permissions
   were requested or granted.
8. **JWT_SECRET was not rotated** in Phase 8.4 (and not in
   Phase 8.3).  No evidence proves exposure; rotating would
   invalidate every issued session.
9. **TLS HSTS without `includeSubDomains` or `preload`**
   remains the chosen policy (Phase 8.1 §10 / Phase 8.4 gate 3).
   Adding `preload` is a future hardening step that requires
   the wildcard SAN to be reviewed against every subdomain.

---

## 20. Validation matrix

| Validator | Result | Notes |
|---|---|---|
| Phase 8.4 final | **91 PASS / 0 FAIL / 1 accepted WARN** | `scripts/phase8_4_final_security_validation.sh` |
| Phase 8.3 regression | **113 PASS / 0 FAIL / 1 accepted WARN** | `scripts/phase8_3_container_security_validation.sh` |
| Phase 8.2 regression | **20 PASS / 0 FAIL / 0 WARN** | `scripts/phase8_2_auth_security_validation.sh` |
| Phase 8.1 regression | **27 PASS / 0 FAIL / 0 PEND / 0 SKIP** | `scripts/phase8_1_https_validation.sh` |
| Phase 7D.5 regression | **17 PASS / 0 FAIL / 0 SKIP** | `scripts/phase7d5_release_validation.sh` |

All five gates were run exactly once at the Phase 8.4 cut.
No validator was re-run after the cut.

The single accepted warning in Phase 8.4 and Phase 8.3 is the
same item — `litellm health=starting` while `AI_ENABLED=false`.
This is **explicitly classified as accepted/deferred** in the
validator and is **NOT a release blocker**.

---

## 21. Rollback procedure

Phase 8.4 is documentation-and-validation only — no application
code, no `docker-compose.yml`, no `.env`, no certificate, no
`/etc/finops/tls/` changes.  **No rollback of the application
is required.**  The validator script is the only new artifact.

If a future commit on this branch regresses a release gate:

1. `git revert <commit>`
2. Re-run the five validators (§20).
3. Push the revert to `phase-8-4-security-release`.

If the deployment needs to be reverted to a prior release tag:

1. `git checkout <release-tag> -- docker-compose.yml .env.example`
2. `docker compose down`
3. Restore the prior `.env` from backup.
4. `docker compose up -d`
5. Re-run the five validators.

The DB password rotation is reversible only at the SQL layer
(`ALTER USER … WITH PASSWORD`) because the pre-rotation password
is no longer recoverable from the deployment.  This is
intentional — the Phase 8.3 rotation is a one-way decision.

---

## 22. Files changed in Phase 8.4

* `scripts/phase8_4_final_security_validation.sh` — new validator
  (§16 of the Phase 8.4 spec).  91 PASS / 0 FAIL / 1 accepted
  WARN.
* `docs/phase8-final-security-release.md` — this document.

No application code, no `docker-compose.yml`, no `.env`, no
TLS files, no IAM, no security-group rules were modified in
Phase 8.4.

---

## 23. Certificate renewal reminder

* **Cert notAfter**: `Jan 20 23:59:59 2027 GMT`
* **Days remaining at Phase 8.4 cut**: 91
* **Recommended renewal window**: by **Dec 20 2026**
  (≥ 30 days before expiry)
* **Renewal operator action**: replace
  `/etc/finops/tls/fullchain.pem` and
  `/etc/finops/tls/privkey.pem` on the host, then
  `docker compose restart nginx`.  No other change is required;
  the host-path bind mount is read-only inside the container.
