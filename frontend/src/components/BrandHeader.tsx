// Phase 6A — BrandHeader.
//
// The HipLink reference UI shows a compact brand mark at the
// top-left of the header (logo glyph + "HipLink" wordmark).  No
// HipLink logo asset exists in this repository, so the BrandHeader
// renders an inline wordmark in semantic primary color.  Drop a
// real SVG/PNG into `frontend/public/` and replace this component
// (or import a static asset) to ship the real logo.
//
// Visual hierarchy:
//   [glyph]  HipLink   AI Cloud Cost Detective
//              |___________________________|
//                  subtle separator dot or vertical rule

import { Link } from '../lib/router'

export function BrandHeader({ compact = false }: { compact?: boolean }) {
  return (
    <Link
      to="/"
      className="flex items-center gap-3 rounded-md focus:outline-none focus-visible:shadow-focus"
      aria-label="AI Cloud Cost Detective — go to dashboard"
    >
      <BrandGlyph />
      {!compact && (
        <span className="flex items-baseline gap-2">
          <span className="text-sm font-semibold tracking-wide text-primary">
            HIPLINK
          </span>
          <span aria-hidden className="text-fg-muted">·</span>
          <span className="text-sm font-semibold text-fg-primary">
            AI Cloud Cost Detective
          </span>
        </span>
      )}
      {compact && (
        <span className="text-sm font-semibold text-fg-primary">
          AI Cloud Cost Detective
        </span>
      )}
    </Link>
  )
}

function BrandGlyph() {
  // Geometric mark — a rounded square with the "H" letterform.
  // Visual placeholder; swap for the real HipLink mark.
  return (
    <span
      aria-hidden
      className="
        inline-flex h-8 w-8 items-center justify-center rounded-md
        bg-primary-soft text-primary ring-1 ring-border
      "
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M3 2v12" />
        <path d="M13 2v12" />
        <path d="M3 8h10" />
      </svg>
    </span>
  )
}
