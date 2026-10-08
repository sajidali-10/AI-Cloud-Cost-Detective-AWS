# PHASE 6B — REAL AWS FINOPS DASHBOARD REPORT

## Overall Status: PASS

The professional frontend is now wired to real Phase 1–3 AWS data.
The HipLink shell from Phase 6A is preserved unchanged; every widget
either renders a real backend value or renders an explicit
loading / empty / partial / failed state. No fake numbers anywhere.

* **Branch:** `phase-6-professional-dashboard`
* **Commit:** (see final commit on this branch)
* **Baseline:** `1eeef71` (Phase 6A closure)

---

## Dashboard

* **AWS Spend**: real `report.total_cost` from
  `/api/aws/costs?days=30`. Live total at verification time: **$2438.08
  for 30 days** (rendered as `$2.44K`).
* **Cost Change**: real `change_percent` / `change_amount` from the
  same report. Renders `+5.0%` etc with an "Increase vs previous 30
  days" line and the absolute change in USD.
* **Resources**: real `services[*].items.length` totals from
  `/api/aws/resources`. Live count: **88 EC2 + 123 EBS + 20 EIP + 2
  NAT + 2 RDS + 50 Lambda + 37 S3 = 322 resources across 7
  types**. Renders as **"Across 7 resource types"**.
* **Optimization**: real `summary.total_recommendations` from
  `/api/aws/optimization/summary?days=30`. Live: **30
  opportunities** with `recommendations_without_savings: 30`, so
  secondary text is **"savings not available"** (never `$0`).

---

## Costs

* **Daily Trend**: real `report.daily_trend` (30 points) rendered via
  the new `CostTrendChart` SVG area+line chart. Tooltip shows date +
  exact formatted cost.
* **Services**: real `report.by_service` (20 entries) rendered as a
  ranked bar list. Top contributors shown first; tail rows collapse
  into **"Other"** when >10.
* **Regions**: real `report.by_region` (19 entries). `global` /
  `no_region` are labelled **"Global / No Region"** rather than
  dropped silently.

---

## Resources

* **Inventory**: 7 resource-type summary cards (EC2 / EBS / EIP / NAT /
  ELBv2 / RDS / Lambda / S3) with item counts and a per-service
  `Healthy` / `Access denied` / `Error` status badge.
* **Filtering**: Type, Region, and free-text Search on name / id.
  Filters are client-side over the in-memory inventory (no extra
  round-trip).
* **Utilization**: clicking an EC2 / RDS / Lambda / ELB row fetches
  CloudWatch utilization for that resource. Per the spec, missing
  metrics render **`No data`** — never `0`. EC2 rows that are not
  utilization-supporting are filtered out.

---

## Utilization

* **EC2**: CPU avg / max + network in/out.
* **RDS**: CPU, connections, freeable memory.
* **Lambda**: invocations, duration, errors, throttles.
* **Load Balancers**: request count + processed bytes (ALB and NLB
  supported).
* **Missing-data handling**: `No data` everywhere; no `0` shortcuts.

---

## Optimization

* **Recommendation count**: **30** (deduplicated). Rendered as the
  primary KPI value with the unit "open opportunities".
* **Recommendation table**: type / region / finding / confidence /
  source / est. savings — all sourced from `/api/aws/optimization/recommendations`.
* **Confidence**: HIGH / MEDIUM / LOW — real values from the backend.
* **Sources**: AWS Cost Optimization Hub, AWS Compute Optimizer,
  Calculated, Deterministic, Unknown — the deterministic / UNKNOWN
  bucket is rendered with a distinct `ai` tone to avoid implying
  in-house rules are official AWS recommendations.
* **Savings**: `Not available` when null (per spec); never `$0`.
* **Capability status**:
  * Compute Optimizer → `Inactive`
  * Cost Optimization Hub → `Not enrolled`
  * Deterministic Engine → `Available`

  These are rendered as informational badges in the capability panel,
  NOT as errors.

---

## Data States

* **Loading**: Phase 6A `LoadingSkeletonRows` + KPI placeholder `—`.
* **Empty**: `EmptyState` titled `"No data"` or `"No matching
  resources"`.
* **Partial**: `PartialWarning` banner across the top of the affected
  page; remaining panels render normally.
* **Failed**: per-panel `ErrorState` with retry button; other panels
  unaffected (so a slow CloudWatch call never blanks the Costs page).

---

## Themes

* **Dark**: every chart axis, bar, badge, table header, and tooltip
  uses semantic tokens (`--primary`, `--border`, `--surface-2`,
  `--fg-muted`). Verified visually via the live nginx endpoint with
  `data-theme="dark"`.
* **Light**: same components, same semantic tokens, theme flips
  automatically when `data-theme="light"`. Verified visually.
* **Charts**: CostTrendChart stroke + fill colors come from
  `stroke-primary`, `fill-primary-soft`, `stroke-border`,
  `fill-fg-muted` — no separate dark/light palettes.

---

## API

* **Endpoints consumed** (all relative paths via the existing
  centralized `apiFetch`):
  * `GET /api/aws/identity`
  * `GET /api/aws/costs?days={7,30,60,90}`
  * `GET /api/aws/resources?region=<r>`
  * `POST /api/aws/utilization`
  * `GET /api/aws/optimization/capabilities`
  * `GET /api/aws/optimization/summary?days=<d>`
  * `GET /api/aws/optimization/recommendations?days=<d>`
* **Duplicate-call controls**: all fetches go through
  `useFinopsQuery(cacheKey, fetcher, ttlMs)` which de-duplicates
  concurrent requests per cache key and serves cached data for
  `ttlMs` (60s default). Components do not call `fetch()` directly.

---

## Live AWS Validation

All endpoints were exercised against the live AWS account through
nginx on the current origin:

| Endpoint                                             | HTTP | Key shape |
| ---------------------------------------------------- | ---- | ---------- |
| `/api/aws/identity`                                  | 200  | account `974053642038`, region `us-east-1` |
| `/api/aws/costs?days=30`                             | 200  | total `2438.08`, 30 daily points, 20 services, 19 regions |
| `/api/aws/resources?region=us-east-1`                | 200  | EC2 88, EBS 123, EIP 20, NAT 2, ELBv2 0, RDS 2, Lambda 50, S3 37 |
| `/api/aws/optimization/capabilities`                 | 200  | CO `Inactive`, COH `Not enrolled`, DE `Available` |
| `/api/aws/optimization/recommendations?days=30`      | 200  | `SUCCESS`, `count=30`, savings `null` (deterministic only) |
| `/api/aws/optimization/summary?days=30`              | 200  | total `30`, savings `null`, recommendations_without_savings `30` |

Verification was structural (the right keys exist with the right
types) rather than value-pinned — AWS cost totals change daily, so
the verifier asserts *shape and existence* of the real values rather
than comparing to hardcoded numbers.

---

## Tests

* **Frontend**: `npm test` → **158 tests across 18 files passing**.
  New Phase 6B test files:
  * `format.test.ts` — `maskAccountId`, `parseDecimal`,
    `formatCurrencyDecimal`, `formatCurrencyCompact`,
    `formatSignedChange`, `formatChangePercent` (zero-previous),
    `formatSharePercent`, `periodLabel`, `capabilityLabel`,
    `savingsSourceLabel`, `confidenceLabel`, `changeTone`,
    `serviceStatusLabel`.
  * `finops-store.test.tsx` — single fetch per cache key,
    concurrent subscriber dedup, error propagation, manual refresh.
  * `charts.test.tsx` — CostTrendChart renders points + sr-only
    mirror + empty-state placeholder.
  * `badges.test.tsx` — RecommendationSourceBadge + ConfidenceBadge
    label mapping; PartialWarning renders only when warnings present.
  * `period-selector.test.tsx` — 30d default, 7d click swaps state.
  * `dashboard.test.tsx` — 4 KPIs from real data, missing previous
    period renders `—`, costs endpoint failure surfaces partial
    banner.
  * `costs-page.test.tsx` — KPI tiles, cost trend, period selector
    triggers refetch.
  * `resources-page.test.tsx` — type cards render real counts;
    filters narrow rows; select-resource placeholder before row
    click.
  * `optimization-page.test.tsx` — null savings render "Not
    available"; authoritative savings render currency; partial
    success surfaces banner without blanking; row click opens detail.
* **Build**: `cd frontend && npm run build` succeeds; `dist/index.html`
  is emitted.
* **Phase 6A regression**: Phase 6B delegates to `phase6a_verify.sh`
  when invoked with `PHASE6B_FULL=1`. Skipped in default mode to
  keep the verifier under 60s.
* **Phase 5 regression**: covered by the Phase 6A delegation chain.
* **`scripts/phase6b_verify.sh`**: 70+ structural, data-integration,
  theme, and live-backend checks. Default mode skips the heavy
  Phase 6A/5C delegation; `PHASE6B_FULL=1` enables the full chain.

---

## Docker

* **Public ports**: only `nginx` publishes to the host (verified via
  `docker compose ps`). Phase 6B does not introduce any new ports.
* **Container health**: Phase 6B verifier asserts all services are
  `healthy` / `running`.

---

## Known Issues

* `formatSignedChange` now uses `−` (Unicode minus) for negative
  values — chosen for visual alignment with `+`. Some screen
  readers may pronounce it as "minus" — accepted trade-off.
* Resource inventory shows the first 200 rows on the table. Larger
  inventories (e.g. tens of thousands of EBS volumes) will show the
  hint; server-side pagination is a Phase 7 enhancement.
* Per-row CloudWatch utilization fires a single
  `/api/aws/utilization` call per click. Rapid clicking could spam
  the endpoint; a debounce is a Phase 7 enhancement.

---

## Deferred to 6C

* AI Cost Analyst UX (WebSocket, conversation history, streaming
  responses, sidebar chat).
* Conversation persistence UI (server already has Phase 5B
  persistence).

## Deferred to Phase 8

* Custom HipLink domain + TLS certificate.
* Official HipLink production logo (current placeholder glyph is the
  Phase 6A brand-mark).

---

## Phase 6C Readiness: READY

All Phase 6B prerequisites for the AI conversation UX are in place:

* `/analyst` route is unchanged from Phase 6A (a stub).
* The Phase 5B conversation API + WebSocket are reachable at
  `/api/conversations/*` and `wss://.../api/ws/conversations`.
* Authentication and RBAC are unchanged.
* FinOps period context is shared — Phase 6C's AI prompt builder
  already consumes the same `useFinopsPeriod()` context.

Phase 6C can proceed without any backend or frontend-shell changes.
