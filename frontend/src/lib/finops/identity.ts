// Phase 6B — Identity service.
//
// Thin wrapper around `apiFetch` for `/api/aws/identity`.  Errors
// are normalised so callers can render a single `BackendUnavailable`
// without inspecting the raw ApiError.
//
// Phase 7B: optional `accountId` selects a specific registry row.

import { apiFetch } from '../api'
import type { AwsIdentity } from '../../types/finops'

export async function fetchIdentity(
  options: { accountId?: string | null } = {},
): Promise<AwsIdentity> {
  const params = new URLSearchParams()
  if (options.accountId) params.set('account_id', options.accountId)
  const qs = params.toString()
  return apiFetch<AwsIdentity>(`/api/aws/identity${qs ? `?${qs}` : ''}`, {
    timeoutMs: 10_000,
  })
}
