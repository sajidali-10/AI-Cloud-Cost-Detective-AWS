// Phase 7C.2A — Cloud provider selector.
//
// First Phase 7C change that visibly changes the application
// shell: a top-bar selector that lets the operator switch between
// registered cloud providers (AWS today; Azure via Phase 7C.2A;
// Rackspace coming in 7C.3).
//
// UX target:
//   Cloud Provider
//   [ AWS ▼ ]
//
// Behaviour:
//   * Only providers with ``is_registered=true`` appear as
//     selectable.  RACKSPACE is currently NOT registered, so it
//     does NOT show up as a user-selectable option yet (per
//     spec — "do NOT show Rackspace as selectable yet").
//   * Selecting a provider updates the shared ``ProviderContext``;
//     the account/subscription selector automatically re-renders
//     the rows that belong to the new provider (see
//     ``useAccountContext`` which filters by provider on refresh).
//   * Selection is persisted to ``sessionStorage`` via the existing
//     ``ProviderProvider`` hook, not localStorage.  On logout
//     (auth token disappears) the selection resets to ``AWS``.
//   * When the provider switches, every FinOps cache key is
//     invalidated by the existing ``providerVersion`` counter —
//     AWS data can never satisfy an Azure request that flips in.

import { useEffect, useMemo, useRef, useState } from 'react'
import { useProviderContext, type ProviderKind } from '../lib/providerContext'

interface ProviderSelectorProps {
  className?: string
}

function labelForProvider(kind: ProviderKind): string {
  switch (kind) {
    case 'AWS':
      return 'AWS'
    case 'AZURE':
      return 'Azure'
    case 'RACKSPACE':
      return 'Rackspace'
    default:
      return kind
  }
}

export function ProviderSelector({ className = '' }: ProviderSelectorProps) {
  const { provider, setProvider, providers, refreshProviders } = useProviderContext()
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement | null>(null)

  // Refresh the provider capability snapshot on first mount if it
  // hasn't been fetched yet.  Mirrors the AccountSelector pattern.
  useEffect(() => {
    if (providers.length === 0) {
      void refreshProviders()
    }
  }, [providers.length, refreshProviders])

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

  // Only providers that are registered AND enabled are
  // user-selectable.  Phase 7C.2A: AWS is enabled; Azure is
  // enabled only when the runtime is configured (the adapter
  // self-registers as DISABLED otherwise — see
  // ``AzureCloudProvider._register``).
  const selectable = useMemo(
    () =>
      providers
        .filter((p) => p.is_registered && p.is_enabled)
        .map((p) => p.provider),
    [providers],
  )

  // If the persisted selection points at a provider that is no
  // longer registered (e.g. operator unset Azure runtime
  // configuration), fall back to AWS so the UI never gets
  // stuck on a dead option.
  useEffect(() => {
    if (selectable.length > 0 && !selectable.includes(provider)) {
      const fallback = selectable.includes('AWS')
        ? 'AWS'
        : (selectable[0] as ProviderKind)
      setProvider(fallback)
    }
  }, [selectable, provider, setProvider])

  function pick(next: ProviderKind) {
    setProvider(next)
    setOpen(false)
  }

  // Hide the selector entirely until the provider list has
  // loaded — otherwise the UI flashes an empty dropdown for
  // one tick on first mount.
  if (providers.length === 0) {
    return null
  }

  return (
    <div ref={rootRef} className={`relative inline-block text-left ${className}`}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="inline-flex items-center gap-2 rounded-md border border-border bg-surface px-3 py-1.5 text-sm font-medium text-fg-primary shadow-sm hover:bg-surface-hover"
        aria-haspopup="listbox"
        aria-expanded={open}
        data-testid="provider-selector-button"
      >
        <span className="text-fg-muted">Cloud Provider:</span>
        <span className="font-semibold">{labelForProvider(provider)}</span>
        <svg
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 20 20"
          fill="currentColor"
          className="h-4 w-4"
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
          className="absolute right-0 z-30 mt-1 max-h-72 w-56 overflow-auto rounded-md border border-border bg-surface py-1 text-sm shadow-lg"
          data-testid="provider-selector-menu"
        >
          {selectable.length === 0 ? (
            <li className="px-3 py-2 text-xs text-fg-muted">
              No providers registered.
            </li>
          ) : (
            selectable.map((kind) => (
              <li key={kind}>
                <button
                  type="button"
                  role="option"
                  aria-selected={provider === kind}
                  onClick={() => pick(kind)}
                  className={`flex w-full items-center justify-between px-3 py-2 text-left ${
                    provider === kind
                      ? 'bg-info-soft text-info'
                      : 'text-fg-primary hover:bg-surface-hover'
                  }`}
                  data-testid={`provider-option-${kind.toLowerCase()}`}
                >
                  <span className="font-semibold">{labelForProvider(kind)}</span>
                </button>
              </li>
            ))
          )}
        </ul>
      )}
    </div>
  )
}

export default ProviderSelector
