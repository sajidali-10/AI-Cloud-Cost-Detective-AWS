// Phase 7B runtime closure — frontend regression tests.
//
// Pins the defects caught during live browser acceptance:
//
//  1. AccountSelector must be MOUNTED in the production AppShell.
//     Prior to the fix, grep -Rni 'AccountSelector' frontend/src
//     returned no usages.  These tests prove the component is
//     imported by both TopNavigation and MobileNavigation.
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

// ---------------------------------------------------------------------------
// 1. TopNavigation / MobileNavigation must import AccountSelector
// ---------------------------------------------------------------------------

describe('Phase 7B runtime closure — AccountSelector mounting', () => {
  it('TopNavigation imports AccountSelector', async () => {
    const source = await import('fs').then((fs) =>
      fs.promises.readFile('src/components/TopNavigation.tsx', 'utf-8'),
    )
    expect(source).toMatch(/import\s+\{[^}]*AccountSelector[^}]*\}\s+from\s+'\.\/AccountSelector'/)
    expect(source).toMatch(/<AccountSelector/)
  })

  it('MobileNavigation imports AccountSelector', async () => {
    const source = await import('fs').then((fs) =>
      fs.promises.readFile('src/components/MobileNavigation.tsx', 'utf-8'),
    )
    expect(source).toMatch(/import\s+\{[^}]*AccountSelector[^}]*\}\s+from\s+'\.\/AccountSelector'/)
    expect(source).toMatch(/<AccountSelector/)
  })

  it('AccountSelector renders the "All AWS Accounts" label by default', async () => {
    const { AccountProvider, useAccountContext } = await import('../lib/accountContext')
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
      <AccountProvider>
        <Harness />
      </AccountProvider>,
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
    const { AccountProvider, useAccountContext } = await import('../lib/accountContext')

    function Probe() {
      const ctx = useAccountContext()
      // Render the selection as a data attribute so the test can read it.
      return <div data-testid="probe" data-selection={ctx.selection} />
    }

    render(
      <AccountProvider>
        <Probe />
      </AccountProvider>,
    )
    const probe = screen.getByTestId('probe')
    expect(probe.getAttribute('data-selection')).toBe('acct:42')
  })

  it('writing the selection updates sessionStorage', async () => {
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
      <AccountProvider>
        <Probe />
      </AccountProvider>,
    )
    await userEvent.click(screen.getByTestId('set'))
    expect(window.sessionStorage.getItem('cd.account.selection.v1')).toBe('acct:7')
  })

  it('does NOT use localStorage for the selection', async () => {
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
      <AccountProvider>
        <Probe />
      </AccountProvider>,
    )
    await userEvent.click(screen.getByTestId('set'))
    // localStorage must remain empty — the contract forbids it.
    expect(window.localStorage.getItem('cd.account.selection.v1')).toBeNull()
  })
})
