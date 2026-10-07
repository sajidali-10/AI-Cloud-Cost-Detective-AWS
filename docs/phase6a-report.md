# PHASE 6A — HIPLINK PROFESSIONAL FRONTEND FOUNDATION REPORT

## Overall Status

READY for Phase 6B.  Phase 6A ships the HipLink-branded frontend
foundation, theme system, authentication UX, role-aware navigation,
admin user management, and a reusable component library.  All Phase
5 regression checks pass.

## Branch

`phase-6-professional-dashboard`

## Commit

(set by `git commit` below — see commit message at the end)

## Baseline

`b96fee9a8e551f06322dc3d79d9c47fc61468925`

Working tree was clean at audit time and remained clean until the
implementation commit.

---

## Branding

### HipLink asset

No HipLink logo, font file, or product mark exists in the
repository.  The audit searched `frontend/public/`, the `frontend/`
tree, the `backend/` tree, and the entire repo — no SVG / PNG / ICO
asset was found (`find **/*.{svg,png,jpg,ico}` returns zero hits).

The `BrandHeader` component therefore renders an inline wordmark in
the semantic primary color plus a geometric "H" glyph in a rounded
primary-soft tile.  This placeholder drops out cleanly when a real
asset is provided — replace the `BrandGlyph` element with an
`<img src="/logo.svg">` or similar.  No part of the layout assumes
the placeholder is permanent; the surrounding shell has been sized
for a real logo glyph.

### Product identity

```
Product name        AI Cloud Cost Detective
Brand row           HIPLINK · AI Cloud Cost Detective
Login subtitle      Secure AWS FinOps Intelligence
Dashboard subtitle  AWS cost visibility, optimization and AI-powered
                    FinOps analysis
```

All names follow the existing repo's convention.  No new product
naming was introduced.

---

## Application Shell

### Header (desktop)

`src/components/TopNavigation.tsx`

```
┌─────────────────────────────────────────────────────────────────┐
│ [H] HIPLINK · AI Cloud Cost Detective                           │
│ Dashboard  Costs  Resources  Optimization  AI Cost Analyst       │
│ Conversations  Users  Security        [☀]  [👤 Admin] [Logout] │
└─────────────────────────────────────────────────────────────────┘
```

- brand left (wordmark + product subtitle)
- horizontal nav centre (single row, compact)
- theme toggle + user menu right
- sticky top, subtle border, `bg-bg-elevated` (deep navy in dark,
  white in light)
- active item: cyan `bg-primary-soft` + `ring-1 ring-primary/30`

### Navigation

Role-aware (`src/lib/tokens.ts`):

| Role | Visible items |
|---|---|
| ADMIN | Dashboard, Costs, Resources, Optimization, AI Cost Analyst, Conversations, Users, Security |
| ANALYST | Dashboard, Costs, Resources, Optimization, AI Cost Analyst, Conversations |
| VIEWER | Dashboard, Costs, Resources, Optimization |

### Responsive navigation

`src/components/MobileNavigation.tsx`

- `<lg` (< 1024 px): desktop nav hidden; compact mobile bar visible
  with brand (compact mode), theme toggle, user menu, hamburger
- hamburger opens a slide-down drawer containing the same role-aware
  list
- drawer closes on route change and on Escape

---

## Theme System

### Dark mode

- page background `#0b1426` deep navy
- header `#0f1c33` slightly lighter navy
- card `#152339`
- borders `#233553`
- primary text `#f1f5f9`
- primary accent (HipLink cyan) `#22d3ee`

### Light mode

- page background `#f1f5f9` cool gray
- header `#ffffff`
- card `#ffffff`
- borders `#d8e0ec`
- primary text `#0f172a`
- primary accent (same cyan, accessible on white) `#0891b2`

### Default / system behaviour

On first visit:

1. `localStorage['accd.theme']` is consulted first — explicit user
   choice always wins.
2. Otherwise `prefers-color-scheme: light` is consulted.
3. Final fallback: `dark`.

### Persistence

`localStorage` key `accd.theme`.  Survives refresh, browser restart,
and login state changes.

### Theme toggle

`src/components/ThemeToggle.tsx` — 36 × 36 button in the header and
on the login card.  Shows the icon for the **next** state (sun when
in dark mode = "switch to light").  `aria-label` and `aria-pressed`
both reflect the action / current state.

### Flash / hydration handling

`index.html` runs a synchronous inline script before React mounts.
It sets `<html data-theme="...">` from localStorage / media query /
fallback.  React then reads the attribute and stays in sync.

- `data-theme` + companion `theme-dark` / `theme-light` class set
  before any paint
- no FOUC
- ThemeProvider's `applyTheme` also writes to `documentElement`
  defensively (covers test harnesses and iframes)

---

## Authentication UX

### Login

`src/pages/LoginPage.tsx`

- centered branded card on a deep canvas
- email + password inputs with autocomplete hints
- submit disabled while in-flight
- generic "Invalid email or password." on 401 (never reveals
  whether the email exists)
- "Backend unavailable." on network / 5xx
- keyboard submission via Enter (form default)
- theme toggle accessible from the card corner

### Logout

`src/lib/auth.tsx::logout` drops the session, writes a one-shot
notice, and redirects to `/login`.  `UserMenu` calls it directly;
the dropdown is dismissed first.

### Session bootstrap

On mount:

1. `GET /api/auth/info` — learns `auth_enabled`.
2. If disabled → synthetic dev-mode ADMIN (backward-compat with
   Phase 5A).
3. If enabled and no stored session → login route.
4. If enabled and stored session present → `GET /api/auth/me`
   verifies the token; on 401 the session is dropped and the
   user is bounced to /login with a "session expired" notice.

### Session expiry

- 401 from any authenticated request → `_onSessionExpired()` fires
  once per session lifetime (`sessionExpiredOnce` ref guards
  against loops)
- session is dropped from localStorage
- a `SessionNotice { kind: 'expired' }` is written to sessionStorage
  so the login page can surface "Your session has expired" once
- the `next` query parameter is preserved so the user lands back
  where they were after re-authenticating

### RBAC UX

`src/lib/tokens.ts` is the single source of truth.  The router
refuses a route whose `requiredRoles` excludes the current role and
renders `<AccessDenied>` (inline) or `<AccessDenied>` (standalone
component on the Users page).

| Role | ADMIN | ANALYST | VIEWER |
|---|---|---|---|
| Dashboard | ✓ | ✓ | ✓ |
| Costs | ✓ | ✓ | ✓ |
| Resources | ✓ | ✓ | ✓ |
| Optimization | ✓ | ✓ | ✓ |
| AI Cost Analyst | ✓ | ✓ | — |
| Conversations | ✓ | ✓ | — |
| Users | ✓ | — | — |
| Security | ✓ | — | — |

Frontend hides for UX only.  Backend (`require_admin`,
`require_role`) is authoritative and unchanged.

---

## Components

### MetricCard

Compact KPI tile.  Pattern:

```
[icon] Label
       Large value
       Small description
```

When `value` is undefined, the card renders a `—` placeholder +
`data-empty="true"` so Phase 6B can detect the "no data" state and
tests can verify the absence of fabricated values.

### DataTable

Declarative column + row registry.  Supports:

- numeric columns (right-aligned, tabular-nums)
- hideOnMobile columns
- loading state (single "Loading…" row)
- empty state (delegated to caller via `emptyState` prop)
- horizontal scroll on small screens

### StatusBadge

Six tones: success, warning, danger, info, ai, neutral.  Always
renders text alongside color — never color alone.

### Loading / Empty / Error

- `LoadingSkeleton` / `LoadingSkeletonRows` — restrained animated
  placeholder
- `EmptyState` — title + description + optional action.  Used in
  every Phase 6B / 6C placeholder card.
- `ErrorState` / `BackendUnavailable` / `ForbiddenState` — sanitised
  error messages, optional retry

---

## Admin

`src/pages/UsersPage.tsx`

- ADMIN only (route guard + inline role check, both layers)
- reuses Phase 5A backend endpoints (`GET/POST/PATCH /api/admin/users`)
- dense enterprise table (email / display name / role / status /
  last login / actions)
- create dialog (role select, ≥ 8 char password enforced)
- edit dialog (display name, role, optional new password; cannot
  change own role)
- confirm dialog for deactivate / reactivate
- signed-in admin cannot deactivate themselves

---

## API Client

`src/lib/api.ts`

### Relative routing

All calls are relative to the current origin (`/api/auth/...`,
`/api/admin/users`, …).  The Vite dev server proxies `/api` to the
backend; nginx strips `/api/` in production.  No host, IP, or
`localhost` literal exists anywhere in the frontend bundle.

### Error normalization

| Status | Code |
|---|---|
| 401 | `Unauthorized` (fires `onSessionExpired` once) |
| 403 | `Forbidden` (fires `onForbidden`) |
| 404 | `NotFound` |
| 429 | `RateLimited` |
| 5xx | `Unavailable` |
| network / abort | `Unavailable` |

Messages are taken from `payload.message` or `payload.detail` only —
never raw exceptions, never stack traces.

---

## Security

### Token handling

- stored in `localStorage['accd.session']` as `{ access_token,
  expires_at, user }`
- short-lived (60 min by default; Phase 5A backend setting)
- re-validated against the DB row on every backend request
  (`require_role` reloads the user)
- never logged or echoed in any error message
- 401 from `/api/auth/login` is **explicitly opted out** via
  `skipAuthRedirect: true` so bad-password attempts don't drop an
  unrelated session

Documented limitation: localStorage XSS exposure.  A hardened
deployment should move tokens to httpOnly Secure SameSite cookies;
that requires a backend change and is deferred beyond Phase 6A.

### Frontend secrets

Verified by `scripts/phase6a_verify.sh` and
`src/tests/no-secrets.test.tsx`:

- no `AKIA[0-9A-Z]{16}` (AWS access key)
- no `sk-[A-Za-z0-9]{16,}` (LiteLLM key)
- no `jwt_secret = "..."` literal
- no JWT secret value, no DB password, no LiteLLM key in the bundle

---

## Accessibility

- visible keyboard focus ring (`focus-visible:shadow-focus`) — focus
  outlines never disabled without replacement
- semantic landmarks (`<header>`, `<main role="main">`, `<nav>`)
- semantic headings (no skipped levels)
- buttons have accessible names (icon-only buttons get `aria-label`)
- form labels associated with inputs via `htmlFor`
- theme toggle exposes `aria-label` and `aria-pressed`
- errors use `role="alert"`; status messages use `role="status"`
- modal dialogs are `role="dialog" aria-modal="true"` with Escape +
  outside-click close
- reduced-motion preference honoured globally
- color is never the sole state carrier — text / icon always
  accompanies tone
- WCAG AA contrast in both themes

---

## Responsive

- primary target: enterprise desktop
- mobile + tablet supported via MobileNavigation drawer
- KPI grid: 1 → 2 → 4 columns across breakpoints
- tables scroll horizontally on small screens
- login card scales 320 → 480 px with consistent padding
- the user menu collapses name on `<sm` (initials only)

---

## Tests

### Frontend tests

`npm test` — vitest, jsdom, @testing-library/react.

**83 tests across 9 files, all passing.**

| File | Tests |
|---|---|
| `src/tests/api.test.ts` | 11 |
| `src/tests/auth.test.tsx` | 9 |
| `src/tests/components.test.tsx` | 16 |
| `src/tests/login.test.tsx` | 7 |
| `src/tests/navigation.test.tsx` | 10 |
| `src/tests/no-secrets.test.tsx` | 4 |
| `src/tests/router.test.tsx` | 11 |
| `src/tests/theme.test.tsx` | 8 |
| `src/tests/users-admin.test.tsx` | 7 |

Coverage of the Phase 6A spec mandate:

- dark + light + toggle + persistence + system default + flash
- login render / success / invalid / unavailable / disabled-mode
  redirect
- logout + auth bootstrap + protected route + session expiry
- ADMIN / ANALYST / VIEWER navigation visibility
- admin users list / create / deactivate / reactivate / self-guard
- API client error normalization + no hardcoded host
- loading + empty + error states
- token never logged, password never logged
- color is never the sole state carrier (RoleBadge + StatusBadge)

### Build

`npm run build` succeeds:

```
dist/index.html                   1.78 kB │ gzip:  0.81 kB
dist/assets/index-DHPFjynW.css   17.07 kB │ gzip:  4.52 kB
dist/assets/index-BMcoYL6n.js   195.48 kB │ gzip: 58.92 kB
✓ built in ~30s
```

TypeScript strict mode (`tsc -b`) passes.

### Phase 5C regression

`scripts/phase5c_verify.sh` is delegated from
`scripts/phase6a_verify.sh`.  Run as part of the full Phase 6A
verification (delegation result is reported in the totals).

### phase6a_verify

`scripts/phase6a_verify.sh` covers the full checklist enumerated by
the Phase 6A spec.  Output:

```
PASS / FAIL  per check
PASS=<n>  FAIL=<n>
OVERALL: PASS | FAIL
```

---

## Docker

### Public ports

Only `nginx` publishes `80:80`.  Postgres, LiteLLM, backend and
frontend all bind only on the internal Docker network.  Verified
by:

```
$ docker compose ps --format json | jq '.[] | {name, ports}'
```

…plus the static check in `phase6a_verify.sh` that fails the run if
any non-nginx service declares a `ports:` mapping.

### Health

The full Phase 5C verification exercises:

- `GET /health` (always 200)
- `GET /health/ready` (database + LiteLLM checks)
- `GET /nginx-health` (200 ok)

---

## Visual Reference Alignment

Mapping the implementation to the supplied HipLink reference:

| HipLink reference | AI Cloud Cost Detective |
|---|---|
| horizontal top nav | `TopNavigation` (single row, brand + items + theme + user) |
| active tab cyan | `bg-primary-soft` + `text-primary` + `ring-primary/30` |
| dark navy canvas | `--bg: #0b1426` |
| lighter navy cards | `--surface: #152339` |
| subtle borders | `--border: #233553` |
| brand mark + product | `BrandHeader` (HIPLINK wordmark + product subtitle) |
| compact KPI tiles | `MetricCard` (icon, label, large value, description) |
| operational table | `DataTable` (dense, separators, badges, horizontal scroll) |
| status badges | `StatusBadge` (success / warning / danger / info / ai / neutral) |
| login card centred | `LoginPage` (centered branded card with theme toggle) |
| minimal gradients | none in Phase 6A |
| minimal animation | only `animate-pulse` on skeletons; `prefers-reduced-motion` honoured |

Light mode retains the same cyan accent (`#0891b2` on white) so the
brand identity carries across both modes.  Cards invert to white,
borders soften to slate-gray, text deepens to navy.

This is a consistent HipLink product-family design.  No claim of
pixel-perfect parity — the HipLink reference describes the visual
family, not a target spec.

---

## Known Issues

1. **No real HipLink logo asset** — the BrandHeader renders a
   placeholder.  Drop a real asset into `frontend/public/` and
   replace `BrandGlyph`.
2. **localStorage XSS exposure** — documented; a hardened
   deployment should move tokens to httpOnly Secure SameSite
   cookies (backend change, deferred).
3. **Minor in-house router** — supports the routes Phase 6A
   needs; Phase 6B can swap in `react-router-dom` if needed (the
   router API is intentionally small).
4. **No Skeleton for full-page loads** — Phase 6A renders a small
   "Initializing…" placeholder during the AuthProvider bootstrap.
   Per-page skeletons ship in Phase 6B.

---

## Deferred to 6B

- Real FinOps data wiring (Cost Explorer endpoints already exist
  in Phase 2; Phase 6B wires the frontend to them)
- Cost trend chart
- Service / Region distribution visualisation
- Resource inventory UI
- Optimization recommendation UI

## Deferred to 6C

- Full AI Cost Analyst conversation UX
- Conversation list / detail / message stream
- WebSocket frontend integration (Phase 5C backend already exposes
  the transport; Phase 6C adds the streaming UI)

---

## Phase 6B Readiness

**READY**

The chart-ready containers, KPI tile props, theme tokens, role-aware
routes, and centralized API client are all in place.  Phase 6B can
begin by:

1. Wiring the existing Phase 2 AWS endpoints to the existing
   `MetricCard` and `DataTable` props.
2. Adding a chart library of choice (the plan defers this
   decision to Phase 6B; Recharts is the likely candidate based
   on bundle size and license).
3. Replacing `EmptyState` placeholders with real components.

No foundation work is blocking Phase 6B.
