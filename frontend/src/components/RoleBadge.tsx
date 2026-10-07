// Phase 6A — RoleBadge.
//
// Compact pill for ADMIN / ANALYST / VIEWER.  Color is consistent
// with the role's authority — admin = primary cyan, analyst =
// info blue, viewer = muted.  Always paired with text — color is
// never the sole state carrier.

import type { Role } from '../lib/tokens'
import { roleDisplayLabel } from '../lib/tokens'

export function RoleBadge({ role, compact = false }: { role: Role; compact?: boolean }) {
  const styles = roleStyles(role)
  return (
    <span
      className={[
        'inline-flex items-center rounded-full border font-medium',
        compact ? 'px-1.5 py-0.5 text-[10px]' : 'px-2 py-0.5 text-xs',
        styles.bg,
        styles.text,
        styles.border,
      ].join(' ')}
      aria-label={`Role: ${roleDisplayLabel(role)}`}
    >
      {compact ? role : roleDisplayLabel(role)}
    </span>
  )
}

function roleStyles(role: Role): { bg: string; text: string; border: string } {
  switch (role) {
    case 'ADMIN':
      return {
        bg: 'bg-primary-soft',
        text: 'text-primary',
        border: 'border-primary/30',
      }
    case 'ANALYST':
      return {
        bg: 'bg-info-soft',
        text: 'text-info',
        border: 'border-info/30',
      }
    case 'VIEWER':
      return {
        bg: 'bg-surface-2',
        text: 'text-fg-secondary',
        border: 'border-border',
      }
  }
}
