// Phase 6A — MobileNavigation.
//
// The desktop top nav collapses on small screens.  We render a
// compact header bar (brand + theme + user) plus a hamburger that
// opens a slide-down drawer containing the full nav list.  No
// pinned sidebar — matches the HipLink reference's "header + drawer"
// pattern.

import { useEffect, useState } from 'react'
import { Link, useLocation } from '../lib/router'
import { navItemsForRole } from '../lib/tokens'
import { useAuth } from '../lib/auth'
import { BrandHeader } from './BrandHeader'
import { ThemeToggle } from './ThemeToggle'
import { UserMenu } from './UserMenu'

export function MobileNavigation() {
  const { role } = useAuth()
  const { pathname } = useLocation()
  const [open, setOpen] = useState(false)
  const items = navItemsForRole(role)

  // Close drawer on route change.
  useEffect(() => {
    setOpen(false)
  }, [pathname])

  // Close on Escape.
  useEffect(() => {
    if (!open) return
    const esc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('keydown', esc)
    return () => document.removeEventListener('keydown', esc)
  }, [open])

  return (
    <>
      <header
        className="sticky top-0 z-20 border-b border-border bg-bg-elevated lg:hidden"
        data-testid="mobile-navigation"
      >
        <div className="flex h-14 items-center gap-3 px-3">
          <button
            type="button"
            aria-label={open ? 'Close menu' : 'Open menu'}
            aria-expanded={open}
            aria-controls="mobile-nav-drawer"
            onClick={() => setOpen((v) => !v)}
            className="
              inline-flex h-9 w-9 items-center justify-center rounded-md
              border border-border bg-surface text-fg-secondary
              hover:bg-surface-hover hover:text-fg-primary
              focus:outline-none focus-visible:shadow-focus
            "
          >
            <svg
              aria-hidden
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              {open ? (
                <>
                  <path d="M18 6 6 18" />
                  <path d="m6 6 12 12" />
                </>
              ) : (
                <>
                  <path d="M4 6h16" />
                  <path d="M4 12h16" />
                  <path d="M4 18h16" />
                </>
              )}
            </svg>
          </button>
          <BrandHeader compact />
          <div className="ml-auto flex items-center gap-2">
            <ThemeToggle />
            <UserMenu />
          </div>
        </div>
        {open && (
          <nav
            id="mobile-nav-drawer"
            aria-label="Primary mobile"
            className="border-t border-border bg-bg-elevated"
          >
            <ul className="mx-auto flex max-w-screen-2xl flex-col px-2 py-2">
              {items.map((item) => {
                const active =
                  item.path === '/'
                    ? pathname === '/'
                    : pathname === item.path || pathname.startsWith(item.path + '/')
                return (
                  <li key={item.path}>
                    <Link
                      to={item.path}
                      aria-current={active ? 'page' : undefined}
                      className={[
                        'block rounded-md px-3 py-2 text-sm transition-colors',
                        active
                          ? 'bg-primary-soft text-primary ring-1 ring-primary/30'
                          : 'text-fg-secondary hover:bg-surface-hover hover:text-fg-primary',
                      ].join(' ')}
                    >
                      {item.label}
                    </Link>
                  </li>
                )
              })}
            </ul>
          </nav>
        )}
      </header>
    </>
  )
}
