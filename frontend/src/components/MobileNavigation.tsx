// Phase 7C.2A — Mobile navigation.
//
// Three-band mobile header that mirrors the desktop layout:
//
//   Row 1 (h-14): [☰]  BrandHeader                              Theme  User
//   Row 2 (h-11): Cloud Context                       Provider [AWS▼]  Account [All…▼]
//   Drawer (when open): grouped nav items.
//
// Drawer groups:
//   * Dashboard                — direct link
//   * FinOps                   — group header
//       - Costs
//       - Resources
//       - Optimization
//   * AI Cost Analyst          — direct link (ADMIN/ANALYST only)
//   * Conversations            — direct link (ADMIN/ANALYST only)
//   * Administration           — group header (ADMIN only)
//       - Users
//       - Accounts
//       - Security
//
// Group headers in the drawer are non-interactive labels (matching
// the HipLink-inspired "section + links" pattern) so a touch user
// always knows which surface a link belongs to.  No horizontal
// overflow: selectors wrap to the next line when the viewport is
// too narrow (e.g. <480 px) thanks to the shared CloudContextBar
// layout primitives.

import { useEffect, useMemo, useState } from 'react'
import { Link, useLocation } from '../lib/router'
import { useAuth } from '../lib/auth'
import {
  ADMIN_GROUP,
  FINOPS_GROUP,
  PRIMARY_DIRECT_PATHS,
  isPathInGroup,
  navGroupItemsForRole,
  navItemsForRole,
} from '../lib/tokens'
import { isItemActive } from './NavDropdown'
import { BrandHeader } from './BrandHeader'
import { ThemeToggle } from './ThemeToggle'
import { UserMenu } from './UserMenu'
import { CloudContextBar } from './CloudContextBar'

export function MobileNavigation() {
  const { role } = useAuth()
  const { pathname } = useLocation()
  const [open, setOpen] = useState(false)

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

  const directItems = useMemo(() => {
    const items = navItemsForRole(role)
    const direct = new Set(PRIMARY_DIRECT_PATHS)
    return items.filter((i) => direct.has(i.path))
  }, [role])

  const finopsItems = useMemo(
    () => navGroupItemsForRole(FINOPS_GROUP, role),
    [role],
  )
  const adminItems = useMemo(
    () => navGroupItemsForRole(ADMIN_GROUP, role),
    [role],
  )

  return (
    <header
      className="sticky top-0 z-20 border-b border-border bg-bg-elevated lg:hidden"
      data-testid="mobile-navigation"
    >
      {/* Row 1 — brand + identity controls. */}
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

      {/* Row 2 — cloud context bar (selectors stay accessible). */}
      <CloudContextBar testId="mobile-cloud-context-bar" />

      {open && (
        <nav
          id="mobile-nav-drawer"
          data-testid="mobile-nav-drawer"
          aria-label="Primary mobile"
          className="border-t border-border bg-bg-elevated"
        >
          <ul className="mx-auto flex max-w-screen-2xl flex-col gap-0.5 px-2 py-2">
            {directItems.map((item) => {
              const active = isItemActive(pathname, item.path)
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
            {finopsItems.length > 0 && (
              <MobileGroup
                label={FINOPS_GROUP.label}
                active={isPathInGroup(pathname, FINOPS_GROUP)}
                items={finopsItems.map((i) => ({ path: i.path, label: i.label }))}
                testIdPrefix="mobile-finops"
              />
            )}
            {adminItems.length > 0 && (
              <MobileGroup
                label={ADMIN_GROUP.label}
                active={isPathInGroup(pathname, ADMIN_GROUP)}
                items={adminItems.map((i) => ({ path: i.path, label: i.label }))}
                testIdPrefix="mobile-admin"
              />
            )}
          </ul>
        </nav>
      )}
    </header>
  )
}

interface MobileGroupProps {
  label: string
  active: boolean
  items: Array<{ path: string; label: string }>
  testIdPrefix: string
}

function MobileGroup({ label, active, items, testIdPrefix }: MobileGroupProps) {
  const { pathname } = useLocation()
  return (
    <li data-testid={`${testIdPrefix}-group`} className="mt-2 first:mt-0">
      <div
        className={[
          'px-3 pt-2 pb-1 text-[11px] font-semibold uppercase tracking-wider',
          active ? 'text-primary' : 'text-fg-muted',
        ].join(' ')}
        data-testid={`${testIdPrefix}-header`}
      >
        {label}
      </div>
      <ul className="flex flex-col gap-0.5">
        {items.map((item) => {
          const itemActive = isItemActive(pathname, item.path)
          return (
            <li key={item.path}>
              <Link
                to={item.path}
                aria-current={itemActive ? 'page' : undefined}
                className={[
                  'block rounded-md px-3 py-2 text-sm transition-colors',
                  itemActive
                    ? 'bg-primary-soft text-primary ring-1 ring-primary/30'
                    : 'text-fg-secondary hover:bg-surface-hover hover:text-fg-primary',
                ].join(' ')}
                data-testid={`${testIdPrefix}-item-${item.path.replace(/^\//, '')}`}
              >
                {item.label}
              </Link>
            </li>
          )
        })}
      </ul>
    </li>
  )
}
