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
  { path: '/cloud-ai', label: 'AI FinOps', visibleTo: ['ADMIN', 'ANALYST'] },
  { path: '/conversations', label: 'Conversations', visibleTo: ['ADMIN', 'ANALYST'] },
  { path: '/users', label: 'Users', visibleTo: ['ADMIN'] },
  { path: '/accounts', label: 'Accounts', visibleTo: ['ADMIN'] },
  { path: '/security', label: 'Security', visibleTo: ['ADMIN'] },
] as const

export function navItemsForRole(role: Role | null | undefined): NavItem[] {
  if (!role) return []
  return NAV_ITEMS.filter((item) => item.visibleTo.includes(role))
}

/**
 * Phase 7C.2A — grouped navigation config.
 *
 * The flat `NAV_ITEMS` list is the source of truth for routes.  The
 * primary navigation row in `TopNavigation` renders these items as
 * either direct links (Dashboard, AI Cost Analyst, Conversations)
 * or as dropdowns (FinOps, Administration) to reduce horizontal
 * pressure.  This grouping config is the single source of truth for
 * which items belong inside which dropdown — `TopNavigation` and
 * `MobileNavigation` both consume it so the two views stay in lock
 * step.
 */
export interface NavGroup {
  /** Visible trigger label. */
  label: string
  /** Paths whose item belongs inside this group. */
  paths: readonly string[]
  /**
   * When non-empty, only roles in this list can SEE the group at
   * all (e.g. Administration is admin-only).  When empty, the
   * group is visible to every authenticated user.
   */
  visibleTo: readonly Role[]
}

export const FINOPS_GROUP: NavGroup = {
  label: 'FinOps',
  paths: ['/costs', '/resources', '/optimization'],
  visibleTo: ALL_ROLES,
}

export const ADMIN_GROUP: NavGroup = {
  label: 'Administration',
  paths: ['/users', '/accounts', '/security'],
  visibleTo: ['ADMIN'],
}

/**
 * The direct-link items that remain visible in the primary
 * navigation row alongside the dropdowns.  These are routes that
 * deserve their own top-level slot — Dashboard is the home page,
 * AI Cost Analyst and Conversations are conversational surfaces
 * that don't fit inside the FinOps grouping.
 */
export const PRIMARY_DIRECT_PATHS: readonly string[] = [
  '/',
  '/analyst',
  '/conversations',
]

export function isPathInGroup(pathname: string, group: NavGroup): boolean {
  return group.paths.some((p) =>
    p === '/' ? pathname === '/' : pathname === p || pathname.startsWith(p + '/'),
  )
}

export function navGroupsForRole(role: Role | null | undefined): NavGroup[] {
  if (!role) return []
  const all: NavGroup[] = [FINOPS_GROUP, ADMIN_GROUP]
  return all.filter((g) => g.visibleTo.includes(role))
}

export function navGroupItemsForRole(
  group: NavGroup,
  role: Role | null | undefined,
): NavItem[] {
  if (!role) return []
  if (!group.visibleTo.includes(role)) return []
  const allowed = new Set(group.paths)
  return NAV_ITEMS.filter(
    (item) => allowed.has(item.path) && item.visibleTo.includes(role),
  )
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
