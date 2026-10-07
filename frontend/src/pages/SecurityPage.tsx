// Phase 6A — Security page (read-only metadata).
//
// Displays the JWT issuer / audience / algorithm / token lifetime
// reported by GET /api/auth/info.  Read-only — no admin actions.
// Useful for operators to confirm the deployment matches their
// environment configuration.
import { useEffect, useState } from 'react'
import { ApiError, apiFetch } from '../lib/api'
import { PageHeader } from '../components/PageHeader'
import { SectionCard } from '../components/SectionCard'
import { EmptyState } from '../components/EmptyState'
import { ErrorState, BackendUnavailable } from '../components/ErrorState'
import { LoadingSkeleton } from '../components/LoadingSkeleton'

interface AuthInfo {
  auth_enabled: boolean
  issuer: string
  audience: string
  algorithm: string
  access_token_minutes: number
}

export function SecurityPage() {
  const [info, setInfo] = useState<AuthInfo | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    apiFetch<AuthInfo>('/api/auth/info', { skipAuthRedirect: true })
      .then((data) => {
        if (cancelled) return
        setInfo(data)
        setLoading(false)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof ApiError && (err.status === 0 || err.code === 'Unavailable')) {
          setError('Backend unavailable')
        } else if (err instanceof ApiError) {
          setError(err.message)
        } else {
          setError('Unable to load security metadata')
        }
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="Security"
        subtitle="Authentication and token metadata"
      />
      <SectionCard title="Authentication metadata" description="Read-only — sourced from /api/auth/info">
        {loading ? (
          <div className="space-y-2">
            <LoadingSkeleton className="h-4 w-40" />
            <LoadingSkeleton className="h-4 w-72" />
            <LoadingSkeleton className="h-4 w-56" />
          </div>
        ) : error ? (
          error === 'Backend unavailable' ? (
            <BackendUnavailable onRetry={() => setReloadKey((n) => n + 1)} />
          ) : (
            <ErrorState message={error} />
          )
        ) : info ? (
          <dl className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <Field k="Auth enabled" v={String(info.auth_enabled)} />
            <Field k="Algorithm" v={info.algorithm} />
            <Field k="Issuer" v={info.issuer} />
            <Field k="Audience" v={info.audience} />
            <Field k="Access token lifetime" v={`${info.access_token_minutes} min`} />
          </dl>
        ) : (
          <EmptyState title="No data loaded" />
        )}
      </SectionCard>
    </div>
  )
}

function Field({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex items-baseline justify-between border-b border-border py-1 text-sm">
      <dt className="text-fg-muted">{k}</dt>
      <dd className="font-mono text-fg-primary">{v}</dd>
    </div>
  )
}
