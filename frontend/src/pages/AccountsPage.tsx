// Phase 7B — ADMIN-only cloud accounts registry page.
//
// Phase 7C.2A — provider-aware lifecycle:
//   * AWS rows: AssumeRole registration (account_id, role_arn,
//     external_id, preferred_region).
//   * Azure rows: subscription registration (subscription GUID,
//     optional tenant GUID).  No IAM role, no client secret —
//     authentication is workload-federation only.
//   * The "Register" form has a provider toggle so the rendered
//     fields always match the destination adapter's schema.
//   * The table groups rows by provider (AWS first, Azure second)
//     so an operator can never confuse the two lifecycles.
//
// AWS account lifecycle (unchanged):
//   * Management row → Edit metadata only.  No Disable / no Delete.
//   * Enabled target row → Edit / Validate / Disable.
//   * Disabled target row → Edit / Validate / Enable / Delete.
//
// Azure subscription lifecycle (Phase 7C.2A):
//   * Every Azure row is a non-management row.
//   * ENABLED row → Edit metadata / Validate (live ARM call).
//   * DISABLED row → Edit / Validate / Enable / Delete.
//
// Auth bootstrap gate (Phase 7B): no anonymous fetch ever fires.

import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch, ApiError } from '../lib/api'
import { useAuth } from '../lib/auth'
import { useAccountContext } from '../lib/accountContext'
import { accountMatchesSelector, type CanonicalProvider } from '../lib/finops/subscriptionSelection'
import { useProviderContext } from '../lib/providerContext'

interface CloudAccount {
  id: number
  provider: string
  account_id: string
  display_name: string
  role_arn: string | null
  enabled: boolean
  is_management: boolean
  tenant_id: string | null
  subscription_id: string | null
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
  tenant_id: string
  subscription_id: string
}

interface EditFormState {
  display_name: string
  role_arn: string
  external_id: string
  preferred_region: string
  tenant_id: string
  subscription_id: string
}

interface ValidateStatus {
  status: string
  reason?: string | null
  arn?: string | null
  region?: string | null
}

type FormProvider = 'AWS' | 'AZURE'

const EMPTY_FORM: CreateFormState = {
  account_id: '',
  display_name: '',
  role_arn: '',
  external_id: '',
  preferred_region: '',
  tenant_id: '',
  subscription_id: '',
}

const EMPTY_EDIT: EditFormState = {
  display_name: '',
  role_arn: '',
  external_id: '',
  preferred_region: '',
  tenant_id: '',
  subscription_id: '',
}

// GUID validator shared by every Azure form input.  Accepts both
// upper and lower case; the backend normalises to lower case.
const GUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

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
    case 'AUTH_FAILED':
      return 'danger'
    case 'NOT_FOUND':
      return 'danger'
    default:
      return 'info'
  }
}

export function AccountsPage() {
  const { ready: authReady, user } = useAuth()
  const { selection, refreshAccounts, setSelection } = useAccountContext()
  const { provider: activeProvider, isProviderReady } = useProviderContext()
  const [accounts, setAccounts] = useState<CloudAccount[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [formProvider, setFormProvider] = useState<FormProvider>('AWS')
  const [form, setForm] = useState<CreateFormState>(EMPTY_FORM)
  const [formError, setFormError] = useState<string | null>(null)
  const [editing, setEditing] = useState<CloudAccount | null>(null)
  const [editForm, setEditForm] = useState<EditFormState>(EMPTY_EDIT)
  const [editError, setEditError] = useState<string | null>(null)
  const [statusById, setStatusById] = useState<Record<number, ValidateStatus>>({})
  const [busyId, setBusyId] = useState<number | null>(null)
  const [pendingDisable, setPendingDisable] = useState<CloudAccount | null>(null)
  const [pendingDelete, setPendingDelete] = useState<CloudAccount | null>(null)
  const [feedback, setFeedback] = useState<
    { kind: 'enable' | 'disable' | 'create' | 'update' | 'delete'; text: string } | null
  >(null)
  const feedbackTimerRef = useRef<number | null>(null)

  const canFetch = authReady && user !== null
  const azureReady = isProviderReady('AZURE')

  const refresh = useCallback(async () => {
    if (!canFetch) return
    setLoading(true)
    setError(null)
    try {
      // Fetch AWS and Azure rows separately so the table can render
      // both provider groups.  Both calls run in parallel.
      const [awsRows, azureRows] = await Promise.all([
        apiFetch<{ items: CloudAccount[]; count: number }>(
          '/api/aws/cloud-accounts?provider=AWS',
        ),
        apiFetch<{ items: CloudAccount[]; count: number }>(
          '/api/aws/cloud-accounts?provider=AZURE',
        ),
      ])
      setAccounts([...(awsRows.items ?? []), ...(azureRows.items ?? [])])
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [canFetch])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // Track which provider the form is configured for so a stale
  // AWS form cannot be submitted with provider=AZURE.  The form
  // provider follows the active provider by default but the
  // operator can flip it explicitly.
  useEffect(() => {
    setFormProvider(activeProvider === 'AZURE' ? 'AZURE' : 'AWS')
    setForm(EMPTY_FORM)
    setFormError(null)
  }, [activeProvider])

  useEffect(() => {
    return () => {
      if (feedbackTimerRef.current !== null) {
        window.clearTimeout(feedbackTimerRef.current)
      }
    }
  }, [])

  function flashFeedback(
    kind: 'enable' | 'disable' | 'create' | 'update' | 'delete',
    text: string,
  ) {
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
    if (!form.display_name.trim()) {
      setFormError('display_name is required')
      return
    }

    if (formProvider === 'AWS') {
      // AWS path — unchanged from Phase 7B.
      if (!/^\d{12}$/.test(form.account_id)) {
        setFormError('AWS account id must be exactly 12 digits')
        return
      }
      if (!/^arn:aws:iam::\d{12}:role\/[\w+=,.@-]+$/.test(form.role_arn)) {
        setFormError('role_arn must be a valid IAM role ARN')
        return
      }
      const payload: Record<string, unknown> = {
        provider: 'AWS',
        account_id: form.account_id,
        display_name: form.display_name.trim(),
        role_arn: form.role_arn,
      }
      if (form.external_id.trim()) payload.external_id = form.external_id.trim()
      if (form.preferred_region.trim())
        payload.preferred_region = form.preferred_region.trim()
      try {
        await apiFetch('/api/aws/cloud-accounts', {
          method: 'POST',
          json: payload,
        })
        setForm(EMPTY_FORM)
        flashFeedback('create', 'AWS account registered')
        await refresh()
        void refreshAccounts()
      } catch (err) {
        setFormError(err instanceof ApiError ? err.message : String(err))
      }
      return
    }

    // AZURE path — Phase 7C.2A.
    if (!GUID_RE.test(form.account_id)) {
      setFormError('Azure subscription id must be a GUID')
      return
    }
    const payload: Record<string, unknown> = {
      provider: 'AZURE',
      account_id: form.account_id,
      display_name: form.display_name.trim(),
    }
    if (form.tenant_id.trim()) {
      if (!GUID_RE.test(form.tenant_id)) {
        setFormError('Azure tenant id must be a GUID')
        return
      }
      payload.tenant_id = form.tenant_id.trim()
    }
    // subscription_id defaults to account_id on the server when
    // omitted; forward explicitly when supplied.
    payload.subscription_id = form.subscription_id.trim() || form.account_id
    try {
      await apiFetch('/api/aws/cloud-accounts', {
        method: 'POST',
        json: payload,
      })
      setForm(EMPTY_FORM)
      flashFeedback('create', 'Azure subscription registered')
      await refresh()
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
    if (row.is_management) return // defence in depth
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

  async function permanentlyDelete(row: CloudAccount) {
    if (!canFetch) return
    if (row.is_management) return // defence in depth — backend also refuses
    if (row.enabled) return // defence in depth — backend also refuses
    setBusyId(row.id)
    try {
      await apiFetch(`/api/aws/cloud-accounts/${row.id}`, {
        method: 'DELETE',
      })
      flashFeedback(
        'delete',
        `${row.display_name} permanently deleted`,
      )
      // Phase 9D.1 — the selection carries the CLOUD-side account
      // id (acct:<account_id> / sub:<guid>), not the local row id,
      // so match on account_id through the shared helper.
      const rowProvider: CanonicalProvider | null =
        row.provider === 'AWS' || row.provider === 'AZURE' || row.provider === 'RACKSPACE'
          ? row.provider
          : null
      if (rowProvider && accountMatchesSelector(row, selection, rowProvider)) {
        setSelection('all')
      }
      await refresh()
      void refreshAccounts()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusyId(null)
    }
  }

  function openEdit(row: CloudAccount) {
    setEditing(row)
    setEditForm({
      display_name: row.display_name,
      role_arn: row.role_arn ?? '',
      external_id: '',
      preferred_region: row.preferred_region ?? '',
      tenant_id: row.tenant_id ?? '',
      subscription_id: row.subscription_id ?? row.account_id,
    })
    setEditError(null)
  }

  function cancelEdit() {
    setEditing(null)
    setEditForm(EMPTY_EDIT)
    setEditError(null)
  }

  async function saveEdit(ev: React.FormEvent<HTMLFormElement>) {
    ev.preventDefault()
    if (!editing) return
    if (!canFetch) {
      setEditError('authentication has not finished initializing')
      return
    }
    if (!editForm.display_name.trim()) {
      setEditError('display_name is required')
      return
    }

    const payload: Record<string, unknown> = {
      display_name: editForm.display_name.trim(),
    }

    if (editing.provider === 'AZURE') {
      // Azure rows: tenant_id / subscription_id are editable;
      // role_arn / external_id are NOT.
      if (editForm.tenant_id.trim()) {
        if (!GUID_RE.test(editForm.tenant_id)) {
          setEditError('Azure tenant id must be a GUID')
          return
        }
        payload.tenant_id = editForm.tenant_id.trim()
      }
      if (editForm.subscription_id.trim()) {
        if (!GUID_RE.test(editForm.subscription_id)) {
          setEditError('Azure subscription id must be a GUID')
          return
        }
        payload.subscription_id = editForm.subscription_id.trim()
      }
    } else if (editing.is_management) {
      // Management AWS rows: display_name + preferred_region only.
      if (editForm.preferred_region.trim()) {
        payload.preferred_region = editForm.preferred_region.trim()
      } else {
        payload.preferred_region = ''
      }
    } else {
      // Target AWS rows: validate role_arn; honour preferred_region +
      // optional external_id replacement.
      if (!/^arn:aws:iam::\d{12}:role\/[\w+=,.@-]+$/.test(editForm.role_arn)) {
        setEditError('role_arn must be a valid IAM role ARN')
        return
      }
      payload.role_arn = editForm.role_arn
      if (editForm.preferred_region.trim()) {
        payload.preferred_region = editForm.preferred_region.trim()
      } else {
        payload.preferred_region = ''
      }
      if (editForm.external_id.trim()) {
        payload.external_id = editForm.external_id.trim()
      }
    }

    setBusyId(editing.id)
    try {
      await apiFetch(`/api/aws/cloud-accounts/${editing.id}`, {
        method: 'PATCH',
        json: payload,
      })
      flashFeedback('update', `${editing.display_name} updated`)
      cancelEdit()
      await refresh()
      void refreshAccounts()
    } catch (err) {
      setEditError(err instanceof ApiError ? err.message : String(err))
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
          Cloud Accounts
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
          Cloud Accounts
        </h1>
        <p className="text-sm text-slate-500 dark:text-slate-400">
          You must sign in to manage cloud accounts.
        </p>
      </div>
    )
  }

  const awsRows = accounts.filter((a) => a.provider === 'AWS')
  const azureRows = accounts.filter((a) => a.provider === 'AZURE')

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-6 px-6 py-8">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">
          Cloud Accounts
        </h1>
        <p className="text-sm text-slate-600 dark:text-slate-400">
          Register AWS accounts (AssumeRole) and Azure subscriptions
          (workload-federation). Only metadata is stored — no long-lived
          credentials ever leave the management account.
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

      <section
        className="rounded-md border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-800"
        data-testid="register-section"
      >
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">
            Register an account
          </h2>
          <div className="inline-flex rounded-md border border-slate-300 dark:border-slate-600">
            <button
              type="button"
              onClick={() => {
                setFormProvider('AWS')
                setFormError(null)
              }}
              data-testid="register-provider-aws"
              className={
                formProvider === 'AWS'
                  ? 'rounded-l-md bg-sky-600 px-3 py-1 text-xs font-semibold text-white'
                  : 'px-3 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50 dark:text-slate-300 dark:hover:bg-slate-700'
              }
            >
              AWS account
            </button>
            <button
              type="button"
              onClick={() => {
                if (!azureReady) return
                setFormProvider('AZURE')
                setFormError(null)
              }}
              disabled={!azureReady}
              title={
                azureReady
                  ? 'Register an Azure subscription'
                  : 'Azure is not configured in this deployment'
              }
              data-testid="register-provider-azure"
              className={
                formProvider === 'AZURE'
                  ? 'rounded-r-md bg-sky-600 px-3 py-1 text-xs font-semibold text-white disabled:opacity-50'
                  : 'rounded-r-md px-3 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50 dark:text-slate-300 dark:hover:bg-slate-700'
              }
            >
              Azure subscription
            </button>
          </div>
        </div>

        {!azureReady && (
          <p
            className="mt-2 text-[11px] text-slate-500 dark:text-slate-400"
            data-testid="azure-not-configured-hint"
          >
            Azure registration is unavailable because the Azure runtime is
            not configured (AZURE_TENANT_ID / AZURE_CLIENT_ID missing).
            The Azure adapter is registered as disabled until that is
            resolved.
          </p>
        )}

        <form
          onSubmit={submit}
          className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2"
          data-testid="register-form"
        >
          {formProvider === 'AWS' ? (
            <>
              <label className="flex flex-col gap-1 text-xs font-medium text-slate-700 dark:text-slate-300">
                AWS account ID
                <input
                  value={form.account_id}
                  onChange={(e) => setForm({ ...form, account_id: e.target.value })}
                  placeholder="111122223333"
                  className="rounded-md border border-slate-300 px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-900"
                  required
                  data-testid="register-aws-account-id"
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
                  data-testid="register-aws-display-name"
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
                  data-testid="register-aws-role-arn"
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
                  data-testid="register-aws-external-id"
                />
              </label>
              <label className="flex flex-col gap-1 text-xs font-medium text-slate-700 dark:text-slate-300">
                Preferred region (optional)
                <input
                  value={form.preferred_region}
                  onChange={(e) =>
                    setForm({ ...form, preferred_region: e.target.value })
                  }
                  placeholder="us-east-1"
                  className="rounded-md border border-slate-300 px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-900"
                  data-testid="register-aws-preferred-region"
                />
              </label>
            </>
          ) : (
            <>
              <label className="flex flex-col gap-1 text-xs font-medium text-slate-700 dark:text-slate-300 sm:col-span-2">
                Azure subscription GUID
                <input
                  value={form.account_id}
                  onChange={(e) => setForm({ ...form, account_id: e.target.value })}
                  placeholder="00000000-0000-0000-0000-000000000000"
                  className="rounded-md border border-slate-300 px-3 py-2 text-sm font-mono dark:border-slate-600 dark:bg-slate-900"
                  required
                  data-testid="register-azure-subscription"
                />
              </label>
              <label className="flex flex-col gap-1 text-xs font-medium text-slate-700 dark:text-slate-300 sm:col-span-2">
                Display name
                <input
                  value={form.display_name}
                  onChange={(e) => setForm({ ...form, display_name: e.target.value })}
                  placeholder="MCPP Subscription"
                  className="rounded-md border border-slate-300 px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-900"
                  required
                  data-testid="register-azure-display-name"
                />
              </label>
              <label className="flex flex-col gap-1 text-xs font-medium text-slate-700 dark:text-slate-300">
                Tenant GUID (optional — uses runtime default when blank)
                <input
                  value={form.tenant_id}
                  onChange={(e) => setForm({ ...form, tenant_id: e.target.value })}
                  placeholder="11111111-2222-3333-4444-555555555555"
                  className="rounded-md border border-slate-300 px-3 py-2 text-sm font-mono dark:border-slate-600 dark:bg-slate-900"
                  data-testid="register-azure-tenant-id"
                />
              </label>
              <label className="flex flex-col gap-1 text-xs font-medium text-slate-700 dark:text-slate-300">
                Subscription GUID override (defaults to subscription)
                <input
                  value={form.subscription_id}
                  onChange={(e) => setForm({ ...form, subscription_id: e.target.value })}
                  placeholder="(same as subscription)"
                  className="rounded-md border border-slate-300 px-3 py-2 text-sm font-mono dark:border-slate-600 dark:bg-slate-900"
                  data-testid="register-azure-subscription-id"
                />
              </label>
              <p
                className="sm:col-span-2 text-[11px] text-slate-500 dark:text-slate-400"
                data-testid="register-azure-info"
              >
                Azure rows authenticate via AWS workload identity federation —
                no client secret or static API key is ever stored. The
                Entra tenant / client ids are runtime configuration.
              </p>
            </>
          )}
          {formError && (
            <div
              className="sm:col-span-2 rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-800 dark:bg-rose-900/40 dark:text-rose-100"
              data-testid="register-form-error"
            >
              {formError}
            </div>
          )}
          <div className="sm:col-span-2 flex justify-end">
            <button
              type="submit"
              disabled={formProvider === 'AZURE' && !azureReady}
              data-testid="register-submit"
              className="rounded-md bg-sky-600 px-4 py-2 text-sm font-semibold text-white hover:bg-sky-700 disabled:opacity-50"
            >
              {formProvider === 'AWS' ? 'Register AWS account' : 'Register Azure subscription'}
            </button>
          </div>
        </form>
      </section>

      <AccountsTableSection
        title="AWS accounts"
        emptyMessage="No AWS accounts registered yet."
        rows={awsRows}
        providerLabel="AWS"
        loading={loading}
        error={error}
        onValidate={validate}
        onDisable={(row) => setPendingDisable(row)}
        onDelete={(row) => setPendingDelete(row)}
        onEnable={(row) => void setEnabled(row, true)}
        onEdit={openEdit}
        onRefresh={() => void refresh()}
        statusById={statusById}
        busyId={busyId}
      />

      <AccountsTableSection
        title="Azure subscriptions"
        emptyMessage={
          azureReady
            ? 'No Azure subscriptions registered yet.'
            : 'Azure runtime is not configured — no subscriptions can be registered.'
        }
        rows={azureRows}
        providerLabel="Azure"
        loading={loading}
        error={null}
        onValidate={validate}
        onDisable={(row) => setPendingDisable(row)}
        onDelete={(row) => setPendingDelete(row)}
        onEnable={(row) => void setEnabled(row, true)}
        onEdit={openEdit}
        onRefresh={() => void refresh()}
        statusById={statusById}
        busyId={busyId}
      />

      {editing && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="edit-account-title"
          data-testid="edit-account-modal"
          className="fixed inset-0 z-40 flex items-center justify-center bg-slate-900/40 px-4 py-8 dark:bg-slate-900/60"
        >
          <div className="w-full max-w-lg rounded-lg border border-slate-200 bg-white p-5 shadow-xl dark:border-slate-700 dark:bg-slate-800">
            <h3
              id="edit-account-title"
              className="text-lg font-semibold text-slate-900 dark:text-slate-100"
            >
              Edit {editing.display_name}
            </h3>
            <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
              <span className="font-mono">{editing.account_id}</span>
              {' · '}
              <span className="font-semibold">{editing.provider}</span>
              {editing.is_management && (
                <span className="ml-2 rounded bg-sky-100 px-1.5 py-0.5 text-[10px] uppercase text-sky-800 dark:bg-sky-900/50 dark:text-sky-200">
                  Management
                </span>
              )}
            </p>
            <form
              onSubmit={saveEdit}
              className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2"
              data-testid="edit-form"
            >
              <label className="flex flex-col gap-1 text-xs font-medium text-slate-700 dark:text-slate-300 sm:col-span-2">
                Display name
                <input
                  value={editForm.display_name}
                  onChange={(e) =>
                    setEditForm({ ...editForm, display_name: e.target.value })
                  }
                  placeholder="Display name"
                  className="rounded-md border border-slate-300 px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-900"
                  required
                  data-testid="edit-display-name"
                />
              </label>

              {editing.provider === 'AZURE' ? (
                <>
                  <label className="flex flex-col gap-1 text-xs font-medium text-slate-700 dark:text-slate-300">
                    Tenant GUID
                    <input
                      value={editForm.tenant_id}
                      onChange={(e) =>
                        setEditForm({ ...editForm, tenant_id: e.target.value })
                      }
                      placeholder="Entra tenant GUID"
                      className="rounded-md border border-slate-300 px-3 py-2 text-sm font-mono dark:border-slate-600 dark:bg-slate-900"
                      data-testid="edit-azure-tenant-id"
                    />
                  </label>
                  <label className="flex flex-col gap-1 text-xs font-medium text-slate-700 dark:text-slate-300">
                    Subscription GUID
                    <input
                      value={editForm.subscription_id}
                      onChange={(e) =>
                        setEditForm({ ...editForm, subscription_id: e.target.value })
                      }
                      placeholder="Subscription GUID"
                      className="rounded-md border border-slate-300 px-3 py-2 text-sm font-mono dark:border-slate-600 dark:bg-slate-900"
                      data-testid="edit-azure-subscription-id"
                    />
                  </label>
                  <p className="sm:col-span-2 text-[11px] text-slate-500 dark:text-slate-400">
                    Azure rows have no IAM role or external ID — authentication
                    is workload-federation only.
                  </p>
                </>
              ) : (
                <>
                  <label className="flex flex-col gap-1 text-xs font-medium text-slate-700 dark:text-slate-300">
                    Preferred region
                    <input
                      value={editForm.preferred_region}
                      onChange={(e) =>
                        setEditForm({ ...editForm, preferred_region: e.target.value })
                      }
                      placeholder="us-east-1"
                      className="rounded-md border border-slate-300 px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-900"
                      data-testid="edit-preferred-region"
                    />
                  </label>
                  {!editing.is_management && (
                    <label className="flex flex-col gap-1 text-xs font-medium text-slate-700 dark:text-slate-300 sm:col-span-2">
                      Role ARN
                      <input
                        value={editForm.role_arn}
                        onChange={(e) =>
                          setEditForm({ ...editForm, role_arn: e.target.value })
                        }
                        placeholder="arn:aws:iam::111122223333:role/CostDetectiveReadOnly"
                        className="rounded-md border border-slate-300 px-3 py-2 text-sm font-mono dark:border-slate-600 dark:bg-slate-900"
                        required
                        data-testid="edit-role-arn"
                      />
                    </label>
                  )}
                  {!editing.is_management && (
                    <label className="flex flex-col gap-1 text-xs font-medium text-slate-700 dark:text-slate-300 sm:col-span-2">
                      External ID (write-only — leave blank to keep current)
                      <input
                        type="password"
                        value={editForm.external_id}
                        onChange={(e) =>
                          setEditForm({ ...editForm, external_id: e.target.value })
                        }
                        placeholder="(unchanged)"
                        className="rounded-md border border-slate-300 px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-900"
                        autoComplete="off"
                        data-testid="edit-external-id"
                      />
                    </label>
                  )}
                  {editing.is_management && (
                    <p className="sm:col-span-2 text-[11px] text-slate-500 dark:text-slate-400">
                      Management account: role ARN and external ID are
                      managed by the platform and cannot be edited from
                      this UI.
                    </p>
                  )}
                </>
              )}
              {editError && (
                <div
                  className="sm:col-span-2 rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-800 dark:bg-rose-900/40 dark:text-rose-100"
                  data-testid="edit-error"
                >
                  {editError}
                </div>
              )}
              <div className="sm:col-span-2 flex justify-end gap-2">
                <button
                  type="button"
                  onClick={cancelEdit}
                  className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50 dark:border-slate-600 dark:hover:bg-slate-700"
                  data-testid="edit-cancel"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={busyId === editing.id}
                  className="rounded-md bg-sky-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-sky-700 disabled:opacity-50"
                  data-testid="edit-save"
                >
                  Save changes
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

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
              {pendingDisable.provider === 'AZURE' ? 'Subscription' : 'Account'}{' '}
              <span className="font-mono">{pendingDisable.account_id}</span> will
              be excluded from the account selector and the
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

      {pendingDelete && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="delete-confirm-title"
          data-testid="delete-confirm-modal"
          className="fixed inset-0 z-40 flex items-center justify-center bg-slate-900/40 px-4 py-8 dark:bg-slate-900/60"
        >
          <div className="w-full max-w-md rounded-lg border border-slate-200 bg-white p-5 shadow-xl dark:border-slate-700 dark:bg-slate-800">
            <h3
              id="delete-confirm-title"
              data-testid="delete-confirm-title"
              className="text-lg font-semibold text-rose-700 dark:text-rose-300"
            >
              Permanently delete {pendingDelete.display_name}?
            </h3>
            <p className="mt-2 text-sm text-slate-700 dark:text-slate-300">
              <span className="font-mono">{pendingDelete.account_id}</span> will be{' '}
              <strong className="text-rose-700 dark:text-rose-300">
                permanently removed
              </strong>{' '}
              from the registry. This action cannot be undone.
            </p>
            <div className="mt-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-xs text-amber-900 dark:border-amber-700 dark:bg-amber-900/40 dark:text-amber-100">
              <p className="font-semibold">Important</p>
              <p className="mt-1">
                Only the registry metadata (display name, role ARN,
                external ID) is removed. Nothing is changed in
                {' '}{pendingDelete.provider === 'AZURE' ? 'Azure' : 'AWS'} — no
                IAM policy, no role, no resource is deleted. To revoke
                access, you must do so separately.
              </p>
            </div>
            <p className="mt-3 text-xs text-slate-500 dark:text-slate-400">
              The account must already be disabled. The management
              account cannot be deleted.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setPendingDelete(null)}
                className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50 dark:border-slate-600 dark:hover:bg-slate-700"
                data-testid="delete-confirm-cancel"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={async () => {
                  const row = pendingDelete
                  setPendingDelete(null)
                  await permanentlyDelete(row)
                }}
                className="rounded-md bg-rose-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-rose-700"
                data-testid="delete-confirm-confirm"
              >
                Permanently delete
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Per-provider accounts table.
// ---------------------------------------------------------------------------

interface AccountsTableSectionProps {
  title: string
  emptyMessage: string
  rows: CloudAccount[]
  providerLabel: string
  loading: boolean
  error: string | null
  onValidate: (id: number) => Promise<void> | void
  onDisable: (row: CloudAccount) => void
  onDelete: (row: CloudAccount) => void
  onEnable: (row: CloudAccount) => void
  onEdit: (row: CloudAccount) => void
  onRefresh: () => void
  statusById: Record<number, ValidateStatus>
  busyId: number | null
}

function AccountsTableSection({
  title,
  emptyMessage,
  rows,
  providerLabel,
  loading,
  error,
  onValidate,
  onDisable,
  onDelete,
  onEnable,
  onEdit,
  onRefresh,
  statusById,
  busyId,
}: AccountsTableSectionProps) {
  const isAzure = providerLabel === 'Azure'
  return (
    <section
      className="rounded-md border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-800"
      data-testid={`accounts-table-${isAzure ? 'azure' : 'aws'}`}
    >
      <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3 dark:border-slate-700">
        <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">
          {title}{' '}
          <span className="ml-1 text-xs font-normal text-slate-500 dark:text-slate-400">
            ({rows.length} registered)
          </span>
        </h2>
        <button
          type="button"
          onClick={onRefresh}
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
            <th className="px-4 py-2 text-left">
              {isAzure ? 'Subscription GUID' : 'Account ID'}
            </th>
            <th className="px-4 py-2 text-left">
              {isAzure ? 'Tenant' : 'Region'}
            </th>
            <th className="px-4 py-2 text-left">
              {isAzure ? 'Subscription ID' : 'Role ARN'}
            </th>
            <th className="px-4 py-2 text-left">State</th>
            <th className="px-4 py-2 text-right">Actions</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
          {rows.map((row) => {
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
                <td className="px-4 py-2 text-xs">
                  {isAzure ? row.tenant_id ?? '—' : row.preferred_region ?? '—'}
                </td>
                <td
                  className={
                    isAzure
                      ? 'px-4 py-2 font-mono text-xs'
                      : 'max-w-xs truncate px-4 py-2 font-mono text-xs'
                  }
                  title={isAzure ? (row.subscription_id ?? row.account_id) : row.role_arn ?? ''}
                >
                  {isAzure ? row.subscription_id ?? row.account_id : row.role_arn ?? '—'}
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
                              : liveStatus === 'ACCESS_DENIED' || liveStatus === 'ROLE_NOT_FOUND' || liveStatus === 'AUTH_FAILED' || liveStatus === 'NOT_FOUND'
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
                      onClick={() => onEdit(row)}
                      disabled={busyId === row.id}
                      data-testid={`edit-${row.id}`}
                      className="rounded border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-50 dark:border-slate-600 dark:hover:bg-slate-700"
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      onClick={() => void onValidate(row.id)}
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
                          onClick={() => onDisable(row)}
                          disabled={busyId === row.id}
                          data-testid={`disable-${row.id}`}
                          className="rounded border border-rose-300 px-2 py-1 text-xs text-rose-700 hover:bg-rose-50 disabled:opacity-50 dark:border-rose-700 dark:text-rose-300 dark:hover:bg-rose-900/40"
                        >
                          Disable
                        </button>
                      ) : (
                        <>
                          <button
                            type="button"
                            onClick={() => onEnable(row)}
                            disabled={busyId === row.id}
                            data-testid={`enable-${row.id}`}
                            className="rounded border border-emerald-300 px-2 py-1 text-xs text-emerald-700 hover:bg-emerald-50 disabled:opacity-50 dark:border-emerald-700 dark:text-emerald-300 dark:hover:bg-emerald-900/40"
                          >
                            Enable
                          </button>
                          <button
                            type="button"
                            onClick={() => onDelete(row)}
                            disabled={busyId === row.id}
                            data-testid={`delete-${row.id}`}
                            className="rounded border border-rose-400 px-2 py-1 text-xs font-semibold text-rose-700 hover:bg-rose-50 disabled:opacity-50 dark:border-rose-600 dark:text-rose-300 dark:hover:bg-rose-900/40"
                          >
                            Delete
                          </button>
                        </>
                      ))}
                  </div>
                </td>
              </tr>
            )
          })}
          {!loading && rows.length === 0 && (
            <tr>
              <td
                colSpan={6}
                className="px-4 py-6 text-center text-sm text-slate-500 dark:text-slate-400"
                data-testid={`accounts-table-${isAzure ? 'azure' : 'aws'}-empty`}
              >
                {emptyMessage}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  )
}

export default AccountsPage
