// Phase 6A — TopNavigation.
//
// Horizontal nav bar patterned after the HipLink reference:
//   [Brand] | [Nav items ...] | [ProviderSelector] [AccountSelector] | [ThemeToggle] [UserMenu]
//
// Phase 7C.2A closure — refined alignment:
//   * every right-side control is h-9 so vertical centering is exact;
//   * the right cluster is a single inline-flex group with a
//     subtle vertical divider between the cloud selectors and the
//     theme / user controls;
//   * long display names truncate cleanly inside each selector;
//   * selectors + theme toggle hide on small screens so the
//     brand and user menu remain the only horizontal content;
//   * wrap is allowed (flex-wrap) so long display names push
//     into a second row without overflowing the viewport.

import { Link, useLocation } from '../lib/router'
import { navItemsForRole } from '../lib/tokens'
import { useAuth } from '../lib/auth'
import { BrandHeader } from './BrandHeader'
import { ThemeToggle } from './ThemeToggle'
import { UserMenu } from './UserMenu'
import { AccountSelector } from './AccountSelector'
import { ProviderSelector } from './ProviderSelector'

export function TopNavigation() {
  const { role } = useAuth()
  const { pathname } = useLocation()
  const items = navItemsForRole(role)

  return (
    <header
      className="sticky top-0 z-20 border-b border-border bg-bg-elevated"
      data-testid="top-navigation"
    >
      <div
        className="
          mx-auto flex h-14 max-w-screen-2xl flex-wrap items-center
          gap-x-3 gap-y-2 px-3 sm:px-5
        "
      >
        <BrandHeader />
        <nav
          aria-label="Primary"
          className="ml-1 hidden min-w-0 flex-1 items-center gap-1 lg:flex"
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
        <div
          className="
            ml-auto flex flex-wrap items-center gap-2
          "
        >
          {/* Provider + Account selectors — only visible on lg+ to
              keep the header breathable on smaller viewports. */}
          <div
            className="
              hidden items-center gap-2 lg:flex
            "
            data-testid="top-nav-selectors"
          >
            <ProviderSelector />
            <AccountSelector />
          </div>
          {/* Divider — separates the cloud-context selectors from the
              user-facing controls (theme + identity).  Hidden on
              small viewports because the selectors themselves are
              hidden there. */}
          <span
            aria-hidden
            className="mx-1 hidden h-6 w-px bg-border lg:inline-block"
          />
          <span className="hidden md:inline-flex">
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
