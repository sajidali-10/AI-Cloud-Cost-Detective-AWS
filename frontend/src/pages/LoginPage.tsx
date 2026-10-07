// Phase 6A — Login page.
//
// Centered branded card patterned after the HipSignIn reference.
// Form behaviour:
//   - email + password (keyboard submission on Enter)
//   - submit disabled while in-flight
//   - generic "Invalid email or password" on 401 (does NOT reveal
//     whether the email exists)
//   - "Backend unavailable" on network failure or 5xx
//   - theme toggle accessible from the card (top-right)
//
// Security:
//   - password is NEVER logged
//   - password is never echoed in error messages
//   - no autocomplete hints beyond the standard email / current-password

import { useEffect, useId, useState, type FormEvent } from 'react'
import { ApiError } from '../lib/api'
import { useAuth, type SessionNotice } from '../lib/auth'
import { BrandHeader } from '../components/BrandHeader'
import { ThemeToggle } from '../components/ThemeToggle'
import { useNavigate, useLocation } from '../lib/router'

export function LoginPage() {
  const { login, authEnabled, notice, clearNotice, status } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const emailId = useId()
  const passwordId = useId()
  const navigate = useNavigate()
  const location = useLocation()

  // If the auth bootstrap reports AUTH_ENABLED=false, there is
  // nothing to log in to — bounce to the dashboard immediately.
  useEffect(() => {
    if (status.phase === 'disabled') {
      navigate('/', { replace: true })
    }
  }, [status.phase, navigate])

  const next = location.query.next ?? '/'

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    if (submitting) return
    setError(null)
    setSubmitting(true)
    try {
      await login(email, password)
      navigate(next, { replace: true })
    } catch (err) {
      // Sanitised message — never includes the password or backend
      // stack trace.  The api client throws ApiError with a
      // pre-cleaned message.
      if (err instanceof ApiError) {
        if (err.status === 0 || err.code === 'Unavailable') {
          setError('Backend unavailable. Please try again in a moment.')
        } else if (err.status === 401) {
          setError('Invalid email or password.')
        } else {
          setError(err.message || 'Sign-in failed. Please try again.')
        }
      } else {
        setError('Sign-in failed. Please try again.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center bg-bg px-4 py-10">
      {/* Theme toggle sits outside the card so it remains visible
          regardless of card width on small screens. */}
      <div className="absolute right-3 top-3">
        <ThemeToggle />
      </div>

      <main
        className="
          w-full max-w-md rounded-xl border border-border bg-surface
          p-6 shadow-card-md sm:p-8
        "
        data-testid="login-card"
      >
        <div className="mb-6 flex flex-col items-center text-center">
          <BrandHeader />
          <h1 className="mt-4 text-lg font-semibold text-fg-primary">
            AI Cloud Cost Detective
          </h1>
          <p className="mt-1 text-xs text-fg-muted">
            Secure AWS FinOps Intelligence
          </p>
        </div>

        {notice && (
          <SessionNoticeBanner notice={notice} onDismiss={clearNotice} />
        )}

        <form onSubmit={handleSubmit} noValidate className="space-y-4">
          <div>
            <label
              htmlFor={emailId}
              className="block text-xs font-medium text-fg-secondary"
            >
              Email
            </label>
            <input
              id={emailId}
              name="email"
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={submitting}
              className="
                mt-1 w-full rounded-md border border-border bg-bg px-3 py-2
                text-sm text-fg-primary placeholder:text-fg-muted
                focus:outline-none focus-visible:shadow-focus
                disabled:opacity-60
              "
              placeholder="you@example.com"
            />
          </div>
          <div>
            <label
              htmlFor={passwordId}
              className="block text-xs font-medium text-fg-secondary"
            >
              Password
            </label>
            <input
              id={passwordId}
              name="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={submitting}
              className="
                mt-1 w-full rounded-md border border-border bg-bg px-3 py-2
                text-sm text-fg-primary placeholder:text-fg-muted
                focus:outline-none focus-visible:shadow-focus
                disabled:opacity-60
              "
              placeholder="••••••••"
            />
          </div>

          {error && (
            <p
              role="alert"
              className="rounded-md border border-danger/30 bg-danger-soft px-3 py-2 text-xs text-danger"
              data-testid="login-error"
            >
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={submitting || !email || !password}
            data-testid="login-submit"
            className="
              inline-flex w-full items-center justify-center rounded-md
              bg-primary px-3 py-2 text-sm font-medium text-primary-foreground
              hover:bg-primary-hover focus:outline-none focus-visible:shadow-focus
              disabled:cursor-not-allowed disabled:opacity-60
            "
          >
            {submitting ? 'Signing in…' : 'Sign in'}
          </button>
        </form>

        <p className="mt-6 text-center text-[11px] text-fg-muted">
          {!authEnabled && status.phase === 'booting' ? (
            <>Checking sign-in configuration…</>
          ) : (
            <>Protected by HipLink · AI Cloud Cost Detective</>
          )}
        </p>
      </main>
    </div>
  )
}

function SessionNoticeBanner({
  notice,
  onDismiss,
}: {
  notice: SessionNotice
  onDismiss: () => void
}) {
  const tone =
    notice.kind === 'expired' ? 'border-warning/30 bg-warning-soft text-warning' :
    'border-info/30 bg-info-soft text-info'
  return (
    <div
      role="status"
      className={['mb-4 rounded-md border px-3 py-2 text-xs', tone].join(' ')}
      data-testid="session-notice"
    >
      <div className="flex items-start justify-between gap-2">
        <span>{notice.message}</span>
        <button
          type="button"
          aria-label="Dismiss notice"
          onClick={onDismiss}
          className="
            inline-flex h-5 w-5 items-center justify-center rounded
            text-current hover:bg-surface-hover
            focus:outline-none focus-visible:shadow-focus
          "
        >
          <svg aria-hidden width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M18 6 6 18" />
            <path d="m6 6 12 12" />
          </svg>
        </button>
      </div>
    </div>
  )
}
