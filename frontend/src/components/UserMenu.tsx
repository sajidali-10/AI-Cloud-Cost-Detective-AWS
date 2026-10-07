// Phase 6A — UserMenu.
//
// Compact pattern from the HipLink reference: display name + role
// badge, single click opens a small dropdown with logout.  No
// token, no email address, no claims.
//
// Auth-disabled state: the user menu is hidden because no user
// exists (anonymous synthetic identity is intentionally not
// surfaced as a "real" account).

import { useEffect, useRef, useState } from 'react'
import { useAuth } from '../lib/auth'
import { useNavigate } from '../lib/router'
import { RoleBadge } from './RoleBadge'

export function UserMenu() {
  const { user, logout, authEnabled, role } = useAuth()
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const esc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    document.addEventListener('keydown', esc)
    return () => {
      document.removeEventListener('mousedown', handler)
      document.removeEventListener('keydown', esc)
    }
  }, [open])

  // Auth-disabled mode: show a neutral "dev mode" pill so the
  // user knows auth is off without inventing a fake account.
  if (!authEnabled) {
    return (
      <span
        className="
          inline-flex items-center gap-2 rounded-md border border-border
          bg-surface px-3 py-1.5 text-xs text-fg-secondary
        "
        title="AUTH_ENABLED=false — development compatibility mode"
      >
        <span aria-hidden className="h-2 w-2 rounded-full bg-warning" />
        Dev mode (auth disabled)
      </span>
    )
  }

  if (!user) return null

  const initials = (user.display_name || user.email || '?')
    .split(/\s+/)
    .map((part) => part[0])
    .join('')
    .slice(0, 2)
    .toUpperCase()

  const handleLogout = async () => {
    setOpen(false)
    await logout()
    navigate('/login', { replace: true })
  }

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Account menu for ${user.display_name}`}
        onClick={() => setOpen((v) => !v)}
        className="
          inline-flex items-center gap-2 rounded-md border border-border
          bg-surface px-2 py-1 text-sm text-fg-primary
          hover:bg-surface-hover focus:outline-none focus-visible:shadow-focus
        "
      >
        <span
          aria-hidden
          className="
            inline-flex h-7 w-7 items-center justify-center rounded-full
            bg-primary-soft text-xs font-semibold text-primary
          "
        >
          {initials}
        </span>
        <span className="hidden sm:inline">{user.display_name}</span>
        {role && <RoleBadge role={role} compact />}
        <svg
          aria-hidden
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
        >
          <path d="m6 9 6 6 6-6" />
        </svg>
      </button>
      {open && (
        <div
          role="menu"
          aria-label="Account"
          className="
            absolute right-0 z-30 mt-2 w-60 overflow-hidden rounded-md
            border border-border bg-surface shadow-card-md
          "
        >
          <div className="border-b border-border px-3 py-2">
            <p className="truncate text-sm font-medium text-fg-primary">
              {user.display_name}
            </p>
            <p className="truncate text-xs text-fg-muted">{user.email}</p>
          </div>
          <button
            type="button"
            role="menuitem"
            onClick={handleLogout}
            className="
              w-full px-3 py-2 text-left text-sm text-fg-primary
              hover:bg-surface-hover focus:bg-surface-hover
              focus:outline-none focus-visible:shadow-focus
            "
          >
            Sign out
          </button>
        </div>
      )}
    </div>
  )
}
