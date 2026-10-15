// Phase 7B — AccountsPage metadata editing regression.
//
// Source-level pin for the Edit feature contract:
//   * ADMIN can edit display_name.
//   * Selector reflects the updated display_name after save.
//   * account_id is immutable (not a form field, not in the payload).
//   * provider and is_management are immutable (not form fields).
//   * Management rows: only display_name + preferred_region editable;
//     role_arn + external_id are read-only in the UI for management.
//   * ANALYST / VIEWER cannot edit the registry.
//   * external_id is write-only — never displayed in the form, never
//     read from state.

import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

// __dirname for this file is frontend/src/tests/.  Walking up two
// levels lands at the repo root.  Hardcoding keeps the test
// resilient against vitest config changes.
const REPO = resolve(__dirname, '..', '..', '..')

function read(relative: string): string {
  return readFileSync(resolve(REPO, relative), 'utf-8')
}

const page = read('frontend/src/pages/AccountsPage.tsx')
const api = read('backend/app/api/cloud_accounts.py')
const schema = read('backend/app/schemas/cloud_account.py')
const provider = read('backend/app/services/aws/account_provider.py')
const ctx = read('frontend/src/lib/accountContext.tsx')

describe('Phase 7B — AccountsPage Edit feature (source)', () => {
  it('exposes an Edit action button for every row', () => {
    expect(page).toMatch(/testid=\{`edit-\$\{row\.id}`\}/)
  })

  it('uses the centralized PATCH API (no direct db access)', () => {
    expect(page).toMatch(/apiFetch\(\s*`\/api\/aws\/cloud-accounts\/\$\{editing\.id\}`/)
    expect(page).toMatch(/method:\s*['"]PATCH['"]/)
  })

  it('does not expose account_id / provider / is_management as form fields', () => {
    expect(page).not.toMatch(/testid=['"]edit-account-id['"]/)
    expect(page).not.toMatch(/testid=['"]edit-provider['"]/)
    expect(page).not.toMatch(/testid=['"]edit-is-management['"]/)
    expect(page).not.toMatch(/testid=['"]edit-enabled['"]/)
  })

  it('admin can edit display_name', () => {
    expect(page).toMatch(/testid=['"]edit-display-name['"]/)
    expect(page).toMatch(/display_name: editForm\.display_name\.trim\(\)/)
  })

  it('admin can edit preferred_region', () => {
    expect(page).toMatch(/testid=['"]edit-preferred-region['"]/)
    expect(page).toMatch(/preferred_region/)
  })

  it('non-management rows expose role_arn editing', () => {
    expect(page).toMatch(/testid=['"]edit-role-arn['"]/)
    expect(page).toMatch(/!editing\.is_management/)
  })

  it('non-management rows expose external_id editing (write-only)', () => {
    expect(page).toMatch(/testid=['"]edit-external-id['"]/)
    expect(page).toMatch(/\(unchanged\)/)
    expect(page).toMatch(/editForm\.external_id\.trim\(\)/)
  })

  it('management rows cannot edit role_arn or external_id', () => {
    expect(page).toMatch(/editing\.is_management/)
    // Look for the management-branch message.  JSX may split the
    // copy across lines, so assert on the stable substrings.
    expect(page).toContain('role ARN and external ID are')
    expect(page).toContain('cannot be edited')
    // The role_arn / external_id inputs are gated on
    // ``!editing.is_management`` so management rows never render
    // them.
    expect(page).toMatch(/!editing\.is_management/)
  })

  it('Phase 7C.2A: management edit hint matches the documented literal', () => {
    // Phase 7C.2A refactored the Edit modal so the management hint
    // is rendered as a single paragraph.  The literal copy below
    // is broken across lines by JSX whitespace so we assert the
    // substrings individually rather than the whole concatenated
    // sentence.
    expect(page).toContain('Management account: role ARN and external ID are')
    expect(page).toContain('managed by the platform and cannot be edited from')
    expect(page).toContain('this UI.')
  })

  it('rejects bad role_arn on save', () => {
    expect(page).toMatch(/role_arn must be a valid IAM role ARN/)
  })

  it('refreshes the registered accounts table after save', () => {
    expect(page).toMatch(/await refresh\(\)/)
  })

  it('refreshes AccountSelector after save (selection data)', () => {
    expect(page).toMatch(/void refreshAccounts\(\)/)
  })

  it('preserves the currently selected account when refreshAccounts fires', () => {
    expect(ctx).toMatch(/refreshAccounts/)
    expect(page).toMatch(/void refreshAccounts\(\)/)
  })

  it('external_id is never displayed in the edit form', () => {
    expect(page).toMatch(/external_id: ''/)
    expect(page).not.toMatch(/value=\{editing\.external_id\}/)
  })

  it('closes the modal on Cancel and resets state', () => {
    expect(page).toMatch(/data-testid=['"]edit-cancel['"]/)
    expect(page).toMatch(/cancelEdit/)
  })
})

describe('Phase 7B — RBAC: ANALYST / VIEWER cannot edit registry', () => {
  it('PATCH endpoint requires ADMIN', () => {
    expect(api).toMatch(/def update_cloud_account\(/)
    expect(api).toMatch(/require_admin/)
  })

  it('POST endpoint requires ADMIN', () => {
    expect(api).toMatch(/def create_cloud_account\(/)
    expect(api).toMatch(/require_admin/)
  })

  it('DELETE endpoint requires ADMIN', () => {
    expect(api).toMatch(/def delete_cloud_account\(/)
    expect(api).toMatch(/require_admin/)
  })

  it('VALIDATE endpoint requires ADMIN', () => {
    expect(api).toMatch(/def validate_cloud_account\(/)
    expect(api).toMatch(/require_admin/)
  })

  it('LIST endpoint requires ADMIN', () => {
    expect(api).toMatch(/def list_cloud_accounts\(/)
    expect(api).toMatch(/require_admin/)
  })
})

describe('Phase 7B — backend PATCH schema enforces immutability', () => {
  it('CloudAccountUpdate does not expose account_id', () => {
    expect(schema).not.toMatch(/account_id:\s*Optional\[str\]/)
  })

  it('CloudAccountUpdate does not expose provider', () => {
    expect(schema).not.toMatch(/provider:\s*Optional\[str\]/)
  })

  it('CloudAccountUpdate does not expose is_management', () => {
    // Capture from ``class CloudAccountUpdate`` until the closing
    // brace at column 0 (top-level class body).  ``is_management``
    // appears in the ORM but must NOT appear as a field on the
    // Pydantic update schema.
    const idx = schema.indexOf('class CloudAccountUpdate')
    expect(idx).toBeGreaterThanOrEqual(0)
    const tail = schema.slice(idx)
    // Match the class body up to the next top-level class.
    const m = tail.match(/\n(class|def) /)
    const block = m ? tail.slice(0, m.index) : tail
    expect(block).not.toMatch(/^\s*is_management\s*:/m)
    expect(block).not.toMatch(/^\s*provider\s*:/m)
    expect(block).not.toMatch(/^\s*account_id\s*:/m)
  })

  it('CloudAccountUpdate does expose display_name, role_arn, external_id, preferred_region', () => {
    expect(schema).toMatch(/display_name:\s*Optional\[str\]/)
    expect(schema).toMatch(/role_arn:\s*Optional\[str\]/)
    expect(schema).toMatch(/external_id:\s*Optional\[str\]/)
    expect(schema).toMatch(/preferred_region:\s*Optional\[str\]/)
  })

  it('CloudAccountResponse never includes external_id as a field', () => {
    const idx = schema.indexOf('class CloudAccountResponse')
    expect(idx).toBeGreaterThanOrEqual(0)
    const tail = schema.slice(idx)
    const m = tail.match(/\n(class|def) /)
    const block = m ? tail.slice(0, m.index) : tail
    // external_id may appear in the docstring — assert only that it
    // is NOT a field.
    expect(block).not.toMatch(/^\s*external_id\s*:/m)
  })
})

describe('Phase 7B — backend update_account enforces immutability', () => {
  it('rejects disabling the management account', () => {
    expect(provider).toMatch(/ManagementAccountImmutable/)
  })

  it('rejects role_arn changes that target a different account_id', () => {
    expect(provider).toMatch(/role_arn embeds an account_id that does not match/)
  })
})
