// Phase 6C — Centralized HipLink brand logo.
//
// ONE component for the official HipLink wordmark logo.  Selects the
// theme-aware asset so the wordmark colour matches the active palette:
//
//   dark theme  -> /branding/hiplink-logo-on-dark.png   (light wordmark)
//   light theme -> /branding/hiplink-logo-on-light.png  (dark wordmark)
//
// Two visual sizes:
//
//   size="compact"  h-8 w-auto  — top nav / header strip
//   size="hero"     h-16 w-auto — dashboard hero / login / large cards
//
// Both preserve the asset's intrinsic aspect ratio (`w-auto`), never
// stretch, never crop, always expose `alt="HipLink"` for screen
// readers.  The `<img>` is also marked non-draggable so it cannot be
// accidentally pulled out of the layout by the user.
//
// Every page that renders the logo MUST use this component.  No
// page-specific duplicate logo logic is allowed.

import { useTheme } from '../lib/theme'

export type HiplinkLogoSize = 'compact' | 'hero'

const LOGO_SRC: Record<'dark' | 'light', string> = {
  dark: '/branding/hiplink-logo-on-dark.png',
  light: '/branding/hiplink-logo-on-light.png',
}

const SIZE_CLASS: Record<HiplinkLogoSize, string> = {
  // h-8 (32 px) — matches the legacy BrandGlyph footprint so the
  // compact top nav row keeps its existing layout.
  compact: 'h-8 w-auto',
  // h-16 (64 px) — used by the dashboard hero and other large
  // placements.  Width stays auto so the PNG's intrinsic aspect
  // ratio is preserved.
  hero: 'h-16 w-auto',
}

export interface HiplinkLogoProps {
  size?: HiplinkLogoSize
  /** Optional className override (e.g. for layout spacing). */
  className?: string
  /** Optional aria-label override; defaults to "HipLink". */
  ariaLabel?: string
  /** Test hook — overrides the data-testid. */
  testId?: string
}

export function HiplinkLogo({
  size = 'compact',
  className,
  ariaLabel = 'HipLink',
  testId = 'hiplink-logo',
}: HiplinkLogoProps) {
  const { theme } = useTheme()
  const src = LOGO_SRC[theme]
  return (
    <img
      src={src}
      alt={ariaLabel}
      draggable={false}
      // `width={0}` removes the intrinsic HTML width attribute so
      // CSS (`h-{n} w-auto`) drives sizing — this is what preserves
      // the aspect ratio without stretching.
      width={0}
      height={0}
      data-testid={testId}
      data-theme-asset={theme}
      data-size={size}
      className={['block select-none', SIZE_CLASS[size], className ?? ''].join(' ').trim()}
    />
  )
}

/** Internal export for tests — source map per theme. */
export const __hiplinkLogoSourceForTheme = LOGO_SRC
