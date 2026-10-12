// Phase 7B — ADMIN-only cloud accounts registry page.
//
// Account lifecycle:
//   * ENABLED rows expose Validate + Disable (destructive — confirms).
//   * DISABLED rows expose Validate + Enable.
//   * Management rows are immutable — the backend rejects any
//     disable / is_management=false change.  The UI never exposes
//     the destructive action so the operator cannot trigger the
//     error path in the first place.
//
// Status presentation covers the full Phase 7B enum:
//   CONNECTED | ENABLED | DISABLED | ACCESS_DENIED | ROLE_NOT_FOUND | ERROR
//
// Auth bootstrap gate (Phase 7B runtime closure): no anonymous /
// pre-auth fetch ever fires.

import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch, ApiError } from '../lib/api'
import { useAuth } from '../lib/auth'
import { useAccountContext, AccountSelection } from '../lib/accountContext'

interface CloudAccount {
  id: number
  provider: string
  account_id: string
  display_name: string
  role_arn: string
  enabled: boolean
  is_management: boolean
  preferred_region: string | null
  created_at: string
  updated_at: string
}

interface CreateFormState {
  account_id: string
  display_name: string
  role_arn: string
  external_id: string
  preferred_region: string
}

interface ValidateStatus {
  status: string
  reason?: string | null
  arn?: string | null
  region?: string | null
}

const EMPTY_FORM: CreateFormState = {
  account_id: '',
  display_name: '',
  role_arn: '',
  external_id: '',
  preferred_region: '',
}

type StatusTone = 'success' | 'neutral' | 'danger' | 'info'

function statusTone(status: string | undefined): StatusTone {
  switch (status) {
    case 'CONNECTED':
      return 'success'
    case 'ENABLED':
      return 'success'
    case 'DISABLED':
      return 'neutral'
    case 'ACCESS_DENIED':
      return 'danger'
    case 'ROLE_NOT_FOUND':
      return 'danger'
    default:
      return 'info'
  }
}

export function AccountsPage() {
  const { ready: authReady, user } = useAuth()
  const { selection, refreshAccounts } = useAccountContext()
  const [accounts, setAccounts] = useState<CloudAccount[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [form, setForm] = useState<CreateFormState>(EMPTY_FORM)
  const [formError, setFormError] = useState<string | null>(null)
  const [statusById, setStatusById] = useState<Record<number, ValidateStatus>>({})
  const [busyId, setBusyId] = useState<number | null>(null)
  const [pendingDisable, setPendingDisable] = useState<CloudAccount | null>(null)
  const [feedback, setFeedback] = useState<{ kind: 'enable' | 'disable' | 'create'; text: string } | null>(null)
  const feedbackTimerRef = useRef<number | null>(null)

  const canFetch = authReady && user !== null

  const refresh = useCallback(async () => {
    if (!canFetch) return
    setLoading(true)
    setError(null)
    try {
      const data = await apiFetch<{ items: CloudAccount[]; count: number }>(
        '/api/aws/cloud-accounts',
      )
      setAccounts(data.items ?? [])
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [canFetch])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // After we change a registry row's enabled flag we want the
  // selector (and the All-Accounts aggregate) to update immediately
  // — refresh the shared account context so the next page hits a
  // fresh /selectable response.
  useEffect(() => {
    return () => {
      if (feedbackTimerRef.current !== null) {
        window.clearTimeout(feedbackTimerRef.current)
      }
    }
  }, [])

  function flashFeedback(kind: 'enable' | 'disable' | 'create', text: string) {
    setFeedback({ kind, text })
    if (feedbackTimerRef.current !== null) {
      window.clearTimeout(feedbackTimerRef.current)
    }
    feedbackTimerRef.current = window.setTimeout(() => {
      setFeedback(null)
      feedbackTimerRef.current = null
    }, 4000)
  }

  async function submit(ev: React.FormEvent<HTMLFormElement>) {
    ev.preventDefault()
    setFormError(null)
    if (!canFetch) {
      setFormError('authentication has not finished initializing')
      return
    }
    if (!/^\d{12}$/.test(form.account_id)) {
      setFormError('AWS account id must be exactly 12 digits')
      return
    }
    if (!/^arn:aws:iam::\d{12}:role\/[\w+=,.@-]+$/.test(form.role_arn)) {
      setFormError('role_arn must be a valid IAM role ARN')
      return
    }
    if (!form.display_name.trim()) {
      setFormError('display_name is required')
      return
    }
    try {
      const payload: Record<string, unknown> = {
        account_id: form.account_id,
        display_name: form.display_name.trim(),
        role_arn: form.role_arn,
      }
      if (form.external_id.trim()) payload.external_id = form.external_id.trim()
      if (form.preferred_region.trim())
        payload.preferred_region = form.preferred_region.trim()
      await apiFetch('/api/aws/cloud-accounts', {
        method: 'POST',
        json: payload,
      })
      setForm(EMPTY_FORM)
      flashFeedback('create', 'Account registered')
      await refresh()
      // Selection-side cache (selectable list) must be invalidated
      // so the new account appears in the selector without an F5.
      void refreshAccounts()
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : String(err))
    }
  }

  async function validate(id: number) {
    if (!canFetch) return
    setBusyId(id)
    try {
      const resp = await apiFetch<ValidateStatus>(
        `/api/aws/cloud-accounts/${id}/validate`,
        { method: 'POST' },
      )
      setStatusById((prev) => ({ ...prev, [id]: resp }))
    } catch (err) {
      setStatusById((prev) => ({
        ...prev,
        [id]: {
          status: 'ERROR',
          reason: err instanceof ApiError ? err.message : String(err),
        },
      }))
    } finally {
      setBusyId(null)
    }
  }

  async function setEnabled(row: CloudAccount, enabled: boolean) {
    if (!canFetch) return
    if (row.is_management) return  // defence in depth
    setBusyId(row.id)
    try {
      await apiFetch(`/api/aws/cloud-accounts/${row.id}`, {
        method: 'PATCH',
        json: { enabled },
      })
      flashFeedback(
        enabled ? 'enable' : 'disable',
        `${row.display_name} ${enabled ? 'enabled' : 'disabled'}`,
      )
      await refresh()
      void refreshAccounts()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusyId(null)
    }
  }

  if (!authReady) {
    return (
      <div
        className="mx-auto flex max-w-6xl flex-col gap-2 px-6 py-8"
        data-testid="accounts-page-loading"
      >
        <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">
          AWS Accounts
        </h1>
        <p className="text-sm text-slate-500 dark:text-slate-400">
          Initializing session…
        </p>
      </div>
    )
  }

  if (!user) {
    return (
      <div
        className="mx-auto flex max-w-6xl flex-col gap-2 px-6 py-8"
        data-testid="accounts-page-anonymous"
      >
        <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">
          AWS Accounts
        </h1>
        <p className="text-sm text-slate-500 dark:text-slate-400">
          You must sign in to manage AWS accounts.
        </p>
      </div>
    )
  }

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-6 px-6 py-8">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">
          AWS Accounts
        </h1>
        <p className="text-sm text-slate-600 dark:text-slate-400">
          Register target AWS accounts accessible via STS AssumeRole. Only
          metadata is stored — no long-lived AWS keys ever leave the
          management account.
        </p>
      </header>

      {feedback && (
        <div
          role="status"
          aria-live="polite"
          data-testid="accounts-feedback"
          className={
            feedback.kind === 'disable'
              ? 'rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-700 dark:bg-amber-900/40 dark:text-amber-100'
              : 'rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-900 dark:border-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-100'
          }
        >
          {feedback.text}
        </div>
      )}

      <section className="rounded-md border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-800">
        <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">
          Register an account
        </h2>
        <form
          onSubmit={submit}
          className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2"
          data-testid="register-form"
        >
          <label className="flex flex-col gap-1 text-xs font-medium text-slate-700 dark:text-slate-300">
            AWS account ID
            <input
              value={form.account_id}
              onChange={(e) => setForm({ ...form, account_id: e.target.value })}
              placeholder="111122223333"
              className="rounded-md border border-slate-300 px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-900"
              required
            />
          </label>
          <label className="flex flex-col gap-1 text-xs font-medium text-slate-700 dark:text-slate-300">
            Display name
            <input
              value={form.display_name}
              onChange={(e) => setForm({ ...form, display_name: e.target.value })}
              placeholder="HipLink Production"
              className="rounded-md border border-slate-300 px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-900"
              required
            />
          </label>
          <label className="flex flex-col gap-1 text-xs font-medium text-slate-700 dark:text-slate-300 sm:col-span-2">
            Role ARN
            <input
              value={form.role_arn}
              onChange={(e) => setForm({ ...form, role_arn: e.target.value })}
              placeholder="arn:aws:iam::111122223333:role/CostDetectiveReadOnly"
              className="rounded-md border border-slate-300 px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-900"
              required
            />
          </label>
          <label className="flex flex-col gap-1 text-xs font-medium text-slate-700 dark:text-slate-300">
            External ID (write-only)
            <input
              type="password"
              value={form.external_id}
              onChange={(e) => setForm({ ...form, external_id: e.target.value })}
              placeholder="Optional"
              className="rounded-md border border-slate-300 px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-900"
              autoComplete="off"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs font-medium text-slate-700 dark:text-slate-300">
            Preferred region (optional)
            <input
              value={form.preferred_region}
              onChange={(e) => setForm({ ...form, preferred_region: e.target.value })}
              placeholder="us-east-1"
              className="rounded-md border border-slate-300 px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-900"
            />
          </label>
          {formError && (
            <div className="sm:col-span-2 rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-800 dark:bg-rose-900/40 dark:text-rose-100">
              {formError}
            </div>
          )}
          <div className="sm:col-span-2 flex justify-end">
            <button
              type="submit"
              className="rounded-md bg-sky-600 px-4 py-2 text-sm font-semibold text-white hover:bg-sky-700"
            >
              Register account
            </button>
          </div>
        </form>
      </section>

      <section className="rounded-md border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-800">
        <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3 dark:border-slate-700">
          <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">
            Registered accounts
          </h2>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="rounded-md border border-slate-300 px-3 py-1 text-xs font-medium hover:bg-slate-50 disabled:opacity-50 dark:border-slate-600 dark:hover:bg-slate-700"
          >
            {loading ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
        {error && (
          <div className="px-4 py-3 text-sm text-rose-700 dark:text-rose-300">{error}</div>
        )}
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-600 dark:bg-slate-900/40 dark:text-slate-300">
            <tr>
              <th className="px-4 py-2 text-left">Name</th>
              <th className="px-4 py-2 text-left">Account ID</th>
              <th className="px-4 py-2 text-left">Region</th>
              <th className="px-4 py-2 text-left">Role ARN</th>
              <th className="px-4 py-2 text-left">State</th>
              <th className="px-4 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
            {accounts.map((row) => {
              const liveStatus = statusById[row.id]?.status
              return (
                <tr key={row.id} data-testid={`account-row-${row.id}`}>
                  <td className="px-4 py-2 font-medium text-slate-900 dark:text-slate-100">
                    {row.display_name}
                    {row.is_management && (
                      <span className="ml-2 rounded bg-sky-100 px-1.5 py-0.5 text-[10px] uppercase text-sky-800 dark:bg-sky-900/50 dark:text-sky-200">
                        Management
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2 font-mono text-xs">{row.account_id}</td>
                  <td className="px-4 py-2 text-xs">{row.preferred_region ?? '—'}</td>
                  <td className="max-w-xs truncate px-4 py-2 font-mono text-xs" title={row.role_arn}>
                    {row.role_arn}
                  </td>
                  <td className="px-4 py-2">
                    <div className="flex flex-col gap-1">
                      <span
                        className={
                          row.enabled
                            ? 'inline-flex w-fit rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-200'
                            : 'inline-flex w-fit rounded bg-slate-200 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-slate-700 dark:bg-slate-700 dark:text-slate-200'
                        }
                        data-testid={`account-state-${row.id}`}
                      >
                        {row.enabled ? 'enabled' : 'disabled'}
                      </span>
                      {liveStatus && (
                        <span
                          data-testid={`account-live-status-${row.id}`}
                          className={
                            liveStatus === 'CONNECTED'
                              ? 'inline-flex w-fit rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-200'
                              : liveStatus === 'DISABLED'
                                ? 'inline-flex w-fit rounded bg-slate-200 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-slate-700 dark:bg-slate-700 dark:text-slate-200'
                                : liveStatus === 'ACCESS_DENIED' || liveStatus === 'ROLE_NOT_FOUND'
                                  ? 'inline-flex w-fit rounded bg-rose-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-rose-800 dark:bg-rose-900/50 dark:text-rose-200'
                                  : liveStatus === 'ERROR'
                                    ? 'inline-flex w-fit rounded bg-rose-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-rose-800 dark:bg-rose-900/50 dark:text-rose-200'
                                    : 'inline-flex w-fit rounded bg-sky-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-sky-800 dark:bg-sky-900/50 dark:text-sky-200'
                          }
                        >
                          {liveStatus}
                        </span>
                      )}
                      {statusById[row.id]?.reason && (
                        <span className="text-[10px] text-slate-500 dark:text-slate-400">
                          {statusById[row.id]?.reason}
                        </span>
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-2 text-right">
                    <div className="flex justify-end gap-2">
                      <button
                        type="button"
                        onClick={() => void validate(row.id)}
                        disabled={busyId === row.id}
                        data-testid={`validate-${row.id}`}
                        className="rounded border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-50 dark:border-slate-600 dark:hover:bg-slate-700"
                      >
                        Validate
                      </button>
                      {!row.is_management &&
                        (row.enabled ? (
                          <button
                            type="button"
                            onClick={() => setPendingDisable(row)}
                            disabled={busyId === row.id}
                            data-testid={`disable-${row.id}`}
                            className="rounded border border-rose-300 px-2 py-1 text-xs text-rose-700 hover:bg-rose-50 disabled:opacity-50 dark:border-rose-700 dark:text-rose-300 dark:hover:bg-rose-900/40"
                          >
                            Disable
                          </button>
                        ) : (
                          <button
                            type="button"
                            onClick={() => void setEnabled(row, true)}
                            disabled={busyId === row.id}
                            data-testid={`enable-${row.id}`}
                            className="rounded border border-emerald-300 px-2 py-1 text-xs text-emerald-700 hover:bg-emerald-50 disabled:opacity-50 dark:border-emerald-700 dark:text-emerald-300 dark:hover:bg-emerald-900/40"
                          >
                            Enable
                          </button>
                        ))}
                    </div>
                  </td>
                </tr>
              )
            })}
            {!loading && accounts.length === 0 && (
              <tr>
                <td
                  colSpan={6}
                  className="px-4 py-6 text-center text-sm text-slate-500 dark:text-slate-400"
                >
                  No accounts registered yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      {pendingDisable && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="disable-confirm-title"
          data-testid="disable-confirm-modal"
          className="fixed inset-0 z-40 flex items-center justify-center bg-slate-900/40 px-4 py-8 dark:bg-slate-900/60"
        >
          <div className="w-full max-w-md rounded-lg border border-slate-200 bg-white p-5 shadow-xl dark:border-slate-700 dark:bg-slate-800">
            <h3
              id="disable-confirm-title"
              className="text-lg font-semibold text-slate-900 dark:text-slate-100"
            >
              Disable {pendingDisable.display_name}?
            </h3>
            <p className="mt-2 text-sm text-slate-600 dark:text-slate-400">
              Account <span className="font-mono">{pendingDisable.account_id}</span>{' '}
              will be excluded from the account selector and the
              All-Accounts aggregate. You can re-enable it later from
              this page.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setPendingDisable(null)}
                className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50 dark:border-slate-600 dark:hover:bg-slate-700"
                data-testid="disable-confirm-cancel"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={async () => {
                  const row = pendingDisable
                  setPendingDisable(null)
                  await setEnabled(row, false)
                }}
                className="rounded-md bg-rose-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-rose-700"
                data-testid="disable-confirm-confirm"
              >
                Disable account
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default AccountsPage
