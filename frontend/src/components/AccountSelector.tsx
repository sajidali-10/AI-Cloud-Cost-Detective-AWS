// Phase 7B — AWS Account selector (HipLink top-bar).

import { useEffect, useMemo, useRef, useState } from 'react'
import { useAccountContext, AccountSelection } from '../lib/accountContext'

interface AccountSelectorProps {
  className?: string
}

export function AccountSelector({ className = '' }: AccountSelectorProps) {
  const { selection, setSelection, accounts, refreshAccounts, loading, error } =
    useAccountContext()
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement | null>(null)

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

  const label = useMemo(() => {
    if (selection === 'all') return 'All AWS Accounts'
    const id = selection.replace(/^acct:/, '')
    const found = accounts.find((a) => String(a.id) === id)
    if (found) return `${found.display_name} — ${found.account_id}`
    return `Account ${id}`
  }, [selection, accounts])

  function pick(value: AccountSelection) {
    setSelection(value)
    setOpen(false)
  }

  return (
    <div ref={rootRef} className={`relative inline-block text-left ${className}`}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="inline-flex items-center gap-2 rounded-md border border-border bg-surface px-3 py-1.5 text-sm font-medium text-fg-primary shadow-sm hover:bg-surface-hover"
        aria-haspopup="listbox"
        aria-expanded={open}
        data-testid="account-selector-button"
      >
        <span className="text-fg-muted">AWS Account:</span>
        <span className="font-semibold">{label}</span>
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
          className="absolute right-0 z-30 mt-1 max-h-72 w-72 overflow-auto rounded-md border border-border bg-surface py-1 text-sm shadow-lg"
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
              <span className="font-semibold">All AWS Accounts</span>
              <span className="text-xs text-fg-muted">
                {accounts.length} registered
              </span>
            </button>
          </li>
          {accounts.length > 0 && (
            <li className="my-1 border-t border-border" />
          )}
          {accounts.map((acc) => {
            const value = `acct:${acc.id}` as AccountSelection
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
                  data-testid={`account-option-${acc.id}`}
                >
                  <span className="flex flex-col">
                    <span className="font-semibold">{acc.display_name}</span>
                    <span className="text-xs text-fg-muted">
                      {acc.account_id}
                      {acc.is_management ? ' · management' : ''}
                    </span>
                  </span>
                </button>
              </li>
            )
          })}
          {!loading && accounts.length === 0 && (
            <li className="px-3 py-2 text-xs text-fg-muted">
              No accounts registered yet. Ask an ADMIN to add one.
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
