// Phase 6A — Admin user management page.
//
// Reuses the Phase 5A backend endpoints:
//   - GET    /api/admin/users
//   - POST   /api/admin/users
//   - PATCH  /api/admin/users/{id}
//
// The page is ADMIN only — the route guard refuses other roles.
// All actions use confirmation dialogs for sensitive operations.

import { useCallback, useEffect, useId, useState, type FormEvent } from 'react'
import { ApiError, apiFetch } from '../lib/api'
import { useAuth } from '../lib/auth'
import { PageHeader } from '../components/PageHeader'
import { SectionCard } from '../components/SectionCard'
import { DataTable } from '../components/DataTable'
import { StatusBadge, type StatusTone } from '../components/StatusBadge'
import { RoleBadge } from '../components/RoleBadge'
import { EmptyState } from '../components/EmptyState'
import { ErrorState, BackendUnavailable } from '../components/ErrorState'
import { LoadingSkeleton } from '../components/LoadingSkeleton'
import { AccessDenied } from '../components/AccessDenied'
import type { Role } from '../lib/tokens'
import type { RoleName } from '../lib/router'

interface AdminUser {
  id: number
  email: string
  display_name: string
  role: RoleName
  is_active: boolean
  created_at: string
  updated_at: string
  last_login_at: string | null
}

export function UsersPage() {
  const { role, user, ready } = useAuth()

  if (!ready) {
    return (
      <div role="status" aria-label="Checking permissions" className="space-y-2">
        <div className="h-6 w-32 animate-pulse rounded bg-surface-2" />
        <div className="h-4 w-64 animate-pulse rounded bg-surface-2" />
      </div>
    )
  }

  if (role !== 'ADMIN') {
    return (
      <AccessDenied
        requiredRoles={['ADMIN']}
        message="Only administrators can manage users."
      />
    )
  }

  return <UsersAdmin currentUserId={user?.id ?? -1} />
}

function UsersAdmin({ currentUserId }: { currentUserId: number }) {
  const [users, setUsers] = useState<AdminUser[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<AdminUser | null>(null)
  const [confirmAction, setConfirmAction] = useState<{ user: AdminUser; kind: 'deactivate' | 'reactivate' } | null>(null)
  const [reloadKey, setReloadKey] = useState(0)

  const reload = useCallback(() => setReloadKey((n) => n + 1), [])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    apiFetch<{ users: AdminUser[]; count: number }>('/api/admin/users', {
      skipAuthRedirect: true,
    })
      .then((data) => {
        if (cancelled) return
        setUsers(data.users)
        setLoading(false)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof ApiError && (err.status === 0 || err.code === 'Unavailable')) {
          setError('unavailable')
        } else if (err instanceof ApiError) {
          setError(err.message)
        } else {
          setError('Unable to load users')
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
        title="Users"
        subtitle="Manage application users and their roles"
        actions={
          <button
            type="button"
            onClick={() => setCreateOpen(true)}
            data-testid="users-create-button"
            className="
              inline-flex items-center rounded-md bg-primary px-3 py-1.5
              text-sm font-medium text-primary-foreground
              hover:bg-primary-hover focus:outline-none focus-visible:shadow-focus
            "
          >
            New user
          </button>
        }
      />

      <SectionCard title="All users" description={`${users.length} total`}>
        {loading ? (
          <div className="space-y-2" aria-label="Loading users">
            <LoadingSkeleton className="h-9 w-full" />
            <LoadingSkeleton className="h-9 w-full" />
            <LoadingSkeleton className="h-9 w-full" />
          </div>
        ) : error === 'unavailable' ? (
          <BackendUnavailable onRetry={reload} />
        ) : error ? (
          <ErrorState message={error} onRetry={reload} />
        ) : users.length === 0 ? (
          <EmptyState
            title="No users"
            description="Create the first user to get started."
          />
        ) : (
          <DataTable
            caption="Application users"
            columns={[
              {
                key: 'email',
                header: 'Email',
                cell: (row) => (
                  <span className="font-mono text-xs text-fg-primary">{row.email}</span>
                ),
              },
              {
                key: 'display_name',
                header: 'Display name',
                cell: (row) => row.display_name,
              },
              {
                key: 'role',
                header: 'Role',
                cell: (row) => <RoleBadge role={row.role} />,
              },
              {
                key: 'is_active',
                header: 'Status',
                cell: (row) => (
                  <StatusBadge tone={row.is_active ? 'success' : 'danger'}>
                    {row.is_active ? 'Active' : 'Inactive'}
                  </StatusBadge>
                ),
              },
              {
                key: 'last_login_at',
                header: 'Last login',
                hideOnMobile: true,
                cell: (row) =>
                  row.last_login_at
                    ? new Date(row.last_login_at).toLocaleString()
                    : '—',
              },
              {
                key: 'actions',
                header: 'Actions',
                width: 'w-40',
                cell: (row) => (
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => setEditTarget(row)}
                      className="
                        inline-flex items-center rounded-md border border-border
                        bg-surface px-2 py-1 text-xs font-medium text-fg-primary
                        hover:bg-surface-hover focus:outline-none focus-visible:shadow-focus
                      "
                      data-testid={`users-edit-${row.id}`}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        setConfirmAction({
                          user: row,
                          kind: row.is_active ? 'deactivate' : 'reactivate',
                        })
                      }
                      disabled={row.id === currentUserId}
                      className="
                        inline-flex items-center rounded-md border border-border
                        bg-surface px-2 py-1 text-xs font-medium
                        hover:bg-surface-hover focus:outline-none focus-visible:shadow-focus
                        disabled:opacity-50
                      "
                      data-testid={`users-toggle-${row.id}`}
                    >
                      {row.is_active ? 'Deactivate' : 'Reactivate'}
                    </button>
                  </div>
                ),
              },
            ]}
            rows={users}
            rowKey={(row) => row.id}
          />
        )}
      </SectionCard>

      {createOpen && (
        <CreateUserDialog
          onClose={() => setCreateOpen(false)}
          onCreated={() => {
            setCreateOpen(false)
            reload()
          }}
        />
      )}

      {editTarget && (
        <EditUserDialog
          user={editTarget}
          currentUserId={currentUserId}
          onClose={() => setEditTarget(null)}
          onSaved={() => {
            setEditTarget(null)
            reload()
          }}
        />
      )}

      {confirmAction && (
        <ConfirmDialog
          title={
            confirmAction.kind === 'deactivate'
              ? 'Deactivate user?'
              : 'Reactivate user?'
          }
          message={
            confirmAction.kind === 'deactivate'
              ? `${confirmAction.user.display_name} will no longer be able to sign in.`
              : `${confirmAction.user.display_name} will be able to sign in again.`
          }
          tone={confirmAction.kind === 'deactivate' ? 'warning' : 'info'}
          confirmLabel={confirmAction.kind === 'deactivate' ? 'Deactivate' : 'Reactivate'}
          onConfirm={async () => {
            try {
              await apiFetch(`/api/admin/users/${confirmAction.user.id}`, {
                method: 'PATCH',
                json: { is_active: confirmAction.kind === 'reactivate' },
                skipAuthRedirect: true,
              })
              setConfirmAction(null)
              reload()
            } catch (err) {
              // Surface the error inline; keep the dialog open.
              if (err instanceof ApiError) {
                alert(err.message)
              }
            }
          }}
          onCancel={() => setConfirmAction(null)}
        />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Create user dialog
// ---------------------------------------------------------------------------

function CreateUserDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: () => void
}) {
  const emailId = useId()
  const nameId = useId()
  const passwordId = useId()
  const roleId = useId()
  const [email, setEmail] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<Role>('VIEWER')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    if (submitting) return
    setSubmitting(true)
    setError(null)
    try {
      await apiFetch('/api/admin/users', {
        method: 'POST',
        json: {
          email: email.trim().toLowerCase(),
          display_name: displayName.trim(),
          password,
          role,
        },
        skipAuthRedirect: true,
      })
      onCreated()
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError('Unable to create user')
      }
      setSubmitting(false)
    }
  }

  return (
    <Modal title="New user" onClose={onClose}>
      <form onSubmit={handleSubmit} className="space-y-3">
        <Field id={emailId} label="Email">
          <input
            id={emailId}
            type="email"
            required
            autoComplete="off"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="input"
            data-testid="create-email"
          />
        </Field>
        <Field id={nameId} label="Display name">
          <input
            id={nameId}
            type="text"
            required
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            className="input"
            data-testid="create-display-name"
          />
        </Field>
        <Field id={passwordId} label="Password (≥ 8 characters)">
          <input
            id={passwordId}
            type="password"
            required
            minLength={8}
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="input"
            data-testid="create-password"
          />
        </Field>
        <Field id={roleId} label="Role">
          <select
            id={roleId}
            value={role}
            onChange={(e) => setRole(e.target.value as Role)}
            className="input"
            data-testid="create-role"
          >
            <option value="VIEWER">Viewer</option>
            <option value="ANALYST">Analyst</option>
            <option value="ADMIN">Administrator</option>
          </select>
        </Field>
        {error && (
          <p
            role="alert"
            className="rounded-md border border-danger/30 bg-danger-soft px-3 py-2 text-xs text-danger"
          >
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm text-fg-primary hover:bg-surface-hover focus:outline-none focus-visible:shadow-focus"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={submitting || !email || !displayName || password.length < 8}
            className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary-hover focus:outline-none focus-visible:shadow-focus disabled:opacity-60"
            data-testid="create-submit"
          >
            {submitting ? 'Creating…' : 'Create user'}
          </button>
        </div>
      </form>
      <style>{`.input { width:100%; border-radius:0.375rem; border:1px solid var(--border); background:var(--bg); padding:0.5rem 0.75rem; color:var(--fg-primary); font-size:0.875rem; } .input:focus { outline:none; box-shadow: var(--ring); }`}</style>
    </Modal>
  )
}

// ---------------------------------------------------------------------------
// Edit user dialog
// ---------------------------------------------------------------------------

function EditUserDialog({
  user,
  currentUserId,
  onClose,
  onSaved,
}: {
  user: AdminUser
  currentUserId: number
  onClose: () => void
  onSaved: () => void
}) {
  const nameId = useId()
  const roleId = useId()
  const passwordId = useId()
  const [displayName, setDisplayName] = useState(user.display_name)
  const [role, setRole] = useState<Role>(user.role)
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const isSelf = user.id === currentUserId

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    if (submitting) return
    setSubmitting(true)
    setError(null)
    try {
      const body: Record<string, unknown> = {
        display_name: displayName.trim(),
        role,
      }
      if (password.length >= 8) body.password = password
      await apiFetch(`/api/admin/users/${user.id}`, {
        method: 'PATCH',
        json: body,
        skipAuthRedirect: true,
      })
      onSaved()
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError('Unable to update user')
      }
      setSubmitting(false)
    }
  }

  return (
    <Modal title={`Edit ${user.display_name}`} onClose={onClose}>
      <form onSubmit={handleSubmit} className="space-y-3">
        <Field id={nameId} label="Display name">
          <input
            id={nameId}
            type="text"
            required
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            className="input"
          />
        </Field>
        <Field id={roleId} label="Role">
          <select
            id={roleId}
            value={role}
            onChange={(e) => setRole(e.target.value as Role)}
            className="input"
            disabled={isSelf}
            title={isSelf ? 'You cannot change your own role' : undefined}
          >
            <option value="VIEWER">Viewer</option>
            <option value="ANALYST">Analyst</option>
            <option value="ADMIN">Administrator</option>
          </select>
        </Field>
        <Field id={passwordId} label="New password (leave blank to keep current)">
          <input
            id={passwordId}
            type="password"
            minLength={8}
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="input"
          />
        </Field>
        {error && (
          <p
            role="alert"
            className="rounded-md border border-danger/30 bg-danger-soft px-3 py-2 text-xs text-danger"
          >
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm text-fg-primary hover:bg-surface-hover focus:outline-none focus-visible:shadow-focus"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={submitting || !displayName.trim()}
            className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary-hover focus:outline-none focus-visible:shadow-focus disabled:opacity-60"
          >
            {submitting ? 'Saving…' : 'Save changes'}
          </button>
        </div>
      </form>
      <style>{`.input { width:100%; border-radius:0.375rem; border:1px solid var(--border); background:var(--bg); padding:0.5rem 0.75rem; color:var(--fg-primary); font-size:0.875rem; } .input:focus { outline:none; box-shadow: var(--ring); }`}</style>
    </Modal>
  )
}

// ---------------------------------------------------------------------------
// Confirm dialog
// ---------------------------------------------------------------------------

function ConfirmDialog({
  title,
  message,
  tone,
  confirmLabel,
  onConfirm,
  onCancel,
}: {
  title: string
  message: string
  tone: StatusTone
  confirmLabel: string
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <Modal title={title} onClose={onCancel}>
      <p className="text-sm text-fg-secondary">{message}</p>
      <div className="mt-4 flex justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm text-fg-primary hover:bg-surface-hover focus:outline-none focus-visible:shadow-focus"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={onConfirm}
          className={[
            'rounded-md px-3 py-1.5 text-sm font-medium focus:outline-none focus-visible:shadow-focus',
            tone === 'warning'
              ? 'bg-warning text-fg-inverse hover:opacity-90'
              : 'bg-primary text-primary-foreground hover:bg-primary-hover',
          ].join(' ')}
        >
          {confirmLabel}
        </button>
      </div>
    </Modal>
  )
}

// ---------------------------------------------------------------------------
// Modal + field primitives
// ---------------------------------------------------------------------------

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  useEffect(() => {
    const esc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', esc)
    return () => document.removeEventListener('keydown', esc)
  }, [onClose])
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      className="fixed inset-0 z-40 flex items-center justify-center bg-fg-inverse/40 p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div className="w-full max-w-md rounded-xl border border-border bg-surface p-5 shadow-card-md">
        <h2 className="text-sm font-semibold text-fg-primary">{title}</h2>
        <div className="mt-3">{children}</div>
      </div>
    </div>
  )
}

function Field({ id, label, children }: { id: string; label: string; children: React.ReactNode }) {
  return (
    <div>
      <label htmlFor={id} className="block text-xs font-medium text-fg-secondary">
        {label}
      </label>
      <div className="mt-1">{children}</div>
    </div>
  )
}
