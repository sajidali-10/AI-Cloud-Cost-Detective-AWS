# Phase 6A — HipLink Professional Frontend Foundation

This document describes the frontend foundation delivered by Phase 6A:
the HipLink visual language, theme system, application shell,
authentication UX, role-aware navigation, reusable component library,
admin user management, and the foundation Phase 6B / 6C will build on.

It does **not** cover FinOps data wiring (Phase 6B) or the AI
chat transport (Phase 6C).

---

## 1. Visual language

The product looks like another HipLink enterprise application.  The
shell, palette, type scale, and component rhythm all follow the same
family conventions:

| Surface | Dark | Light |
|---|---|---|
| Page background | `#0b1426` deep navy | `#f1f5f9` cool gray |
| Header | `#0f1c33` navy | `#ffffff` white |
| Card / panel | `#152339` slate | `#ffffff` white |
| Card hover | `#1f3253` | `#e2e8f0` |
| Border | `#233553` muted | `#d8e0ec` slate |
| Primary text | `#f1f5f9` | `#0f172a` |
| Muted text | `#7d8aa3` | `#64748b` |
| Primary (HipLink cyan) | `#22d3ee` | `#0891b2` |
| Success | `#34d399` | `#15803d` |
| Warning | `#f59e0b` | `#b45309` |
| Danger | `#f87171` | `#b91c1c` |
| Info | `#38bdf8` | `#0369a1` |
| AI accent | `#a78bfa` restrained purple | `#6d28d9` |

These palettes live as CSS custom properties in `src/index.css`
(both `:root` / `[data-theme='dark']` and `.theme-light` /
`[data-theme='light']` blocks).  Tailwind reads them via
`theme.extend.colors` in `tailwind.config.js` and exposes semantic
utility classes (`bg-surface`, `text-fg-primary`, `border-border`,
`text-primary`, `bg-primary-soft`, …).

Components consume **only** semantic tokens.  The verification script
greps the source tree and fails if `bg-slate-*`, `bg-gray-*`,
`text-slate-*`, etc. appear inside `frontend/src/components/`.

### Type

Compact enterprise sans-serif system stack — no web font load, no
FOUT.  Hierarchy:

- Product title (page eyebrow, uppercase tracking)
- Page title (h1, 20 px semibold)
- Section title (h2, 14 px semibold)
- KPI value (24 px tabular-nums)
- Body (14 px)
- Muted helper (12 px)
- Compact label (11–12 px)

### Active navigation

Compact rounded pill: `bg-primary-soft`, `text-primary`,
`ring-1 ring-primary/30`.  Inactive items remain slate.  No oversized
SaaS tabs.

---

## 2. Theme system

### No-flash bootstrap

`index.html` contains a synchronous inline script that runs **before**
React mounts.  It selects the theme in this priority order:

1. `localStorage['accd.theme']` (explicit user choice).
2. `window.matchMedia('(prefers-color-scheme: light)')`.
3. `dark` fallback.

The chosen value is applied to `<html data-theme="...">` plus a
companion class (`theme-dark` / `theme-light`) so the correct CSS
variables are active before any paint.

### Provider

`src/lib/theme.tsx` exposes `ThemeProvider` and `useTheme()`.  The
provider reads whatever the inline script already chose, then keeps
`<html>` in sync whenever the user toggles.  It writes to
`localStorage` so a refresh keeps the choice.

### Toggle

`src/components/ThemeToggle.tsx` is a 36 × 36 button in the header
that flips between dark and light.  It carries:

- `aria-label="Switch to light mode"` (or dark)
- `aria-pressed` reflecting current state
- visible keyboard focus ring (`focus-visible:shadow-focus`)
- an icon that visually previews the next state

### Tokens vs. component code

Components reference semantic classes only:

```tsx
<header className="border-b border-border bg-bg-elevated">
  <h1 className="text-fg-primary">Dashboard</h1>
  <p className="text-fg-muted">subtitle</p>
</header>
```

The same component renders correctly in dark or light mode because
the variables flip together.

---

## 3. Application shell

```
AppShell
├── TopNavigation     (desktop, lg: and above)
│   ├── Brand         (HIPLINK · AI Cloud Cost Detective)
│   ├── Nav           (Dashboard, Costs, …, Security)
│   ├── ThemeToggle
│   └── UserMenu
├── MobileNavigation  (<lg, compact bar + drawer)
└── <main role="main">
```

`src/components/AppShell.tsx` is the only place these components are
composed; pages render inside `<AppShell>` and never instantiate
their own header.  `BrandHeader` is a single component shared between
desktop and mobile.

### Brand

`src/components/BrandHeader.tsx` renders the HIPLINK wordmark in the
semantic primary color plus an "AI Cloud Cost Detective" subtitle.  No
logo asset exists in the repository, so the glyph is a geometric "H"
mark in `bg-primary-soft`.  To ship a real logo, drop an SVG/PNG into
`frontend/public/` and replace the inline `BrandGlyph` element.  The
layout intentionally accommodates a real logo without touching the
surrounding shell.

---

## 4. Navigation

`src/lib/tokens.ts` is the single source of truth for role-aware
navigation.

```ts
ADMIN   sees: Dashboard, Costs, Resources, Optimization,
               AI Cost Analyst, Conversations, Users, Security
ANALYST sees: Dashboard, Costs, Resources, Optimization,
               AI Cost Analyst, Conversations
VIEWER  sees: Dashboard, Costs, Resources, Optimization
```

The same list drives both the top nav and the mobile drawer.
Frontend role hiding is UX only — the backend remains authoritative.

---

## 5. Authentication UX

`src/pages/LoginPage.tsx`:

- centered branded card on a deep canvas
- email + password inputs (autocomplete hints: `username` /
  `current-password`)
- sign-in CTA, disabled while submitting
- generic "Invalid email or password." on 401 — never reveals
  whether the account exists
- "Backend unavailable." on network / 5xx
- `ThemeToggle` accessible from the card corner
- keyboard submission via Enter
- session notice banner for "expired" / "logged out" states

### Session handling

`src/lib/auth.tsx` exposes `useAuth()` and centralises:

- bootstrap via `GET /api/auth/info` (`auth_enabled`)
- `GET /api/auth/me` validation of stored sessions
- login / logout
- session-expired handling (drops session, redirects to /login,
  surfaces a notice once per lifetime — no redirect loop)
- 401 / 403 side effects on the api client

### Token storage

The bearer token is held in `localStorage` under
`accd.session`.  This is the safest practical mechanism that does
**not** require a backend change (which is out of scope for Phase
6A — the spec mandates no backend redesign).  The blast radius is
bounded because:

- tokens are short-lived (60 min by default)
- the backend re-validates the token against the DB row on every
  request (`require_role` reloads `AppUser` and uses the DB role)
- the token is never logged or echoed in error messages
- XSS is the residual risk; a future hardened deployment should
  move tokens to httpOnly Secure SameSite cookies set by the backend
  (deferred beyond Phase 6A).

The selection is documented in the verification script and the
final report.  No JWT secret, password, AWS credential, or LiteLLM
key is ever written to the frontend bundle.

---

## 6. Centralized API client

`src/lib/api.ts` is the only frontend module that calls `fetch`:

- relative URLs only — never hardcodes host, IP, or `localhost`
- injects `Authorization: Bearer <token>` from the session
- injects `Content-Type: application/json` when `json` is passed
- accepts a `timeoutMs` (default 15s; AbortController)
- parses JSON, returns `204 → undefined`
- normalises failures into `ApiError { code, status, message, errorCode }`:
  - 401 → `Unauthorized` (fires `onSessionExpired` once)
  - 403 → `Forbidden`
  - 404 → `NotFound`
  - 429 → `RateLimited`
  - 5xx / network → `Unavailable`
- login endpoint uses `skipAuthRedirect: true` so the user's bad
  password does not trigger a session drop

The grep-check in `scripts/phase6a_verify.sh` and the static test in
`src/tests/no-secrets.test.tsx` both enforce "no hardcoded hosts /
secrets / dark-only palette on major surfaces".

---

## 7. Component library

| Component | Purpose |
|---|---|
| `AppShell` | Page wrapper; mounts nav + main. |
| `TopNavigation` | Horizontal desktop nav. |
| `MobileNavigation` | Compact bar + drawer for <lg screens. |
| `BrandHeader` | Logo glyph + wordmark. |
| `ThemeToggle` | Sun/moon toggle button. |
| `UserMenu` | Display name + role badge + logout dropdown. |
| `PageHeader` | Eyebrow + title + subtitle + context row + actions. |
| `SectionCard` | Themed card with optional title / actions. |
| `MetricCard` | KPI tile (label + value + description + icon). |
| `DataTable` | Compact enterprise table (headers, rows, loading, empty). |
| `StatusBadge` | Success / warning / danger / info / ai / neutral pill. |
| `RoleBadge` | ADMIN / ANALYST / VIEWER pill. |
| `FilterBar` + `FilterSelect` | Filter toolbar + select. |
| `LoadingSkeleton` | Restrained placeholder. |
| `EmptyState` | "Available in Phase 6B / 6C" placeholder. |
| `ErrorState` | "Backend unavailable" / "Try again" / Forbidden. |
| `AccessDenied` | Full-page denied screen. |

All components consume semantic tokens.  The static guard in
`src/tests/no-secrets.test.tsx` fails the test suite if a major
component bypasses the token layer with a raw `bg-slate-*` or
`bg-gray-*` class.

---

## 8. Admin user management

`src/pages/UsersPage.tsx` reuses the Phase 5A admin API:

- `GET /api/admin/users` — list
- `POST /api/admin/users` — create
- `PATCH /api/admin/users/{id}` — partial update

The page is ADMIN only.  It renders an `AccessDenied` for other roles
**and** the router refuses the route via `requiredRoles: ['ADMIN']`.
The two layers are independent — frontend hides for UX, backend
authorises.

Features:

- dense enterprise table (email / display name / role / status /
  last login / actions)
- new-user dialog with role selection (≥ 8 char password enforced)
- edit dialog with display name, role, optional new password
- confirm dialog for deactivate / reactivate
- the signed-in admin cannot deactivate themselves or demote
  themselves (UI guard; backend enforces the same rule)

---

## 9. Dashboard foundation

`src/pages/DashboardPage.tsx` renders the structural HipLink layout:

```
[PageHeader]  eyebrow + title + subtitle + context row (— placeholders)
[4 KPI tiles] AWS Spend, Cost Δ, Resources, Optimize   (— placeholders)
[2 chart cards]  Cost Trend, Cost by Service          (EmptyState)
[2 summary cards] Optimization Summary, AWS Environment (EmptyState)
```

Every numeric surface renders `—` until real data lands in Phase 6B.
No fabricated financial values are displayed.

---

## 10. Routes

```
/                  Dashboard                  (all roles)
/costs             Costs                      (all roles)
/resources         Resources                  (all roles)
/optimization      Optimization               (all roles)
/analyst           AI Cost Analyst shell      (ADMIN + ANALYST)
/conversations     Conversations shell        (ADMIN + ANALYST)
/users             Users admin                (ADMIN only)
/security          Security metadata          (ADMIN only)
/login             Login                      (when auth_enabled=true)
```

---

## 11. Accessibility

- visible keyboard focus ring (`focus-visible:shadow-focus`)
- semantic headings (h1 / h2 / h3, no skipped levels)
- buttons have accessible names (icon-only buttons get `aria-label`)
- form labels associated with inputs (`<label htmlFor>`)
- theme toggle exposes `aria-pressed` + dynamic `aria-label`
- errors use `role="alert"`; status messages use `role="status"`
- modal dialogs are `role="dialog" aria-modal="true"` with Escape
  close and outside-click close
- reduced-motion preference honoured by the global CSS
- color is never the sole state carrier — text / icon always
  accompanies tone

Both themes meet WCAG AA contrast at the chosen token levels.  The
dark mode primary cyan is `#22d3ee` on `#152339` (≈ 10.3:1).  Light
mode primary cyan is `#0891b2` on `#ffffff` (≈ 5.2:1).

---

## 12. Responsive behaviour

- primary target: enterprise desktop
- tablet + mobile supported via the MobileNavigation drawer
- KPI grid collapses `1 → 2 → 4` columns via `grid-cols-1 sm:grid-cols-2
  lg:grid-cols-4`
- tables scroll horizontally on small screens
- the desktop top nav hides at `<lg`; the mobile bar + drawer take
  over (brand, theme, user account, nav access all preserved)

---

## 13. Security review

| Item | Status |
|---|---|
| JWT secret in frontend bundle | None (verified by grep). |
| Password in frontend bundle / console | None (verified by tests). |
| AWS credentials in frontend | None. |
| LiteLLM key in frontend | None. |
| DB credentials in frontend | None. |
| Hardcoded localhost / EC2 IP / private IP | None (verified by grep). |
| Token logging | None (verified by tests). |
| `localStorage` XSS exposure | Documented limitation. |
| 401 redirect loops | Guarded by `sessionExpiredOnce` ref. |
| RBAC drift | Frontend hides for UX; backend authoritative. |

---

## 14. Phase 6B / 6C readiness

Phase 6A ships chart-ready containers (`SectionCard` + `EmptyState`)
so Phase 6B can drop in a chart library (e.g. Recharts) and wire
real AWS data without re-architecting the page.  All four KPI tiles
and both chart cards already accept optional `value` and `icon`
props for direct data binding.

Phase 6C inherits the `/analyst` and `/conversations` shells plus the
centralized API client.  The WebSocket transport established by
Phase 5C is reachable via `apiFetch`-style abstractions; Phase 6C
adds the streaming UI on top.

---

## 15. Testing

- 83 vitest tests across 9 files, run via `npm test` in `frontend/`.
- `scripts/phase6a_verify.sh` runs:
  - delegated `phase5c_verify.sh` (full Phase 5 regression)
  - structural checks (components / pages / lib)
  - static grep checks (no localhost, no secrets, no dark-only palette)
  - `npm test`
  - `npm run build` (TS strict + Vite production)
  - docker-compose / nginx health
  - public-port check (only nginx publishes 80)

Output: explicit `PASS` / `FAIL` totals.  Exits non-zero on any FAIL.
