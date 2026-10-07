// Phase 6A — Router tests.
import { describe, expect, it } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {
  Router,
  Routes,
  Link,
  Navigate,
  useNavigate,
  useParams,
  __routerTestInternals,
  type RouteSpec,
} from '../lib/router'

function renderAt(path: string, routes: RouteSpec[]) {
  window.history.replaceState({}, '', path)
  return render(
    <Router>
      <Routes routes={routes} />
    </Router>,
  )
}

describe('Router internals', () => {
  it('parseQuery extracts search params', () => {
    const out = __routerTestInternals.parseQuery('?a=1&b=hello%20world')
    expect(out).toEqual({ a: '1', b: 'hello world' })
  })

  it('matchRoute picks the most specific path', () => {
    const routes: RouteSpec[] = [
      { path: '/', element: <div>root</div> },
      { path: '/conversations', element: <div>list</div> },
      { path: '/conversations/:id', element: <div>detail</div> },
    ]
    expect(__routerTestInternals.matchRoute(routes, '/')?.path).toBe('/')
    expect(__routerTestInternals.matchRoute(routes, '/conversations')?.path).toBe(
      '/conversations',
    )
    expect(__routerTestInternals.matchRoute(routes, '/conversations/42')?.path).toBe(
      '/conversations/:id',
    )
  })
})

describe('Router runtime', () => {
  it('renders the matching route', () => {
    renderAt('/', [
      { path: '/', element: <div>home</div> },
      { path: '/about', element: <div>about</div> },
    ])
    expect(screen.getByText('home')).toBeInTheDocument()
  })

  it('Link navigates without a full page reload', async () => {
    const user = userEvent.setup()
    render(
      <Router>
        <Routes
          routes={[
            { path: '/', element: <Link to="/about">go</Link> },
            { path: '/about', element: <div>about-page</div> },
          ]}
        />
      </Router>,
    )
    await user.click(screen.getByText('go'))
    expect(screen.getByText('about-page')).toBeInTheDocument()
    expect(window.location.pathname).toBe('/about')
  })

  it('Navigate performs a redirect', () => {
    renderAt('/old', [
      { path: '/old', element: <Navigate to="/new" replace /> },
      { path: '/new', element: <div>new-page</div> },
    ])
    expect(screen.getByText('new-page')).toBeInTheDocument()
    expect(window.location.pathname).toBe('/new')
  })

  it('renders NotFound when no route matches', () => {
    renderAt('/nowhere', [{ path: '/', element: <div>home</div> }])
    expect(screen.getByText('Page not found')).toBeInTheDocument()
  })

  it('renders AccessDenied when currentRole is not in requiredRoles', () => {
    renderAt('/admin', [
      { path: '/admin', requiredRoles: ['ADMIN'], element: <div>secret</div> },
    ])
    // No currentRole prop = treated as null/unauthenticated -> denied.
    expect(screen.getByText('Access denied')).toBeInTheDocument()
  })

  it('renders AccessDenied when the role is not in requiredRoles', () => {
    window.history.replaceState({}, '', '/admin')
    render(
      <Router>
        <Routes
          currentRole={'VIEWER' as never}
          routes={[{ path: '/admin', requiredRoles: ['ADMIN'], element: <div>secret</div> }]}
        />
      </Router>,
    )
    expect(screen.getByText('Access denied')).toBeInTheDocument()
  })

  it('allows access when the role matches', () => {
    window.history.replaceState({}, '', '/admin')
    render(
      <Router>
        <Routes
          currentRole={'ADMIN' as never}
          routes={[{ path: '/admin', requiredRoles: ['ADMIN'], element: <div>secret</div> }]}
        />
      </Router>,
    )
    expect(screen.getByText('secret')).toBeInTheDocument()
  })

  it('captures :param via useParams', () => {
    window.history.replaceState({}, '', '/conversations/abc-123')
    function Page() {
      const params = useParams()
      return <div>id={params.id ?? 'none'}</div>
    }
    render(
      <Router>
        <Routes routes={[{ path: '/conversations/:id', element: <Page /> }]} />
      </Router>,
    )
    expect(screen.getByText('id=abc-123')).toBeInTheDocument()
  })

  it('useNavigate triggers a route change', () => {
    function Trigger() {
      const nav = useNavigate()
      return (
        <div>
          <button onClick={() => nav('/y')}>go-y</button>
        </div>
      )
    }
    render(
      <Router>
        <Routes
          routes={[
            { path: '/', element: <Trigger /> },
            { path: '/y', element: <div>Y</div> },
          ]}
        />
      </Router>,
    )
    expect(screen.getByText('go-y')).toBeInTheDocument()
    act(() => {
      screen.getByText('go-y').click()
    })
    expect(screen.getByText('Y')).toBeInTheDocument()
    expect(window.location.pathname).toBe('/y')
  })
})
