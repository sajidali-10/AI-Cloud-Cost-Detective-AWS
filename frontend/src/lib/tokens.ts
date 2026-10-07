// Phase 6A — RBAC token + role-based navigation config.
//
// Role strings must match the backend `RoleName` literal in
// `backend/app/schemas/auth.py` exactly.  The frontend NEVER
// re-implements authorization; it only HIDES controls the
// backend would also reject.  Backend is authoritative.

import type { RoleName } from './router'

export type Role = RoleName

export const ALL_ROLES: readonly Role[] = ['ADMIN', 'ANALYST', 'VIEWER'] as const

export interface NavItem {
  /** Route path; matched against the router. */
  path: string
  /** Display label. */
  label: string
  /** Roles that may see this nav entry.  Empty = all roles. */
  visibleTo: readonly Role[]
}

// Single source of truth for navigation.  Order matters — this is
// the order items appear in the top nav and mobile drawer.
export const NAV_ITEMS: readonly NavItem[] = [
  { path: '/', label: 'Dashboard', visibleTo: ALL_ROLES },
  { path: '/costs', label: 'Costs', visibleTo: ALL_ROLES },
  { path: '/resources', label: 'Resources', visibleTo: ALL_ROLES },
  { path: '/optimization', label: 'Optimization', visibleTo: ALL_ROLES },
  { path: '/analyst', label: 'AI Cost Analyst', visibleTo: ['ADMIN', 'ANALYST'] },
  { path: '/conversations', label: 'Conversations', visibleTo: ['ADMIN', 'ANALYST'] },
  { path: '/users', label: 'Users', visibleTo: ['ADMIN'] },
  { path: '/security', label: 'Security', visibleTo: ['ADMIN'] },
] as const

export function navItemsForRole(role: Role | null | undefined): NavItem[] {
  if (!role) return []
  return NAV_ITEMS.filter((item) => item.visibleTo.includes(role))
}

export function isRoleAllowed(role: Role | null | undefined, allowed: readonly Role[]): boolean {
  if (!role) return false
  return allowed.includes(role)
}

export function roleDisplayLabel(role: Role): string {
  switch (role) {
    case 'ADMIN':
      return 'Administrator'
    case 'ANALYST':
      return 'Analyst'
    case 'VIEWER':
      return 'Viewer'
  }
}
