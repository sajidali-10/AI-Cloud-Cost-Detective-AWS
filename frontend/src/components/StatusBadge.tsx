// Phase 6A — StatusBadge.
//
// Compact pill for operational states.  Color is consistent with
// the semantic palette (success / warning / danger / info / ai).
// Always paired with text — color is never the sole state carrier.

import type { ReactNode } from 'react'

export type StatusTone = 'success' | 'warning' | 'danger' | 'info' | 'ai' | 'neutral'

export function StatusBadge({
  tone = 'neutral',
  children,
  title,
}: {
  tone?: StatusTone
  children: ReactNode
  title?: string
}) {
  const styles = toneStyles(tone)
  return (
    <span
      title={title}
      className={[
        'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium',
        styles.bg,
        styles.text,
        styles.border,
      ].join(' ')}
    >
      <span
        aria-hidden
        className={['h-1.5 w-1.5 rounded-full', styles.dot].join(' ')}
      />
      <span>{children}</span>
    </span>
  )
}

function toneStyles(tone: StatusTone): {
  bg: string
  text: string
  border: string
  dot: string
} {
  switch (tone) {
    case 'success':
      return {
        bg: 'bg-success-soft',
        text: 'text-success',
        border: 'border-success/30',
        dot: 'bg-success',
      }
    case 'warning':
      return {
        bg: 'bg-warning-soft',
        text: 'text-warning',
        border: 'border-warning/30',
        dot: 'bg-warning',
      }
    case 'danger':
      return {
        bg: 'bg-danger-soft',
        text: 'text-danger',
        border: 'border-danger/30',
        dot: 'bg-danger',
      }
    case 'info':
      return {
        bg: 'bg-info-soft',
        text: 'text-info',
        border: 'border-info/30',
        dot: 'bg-info',
      }
    case 'ai':
      return {
        bg: 'bg-ai-soft',
        text: 'text-ai',
        border: 'border-ai/30',
        dot: 'bg-ai',
      }
    case 'neutral':
      return {
        bg: 'bg-surface-2',
        text: 'text-fg-secondary',
        border: 'border-border',
        dot: 'bg-fg-muted',
      }
  }
}
