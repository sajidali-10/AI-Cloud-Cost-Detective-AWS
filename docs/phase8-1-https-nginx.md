# Phase 8.1 — Production DNS, HTTPS & Nginx Hardening

> Branch: `phase-8-1-https-nginx` (no merge to `main`, no completion tag yet)
> Scope: DNS/hostname readiness, HTTPS/TLS termination, Docker Nginx
> configuration, HTTP→HTTPS redirect, basic production security headers,
> external port validation, HTTPS validation, documentation.

This document is the canonical reference for the production HTTPS stack of
AI Cloud Cost Detective.

---

## 1. Architecture

```
                            Internet
                                ↓
                       AWS Elastic IP  (EC2)
                                ↓
                  finops.hiplink.com  (DNS A record)
                                ↓
                       Docker Nginx  (cost-detective-nginx)
                       ┌────────────┴────────────┐
                       │                         │
   :80  HTTP→HTTPS    │   :443  HTTPS termination │
   (301 redirect to   │   TLS 1.2 / TLS 1.3 only  │
    https://…/…)      │   HTTP/2 enabled          │
                       └──────┬───────────────┬───┘
                              │               │
                ┌─────────────┘               └──────────────┐
                ↓                                            ↓
        cost-detective-frontend                  cost-detective-backend
        (React SPA, internal :8080)              (FastAPI, internal :8000)
                                                       │
                                                       ↓
                                              cost-detective-litellm
                                              (internal :4000)
                                                       │
                                                       ↓
                                              cost-detective-postgres
                                              (internal :5432)
```

Only Nginx publishes application ports to the EC2 host (`:80`, `:443`).
All other services live on the `internal` Docker bridge network and are
unreachable from the public network.

---

## 2. Hostname

| Property        | Value                                          |
|-----------------|------------------------------------------------|
| Production FQDN | `finops.hiplink.com`                           |
| Wildcard cert   | `*.hiplink.com` (covers `finops.hiplink.com`)  |
| Port            | `:443` (TLS termination)                       |
| Plain HTTP      | `:80` — permanent redirect to HTTPS only       |

---

## 3. DNS — Required A record

| Field  | Value             |
|--------|-------------------|
| Type   | `A`               |
| Name   | `finops`          |
| Value  | current Elastic IP of the EC2 instance |
| TTL    | `300` (initial)   |

DNS is managed **outside** the application.  The application does NOT
hardcode the Elastic IP; operators must update the A record when the
EC2 instance changes (e.g. fresh Elastic IP after rebuild).

Verify DNS propagation from the EC2 instance or any host:

```bash
getent hosts finops.hiplink.com
# or
dig +short finops.hiplink.com
```

---

## 4. Elastic IP requirement

The EC2 instance **must** have a stable Elastic IP attached.  Phase 8.1
requires a stable A record; without an Elastic IP, a reboot reassigns
the public IP and breaks the production hostname.

---

## 5. Certificate architecture

### Files

| Path on EC2 host                  | Mode   | Notes                          |
|-----------------------------------|--------|--------------------------------|
| `/etc/finops/tls/fullchain.pem`   | 0644   | Public certificate + chain     |
| `/etc/finops/tls/privkey.pem`     | 0600   | Private key (root-only)        |
| `/etc/finops/tls/`                | 0755   | Owned by `root:root`           |

### Mount in Docker

`docker-compose.yml` mounts the directory read-only into Nginx:

```
/etc/finops/tls:/etc/nginx/tls:ro
```

The Nginx config references:

```
ssl_certificate     /etc/nginx/tls/fullchain.pem;
ssl_certificate_key /etc/nginx/tls/privkey.pem;
```

### Installation

Until the real `*.hiplink.com` wildcard is installed, a **self-signed
dev placeholder** is generated locally for deterministic `nginx -t`
validation only.  That placeholder is NOT a production certificate and
must be replaced.

To install the real wildcard:

```bash
sudo install -d -m 0755 -o root -g root /etc/finops/tls
sudo install -m 0644 -o root -g root fullchain.pem /etc/finops/tls/fullchain.pem
sudo install -m 0600 -o root -g root privkey.pem   /etc/finops/tls/privkey.pem
sudo ./scripts/phase8_1_nginx_config_test.sh
```

After installation, `nginx -t` will succeed and Phase 8.1 validation
gate #13 will move from `PEND` to `PASS`.

### Secrets policy

The private key MUST NOT be:

* committed to the repository,
* placed in any `.env` file,
* logged or printed by any script,
* baked into a Docker image,
* exposed to backend/frontend/LiteLLM/Postgres.

The Phase 8.1 validator never reads or prints `privkey.pem`.

---

## 6. HTTPS configuration

* Protocols: `TLSv1.2`, `TLSv1.3` only.  TLS 1.0 and 1.1 are explicitly
  rejected (validated by the Phase 8.1 script).
* Cipher list: Mozilla "intermediate" profile — ECDHE-only on TLS 1.2
  plus the TLS 1.3 AEAD suites.
* HTTP/2 enabled (`http2 on;`).
* OCSP stapling enabled (`ssl_stapling on;`).
* `ssl_prefer_server_ciphers off;` — TLS 1.3 has its own ordering and
  off is the secure default.
* Session tickets disabled.
* Default-server catch-all on `:443` returns `444` (connection closed
  without a response) for any Host header that is not
  `finops.hiplink.com`.

---

## 7. HTTP redirect

Port 80 serves two host categories:

| Host header                                        | Behaviour                  |
|----------------------------------------------------|----------------------------|
| `finops.hiplink.com` (and any other non-internal)  | `301 https://$host$request_uri` |
| `localhost`, `127.0.0.1`, `127.0.0.53`, `::1`      | Serve SPA + `/api/` over plain HTTP |

The internal-host exemption preserves the Phase 7D.5 regression path,
which probes `http://localhost` end-to-end.  Production traffic from
`finops.hiplink.com` (and any other public Host, including the bare
Elastic IP) is **always** redirected to HTTPS.

Path and query string are preserved via `$request_uri`.

---

## 8. Security headers

Production HTTPS responses include (always, including 4xx):

| Header                       | Value                                            |
|------------------------------|--------------------------------------------------|
| `X-Content-Type-Options`     | `nosniff`                                        |
| `X-Frame-Options`            | `DENY`                                           |
| `Referrer-Policy`            | `strict-origin-when-cross-origin`                |
| `Permissions-Policy`         | `geolocation=(), microphone=(), camera=(), payment=()` |
| `Server`                     | `nginx` (version suppressed via `server_tokens off`) |

Phase 7B's sensitive-path deny-list (`.env`, `.git/`, `terraform.tfstate`,
`docker-compose*`, …) is preserved verbatim on every server block.

A Content-Security-Policy is **not** introduced in Phase 8.1 — it must
be hand-tested against the React bundle (JS, fonts, charts, AI page)
before deployment to avoid breaking the SPA.

---

## 9. HSTS activation rule

> **HSTS is intentionally NOT enabled in Phase 8.1.**

HSTS will only be enabled after the live HTTPS path has been
successfully validated end-to-end with the real `*.hiplink.com`
wildcard.  Activation procedure:

1. Confirm `curl https://finops.hiplink.com/ -I` returns 200 with a
   browser-trusted certificate.
2. Confirm the validator (`scripts/phase8_1_https_validation.sh`)
   reports `pass=N fail=0 pending=0`.
3. Edit `nginx/default.conf` and uncomment the HSTS directive in the
   finops.hiplink.com `:443` server block:

   ```
   add_header Strict-Transport-Security "max-age=31536000" always;
   ```

4. Restart Nginx: `docker compose up -d --force-recreate --no-deps nginx`.

**Do NOT add `includeSubDomains` or `preload`** in Phase 8.1 — that
would impose HTTPS-only policy on every `*.hiplink.com` vhost without
explicit sign-off from the owners of those vhosts.

---

## 10. Reverse-proxy headers

Every request proxied to the backend includes:

```
Host:              $host;
X-Real-IP:         $remote_addr;
X-Forwarded-For:   $proxy_add_x_forwarded_for;
X-Forwarded-Proto: https;     # not $scheme — this vhost only listens on 443
```

`X-Forwarded-Proto` is hardcoded to `https` because the `:443` server
block only ever serves TLS — there is no upstream-side plaintext path.

---

## 11. Same-origin application model

The browser accesses the application via:

```
https://finops.hiplink.com/          (SPA)
https://finops.hiplink.com/api/...   (REST)
```

No production-browser URL contains the Elastic IP, the bare EC2 IP,
`localhost`, `backend:8000`, or `http://`.  Docker-internal upstream
URLs (`http://backend:8000`, `http://frontend:8080`) are only used
inside Nginx/backend configuration.

---

## 12. Public vs. private ports

| Port | Service                    | Host-published? |
|------|----------------------------|-----------------|
| 80   | Docker Nginx (redirect)    | YES             |
| 443  | Docker Nginx (TLS)         | YES             |
| 22   | SSH                        | YES             |
| 8000 | Backend (FastAPI)          | NO (internal)   |
| 8080 | Frontend (SPA)             | NO (internal)   |
| 4000 | LiteLLM                    | NO (internal)   |
| 5432 | Postgres                   | NO (internal)   |

---

## 13. Host-OS Nginx conflict prevention

A host-level Nginx was previously observed stealing port 80 from
Docker Nginx after EC2 reboot.  Phase 8.1 requires the host OS Nginx
to remain:

```
systemctl is-enabled nginx   →  disabled
systemctl is-active  nginx   →  inactive
```

The Phase 8.1 validator (gate #1) checks both.  If a future reboot
re-enables the service, run:

```
sudo systemctl disable --now nginx
```

Docker Nginx is the **only** reverse proxy permitted to bind
`:80`/`:443` on this host.

---

## 14. AWS Security Group guidance (informational)

Per Phase 8.1 §14, the AWS Security Group is **not** automatically
edited.  The desired inbound policy is:

| Protocol | Port | Source              | Notes                              |
|----------|------|---------------------|------------------------------------|
| TCP      | 443  | intended users      | Public HTTPS access                |
| TCP      | 80   | intended users      | HTTP → HTTPS redirect              |
| TCP      | 22   | restricted admin IPs| SSH administrative access only     |

Do NOT publicly expose 8000, 8080, 4000, 5432.  The current security
group was not modified by Phase 8.1.

---

## 15. Deterministic Nginx config validation

Before any production restart, run:

```
./scripts/phase8_1_nginx_config_test.sh
```

This wraps `docker compose run --rm --no-deps nginx nginx -t` and
returns a non-zero exit on any configuration error.  It also verifies
that `/etc/finops/tls/{fullchain.pem,privkey.pem}` exist and are
non-empty.

The script NEVER restarts the production container.

---

## 16. Phase 8.1 validation

```
./scripts/phase8_1_https_validation.sh
```

Verifies 14 gates:

1. Host OS nginx disabled + inactive
2. Docker nginx healthy
3. `:80` and `:443` host-published
4. Internal services NOT host-published
5. `nginx -t` PASS
6. DNS resolves `finops.hiplink.com`
7. HTTP→HTTPS 301 redirect for production Host
8. HTTPS `/` returns 2xx
9. HTTPS `/api/health` returns 2xx
10. Security headers present
11. TLS 1.2 + 1.3 succeed; TLS 1.0 + 1.1 rejected
12. Certificate covers `finops.hiplink.com`
13. Certificate chain validates
14. Internal `http://localhost` path still serves the SPA (Phase 7)

Exit codes:

| Code | Meaning                                                 |
|------|---------------------------------------------------------|
| 0    | All deterministic checks PASS; live checks PASS or PEND |
| 1    | One or more deterministic checks FAIL                   |
| 2    | Pre-condition violated                                  |

Until the real `*.hiplink.com` wildcard is installed, gate #13 reports
`PENDING` with self-signed-certificate info.  This is intentional.

---

## 17. Deployment

```bash
# 1. Pre-flight
git checkout phase-8-1-https-nginx
docker compose ps

# 2. Validate config (no restart)
./scripts/phase8_1_nginx_config_test.sh

# 3. Apply
docker compose up -d --force-recreate --no-deps nginx

# 4. Validate live
./scripts/phase8_1_https_validation.sh

# 5. Phase 7 regression
./scripts/phase7d5_release_validation.sh
```

The Phase 8.1 spec §18 explicitly states that backend/frontend images
are rebuilt only if their source/config changed.  In Phase 8.1 only
`docker-compose.yml`, `nginx/default.conf`, and the new scripts
changed — so a `docker compose build backend frontend` is not required
unless you also change backend or frontend code.

---

## 18. Certificate renewal / replacement

1. Drop the new certificate files into `/etc/finops/tls/`:

   ```
   /etc/finops/tls/fullchain.pem   (new)
   /etc/finops/tls/privkey.pem     (new)
   ```

2. Validate the config without restarting:

   ```
   ./scripts/phase8_1_nginx_config_test.sh
   ```

3. Reload Nginx (zero-downtime; uses `nginx -s reload` via the
   official entrypoint):

   ```
   docker compose exec nginx nginx -s reload
   ```

   If exec is not permitted, use:

   ```
   docker compose up -d --force-recreate --no-deps nginx
   ```

4. Re-run the validator:

   ```
   ./scripts/phase8_1_https_validation.sh
   ```

---

## 19. Rollback

```bash
git checkout main
docker compose up -d --force-recreate --no-deps nginx
```

Phase 8.1 does not modify backend or frontend code, so a rollback is
limited to restoring the previous `docker-compose.yml` +
`nginx/default.conf`.  The previous nginx image
(`nginx:1.27.4-alpine`) was untouched.

---

## 20. Troubleshooting

| Symptom                                                              | Likely cause                                              |
|----------------------------------------------------------------------|-----------------------------------------------------------|
| `nginx -t` fails with "cannot load certificate"                      | `/etc/finops/tls/` empty or files missing                  |
| `nginx -t` fails with "host not found in upstream"                   | Compose service `nginx` not on `internal` network         |
| `nginx -t` warns "ssl_stapling ignored, issuer certificate not found"| Self-signed dev placeholder; resolves with real CA cert    |
| HTTPS connection reset; HTTP curl returns Apache                     | A second web server bound to public IP; check `ss -lntp`  |
| Phase 7D.5 regression breaks                                         | Backend source changed; restore from `phase-7d5-complete` tag |
| LiteLLM keeps restarting, dashboard unaffected                       | Pre-existing issue; AI is disabled (`AI_ENABLED=false`)   |

---

## 21. Pre-existing deferred items

The following items are explicitly **out of scope** for Phase 8.1 and
are documented here for visibility only:

* **LiteLLM `cost-analyst` model alias** — currently `None`, causing
  `TypeError: argument of type 'NoneType' is not iterable` during
  LiteLLM startup.  Tracked for a future AI/provider configuration
  phase (post-Phase 8.2).  Until resolved, the backend's `depends_on`
  has been relaxed from `litellm: service_healthy` to
  `litellm: service_started` so the dashboard remains available when
  `AI_ENABLED=false`.

* **Host-level Apache interception of the bare Elastic IP** — direct
  `http://18.210.127.233/` reaches a host-level Apache server (out of
  Phase 8.1 scope).  Production DNS resolves `finops.hiplink.com` to
  the same IP and is correctly handled by Docker Nginx (301 redirect).
  Operators should plan a follow-up to remove or rebind the host-level
  listener.

* **HSTS activation** — deferred until live validation passes.

* **CSP** — deferred until hand-tested against the React frontend
  bundle.

* **Phase 8.2 (auth/session hardening), Rackspace integration, SSO,
  WAF, major frontend redesign** — explicitly NOT started by Phase 8.1.
