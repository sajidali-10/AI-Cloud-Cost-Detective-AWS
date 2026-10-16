// Phase 7C post-release hotfix #2 — provider-aware selection coercion.
//
// The canonical cross-cloud API routes resolve accounts differently
// by provider:
//
//   * AWS  — ``acct:<local row id>`` (the Phase 7B route maps the
//     local database primary key → role ARN).
//   * Azure — ``sub:<subscription-guid>`` (Cost Management,
//     Resource Graph and Advisor all resolve the subscription GUID).
//
// The AccountSelector now emits the correct form per provider, so
// new selections are already canonical.  This helper is the
// *defence-in-depth* boundary at the API client layer: a stale
// pre-hotfix session may still hold an ``acct:<n>`` value for an
// Azure selection, and passing it through would produce
// ``no AZURE account matches selection='acct:5'``.  We coerce a
// stale ``acct:<n>`` under the Azure provider back to ``all``
// (self-correction) instead of sending a meaningless selector to
// an Azure route.

export type CanonicalProvider = 'AWS' | 'AZURE' | 'RACKSPACE'

/** Lowercase cache-provider spelling (``aws``/``azure``/``rackspace``). */
export type CacheProviderSlug = 'aws' | 'azure' | 'rackspace'

type ParticipantProvider = CanonicalProvider | CacheProviderSlug

function isAzureProvider(provider: ParticipantProvider): boolean {
  return provider === 'AZURE' || provider === 'azure'
}

/**
 * Return the canonical ``account_id`` query value for a selection
 * under the given provider, or ``undefined`` when the selection
 * should be omitted (i.e. "all" or a self-corrected stale value).
 */
export function canonicalAccountParam(
  accountId: string | null | undefined,
  provider: CanonicalProvider,
): string | undefined {
  if (!accountId || accountId === 'all') return undefined
  // Azure canonical selectors must be ``sub:<guid>``.  A stale
  // ``acct:<n>`` is meaningless to the Azure routes — coerce to
  // "all" (omit the param) rather than emit ``acct:5`` / ``acct:6``.
  if (isAzureProvider(provider)) {
    if (accountId.startsWith('sub:')) return accountId
    return undefined
  }
  return accountId
}

/**
 * Canonical cache-key segment for a selection under a provider.
 * Used by the client cache-key builders so a stale Azure
 * ``acct:<n>`` self-corrects to ``all`` in the key shape too,
 * avoiding a stale authoritative-zero cache entry.
 */
export function canonicalCacheSegment(
  accountId: string | null | undefined,
  provider: ParticipantProvider,
): string {
  if (!accountId || accountId === 'all') return 'all'
  if (isAzureProvider(provider) && !accountId.startsWith('sub:')) {
    // Stale pre-hotfix Azure selection — treat as "all".
    return 'all'
  }
  return accountId
}