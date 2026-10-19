// Phase 7C.2A — Navigation closure tests.
//
// Pins the new two-row navigation architecture:
//
//   * Row 1 hosts Dashboard, FinOps dropdown, AI Cost Analyst,
//     Conversations, Administration dropdown, theme, user.
//   * Row 2 hosts the Cloud Context bar with the
//     ProviderSelector + AccountSelector.
//   * FinOps dropdown contains Costs / Resources / Optimization.
//   * Administration dropdown contains Users / Accounts / Security
//     and only renders for ADMIN.
//   * Active state: /costs lights FinOps; /users lights
//     Administration; Dashboard / AI Cost Analyst / Conversations
//     keep their individual active states.
//   * Dropdown semantics: aria-haspopup="menu", aria-expanded,
//     outside-click closes, Escape closes, keyboard navigation
//     works.
//   * AWS / Azure provider state pins to existing selectors;
//     sessionStorage keys are unchanged.
//   * Mobile navigation exposes grouped items with non-interactive
//     section headers.
//
// We test the building blocks (NavDropdown, CloudContextBar,
// tokens) directly so failures stay local; the FullNavHarness
// composes them with MemoryRouter to exercise the wiring.

import { describe, expect, it, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from '../lib/router'
import {
  ADMIN_GROUP,
  FINOPS_GROUP,
  isPathInGroup,
  navGroupItemsForRole,
  navGroupsForRole,
  navItemsForRole,
} from '../lib/tokens'
import { NavDropdown, isItemActive } from '../components/NavDropdown'
import { CloudContextBar } from '../components/CloudContextBar'
import { TopNavigation } from '../components/TopNavigation'
import { MobileNavigation } from '../components/MobileNavigation'
import { readSourceFile } from './helpers/sourcePath'

// ---------------------------------------------------------------------------
// Test harness: AuthProvider isn't strictly required by these units
// but TopNavigation and MobileNavigation call useAuth().  We mock
// the auth module with a mutable role so individual tests can
// switch ADMIN ↔ VIEWER without re-importing the whole module.
// ---------------------------------------------------------------------------

const authState = vi.hoisted(() => ({
  role: 'ADMIN' as 'ADMIN' | 'ANALYST' | 'VIEWER' | null,
}))

vi.mock('../lib/auth', () => ({
  useAuth: () => ({
    role: authState.role,
    authEnabled: true,
    status: { phase: 'authenticated' },
    ready: true,
    user: null,
    login: () => Promise.resolve(),
    logout: () => Promise.resolve(),
    refresh: () => Promise.resolve(),
  }),
  AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}))

// ---------------------------------------------------------------------------
// Tokens — FinOps / Administration grouping
// ---------------------------------------------------------------------------

describe('Phase 7C.2A — token grouping', () => {
  it('FinOps group lists the three FinOps routes', () => {
    expect(FINOPS_GROUP.paths).toEqual([
      '/costs',
      '/resources',
      '/optimization',
    ])
  })
  it('Administration group lists Users / Accounts / Security', () => {
    expect(ADMIN_GROUP.paths).toEqual(['/users', '/accounts', '/security'])
    expect(ADMIN_GROUP.visibleTo).toEqual(['ADMIN'])
  })
  it('navGroupItemsForRole returns FinOps items for any role', () => {
    for (const role of ['ADMIN', 'ANALYST', 'VIEWER'] as const) {
      const items = navGroupItemsForRole(FINOPS_GROUP, role)
      const labels = items.map((i) => i.label)
      expect(labels).toEqual(['Costs', 'Resources', 'Optimization'])
    }
  })
  it('navGroupItemsForRole hides Administration for non-admins', () => {
    expect(navGroupItemsForRole(ADMIN_GROUP, 'ANALYST')).toEqual([])
    expect(navGroupItemsForRole(ADMIN_GROUP, 'VIEWER')).toEqual([])
    const admin = navGroupItemsForRole(ADMIN_GROUP, 'ADMIN')
    expect(admin.map((i) => i.label)).toEqual(['Users', 'Accounts', 'Security'])
  })
  it('navGroupsForRole only includes Administration for ADMIN', () => {
    const groups = navGroupsForRole('ADMIN').map((g) => g.label)
    expect(groups).toContain('FinOps')
    expect(groups).toContain('Administration')
    const analystGroups = navGroupsForRole('ANALYST').map((g) => g.label)
    expect(analystGroups).toContain('FinOps')
    expect(analystGroups).not.toContain('Administration')
  })
  it('isPathInGroup detects active route', () => {
    expect(isPathInGroup('/costs', FINOPS_GROUP)).toBe(true)
    expect(isPathInGroup('/costs/details', FINOPS_GROUP)).toBe(true)
    expect(isPathInGroup('/users', FINOPS_GROUP)).toBe(false)
    expect(isPathInGroup('/users', ADMIN_GROUP)).toBe(true)
  })
  it('isItemActive matches the router semantics', () => {
    expect(isItemActive('/', '/')).toBe(true)
    expect(isItemActive('/costs', '/costs')).toBe(true)
    expect(isItemActive('/costs/details', '/costs')).toBe(true)
    expect(isItemActive('/', '/costs')).toBe(false)
  })
  it('every NAV_ITEMS path is still reachable (no routes lost)', () => {
    const expected = new Set(
      navItemsForRole('ADMIN').map((i) => i.path),
    )
    // The grouped items cover the FinOps and Administration paths.
    const grouped = new Set<string>([
      ...navGroupItemsForRole(FINOPS_GROUP, 'ADMIN').map((i) => i.path),
      ...navGroupItemsForRole(ADMIN_GROUP, 'ADMIN').map((i) => i.path),
    ])
    expected.delete('/')
    expected.delete('/analyst')
    expected.delete('/cloud-ai')
    expected.delete('/conversations')
    for (const path of grouped) expected.delete(path)
    expect(expected.size).toBe(0)
  })
})

// ---------------------------------------------------------------------------
// NavDropdown primitive
// ---------------------------------------------------------------------------

describe('Phase 7C.2A — NavDropdown', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/')
  })

  const items = [
    { path: '/costs', label: 'Costs' },
    { path: '/resources', label: 'Resources' },
    { path: '/optimization', label: 'Optimization' },
  ]

  function renderDropdown(initialPath = '/') {
    return render(
      <MemoryRouter initialEntries={[initialPath]}>
        <NavDropdown
          label="FinOps"
          active={false}
          items={items}
          triggerTestId="nav-dropdown-finops"
          menuTestId="nav-dropdown-finops-menu"
        />
      </MemoryRouter>,
    )
  }

  it('renders the trigger with aria-haspopup="menu" and aria-expanded=false', () => {
    renderDropdown()
    const trigger = screen.getByTestId('nav-dropdown-finops')
    expect(trigger.tagName).toBe('BUTTON')
    expect(trigger.getAttribute('aria-haspopup')).toBe('menu')
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
  })

  it('does not render menu items until opened', () => {
    renderDropdown()
    expect(screen.queryByTestId('nav-dropdown-finops-menu')).toBeNull()
  })

  it('opens on click and renders menu items with role=menuitem', async () => {
    const user = userEvent.setup()
    renderDropdown()
    await user.click(screen.getByTestId('nav-dropdown-finops'))
    const menu = screen.getByTestId('nav-dropdown-finops-menu')
    expect(menu.getAttribute('role')).toBe('menu')
    const itemButtons = within(menu).getAllByRole('menuitem')
    expect(itemButtons.map((b) => b.textContent)).toEqual([
      'Costs',
      'Resources',
      'Optimization',
    ])
  })

  it('marks the trigger aria-expanded=true when open', async () => {
    const user = userEvent.setup()
    renderDropdown()
    await user.click(screen.getByTestId('nav-dropdown-finops'))
    expect(
      screen.getByTestId('nav-dropdown-finops').getAttribute('aria-expanded'),
    ).toBe('true')
  })

  it('closes when clicking outside', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter initialEntries={['/']}>
        <div>
          <NavDropdown
            label="FinOps"
            active={false}
            items={items}
            triggerTestId="dd-trigger"
            menuTestId="dd-menu"
          />
          <button data-testid="outside">outside</button>
        </div>
      </MemoryRouter>,
    )
    await user.click(screen.getByTestId('dd-trigger'))
    expect(screen.getByTestId('dd-menu')).toBeInTheDocument()
    // Outside click — fire mousedown to match the listener.
    fireEvent.mouseDown(screen.getByTestId('outside'))
    expect(screen.queryByTestId('dd-menu')).toBeNull()
  })

  it('closes on Escape', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter initialEntries={['/']}>
        <NavDropdown
          label="FinOps"
          active={false}
          items={items}
          triggerTestId="dd-trigger"
          menuTestId="dd-menu"
        />
      </MemoryRouter>,
    )
    await user.click(screen.getByTestId('dd-trigger'))
    expect(screen.getByTestId('dd-menu')).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByTestId('dd-menu')).toBeNull()
  })

  it('navigates when an item is clicked and closes the menu', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter initialEntries={['/']}>
        <NavDropdown
          label="FinOps"
          active={false}
          items={items}
          triggerTestId="dd-trigger"
          menuTestId="dd-menu"
        />
      </MemoryRouter>,
    )
    await user.click(screen.getByTestId('dd-trigger'))
    await user.click(screen.getByTestId('nav-dropdown-item-/resources'))
    // After navigation, the menu must auto-close.
    expect(screen.queryByTestId('dd-menu')).toBeNull()
  })

  it('marks the active item with aria-current=page', async () => {
    const user = userEvent.setup()
    renderDropdown('/resources')
    await user.click(screen.getByTestId('nav-dropdown-finops'))
    const item = screen.getByTestId('nav-dropdown-item-/resources')
    expect(item.getAttribute('aria-current')).toBe('page')
  })

  it('highlights the trigger when its group is active', () => {
    render(
      <MemoryRouter initialEntries={['/costs']}>
        <NavDropdown
          label="FinOps"
          active={isPathInGroup('/costs', FINOPS_GROUP)}
          items={items}
          triggerTestId="dd-trigger"
        />
      </MemoryRouter>,
    )
    const trigger = screen.getByTestId('dd-trigger')
    expect(trigger.getAttribute('aria-current')).toBe('page')
  })
})

// ---------------------------------------------------------------------------
// CloudContextBar
// ---------------------------------------------------------------------------

describe('Phase 7C.2A — CloudContextBar', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
    window.localStorage.clear()
    // Stub fetch so ProviderSelector / AccountSelector don't blow up.
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [], count: 0, providers: [] }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    ) as unknown as typeof fetch
  })

  it('renders the cloud context bar with a Cloud Context label', async () => {
    const { ProviderProvider } = await import('../lib/providerContext')
    const { AccountProvider } = await import('../lib/accountContext')
    render(
      <MemoryRouter initialEntries={['/']}>
        <ProviderProvider>
          <AccountProvider>
            <CloudContextBar />
          </AccountProvider>
        </ProviderProvider>
      </MemoryRouter>,
    )
    const bar = await screen.findByTestId('cloud-context-bar')
    expect(bar).toBeInTheDocument()
    expect(within(bar).getByText('Cloud Context')).toBeInTheDocument()
  })

  it('mounts ProviderSelector + AccountSelector slot', async () => {
    const { ProviderProvider } = await import('../lib/providerContext')
    const { AccountProvider } = await import('../lib/accountContext')
    render(
      <MemoryRouter initialEntries={['/']}>
        <ProviderProvider>
          <AccountProvider>
            <CloudContextBar />
          </AccountProvider>
        </ProviderProvider>
      </MemoryRouter>,
    )
    // Wait for the fetch to resolve so both selectors render.
    const selectors = await screen.findByTestId('cloud-context-selectors')
    expect(selectors).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// TopNavigation — full row composition
// ---------------------------------------------------------------------------

describe('Phase 7C.2A — TopNavigation', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/')
    window.sessionStorage.clear()
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [], count: 0, providers: [] }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    ) as unknown as typeof fetch
    authState.role = 'ADMIN'
  })

  it('renders primary nav with FinOps and Administration dropdowns', async () => {
    const { ProviderProvider } = await import('../lib/providerContext')
    const { AccountProvider } = await import('../lib/accountContext')
    const { ThemeProvider } = await import('../lib/theme')
    render(
      <ThemeProvider>
        <MemoryRouter initialEntries={['/']}>
          <ProviderProvider>
            <AccountProvider>
              <TopNavigation />
            </AccountProvider>
          </ProviderProvider>
        </MemoryRouter>
      </ThemeProvider>,
    )
    expect(screen.getByTestId('top-navigation')).toBeInTheDocument()
    expect(screen.getByTestId('cloud-context-bar')).toBeInTheDocument()
    expect(screen.getByTestId('nav-dropdown-finops')).toBeInTheDocument()
    expect(screen.getByTestId('nav-dropdown-admin')).toBeInTheDocument()
  })

  it('renders Dashboard + AI Cost Analyst + Conversations as direct links', async () => {
    const { ProviderProvider } = await import('../lib/providerContext')
    const { AccountProvider } = await import('../lib/accountContext')
    const { ThemeProvider } = await import('../lib/theme')
    render(
      <ThemeProvider>
        <MemoryRouter initialEntries={['/']}>
          <ProviderProvider>
            <AccountProvider>
              <TopNavigation />
            </AccountProvider>
          </ProviderProvider>
        </MemoryRouter>
      </ThemeProvider>,
    )
    expect(screen.getByTestId('nav-link-root')).toHaveTextContent('Dashboard')
    expect(screen.getByTestId('nav-link-analyst')).toHaveTextContent(
      'AI Cost Analyst',
    )
    expect(screen.getByTestId('nav-link-conversations')).toHaveTextContent(
      'Conversations',
    )
  })

  it('hides Administration for non-admin users', async () => {
    authState.role = 'VIEWER'
    const { ProviderProvider } = await import('../lib/providerContext')
    const { AccountProvider } = await import('../lib/accountContext')
    const { ThemeProvider } = await import('../lib/theme')
    render(
      <ThemeProvider>
        <MemoryRouter initialEntries={['/']}>
          <ProviderProvider>
            <AccountProvider>
              <TopNavigation />
            </AccountProvider>
          </ProviderProvider>
        </MemoryRouter>
      </ThemeProvider>,
    )
    expect(screen.queryByTestId('nav-dropdown-admin')).toBeNull()
    expect(screen.getByTestId('nav-dropdown-finops')).toBeInTheDocument()
  })

  it('active state: /costs highlights FinOps dropdown trigger', async () => {
    const { ProviderProvider } = await import('../lib/providerContext')
    const { AccountProvider } = await import('../lib/accountContext')
    const { ThemeProvider } = await import('../lib/theme')
    render(
      <ThemeProvider>
        <MemoryRouter initialEntries={['/costs']}>
          <ProviderProvider>
            <AccountProvider>
              <TopNavigation />
            </AccountProvider>
          </ProviderProvider>
        </MemoryRouter>
      </ThemeProvider>,
    )
    expect(
      screen.getByTestId('nav-dropdown-finops').getAttribute('aria-current'),
    ).toBe('page')
  })

  it('active state: /users highlights Administration dropdown trigger', async () => {
    const { ProviderProvider } = await import('../lib/providerContext')
    const { AccountProvider } = await import('../lib/accountContext')
    const { ThemeProvider } = await import('../lib/theme')
    render(
      <ThemeProvider>
        <MemoryRouter initialEntries={['/users']}>
          <ProviderProvider>
            <AccountProvider>
              <TopNavigation />
            </AccountProvider>
          </ProviderProvider>
        </MemoryRouter>
      </ThemeProvider>,
    )
    expect(
      screen.getByTestId('nav-dropdown-admin').getAttribute('aria-current'),
    ).toBe('page')
    expect(
      screen.getByTestId('nav-dropdown-finops').getAttribute('aria-current'),
    ).not.toBe('page')
  })

  it('active state: /analyst highlights AI Cost Analyst link only', async () => {
    const { ProviderProvider } = await import('../lib/providerContext')
    const { AccountProvider } = await import('../lib/accountContext')
    const { ThemeProvider } = await import('../lib/theme')
    render(
      <ThemeProvider>
        <MemoryRouter initialEntries={['/analyst']}>
          <ProviderProvider>
            <AccountProvider>
              <TopNavigation />
            </AccountProvider>
          </ProviderProvider>
        </MemoryRouter>
      </ThemeProvider>,
    )
    expect(
      screen.getByTestId('nav-link-analyst').getAttribute('aria-current'),
    ).toBe('page')
    expect(
      screen.getByTestId('nav-dropdown-finops').getAttribute('aria-current'),
    ).not.toBe('page')
  })
})

// ---------------------------------------------------------------------------
// MobileNavigation — grouped drawer
// ---------------------------------------------------------------------------

describe('Phase 7C.2A — MobileNavigation', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/')
    window.sessionStorage.clear()
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [], count: 0, providers: [] }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    ) as unknown as typeof fetch
    authState.role = 'ADMIN'
  })

  it('renders FinOps + Administration grouped sections when drawer opens', async () => {
    const user = userEvent.setup()
    const { ProviderProvider } = await import('../lib/providerContext')
    const { AccountProvider } = await import('../lib/accountContext')
    const { ThemeProvider } = await import('../lib/theme')
    render(
      <ThemeProvider>
        <MemoryRouter initialEntries={['/']}>
          <ProviderProvider>
            <AccountProvider>
              <MobileNavigation />
            </AccountProvider>
          </ProviderProvider>
        </MemoryRouter>
      </ThemeProvider>,
    )
    const trigger = screen.getByRole('button', { name: /open menu/i })
    await user.click(trigger)
    expect(screen.getByTestId('mobile-finops-group')).toBeInTheDocument()
    expect(screen.getByTestId('mobile-admin-group')).toBeInTheDocument()
    // Group headers are non-interactive (no role=button).
    const finopsHeader = screen.getByTestId('mobile-finops-header')
    expect(finopsHeader.tagName).not.toBe('BUTTON')
  })

  it('exposes all FinOps items inside the FinOps group', async () => {
    const user = userEvent.setup()
    const { ProviderProvider } = await import('../lib/providerContext')
    const { AccountProvider } = await import('../lib/accountContext')
    const { ThemeProvider } = await import('../lib/theme')
    render(
      <ThemeProvider>
        <MemoryRouter initialEntries={['/']}>
          <ProviderProvider>
            <AccountProvider>
              <MobileNavigation />
            </AccountProvider>
          </ProviderProvider>
        </MemoryRouter>
      </ThemeProvider>,
    )
    await user.click(screen.getByRole('button', { name: /open menu/i }))
    const group = screen.getByTestId('mobile-finops-group')
    expect(
      within(group).getByTestId('mobile-finops-item-costs'),
    ).toHaveTextContent('Costs')
    expect(
      within(group).getByTestId('mobile-finops-item-resources'),
    ).toHaveTextContent('Resources')
    expect(
      within(group).getByTestId('mobile-finops-item-optimization'),
    ).toHaveTextContent('Optimization')
  })

  it('exposes all Administration items inside the Administration group (ADMIN)', async () => {
    const user = userEvent.setup()
    const { ProviderProvider } = await import('../lib/providerContext')
    const { AccountProvider } = await import('../lib/accountContext')
    const { ThemeProvider } = await import('../lib/theme')
    render(
      <ThemeProvider>
        <MemoryRouter initialEntries={['/']}>
          <ProviderProvider>
            <AccountProvider>
              <MobileNavigation />
            </AccountProvider>
          </ProviderProvider>
        </MemoryRouter>
      </ThemeProvider>,
    )
    await user.click(screen.getByRole('button', { name: /open menu/i }))
    const group = screen.getByTestId('mobile-admin-group')
    expect(
      within(group).getByTestId('mobile-admin-item-users'),
    ).toHaveTextContent('Users')
    expect(
      within(group).getByTestId('mobile-admin-item-accounts'),
    ).toHaveTextContent('Accounts')
    expect(
      within(group).getByTestId('mobile-admin-item-security'),
    ).toHaveTextContent('Security')
  })

  it('hides Administration group for non-admin users', async () => {
    authState.role = 'VIEWER'
    const user = userEvent.setup()
    const { ProviderProvider } = await import('../lib/providerContext')
    const { AccountProvider } = await import('../lib/accountContext')
    const { ThemeProvider } = await import('../lib/theme')
    render(
      <ThemeProvider>
        <MemoryRouter initialEntries={['/']}>
          <ProviderProvider>
            <AccountProvider>
              <MobileNavigation />
            </AccountProvider>
          </ProviderProvider>
        </MemoryRouter>
      </ThemeProvider>,
    )
    await user.click(screen.getByRole('button', { name: /open menu/i }))
    expect(screen.queryByTestId('mobile-admin-group')).toBeNull()
    expect(screen.getByTestId('mobile-finops-group')).toBeInTheDocument()
  })

  it('closes drawer on Escape', async () => {
    const user = userEvent.setup()
    const { ProviderProvider } = await import('../lib/providerContext')
    const { AccountProvider } = await import('../lib/accountContext')
    const { ThemeProvider } = await import('../lib/theme')
    render(
      <ThemeProvider>
        <MemoryRouter initialEntries={['/']}>
          <ProviderProvider>
            <AccountProvider>
              <MobileNavigation />
            </AccountProvider>
          </ProviderProvider>
        </MemoryRouter>
      </ThemeProvider>,
    )
    await user.click(screen.getByRole('button', { name: /open menu/i }))
    expect(screen.getByTestId('mobile-nav-drawer')).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByTestId('mobile-nav-drawer')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Provider state — AWS / Azure selectors
// ---------------------------------------------------------------------------

describe('Phase 7C.2A — Provider state survives the nav refactor', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
    window.localStorage.clear()
  })

  it('default provider is AWS', async () => {
    const { ProviderProvider, useProviderContext } = await import(
      '../lib/providerContext'
    )
    const { renderHook } = await import('@testing-library/react')
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <ProviderProvider>{children}</ProviderProvider>
    )
    const { result } = renderHook(() => useProviderContext(), { wrapper })
    expect(result.current.provider).toBe('AWS')
  })

  it('setProvider persists to cd.provider.selection.v1', async () => {
    const { ProviderProvider, useProviderContext } = await import(
      '../lib/providerContext'
    )
    const { renderHook, act } = await import('@testing-library/react')
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <ProviderProvider>{children}</ProviderProvider>
    )
    const { result } = renderHook(() => useProviderContext(), { wrapper })
    act(() => result.current.setProvider('AZURE'))
    expect(
      window.sessionStorage.getItem('cd.provider.selection.v1'),
    ).toBe('AZURE')
  })

  it('account selection uses cd.account.selection.v1', async () => {
    window.sessionStorage.setItem('cd.account.selection.v1', 'acct:42')
    const { ProviderProvider } = await import('../lib/providerContext')
    const { AccountProvider, useAccountContext } = await import(
      '../lib/accountContext'
    )
    function Probe() {
      const { selection } = useAccountContext()
      return <span data-testid="sel">{selection}</span>
    }
    render(
      <ProviderProvider>
        <AccountProvider>
          <Probe />
        </AccountProvider>
      </ProviderProvider>,
    )
    expect(screen.getByTestId('sel').textContent).toBe('acct:42')
  })
})

// ---------------------------------------------------------------------------
// No-regression — TopNavigation no longer references the legacy
// top-nav-selectors slot.  This locks the layout invariant that
// selectors live in Row 2, not Row 1.
// ---------------------------------------------------------------------------

describe('Phase 7C.2A — layout invariants', () => {
  // Phase 7C.2E — resolve component paths relative to the test
  // file so the assertions do not depend on vitest's cwd.
  it('TopNavigation does not embed ProviderSelector / AccountSelector directly', async () => {
    const source = await readSourceFile('src/components/TopNavigation.tsx')
    // The legacy inline selectors are now mounted by CloudContextBar.
    expect(source).not.toMatch(/<ProviderSelector\s/)
    expect(source).not.toMatch(/<AccountSelector\s/)
  })
  it('TopNavigation references CloudContextBar', async () => {
    const source = await readSourceFile('src/components/TopNavigation.tsx')
    expect(source).toMatch(/CloudContextBar/)
  })
  it('MobileNavigation references CloudContextBar', async () => {
    const source = await readSourceFile('src/components/MobileNavigation.tsx')
    expect(source).toMatch(/CloudContextBar/)
  })
  it('BrandHeader keeps shrink-0 + whitespace-nowrap so product name does not wrap awkwardly', async () => {
    const source = await readSourceFile('src/components/BrandHeader.tsx')
    expect(source).toMatch(/shrink-0/)
    expect(source).toMatch(/whitespace-nowrap/)
  })
})
