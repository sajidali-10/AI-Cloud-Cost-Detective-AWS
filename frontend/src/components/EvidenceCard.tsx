// Phase 6C — Citation / evidence card.
//
// Renders an `assistant_message.citations[]` entry as a compact
// key/value card.  We render whatever keys the backend supplied —
// we never invent keys, never fabricate values, never render raw
// HTML.  The card is intentionally collapsible because evidence
// can be verbose.

import { useState } from 'react'
import type { AICitation } from '../types/ai'

function safeKey(key: string): string {
  return key.replace(/[^a-zA-Z0-9_.-]/g, '')
}

function renderValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return '—'
  }
}

export function EvidenceCard({ citation, index }: { citation: AICitation; index: number }) {
  const [open, setOpen] = useState(false)
  const keys = Object.keys(citation ?? {}).filter((k) => safeKey(k) === k)
  const preview = keys.slice(0, 3)
  return (
    <div
      role="group"
      aria-label={`Evidence ${index + 1}`}
      data-testid="evidence-card"
      className="
        rounded-md border border-border bg-surface-2 p-2 text-xs
      "
    >
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="
          flex w-full items-center justify-between gap-2 text-left
          focus:outline-none focus-visible:shadow-focus
        "
      >
        <span className="font-medium text-fg-primary">
          Evidence {index + 1}
        </span>
        <span className="font-mono text-[0.7rem] text-fg-muted">
          {preview.map((k) => `${safeKey(k)}: ${renderValue(citation[k])}`).join(' · ')}
        </span>
      </button>
      {open && (
        <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-xs">
          {keys.map((k) => (
            <div key={k} className="contents">
              <dt className="font-mono text-fg-muted">{safeKey(k)}</dt>
              <dd className="break-words text-fg-primary">{renderValue(citation[k])}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  )
}
