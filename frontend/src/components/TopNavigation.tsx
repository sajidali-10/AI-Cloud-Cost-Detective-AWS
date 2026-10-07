// Phase 6A — TopNavigation.
//
// Horizontal nav bar patterned after the HipLink reference:
//   [Brand] | [Nav items ...] | [ThemeToggle] [UserMenu]
//
// Active item uses the cyan primary-soft background with primary
// text and a subtle primary border.  Inactive items remain slate.
// No giant SaaS landing-page tabs — compact, dense, enterprise.

import { Link, useLocation } from '../lib/router'
import { navItemsForRole } from '../lib/tokens'
import { useAuth } from '../lib/auth'
import { BrandHeader } from './BrandHeader'
import { ThemeToggle } from './ThemeToggle'
import { UserMenu } from './UserMenu'

export function TopNavigation() {
  const { role } = useAuth()
  const { pathname } = useLocation()
  const items = navItemsForRole(role)

  return (
    <header
      className="
        sticky top-0 z-20 border-b border-border bg-bg-elevated
      "
      data-testid="top-navigation"
    >
      <div className="mx-auto flex h-14 max-w-screen-2xl items-center gap-3 px-3 sm:px-5">
        <BrandHeader />
        <nav
          aria-label="Primary"
          className="ml-2 hidden flex-1 items-center gap-1 lg:flex"
        >
          {items.map((item) => {
            const active = isActive(pathname, item.path)
            return (
              <Link
                key={item.path}
                to={item.path}
                aria-current={active ? 'page' : undefined}
                className={[
                  'rounded-md px-3 py-1.5 text-sm transition-colors',
                  active
                    ? 'bg-primary-soft text-primary ring-1 ring-primary/30'
                    : 'text-fg-secondary hover:bg-surface-hover hover:text-fg-primary',
                ].join(' ')}
              >
                {item.label}
              </Link>
            )
          })}
        </nav>
        <div className="ml-auto flex items-center gap-2">
          <span className="hidden md:inline">
            <ThemeToggle />
          </span>
          <UserMenu />
        </div>
      </div>
    </header>
  )
}

function isActive(pathname: string, target: string): boolean {
  if (target === '/') return pathname === '/'
  return pathname === target || pathname.startsWith(target + '/')
}
