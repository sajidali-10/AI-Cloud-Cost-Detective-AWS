// Phase 7B — ADMIN-only cloud accounts registry page.

import { useCallback, useEffect, useState } from 'react'
import { apiFetch, ApiError } from '../lib/api'

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

const EMPTY_FORM: CreateFormState = {
  account_id: '',
  display_name: '',
  role_arn: '',
  external_id: '',
  preferred_region: '',
}

export function AccountsPage() {
  const [accounts, setAccounts] = useState<CloudAccount[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [form, setForm] = useState<CreateFormState>(EMPTY_FORM)
  const [formError, setFormError] = useState<string | null>(null)
  const [statusById, setStatusById] = useState<Record<number, string>>({})
  const [busyId, setBusyId] = useState<number | null>(null)

  const refresh = useCallback(async () => {
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
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  async function submit(ev: React.FormEvent<HTMLFormElement>) {
    ev.preventDefault()
    setFormError(null)
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
      if (form.preferred_region.trim()) payload.preferred_region = form.preferred_region.trim()
      await apiFetch('/api/aws/cloud-accounts', {
        method: 'POST',
        json: payload,
      })
      setForm(EMPTY_FORM)
      await refresh()
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : String(err))
    }
  }

  async function validate(id: number) {
    setBusyId(id)
    try {
      const resp = await apiFetch<{ status: string; reason?: string }>(
        `/api/aws/cloud-accounts/${id}/validate`,
        { method: 'POST' },
      )
      setStatusById((prev) => ({
        ...prev,
        [id]: resp.reason ? `${resp.status} — ${resp.reason}` : resp.status,
      }))
    } catch (err) {
      setStatusById((prev) => ({
        ...prev,
        [id]: err instanceof ApiError ? err.message : String(err),
      }))
    } finally {
      setBusyId(null)
    }
  }

  async function disable(id: number) {
    setBusyId(id)
    try {
      await apiFetch(`/api/aws/cloud-accounts/${id}`, { method: 'DELETE' })
      await refresh()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusyId(null)
    }
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
            {accounts.map((row) => (
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
                  <span
                    className={
                      row.enabled
                        ? 'rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-200'
                        : 'rounded bg-slate-200 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-slate-700 dark:bg-slate-700 dark:text-slate-200'
                    }
                  >
                    {row.enabled ? 'enabled' : 'disabled'}
                  </span>
                  {statusById[row.id] && (
                    <div className="mt-1 text-[10px] text-slate-500 dark:text-slate-400">
                      {statusById[row.id]}
                    </div>
                  )}
                </td>
                <td className="px-4 py-2 text-right">
                  <div className="flex justify-end gap-2">
                    <button
                      type="button"
                      onClick={() => void validate(row.id)}
                      disabled={busyId === row.id}
                      className="rounded border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-50 dark:border-slate-600 dark:hover:bg-slate-700"
                    >
                      Validate
                    </button>
                    {!row.is_management && row.enabled && (
                      <button
                        type="button"
                        onClick={() => void disable(row.id)}
                        disabled={busyId === row.id}
                        className="rounded border border-rose-300 px-2 py-1 text-xs text-rose-700 hover:bg-rose-50 disabled:opacity-50 dark:border-rose-700 dark:text-rose-300 dark:hover:bg-rose-900/40"
                      >
                        Disable
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
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
    </div>
  )
}

export default AccountsPage
