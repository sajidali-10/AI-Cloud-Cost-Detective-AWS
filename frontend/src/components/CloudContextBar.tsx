// Phase 7C.2A — Cloud Context bar.
//
// Phase 7C.2A introduced a ProviderSelector and an account/sub­
// scription selector into the top navigation.  At one provider that
// was fine, but as soon as a second provider came online the row
// became crowded and the selectors fought the navigation links for
// horizontal real estate.
//
// This component is the second-row surface that hosts the cloud
// selectors in their own band.  The primary navigation row keeps
// Dashboard / FinOps / AI Cost Analyst / Conversations /
// Administration + theme + user — this row owns the "what cloud
// and which account" context.
//
// Layout rules baked into the markup:
//
//   * Subtle dark/navy background distinct from the primary header
//     (`bg-bg` + inner panel) so the eye reads it as context, not
//     as another row of nav.
//   * Thin top divider so the row visually anchors to the row
//     above without bloating the vertical footprint.
//   * Approx 44 px (h-11) tall — enough to fit the existing h-9
//     selectors with comfortable vertical centering.
//   * Right-aligned cluster (provider + account/subscription) with
//     no wrapping on desktop widths ≥1024 px.
//   * Left-aligned label "Cloud Context" for orientation; on small
//     screens it stacks above the selectors.
//
// The ProviderSelector and AccountSelector stay as separate
// components — this bar is purely the layout wrapper that mounts
// them.  All sessionStorage behaviour, RBAC, and provider
// isolation logic continue to live in those primitives.

import type { ReactNode } from 'react'
import { ProviderSelector } from './ProviderSelector'
import { AccountSelector } from './AccountSelector'

interface CloudContextBarProps {
  /** Optional override content for tests / storybook. */
  children?: ReactNode
  /** Test hook — overrides the row's data-testid. */
  testId?: string
  /** Hide the "Cloud Context" label — used on small screens where
   *  the selectors are stacked and the label would be redundant. */
  hideLabel?: boolean
}

export function CloudContextBar({
  children,
  testId = 'cloud-context-bar',
  hideLabel = false,
}: CloudContextBarProps) {
  return (
    <div
      className="relative border-t border-border bg-bg"
      data-testid={testId}
    >
      <div
        className="
          mx-auto flex h-11 max-w-screen-2xl flex-wrap items-center
          gap-x-4 gap-y-1 px-3 sm:px-5
        "
      >
        {!hideLabel && (
          <span
            className="
              hidden shrink-0 text-[11px] font-semibold uppercase
              tracking-wider text-fg-muted md:inline-flex
            "
          >
            Cloud Context
          </span>
        )}
        <div
          className="
            ml-auto flex flex-wrap items-center justify-end gap-2
          "
          data-testid="cloud-context-selectors"
        >
          {children ?? (
            <>
              <ProviderSelector />
              <AccountSelector />
            </>
          )}
        </div>
      </div>
    </div>
  )
}

export default CloudContextBar
