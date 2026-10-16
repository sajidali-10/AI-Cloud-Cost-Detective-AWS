// Phase 7C.2A — Navigation dropdown primitive.
//
// Used by the primary navigation row to expose grouped routes
// (FinOps, Administration) without the route overload that the
// legacy flat row suffered from.
//
// Behaviour contract (must NOT regress across reworks):
//   * The trigger is a real <button> with `aria-haspopup="menu"`
//     and `aria-expanded` reflecting open state.
//   * Items render as `role="menuitem"` inside a `role="menu"` list.
//     Items are React Router <Link>s so navigation is real and the
//     React Router re-renders the active state without remounting.
//   * Click outside closes the menu.
//   * Escape closes the menu and returns focus to the trigger.
//   * Arrow Down on the trigger opens the menu and focuses the
//     first item; subsequent Down/Up move between items; Home/End
//     jump to first/last.  Tab closes the menu without trapping
//     focus (focus-trap discipline is intentionally NOT applied so
//     keyboard users can keep tabbing through page controls).
//   * Active state on the trigger reflects whether any item's path
//     is active, so the visual group label lights up when on
//     /costs, /resources, /optimization etc.
//   * The trigger label is always visible and never truncates; the
//     dropdown panel can be wider than the trigger.

import {
  forwardRef,
  KeyboardEvent,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type AnchorHTMLAttributes,
} from 'react'
import { useLocation, useNavigate } from '../lib/router'

export interface NavDropdownItem {
  /** Route path; matched against the router. */
  path: string
  /** Visible item label. */
  label: string
  /** Optional secondary descriptor (e.g. role or status). */
  hint?: string
  /** Test id override; defaults to `nav-dropdown-item-<path>`. */
  testId?: string
}

interface NavDropdownProps {
  /** Visible label on the trigger (e.g. "FinOps"). */
  label: string
  /** Whether the trigger should render as active right now. */
  active: boolean
  /** Items rendered inside the menu. */
  items: NavDropdownItem[]
  /** Stable test id for the trigger. */
  triggerTestId?: string
  /** Stable test id for the menu container. */
  menuTestId?: string
  /**
   * Optional extra className for the trigger button — useful when a
   * caller wants a slight visual variation.  Defaults to the shared
   * primary-nav link look so the dropdowns blend with the rest of
   * the row.
   */
  triggerClassName?: string
}

export function NavDropdown({
  label,
  active,
  items,
  triggerTestId,
  menuTestId,
}: NavDropdownProps) {
  const { pathname } = useLocation()
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const menuRef = useRef<HTMLUListElement | null>(null)
  const rootRef = useRef<HTMLDivElement | null>(null)
  const itemRefs = useRef<Array<HTMLAnchorElement | null>>([])
  const menuId = useId()

  // Close on route change — clicking an item navigates, the router
  // updates pathname, and we tear down the panel so the next click
  // outside does not see a stale open menu.
  useEffect(() => {
    setOpen(false)
  }, [pathname])

  // Outside click + Escape close.
  useEffect(() => {
    if (!open) return
    function onMouseDown(ev: MouseEvent) {
      if (!rootRef.current) return
      if (!rootRef.current.contains(ev.target as Node)) {
        setOpen(false)
      }
    }
    function onKey(ev: globalThis.KeyboardEvent) {
      if (ev.key === 'Escape') {
        setOpen(false)
        triggerRef.current?.focus()
      }
    }
    document.addEventListener('mousedown', onMouseDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onMouseDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const focusItem = useCallback((index: number) => {
    const target = itemRefs.current[index]
    if (target) target.focus()
  }, [])

  const onTriggerKey = useCallback(
    (ev: KeyboardEvent<HTMLButtonElement>) => {
      if (ev.key === 'ArrowDown' || ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault()
        setOpen(true)
        // Defer to next tick — the menu items render after `open`
        // becomes true.
        requestAnimationFrame(() => focusItem(0))
      } else if (ev.key === 'ArrowUp') {
        ev.preventDefault()
        setOpen(true)
        requestAnimationFrame(() => focusItem(items.length - 1))
      }
    },
    [focusItem, items.length],
  )

  const onMenuKey = useCallback(
    (ev: KeyboardEvent<HTMLUListElement>) => {
      const currentIndex = itemRefs.current.findIndex(
        (el) => el === document.activeElement,
      )
      switch (ev.key) {
        case 'ArrowDown': {
          ev.preventDefault()
          const next = currentIndex < 0 ? 0 : (currentIndex + 1) % items.length
          focusItem(next)
          break
        }
        case 'ArrowUp': {
          ev.preventDefault()
          const next =
            currentIndex < 0
              ? items.length - 1
              : (currentIndex - 1 + items.length) % items.length
          focusItem(next)
          break
        }
        case 'Home': {
          ev.preventDefault()
          focusItem(0)
          break
        }
        case 'End': {
          ev.preventDefault()
          focusItem(items.length - 1)
          break
        }
        case 'Tab': {
          // Let Tab move focus normally but close the menu so the
          // user isn't trapped.
          setOpen(false)
          break
        }
        default:
          break
      }
    },
    [focusItem, items.length],
  )

  const triggerClasses = useMemo(() => {
    return [
      'inline-flex h-9 items-center gap-1 rounded-md px-3 text-sm transition-colors',
      active
        ? 'bg-primary-soft text-primary ring-1 ring-primary/30'
        : 'text-fg-secondary hover:bg-surface-hover hover:text-fg-primary',
    ].join(' ')
  }, [active])

  return (
    <div ref={rootRef} className="relative inline-block text-left">
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
        aria-current={active ? 'page' : undefined}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={onTriggerKey}
        className={triggerClasses}
        data-testid={triggerTestId}
      >
        <span>{label}</span>
        <svg
          aria-hidden
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          className={
            'shrink-0 transition-transform ' + (open ? 'rotate-180' : '')
          }
        >
          <path d="m6 9 6 6 6-6" />
        </svg>
      </button>
      {open && (
        <ul
          ref={menuRef}
          id={menuId}
          role="menu"
          aria-label={label}
          onKeyDown={onMenuKey}
          data-testid={menuTestId}
          className="
            absolute left-0 z-40 mt-1 min-w-[12rem] overflow-hidden
            rounded-md border border-border bg-surface py-1 text-sm
            shadow-card-md
          "
        >
          {items.map((item, index) => {
            const itemActive = isItemActive(pathname, item.path)
            return (
              <li key={item.path} role="none">
                <MenuLink
                  to={item.path}
                  role="menuitem"
                  tabIndex={-1}
                  ref={(el: HTMLAnchorElement | null) => {
                    itemRefs.current[index] = el
                  }}
                  aria-current={itemActive ? 'page' : undefined}
                  data-testid={
                    item.testId ?? `nav-dropdown-item-${item.path}`
                  }
                  className={[
                    'flex items-center justify-between gap-3 px-3 py-2 transition-colors',
                    'focus:bg-surface-hover focus:outline-none',
                    itemActive
                      ? 'bg-info-soft text-info'
                      : 'text-fg-primary hover:bg-surface-hover',
                  ].join(' ')}
                  onNavigate={() => setOpen(false)}
                >
                  <span className="font-medium">{item.label}</span>
                  {item.hint ? (
                    <span className="text-xs text-fg-muted">{item.hint}</span>
                  ) : null}
                </MenuLink>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

export function isItemActive(pathname: string, target: string): boolean {
  if (target === '/') return pathname === '/'
  return pathname === target || pathname.startsWith(target + '/')
}

// ---------------------------------------------------------------------------
// MenuLink — ref-forwarding <a> with router-aware navigation.
//
// Mirrors the public <Link> component but exposes a ref so callers
// (notably NavDropdown's keyboard navigation) can move focus between
// items without depending on the router's internal ref forwarding.
// ---------------------------------------------------------------------------

interface MenuLinkProps
  extends Omit<AnchorHTMLAttributes<HTMLAnchorElement>, 'href'> {
  to: string
  /** Optional handler fired after navigation succeeds. */
  onNavigate?: () => void
}

const MenuLink = forwardRef<HTMLAnchorElement, MenuLinkProps>(function MenuLink(
  { to, onNavigate, onClick, ...rest },
  ref,
) {
  const navigate = useNavigate()
  return (
    <a
      ref={ref}
      href={to}
      onClick={(event) => {
        if (event.defaultPrevented) return
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
        if (event.button !== 0) return
        onClick?.(event)
        if (event.defaultPrevented) return
        event.preventDefault()
        navigate(to)
        onNavigate?.()
      }}
      {...rest}
    />
  )
})
