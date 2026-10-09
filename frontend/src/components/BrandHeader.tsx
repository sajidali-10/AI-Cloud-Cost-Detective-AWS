// Phase 6A / 6C — BrandHeader.
//
// Compact brand mark for the top navigation row.  Uses the
// centralized <HiplinkLogo size="compact" /> so the theme-aware
// logo selection is not duplicated.
//
// Visual hierarchy:
//   [HipLink logo]  AI Cloud Cost Detective
//                  ─────────────────────────
//                    subtle separator dot or vertical rule
//
// Layout dimensions and the product-name text are preserved from
// the Phase 6A implementation; only the inline BrandGlyph was
// swapped for the centralized <HiplinkLogo>.

import { Link } from '../lib/router'
import { HiplinkLogo } from './HiplinkLogo'

export function BrandHeader({ compact = false }: { compact?: boolean }) {
  return (
    <Link
      to="/"
      className="flex items-center gap-3 rounded-md focus:outline-none focus-visible:shadow-focus"
      aria-label="AI Cloud Cost Detective — go to dashboard"
      data-testid="brand-header"
    >
      <HiplinkLogo size="compact" testId="brand-header-logo" />
      <span className="text-sm font-semibold text-fg-primary">
        {compact ? 'AI Cloud Cost Detective' : 'AI Cloud Cost Detective'}
      </span>
    </Link>
  )
}
