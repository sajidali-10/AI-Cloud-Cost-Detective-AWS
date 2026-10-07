// Phase 6A — Minimal in-house router.
//
// Phase 6A deliberately does NOT introduce a third-party routing
// dependency.  This ~140-line implementation provides everything the
// spec needs:
//
//   * <Routes routes={...}> — declarative route registry (flat array).
//   * <Navigate to="..." replace> — imperative redirect.
//   * <Router> root — listens to popstate and dispatches state updates
//                     after pushState (which itself does not fire
//                     popstate).
//   * useLocation() — current pathname + parsed query.
//   * useNavigate() — typed navigate helper.
//   * useParams()   — :param captures for the matched route.
//   * <Link>        — accessible <a> wrapper that uses pushState.
//
// Each route declares `requiredRoles?: RoleName[]`; the route tree
// renders <AccessDeniedInline> when the auth role is not in the list.
//
// Routes are matched longest-first (by segment count, then by length)
// so `/conversations/:id` beats `/conversations`.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type AnchorHTMLAttributes,
  type MouseEvent,
  type ReactNode,
} from 'react'

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export interface RouterLocation {
  pathname: string
  search: string
  hash: string
  query: Record<string, string>
}

interface NavigateOptions {
  replace?: boolean
}

type NavigateFn = (to: string, opts?: NavigateOptions) => void

interface RouterContextValue {
  location: RouterLocation
  navigate: NavigateFn
}

export type RoleName = 'ADMIN' | 'ANALYST' | 'VIEWER'

export interface RouteSpec {
  path: string
  /** Roles allowed to view; omitted = open to all authenticated users. */
  requiredRoles?: RoleName[]
  element: ReactNode
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function parseQuery(search: string): Record<string, string> {
  const out: Record<string, string> = {}
  if (!search) return out
  const params = new URLSearchParams(search)
  params.forEach((value, key) => {
    out[key] = value
  })
  return out
}

function readLocation(): RouterLocation {
  const url = new URL(window.location.href)
  return {
    pathname: url.pathname || '/',
    search: url.search,
    hash: url.hash,
    query: parseQuery(url.search),
  }
}

function pathToRegex(path: string): {
  regex: RegExp
  keys: string[]
  isWildcard: boolean
} {
  // "*" or "/*" matches any path.  This is intentionally the LAST
  // route in the registry — used as a 404 fallback.
  if (path === '*' || path === '/*') {
    return { regex: /^.*$/, keys: [], isWildcard: true }
  }
  const keys: string[] = []
  const escaped = path
    .split('/')
    .map((seg) => {
      if (seg.startsWith(':')) {
        keys.push(seg.slice(1))
        return '([^/]+)'
      }
      return seg.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    })
    .join('/')
  const re = new RegExp('^' + escaped + '/?$')
  return { regex: re, keys, isWildcard: false }
}

// ---------------------------------------------------------------------------
// Router provider
// ---------------------------------------------------------------------------

const RouterContext = createContext<RouterContextValue | null>(null)

export function Router({ children }: { children: ReactNode }) {
  const [location, setLocation] = useState<RouterLocation>(() => readLocation())

  const navigate = useCallback<NavigateFn>((to, opts) => {
    const url = new URL(to, window.location.origin)
    const path = url.pathname + url.search + url.hash
    if (opts?.replace) {
      window.history.replaceState({}, '', path)
    } else {
      window.history.pushState({}, '', path)
    }
    setLocation(readLocation())
    // pushState does not fire popstate; dispatch manually so listeners
    // (including external subscribers) stay in sync.
    window.dispatchEvent(new PopStateEvent('popstate'))
  }, [])

  useEffect(() => {
    const onPop = () => setLocation(readLocation())
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  const value = useMemo<RouterContextValue>(
    () => ({ location, navigate }),
    [location, navigate],
  )

  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>
}

export function useLocation(): RouterLocation {
  const ctx = useContext(RouterContext)
  if (!ctx) throw new Error('useLocation must be used within a Router')
  return ctx.location
}

export function useNavigate(): NavigateFn {
  const ctx = useContext(RouterContext)
  if (!ctx) throw new Error('useNavigate must be used within a Router')
  return ctx.navigate
}

// ---------------------------------------------------------------------------
// Match context (drives useParams)
// ---------------------------------------------------------------------------

interface MatchContextValue {
  path: string
  params: Record<string, string>
}

const MatchContext = createContext<MatchContextValue | null>(null)

export type ParamsMap = Record<string, string>

export function useParams(): ParamsMap {
  const match = useContext(MatchContext)
  return match?.params ?? {}
}

// ---------------------------------------------------------------------------
// <Routes>
// ---------------------------------------------------------------------------

function matchRoute(routes: RouteSpec[], pathname: string): RouteSpec | null {
  // Wildcard "*" routes go last.
  const nonWild: RouteSpec[] = []
  const wild: RouteSpec[] = []
  for (const r of routes) {
    if (r.path === '*' || r.path === '/*') wild.push(r)
    else nonWild.push(r)
  }
  const sorted = [...nonWild].sort((a, b) => {
    const aSeg = a.path.split('/').length
    const bSeg = b.path.split('/').length
    if (aSeg !== bSeg) return bSeg - aSeg
    return b.path.length - a.path.length
  })
  for (const r of sorted) {
    const { regex } = pathToRegex(r.path)
    if (regex.test(pathname)) return r
  }
  // First wildcard in registration order.
  return wild[0] ?? null
}

export interface RoutesProps {
  routes: RouteSpec[]
  currentRole?: RoleName | null
}

export function Routes({ routes, currentRole }: RoutesProps) {
  const { pathname } = useLocation()
  const matched = matchRoute(routes, pathname)

  if (!matched) {
    return <NotFound />
  }
  if (matched.requiredRoles && matched.requiredRoles.length > 0) {
    if (!currentRole || !matched.requiredRoles.includes(currentRole)) {
      return <AccessDeniedInline requiredRoles={matched.requiredRoles} />
    }
  }
  const { regex, keys, isWildcard } = pathToRegex(matched.path)
  const m = isWildcard ? null : regex.exec(pathname)
  const params: Record<string, string> = {}
  if (m) {
    keys.forEach((k, i) => {
      params[k] = decodeURIComponent(m[i + 1] ?? '')
    })
  }
  return (
    <MatchContext.Provider value={{ path: matched.path, params }}>
      {matched.element}
    </MatchContext.Provider>
  )
}

// ---------------------------------------------------------------------------
// <Navigate> — declarative redirect.
// ---------------------------------------------------------------------------

export function Navigate({ to, replace }: { to: string; replace?: boolean }): null {
  const navigate = useNavigate()
  useEffect(() => {
    navigate(to, { replace })
    // Intentionally run once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  return null
}

// ---------------------------------------------------------------------------
// <Link> — accessible anchor that uses pushState.
// ---------------------------------------------------------------------------

export interface LinkProps extends Omit<AnchorHTMLAttributes<HTMLAnchorElement>, 'href'> {
  to: string
  replace?: boolean
}

export function Link({ to, replace, onClick, ...rest }: LinkProps) {
  const navigate = useNavigate()
  const handle = (e: MouseEvent<HTMLAnchorElement>) => {
    if (e.defaultPrevented) return
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
    if (e.button !== 0) return
    onClick?.(e)
    if (e.defaultPrevented) return
    e.preventDefault()
    navigate(to, { replace })
  }
  return <a href={to} onClick={handle} {...rest} />
}

// ---------------------------------------------------------------------------
// Fallback views
// ---------------------------------------------------------------------------

export function NotFound() {
  return (
    <main className="flex min-h-[50vh] flex-col items-center justify-center px-6 text-center">
      <h1 className="text-2xl font-semibold text-fg-primary">Page not found</h1>
      <p className="mt-2 text-sm text-fg-secondary">
        The page you requested does not exist.
      </p>
      <Link to="/" className="mt-4 text-sm text-primary hover:underline">
        Return to dashboard
      </Link>
    </main>
  )
}

function AccessDeniedInline({ requiredRoles }: { requiredRoles: RoleName[] }) {
  return (
    <main className="flex min-h-[50vh] flex-col items-center justify-center px-6 text-center">
      <h1 className="text-2xl font-semibold text-fg-primary">Access denied</h1>
      <p className="mt-2 text-sm text-fg-secondary">
        Your role does not have permission to view this page.
      </p>
      <p className="mt-1 text-xs text-fg-muted">
        Required role{requiredRoles.length > 1 ? 's' : ''}: {requiredRoles.join(', ')}
      </p>
      <Link to="/" className="mt-4 text-sm text-primary hover:underline">
        Return to dashboard
      </Link>
    </main>
  )
}

// Exported for tests.
export const __routerTestInternals = { matchRoute, pathToRegex, parseQuery, readLocation }
