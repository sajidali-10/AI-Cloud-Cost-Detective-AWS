// Phase 6A / 6C — BrandHeader.
//
// The HipLink reference UI shows a compact brand mark at the
// top-left of the header (logo glyph + product name).  Two logo
// assets live under `frontend/public/branding/`:
//
//   hiplink-logo-on-dark.png   — light wordmark, intended for dark themes
//   hiplink-logo-on-light.png  — dark wordmark, intended for light themes
//
// The header picks the asset whose wordmark colour matches the
// active theme's surface, so the logo is legible on either palette.
// All consumers (TopNavigation, MobileNavigation, LoginPage) render
// this component — no page-specific duplicate logo logic.
//
// Visual hierarchy:
//   [HipLink logo]  AI Cloud Cost Detective   (non-compact)
//   [HipLink logo]  AI Cloud Cost Detective   (compact — same text)
//
// The logo is rendered with `h-8 w-auto` so its aspect ratio is
// preserved at all viewport sizes.  The two PNGs have slightly
// different intrinsic aspect ratios (110×71 vs 129×71), so each
// asset renders at its natural width at the chosen height.

import { Link } from '../lib/router'
import { useTheme } from '../lib/theme'

const LOGO_SRC: Record<'dark' | 'light', string> = {
  dark: '/branding/hiplink-logo-on-dark.png',
  light: '/branding/hiplink-logo-on-light.png',
}

export function BrandHeader({ compact = false }: { compact?: boolean }) {
  const { theme } = useTheme()
  const logoSrc = LOGO_SRC[theme]

  return (
    <Link
      to="/"
      className="flex items-center gap-3 rounded-md focus:outline-none focus-visible:shadow-focus"
      aria-label="AI Cloud Cost Detective — go to dashboard"
      data-testid="brand-header"
    >
      <img
        src={logoSrc}
        alt="HipLink"
        width={0}
        height={32}
        // `h-8` matches the previous BrandGlyph dimensions.  `w-auto`
        // preserves each PNG's intrinsic aspect ratio so the logo is
        // never stretched.  `max-w-none` is implicit because the
        // parent flex container controls the layout.
        className="block h-8 w-auto select-none"
        draggable={false}
        data-testid="brand-header-logo"
        data-theme-asset={theme}
      />
      <span className="text-sm font-semibold text-fg-primary">
        {compact ? 'AI Cloud Cost Detective' : 'AI Cloud Cost Detective'}
      </span>
    </Link>
  )
}
