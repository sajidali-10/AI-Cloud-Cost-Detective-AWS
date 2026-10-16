# Phase 7C.2A — Runtime / UI Closure

> Scope: fix the two remaining issues observed in the live browser
> before Phase 7C.2B begins. **No new features** (no Azure Cost
> Management, Resource Graph, Advisor, Rackspace, client-secret
> auth, or certificate auth). **No merge to main**, no
> Phase-7C-complete tag. Branch `phase-7c-multi-cloud`.

## 1. Issue 1 — Azure selector shows "0 registered"

### Root cause

The frontend's `AccountContext.refreshAccounts` was hard-coded
to fetch `/api/aws/cloud-accounts/selectable` — the Phase 7B
AWS-only route.  When the operator selected Azure in the
`ProviderSelector`, the context still loaded the AWS rows; the
selector then filtered them by `provider === 'AZURE'` and got an
empty list.

The Phase 7C.1 provider-aware route
`/api/cloud/accounts/selectable/{provider}` already existed in
the backend (verified in `backend/app/api/cloud_providers.py`)
but the frontend never called it.

### Fix

`frontend/src/lib/accountContext.tsx` is now provider-aware:

* `AccountProvider` reads the active provider from
  `useProviderContext()`.
* `refreshAccounts` routes to the provider-aware selectable
  endpoint:
  - `provider === 'AWS'` → legacy
    `/api/aws/cloud-accounts/selectable` (Phase 7B surface,
    no behaviour change);
  - any other registered provider →
    `/api/cloud/accounts/selectable/{provider}`.
* A defensive `row.provider === provider` filter runs after the
  fetch so a backend bug or stale cache cannot leak a row
  across provider boundaries.
* A `useEffect` keyed on `[provider, providerVersion,
  refreshAccounts]` rehydrates the rows on first mount and
  refetches them on every provider switch.  A
  `useRef<boolean>` flag prevents the effect from clobbering
  the sessionStorage-rehydrated selection on the very first
  mount — the `selection survives remount` test still holds.

The 3 `phase7b-runtime-closure.test.tsx` tests that wrapped
`<AccountProvider>` directly without `<ProviderProvider>`
were updated to wrap both — the same fix applied to the
"AccountSelector mounting" test in the previous session.  No
assertion was weakened.

### Runtime validation

```
GET /api/cloud/accounts/selectable/AZURE
  → returns 200 with {"items": [{"id":5,"display_name":"MCPP Subscription",
     "provider":"AZURE",...},
     {"id":6,"display_name":"Microsoft Azure Sponsorship",
     "provider":"AZURE",...}],
     "count":2}
```

Both Azure subscriptions now appear under the Azure selector
with the correct count.  AWS selector behaviour is unchanged.

## 2. Issue 2 — Top navigation alignment

### Root cause

The previous header packed `ProviderSelector` + `AccountSelector`
+ `ThemeToggle` + `UserMenu` with mismatched heights, widths,
and no divider.  Long display names wrapped awkwardly.

### Fix

* **Selector buttons** (`AccountSelector`, `ProviderSelector`) are
  now fixed `h-9` with `max-w-[14rem]` / `max-w-[18rem]`,
  truncate long names with `min-w-0 flex-1 truncate`, and rotate
  their chevron icon when open.
* **TopNavigation** wraps the right cluster in a single flex
  group with a `flex-wrap` row, a subtle vertical divider
  (`h-6 w-px bg-border`) between the cloud selectors and the
  user-facing controls, and `gap-x-3 gap-y-2` so a wrap into a
  second row stays balanced.
* **Selectors + theme toggle** are hidden on small viewports
  (`hidden lg:flex` / `hidden md:inline-flex`) so the brand and
  user menu remain the only horizontal content there.
* **MobileNavigation** now also mounts `ProviderSelector` so
  the provider switch is reachable on small viewports too.

The dropdown menus got a small upgrade too (`max-h-80 w-80` /
`w-64`) so longer display names fit cleanly.

## 3. Files changed

| File | Change |
| --- | --- |
| `frontend/src/lib/accountContext.tsx` | Provider-aware refresh; defensive cross-provider filter; first-mount ref so the sessionStorage-rehydrated selection survives. |
| `frontend/src/components/AccountSelector.tsx` | Fixed `h-9` button, `max-w-[18rem]` truncation, larger menu, `min-w-0` inner span, rotating chevron. |
| `frontend/src/components/ProviderSelector.tsx` | Fixed `h-9` button, `max-w-[14rem]` truncation, rotating chevron, larger menu. |
| `frontend/src/components/TopNavigation.tsx` | Flex-wrap row, vertical divider, `gap-x-3 gap-y-2`, refined spacing, grouped selector cluster. |
| `frontend/src/components/MobileNavigation.tsx` | Mount `ProviderSelector` in the mobile header so provider switching is reachable on small viewports. |
| `frontend/src/tests/phase7b-runtime-closure.test.tsx` | Wrap the 3 sessionStorage-persistence tests with `<ProviderProvider>` (no assertion weakened). |
| `docs/phase7c2a-runtime-ui-closure.md` | This report. |

## 4. Regression status (all green)

| Gate | Result |
| --- | --- |
| Frontend `vitest run` (43 files, 396 tests) | **396 passed** in 123.41s |
| Frontend `tsc --noEmit` | exit 0 |
| Frontend `vite build` | built in 29.49s (327.94 kB JS, 31.21 kB CSS) |
| Backend Phase 7C.1 + 7C.2A provider-registry tests | **15 passed** in 3.96s |
| Live Azure `/api/cloud/accounts/selectable/AZURE` | returns 200 with 2 rows (MCPP, Sponsorship) |
| Live Azure `validate_account` (prior closure) | CONNECTED / CONNECTED |
| Frontend image rebuild + container restart | `cost-detective-frontend:phase0` rebuilt and serving 200 |
| Docker 5/5 healthy | yes |

## 5. What this closure does NOT ship

Per the spec: no new features.  No Azure Cost Management,
Resource Graph, Advisor, Rackspace, client-secret auth, or
certificate auth.  No merge to `main`.  No Phase 7C-complete
tag.  AWS behaviour unchanged; Phase 7B and Phase 7C.2A
working behaviour preserved.

## 6. Operator-facing change-set

* The browser URL bar, top-nav layout, and selector dropdowns
  now feel balanced on desktop browsers.
* Selecting Azure shows:
  - **All Azure Subscriptions** (2 registered)
  - **MCPP Subscription**
  - **Microsoft Azure Sponsorship**
* Switching back to AWS preserves the previous AWS behaviour.
* Selection state is per-provider; no leakage between AWS and
  Azure rows.
