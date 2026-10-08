// Phase 6B — Identity service.
//
// Thin wrapper around `apiFetch` for `/api/aws/identity`.  Errors
// are normalised so callers can render a single `BackendUnavailable`
// without inspecting the raw ApiError.

import { apiFetch } from '../api'
import type { AwsIdentity } from '../../types/finops'

export async function fetchIdentity(): Promise<AwsIdentity> {
  return apiFetch<AwsIdentity>('/api/aws/identity', { timeoutMs: 10_000 })
}
