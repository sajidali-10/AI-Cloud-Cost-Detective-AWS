// Phase 7C.2A — Top navigation.
//
// Two-row architecture that replaces the legacy single-row header:
//
//   Row 1 (primary navigation, h-14):
//     [Brand] | Dashboard  FinOps▼  AI Cost Analyst  Conversations  Administration▼  |  Theme  User
//
//   Row 2 (cloud context bar, h-11):
//     Cloud Context:                              Provider [AWS▼]  Account/Subscription [All…▼]
//
// Why this shape:
//   * Grouping Costs/Resources/Optimization under "FinOps" and
//     Users/Accounts/Security under "Administration" cuts the
//     horizontal pressure on Row 1 — fewer triggers, cleaner
//     hierarchy, less risk of selector overlap at narrow widths.
//   * Moving the cloud selectors to a dedicated Row 2 lets them be
//     visually framed as "context" rather than navigation — they
//     stay legible at 1024 px and never collide with route links.
//   * Direct links (Dashboard, AI Cost Analyst, Conversations)
//     keep their own active state because they don't naturally
//     belong to either group.
//   * The existing NavItem role-based visibility list in
//     `lib/tokens.ts` is the single source of truth — Administration
//     items are still gated by ADMIN, no authorization decisions
//     are duplicated here.

import { useMemo } from 'react'
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
import { isItemActive, NavDropdown } from './NavDropdown'
import { BrandHeader } from './BrandHeader'
import { ThemeToggle } from './ThemeToggle'
import { UserMenu } from './UserMenu'
import { CloudContextBar } from './CloudContextBar'

export function TopNavigation() {
  const { role } = useAuth()
  const { pathname } = useLocation()

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

  // FinOps group is visible whenever the operator has access to at
  // least one of its routes; the role gate is already enforced
  // inside `navGroupItemsForRole`, so an empty list here means
  // "nothing to show".
  const showFinops = finopsItems.length > 0
  const showAdmin = adminItems.length > 0

  return (
    <header
      className="sticky top-0 z-20 border-b border-border bg-bg-elevated"
      data-testid="top-navigation"
    >
      {/* Row 1 — primary application navigation. */}
      <div
        className="
          mx-auto flex h-14 max-w-screen-2xl items-center
          gap-x-3 gap-y-2 px-3 sm:px-5
        "
      >
        <BrandHeader />
        <nav
          aria-label="Primary"
          className="
            ml-1 hidden min-w-0 flex-1 items-center gap-1
            whitespace-nowrap lg:flex
          "
          data-testid="primary-nav"
        >
          {directItems.map((item) => {
            const active = isItemActive(pathname, item.path)
            return (
              <Link
                key={item.path}
                to={item.path}
                aria-current={active ? 'page' : undefined}
                className={[
                  'whitespace-nowrap rounded-md px-3 py-1.5 text-sm transition-colors',
                  active
                    ? 'bg-primary-soft text-primary ring-1 ring-primary/30'
                    : 'text-fg-secondary hover:bg-surface-hover hover:text-fg-primary',
                ].join(' ')}
                data-testid={`nav-link-${item.path.replace(/^\//, '') || 'root'}`}
              >
                {item.label}
              </Link>
            )
          })}
          {showFinops && (
            <NavDropdown
              label={FINOPS_GROUP.label}
              active={isPathInGroup(pathname, FINOPS_GROUP)}
              items={finopsItems.map((i) => ({
                path: i.path,
                label: i.label,
              }))}
              triggerTestId="nav-dropdown-finops"
              menuTestId="nav-dropdown-finops-menu"
            />
          )}
          {showAdmin && (
            <NavDropdown
              label={ADMIN_GROUP.label}
              active={isPathInGroup(pathname, ADMIN_GROUP)}
              items={adminItems.map((i) => ({
                path: i.path,
                label: i.label,
              }))}
              triggerTestId="nav-dropdown-admin"
              menuTestId="nav-dropdown-admin-menu"
            />
          )}
        </nav>
        <div
          className="
            ml-auto flex shrink-0 items-center gap-2
          "
        >
          <span className="hidden md:inline-flex">
            <ThemeToggle />
          </span>
          <UserMenu />
        </div>
      </div>

      {/* Row 2 — cloud context bar (selectors moved out of Row 1). */}
      <CloudContextBar />
    </header>
  )
}
