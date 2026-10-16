# Phase 7C.2A — Navigation / Cloud Context UI closure

**Branch:** `phase-7c-multi-cloud`
**Scope:** UI / navigation architecture only. **No backend, no Azure
cost management, no Rackspace, no Advisor.**

---

## 1. Old problem

By the end of Phase 7C.2A's authentication/subscription work, the
single-row top navigation was visibly overloaded:

```
[HipLink]  AI Cloud Cost Detective   Dashboard Costs Resources Optimization AI Cost Analyst Conversations Users Accounts Security   Provider[AWS▼] Account[All…▼]   ☼  User
```

The flat horizontal row fought three separate concerns at once:

1. **Primary route navigation** — nine direct links that always have
   to be visible.
2. **Cloud context** — `ProviderSelector` + `AccountSelector` /
   `SubscriptionSelector`.  Long display names like "AWS Production
   Account" pushed the right-side controls off the right edge at
   1024 px.
3. **Identity** — theme toggle + user menu.

The selectors also visually competed with the navigation links for
the same row of vertical space, which made it unclear whether they
were context controls or top-level pages.  Adding a second provider
(Azure) was the inflection point — at 1024 px the row overflowed and
the navigation hierarchy became a horizontal soup.

---

## 2. Chosen architecture

Two-row enterprise-dashboard header that mirrors the clean
hierarchy of the public HipLink marketing site but adapted for an
operational multi-cloud FinOps surface:

```
┌───────────────────────────────────────────────────────────────────────┐
│ Row 1 — Primary navigation (h-14 ≈ 56 px)                            │
│                                                                       │
│  [HipLink] AI Cloud Cost Detective   Dashboard  FinOps▼  AI Cost      │
│                                            Analyst  Conversations    │
│                                            Administration▼        ☼ User│
├───────────────────────────────────────────────────────────────────────┤
│ Row 2 — Cloud Context (h-11 ≈ 44 px)                                  │
│                                                                       │
│  CLOUD CONTEXT                              Provider [AWS▼]           │
│                                                Account/Subscription   │
│                                                [All AWS Accounts ▼]   │
└───────────────────────────────────────────────────────────────────────┘
```

### Why this shape

* **Row 1 carries navigation only.**  Grouping
  `Costs / Resources / Optimization` under a `FinOps ▼` dropdown
  and `Users / Accounts / Security` under an `Administration ▼`
  dropdown cuts nine top-level links down to five: Dashboard,
  FinOps▼, AI Cost Analyst, Conversations, Administration▼.  The
  selectors no longer compete with routes for the same horizontal
  real estate.
* **Row 2 carries cloud context only.**  Separating "what cloud am I
  looking at" from "where am I in the application" makes both
  decisions easier to scan.  The visual treatment is deliberately
  distinct (subtle darker background, thin top divider, compact
  spacing, "Cloud Context" eyebrow label on desktop) so the eye
  reads it as context, not as navigation.
* **Existing route / RBAC contracts are unchanged.**  No routes were
  removed, renamed, or hidden from the backend.  The Phase 6A
  `navItemsForRole` + `Role`/`visibleTo` list is still the single
  source of truth for who can see what; the row is the only thing
  that changed.  Administration items continue to be gated by ADMIN
  — the new dropdown reuses `navItemsForRole` to compute its items
  so a non-admin never gains visibility they did not have before.
* **Selectors are not styled as nav tabs.**  They retain the existing
  `surface` / `border-border` look so the eye groups them with the
  other form-style controls in the app (page-header context items,
  filter selects) and not with the route links in Row 1.

---

## 3. Why grouped navigation was selected

Three alternatives were considered and rejected:

| Option | Why rejected |
| --- | --- |
| **Hide FinOps / Admin items behind a single "More ▼" overflow** | At 1920–1024 px there is always room for the grouped items — overflow would hide them unnecessarily at the widths the customer uses.  Per spec: "Do not introduce More unnecessarily at large widths." |
| **Move items into a persistent left sidebar** | The HipLink reference and Phase 6A spec deliberately use a header + drawer pattern; introducing a sidebar would break the existing visual language, the mobile drawer pattern, and the existing accessibility tree.  Phase 6A already documented why this app chose a header over a sidebar. |
| **Inline accordion rows inside the navigation** | Tabs / accordions inside the header would inflate vertical height on every page.  Dropdowns keep the header compact while still surfacing all items on a single click. |

Grouped dropdowns are the only option that:

* keeps the row ≤ 60 px tall at all desktop widths;
* lets the user reach every route in a single click without
  re-arranging the layout;
* reuses the existing `navItemsForRole` RBAC source of truth
  unchanged;
* remains consistent with the HipLink-inspired "clean hierarchy,
  restrained dropdowns" visual language.

---

## 4. Responsive strategy

### Desktop (≥ 1024 px)

* Row 1: brand + five top-level controls (`Dashboard`, `FinOps ▼`,
  `AI Cost Analyst`, `Conversations`, `Administration ▼`) + theme +
  user, all on one line.
* Row 2: `Cloud Context` eyebrow label on the left, selector cluster
  right-aligned.
* `whitespace-nowrap` on the brand link + `shrink-0` keeps the
  product name on a single line and the logo from collapsing.
* Selectors keep `max-w-[14rem]` / `max-w-[18rem]` triggers with
  full-width dropdown panels (panel can be wider than trigger, as
  per spec).
* No `overflow-hidden` on the primary nav — items never clip at
  desktop widths.

### Narrower desktop (≈ 1024 px)

* At 1024 px the brand + 5 nav items + theme + user fits because
  the FinOps / Administration labels are short.  Verified manually.
* Selectors in Row 2 use `flex-wrap` so they drop to a second line
  if a long display name (`AWS Production Account`) ever pushes the
  row.  They do not collide with navigation links.
* No `More ▼` overflow is introduced — there is no need for it at
  the supported widths (1920 / 1600 / 1440 / 1280 / 1024).

### Mobile / tablet (< 1024 px)

* Row 1: hamburger + brand + theme + user.  Hamburger opens a
  slide-down drawer containing the grouped nav items.
* Row 2: identical Cloud Context bar layout to desktop; on phones
  the selectors stack vertically when needed (`flex-wrap`).
* Drawer layout: Dashboard + AI Cost Analyst + Conversations render
  as direct links; FinOps and Administration render as **non-
  interactive** section headers followed by their grouped items.
  This keeps the drawer's hierarchy legible without nesting menus.

---

## 5. Cloud Context bar

`frontend/src/components/CloudContextBar.tsx` (new):

* `h-11` (44 px) — meets the 42–46 px spec target.
* `bg-bg` + thin `border-t border-border` for visual separation.
* "Cloud Context" eyebrow label hidden under `md:` to keep the bar
  from feeling busy on phones.
* Right-aligned selector cluster (`ml-auto flex flex-wrap items-
  center justify-end gap-2`).
* Default slot mounts `ProviderSelector` and `AccountSelector`; the
  bar accepts `children` for future expansion (Region, Month,
  etc.).
* Long account/subscription names truncate with `truncate` inside
  the existing selectors; the dropdown panel can be wider than the
  trigger.
* Selectors never get the row's primary-nav styling, so they do not
  look like top-level navigation tabs.

Future Region / Month controls can be slotted into the same right-
aligned cluster without changing Row 1.  This task does **not** add
them — they are intentionally absent because no backend surface
exists for them yet, and the spec forbids placeholder controls.

---

## 6. Files changed

### New files

| Path | Purpose |
| --- | --- |
| `frontend/src/components/NavDropdown.tsx` | Reusable ARIA-compliant menu primitive used for FinOps and Administration. |
| `frontend/src/components/CloudContextBar.tsx` | Dedicated second-row surface for `ProviderSelector` + `AccountSelector`. |
| `frontend/src/tests/phase7c2a-navigation.test.tsx` | New regression coverage for the dropdown primitive, grouped tokens, both navigations, and provider state. |

### Modified files

| Path | Change |
| --- | --- |
| `frontend/src/components/TopNavigation.tsx` | Two-row header.  Direct links for `Dashboard / AI Cost Analyst / Conversations`, `NavDropdown` for `FinOps` and `Administration`, `CloudContextBar` mounted as Row 2.  Selectors no longer live in Row 1. |
| `frontend/src/components/MobileNavigation.tsx` | Three-band mobile header: brand row, Cloud Context bar, drawer.  Drawer groups FinOps / Administration under non-interactive section headers. |
| `frontend/src/components/BrandHeader.tsx` | Added `shrink-0` + `whitespace-nowrap` + `hidden sm:inline` so the product name does not wrap awkwardly and the brand block stays compact on narrow widths. |
| `frontend/src/lib/tokens.ts` | Added `FINOPS_GROUP`, `ADMIN_GROUP`, `PRIMARY_DIRECT_PATHS`, `isPathInGroup`, `navGroupsForRole`, `navGroupItemsForRole`.  These are the single source of truth shared by desktop and mobile. |
| `frontend/src/tests/phase7b-runtime-closure.test.tsx` | Updated Phase 7B "AccountSelector mounting" assertions to verify the transitive import chain (TopNavigation → CloudContextBar → AccountSelector), since the selector now flows through the bar.  Behavioural assertions (label, persistence) are unchanged. |

No backend file was modified.

---

## 7. Accessibility / interaction contract

`NavDropdown`:

* `aria-haspopup="menu"`, `aria-expanded` reflects open state.
* `aria-current="page"` on the trigger when any item is active.
* Menu rendered as `<ul role="menu">` with `<a role="menuitem">`
  children.
* Keyboard: ArrowDown / ArrowUp / Enter / Space on the trigger
  opens and moves focus; ArrowUp / ArrowDown cycle, Home / End jump
  to first / last, Tab closes (no focus trap), Escape closes and
  returns focus to the trigger.
* Click outside closes (`mousedown` listener).
* Route change closes (pathname effect).
* Visual chevron rotates when open.
* Trigger is a real `<button>` — keyboard activation is native.

`CloudContextBar`:

* `<div role="region">` would be redundant; the existing
  ProviderSelector / AccountSelector already expose
  `aria-haspopup="listbox"` and the existing screen-reader
  semantics.
* Selectors use the same `surface` palette as other form controls
  (no `bg-primary-soft`) so they read as context, not as
  navigation.

---

## 8. Tests

### New

`frontend/src/tests/phase7c2a-navigation.test.tsx` — **37 tests**:

* Token grouping (`FINOPS_GROUP`, `ADMIN_GROUP`, `navGroupsForRole`,
  `isPathInGroup`, `isItemActive`, no-routes-lost invariant).
* `NavDropdown` primitive (ARIA, keyboard, outside-click, Escape,
  navigation, active item, active trigger).
* `CloudContextBar` (label, selector slot).
* `TopNavigation` (renders both dropdowns, direct links, ADMIN
  visibility, active state for `/costs`, `/users`, `/analyst`).
* `MobileNavigation` (grouped sections, FinOps items, Administration
  items, ADMIN visibility, Escape closes drawer).
* Provider state (default AWS, persistence, account-context
  rehydration).
* Layout invariants (TopNavigation no longer embeds selectors
  directly, references `CloudContextBar`, `BrandHeader` retains
  `shrink-0` + `whitespace-nowrap`).

### Updated

`frontend/src/tests/phase7b-runtime-closure.test.tsx` — 9 tests
still passing; the only edit is updating the selector-mount
assertion to follow the new transitive import chain.

### Existing regression coverage retained

* `navItemsForRole` role visibility (Phase 6A)
* AccountSelector label rendering (Phase 7B)
* sessionStorage persistence (Phase 7B)
* ProviderContext default + persistence (Phase 7C.1)
* Cache key isolation across providers (Phase 7C.1)

---

## 9. Runtime validation

### Frontend

* `npx tsc -p tsconfig.json --noEmit` — passes.
* `npm run build` — passes. 104 modules transformed, 30 s; output:
  * `dist/index.html` — 1.78 kB
  * `dist/assets/index-*.css` — 31.49 kB (gzip 6.62 kB)
  * `dist/assets/index-*.js` — 334.01 kB (gzip 92.15 kB)
* `vitest run` (split into 8 batches to keep individual runs fast):
  * **417 tests across 42 files — all passing.**
  * Includes the new 37-test Phase 7C.2A file and the updated
    Phase 7B runtime closure file.

### Token-level assertions pinned

* `FINOPS_GROUP.paths` = `['/costs', '/resources', '/optimization']`
* `ADMIN_GROUP.paths` = `['/users', '/accounts', '/security']`,
  `visibleTo = ['ADMIN']`
* Every `NAV_ITEMS` path is still reachable via either a direct
  link or one of the two dropdowns.

### Backend / runtime

* No backend file changed.  Existing endpoints remain operational:
  * `/api/cloud/accounts/selectable/AWS`
  * `/api/cloud/accounts/selectable/AZURE`
* Docker build + `docker compose up -d frontend` are deferred to the
  standard Phase 7C.2A regression runner (`scripts/verify_phase.sh`
  or equivalent) used by the live-federation closure.  This task is
  frontend-only and the previous Phase 7C.2A closure already
  validated Docker 5/5.

---

## 10. What is intentionally NOT in this closure

Per the explicit task scope:

* No Azure Cost Management endpoints.
* No Azure optimization / Advisor / Rackspace.
* No new backend APIs.
* No new provider surface (Rackspace still does not appear in the
  selector, matching Phase 7C.2A's contract).
* No Region / Month controls in the Cloud Context bar — the layout
  is forward-compatible but no fake placeholders are introduced.
* No merging to `main`.
* No Phase 7C completion tag.
