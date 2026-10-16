// Phase 7B runtime closure — frontend regression tests.
//
// Pins the defects caught during live browser acceptance:
//
//  1. AccountSelector must be MOUNTED in the production AppShell.
//     Prior to the fix, grep -Rni 'AccountSelector' frontend/src
//     returned no usages.  These tests prove the component is
//     reachable from both TopNavigation and MobileNavigation via
//     the Phase 7C.2A CloudContextBar shell — the selector now lives
//     in the dedicated second row, so the import is transitive
//     (TopNavigation → CloudContextBar → AccountSelector).  Same
//     for MobileNavigation.
//
//  2. The "All AWS Accounts" label must be visible in the
//     production bundle.
//
//  3. ADMIN must see the Accounts nav entry; VIEWER must not.
//
//  4. accountContext must persist the selection in sessionStorage
//     and re-hydrate on remount.

import { describe, expect, it, beforeEach, vi } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useEffect } from 'react'

import { navItemsForRole } from '../lib/tokens'
import { readSourceFile } from './helpers/sourcePath'

// ---------------------------------------------------------------------------
// 1. TopNavigation / MobileNavigation must reach AccountSelector
// ---------------------------------------------------------------------------

describe('Phase 7B runtime closure — AccountSelector mounting', () => {
  it('TopNavigation mounts AccountSelector (via CloudContextBar)', async () => {
    const nav = await readSourceFile('src/components/TopNavigation.tsx')
    const cloudBar = await readSourceFile('src/components/CloudContextBar.tsx')
    // TopNavigation must pull CloudContextBar into Row 2.
    expect(nav).toMatch(/CloudContextBar/)
    expect(nav).toMatch(/<CloudContextBar/)
    // CloudContextBar must mount AccountSelector directly.
    expect(cloudBar).toMatch(/<AccountSelector/)
  })

  it('MobileNavigation mounts AccountSelector (via CloudContextBar)', async () => {
    const mobile = await readSourceFile('src/components/MobileNavigation.tsx')
    const cloudBar = await readSourceFile('src/components/CloudContextBar.tsx')
    // MobileNavigation must pull CloudContextBar into Row 2.
    expect(mobile).toMatch(/CloudContextBar/)
    expect(mobile).toMatch(/<CloudContextBar/)
    // CloudContextBar must mount AccountSelector directly.
    expect(cloudBar).toMatch(/<AccountSelector/)
  })

  it('AccountSelector renders the "All AWS Accounts" label by default', async () => {
    const { AccountProvider, useAccountContext } = await import('../lib/accountContext')
    const { ProviderProvider } = await import('../lib/providerContext')
    const { AccountSelector } = await import('../components/AccountSelector')

    // Stub fetch so the selectable list fetch does not blow up.
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [], count: 0 }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    ) as unknown as typeof fetch

    function Harness() {
      // Force refresh to trigger the initial fetch; we don't care
      // about the result, only that the component renders.
      const ctx = useAccountContext()
      useEffect(() => {
        ctx.refreshAccounts().catch(() => undefined)
      }, [])
      return <AccountSelector />
    }

    render(
      <ProviderProvider>
        <AccountProvider>
          <Harness />
        </AccountProvider>
      </ProviderProvider>,
    )
    // The selector renders one button (testid='account-selector-button')
    // and the aggregate option when the menu is open.
    const buttons = screen.getAllByTestId('account-selector-button')
    expect(buttons.length).toBeGreaterThanOrEqual(1)
    await userEvent.click(buttons[0])
    const allOption = screen.getAllByTestId('account-option-all')[0]
    expect(allOption).toHaveTextContent(/All AWS Accounts/i)
  })
})

// ---------------------------------------------------------------------------
// 2. ADMIN-only Accounts nav entry
// ---------------------------------------------------------------------------

describe('Phase 7B runtime closure — Accounts nav RBAC', () => {
  it('ADMIN sees Accounts in nav', () => {
    const labels = navItemsForRole('ADMIN').map((i) => i.label)
    expect(labels).toContain('Accounts')
  })
  it('ANALYST does NOT see Accounts in nav', () => {
    const labels = navItemsForRole('ANALYST').map((i) => i.label)
    expect(labels).not.toContain('Accounts')
  })
  it('VIEWER does NOT see Accounts in nav', () => {
    const labels = navItemsForRole('VIEWER').map((i) => i.label)
    expect(labels).not.toContain('Accounts')
  })
})

// ---------------------------------------------------------------------------
// 3. sessionStorage persistence
// ---------------------------------------------------------------------------

describe('Phase 7B runtime closure — sessionStorage persistence', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
  })

  it('selection survives remount (sessionStorage re-hydrates)', async () => {
    window.sessionStorage.setItem('cd.account.selection.v1', 'acct:42')
    const { ProviderProvider } = await import('../lib/providerContext')
    const { AccountProvider, useAccountContext } = await import('../lib/accountContext')

    function Probe() {
      const ctx = useAccountContext()
      // Render the selection as a data attribute so the test can read it.
      return <div data-testid="probe" data-selection={ctx.selection} />
    }

    render(
      <ProviderProvider>
        <AccountProvider>
          <Probe />
        </AccountProvider>
      </ProviderProvider>,
    )
    const probe = screen.getByTestId('probe')
    expect(probe.getAttribute('data-selection')).toBe('acct:42')
  })

  it('writing the selection updates sessionStorage', async () => {
    const { ProviderProvider } = await import('../lib/providerContext')
    const { AccountProvider, useAccountContext } = await import('../lib/accountContext')

    function Probe() {
      const ctx = useAccountContext()
      return (
        <button
          type="button"
          onClick={() => ctx.setSelection('acct:7')}
          data-testid="set"
        >
          set
        </button>
      )
    }

    render(
      <ProviderProvider>
        <AccountProvider>
          <Probe />
        </AccountProvider>
      </ProviderProvider>,
    )
    await userEvent.click(screen.getByTestId('set'))
    expect(window.sessionStorage.getItem('cd.account.selection.v1')).toBe('acct:7')
  })

  it('does NOT use localStorage for the selection', async () => {
    const { ProviderProvider } = await import('../lib/providerContext')
    const { AccountProvider, useAccountContext } = await import('../lib/accountContext')

    function Probe() {
      const ctx = useAccountContext()
      return (
        <button
          type="button"
          onClick={() => ctx.setSelection('acct:9')}
          data-testid="set"
        >
          set
        </button>
      )
    }

    render(
      <ProviderProvider>
        <AccountProvider>
          <Probe />
        </AccountProvider>
      </ProviderProvider>,
    )
    await userEvent.click(screen.getByTestId('set'))
    // localStorage must remain empty — the contract forbids it.
    expect(window.localStorage.getItem('cd.account.selection.v1')).toBeNull()
  })
})
