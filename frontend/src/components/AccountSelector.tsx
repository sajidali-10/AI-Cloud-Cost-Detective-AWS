// Phase 7B — AWS Account selector (HipLink top-bar).
// Phase 7C.2A — provider-aware labels: AWS when AWS is selected,
// Azure when Azure is selected.  Filtering by provider happens at
// the consumer side (the AccountContext still loads every row);
// the selector renders only the rows belonging to the active
// provider so a stale AWS row can never satisfy an Azure request.

import { useEffect, useMemo, useRef, useState } from 'react'
import { useAccountContext, AccountSelection } from '../lib/accountContext'
import { useProviderContext } from '../lib/providerContext'
import {
  buildAccountSelector,
  accountMatchesSelector,
  providerAccountNoun,
  providerAggregateLabel,
} from '../lib/finops/subscriptionSelection'
import type { CanonicalProvider } from '../lib/finops/subscriptionSelection'

interface AccountSelectorProps {
  className?: string
}

export function AccountSelector({ className = '' }: AccountSelectorProps) {
  const { selection, setSelection, accounts, refreshAccounts, loading, error } =
    useAccountContext()
  const { provider } = useProviderContext()
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement | null>(null)

  // Filter rows to the currently selected provider.  Belt-and-
  // braces: the per-page FinOps cache keys already include the
  // provider segment, but the selector UI must never render an
  // AWS row while Azure is selected (or vice versa).
  const visibleAccounts = useMemo(
    () => accounts.filter((a) => a.provider === provider),
    [accounts, provider],
  )

  // When the provider changes, drop any stale specific selection
  // so a stale AWS id never follows the user into an Azure page.
  //
  // The effect MUST NOT fire on the very first mount (same rule
  // AccountContext applies to its own provider-change effect):
  // a dependencies-only-fire-on-change effect still runs once on
  // mount, and on a full page load the AccountContext `useState`
  // initialiser has just rehydrated a persisted selection from
  // sessionStorage — resetting it here clobbered every individual
  // account choice back to the "All …" aggregate on every reload
  // (observed in the Phase 9D.1 browser acceptance after the
  // authenticated-startup fix: individual selection was reflected
  // immediately but lost on the next hard navigation).
  const isFirstProviderMountRef = useRef<boolean>(true)
  useEffect(() => {
    if (isFirstProviderMountRef.current) {
      isFirstProviderMountRef.current = false
      return
    }
    if (selection !== 'all') {
      setSelection('all')
    }
    // We intentionally exclude `selection` from deps so the effect
    // fires only on a real provider change, not on every user
    // click inside the selector.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider])

  useEffect(() => {
    if (accounts.length === 0 && !loading) {
      void refreshAccounts()
    }
  }, [accounts.length, loading, refreshAccounts])

  // Close on outside click.
  useEffect(() => {
    function onClick(ev: MouseEvent) {
      if (!rootRef.current) return
      if (!rootRef.current.contains(ev.target as Node)) {
        setOpen(false)
      }
    }
    if (open) {
      document.addEventListener('mousedown', onClick)
      return () => document.removeEventListener('mousedown', onClick)
    }
    return undefined
  }, [open])

  const isAzure = provider === 'AZURE'
  // Phase 9D.1 — provider-aware labels via the shared mapping
  // helpers (single source of truth).  RACKSPACE renders
  // "Rackspace Account" / "All Rackspace Cloud Accounts" — never
  // the AWS wording the pre-9D.1 build showed.
  const aggregateLabel = providerAggregateLabel(provider as CanonicalProvider)
  const rowLabel = providerAccountNoun(provider as CanonicalProvider)

  const label = useMemo(() => {
    if (selection === 'all') return aggregateLabel
    // Find the matching row by its CLOUD-side account id (the
    // canonical selector contract) — never by the local database
    // row id.  Works for ``acct:<account_id>`` (AWS / Rackspace)
    // and ``sub:<guid>`` (Azure) alike.
    const found = visibleAccounts.find((a) =>
      accountMatchesSelector(a, selection, provider as CanonicalProvider),
    )
    if (found) return `${found.display_name} — ${found.account_id}`
    // Unknown / self-corrected selection — show the identifier
    // shape without pretending it matched a registered row.
    if (isAzure && selection.startsWith('sub:')) {
      const guid = selection.slice('sub:'.length)
      return `Subscription ${guid}`
    }
    if (selection.startsWith('acct:')) {
      return `Account ${selection.slice('acct:'.length)}`
    }
    return selection
  }, [selection, visibleAccounts, aggregateLabel, isAzure, provider])

  function pick(value: AccountSelection) {
    setSelection(value)
    setOpen(false)
  }

  return (
    <div ref={rootRef} className={`relative inline-block text-left ${className}`}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="
          group inline-flex h-9 max-w-[18rem] items-center gap-2
          rounded-md border border-border bg-surface
          px-3 text-sm font-medium text-fg-primary shadow-sm
          hover:bg-surface-hover focus:outline-none
          focus-visible:shadow-focus transition-colors
        "
        aria-haspopup="listbox"
        aria-expanded={open}
        data-testid="account-selector-button"
      >
        <span className="shrink-0 text-fg-muted">{rowLabel}:</span>
        <span className="min-w-0 flex-1 truncate text-left font-semibold">{label}</span>
        <svg
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 20 20"
          fill="currentColor"
          className="h-4 w-4 shrink-0 text-fg-muted transition-transform group-aria-expanded:rotate-180"
          aria-hidden
        >
          <path
            fillRule="evenodd"
            d="M5.23 7.21a.75.75 0 011.06.02L10 11.06l3.71-3.83a.75.75 0 111.08 1.04l-4.24 4.38a.75.75 0 01-1.08 0L5.21 8.27a.75.75 0 01.02-1.06z"
            clipRule="evenodd"
          />
        </svg>
      </button>
      {open && (
        <ul
          role="listbox"
          className="absolute right-0 z-30 mt-1 max-h-80 w-80 overflow-auto rounded-md border border-border bg-surface py-1 text-sm shadow-lg"
          data-testid="account-selector-menu"
        >
          <li>
            <button
              type="button"
              role="option"
              aria-selected={selection === 'all'}
              onClick={() => pick('all')}
              className={`flex w-full items-center justify-between px-3 py-2 text-left ${
                selection === 'all'
                  ? 'bg-info-soft text-info'
                  : 'text-fg-primary hover:bg-surface-hover'
              }`}
              data-testid="account-option-all"
            >
              <span className="font-semibold">{aggregateLabel}</span>
              <span className="text-xs text-fg-muted">
                {visibleAccounts.length} registered
              </span>
            </button>
          </li>
          {visibleAccounts.length > 0 && (
            <li className="my-1 border-t border-border" />
          )}
          {visibleAccounts.map((acc) => {
            // Phase 9D.1 — every provider selection goes through
            // the shared canonical mapping: AWS and Rackspace emit
            // ``acct:<cloud account_id>`` (the registry row's
            // cloud-side id — NEVER the local database primary
            // key), Azure emits ``sub:<subscription-guid>``.
            const value = buildAccountSelector(acc, provider as CanonicalProvider)
            const testId = isAzure
              ? `azure-sub-option-${acc.id}`
              : `account-option-${acc.id}`
            return (
              <li key={acc.id}>
                <button
                  type="button"
                  role="option"
                  aria-selected={selection === value}
                  onClick={() => pick(value)}
                  className={`flex w-full items-center justify-between px-3 py-2 text-left ${
                    selection === value
                      ? 'bg-info-soft text-info'
                      : 'text-fg-primary hover:bg-surface-hover'
                  }`}
                  data-testid={testId}
                >
                  <span className="flex min-w-0 flex-col">
                    <span className="truncate font-semibold">{acc.display_name}</span>
                    <span className="truncate text-xs text-fg-muted">
                      {acc.account_id}
                      {acc.is_management ? ' · management' : ''}
                    </span>
                  </span>
                </button>
              </li>
            )
          })}
          {!loading && visibleAccounts.length === 0 && (
            <li className="px-3 py-2 text-xs text-fg-muted">
              {isAzure
                ? 'No Azure subscriptions registered yet. Ask an ADMIN to add one.'
                : provider === 'RACKSPACE'
                  ? 'No Rackspace accounts registered yet. Ask an ADMIN to add one.'
                  : 'No AWS accounts registered yet. Ask an ADMIN to add one.'}
            </li>
          )}
          {loading && (
            <li className="px-3 py-2 text-xs text-fg-muted">
              Loading accounts…
            </li>
          )}
          {error && (
            <li className="px-3 py-2 text-xs text-danger">
              {error}
            </li>
          )}
        </ul>
      )}
    </div>
  )
}

export default AccountSelector
