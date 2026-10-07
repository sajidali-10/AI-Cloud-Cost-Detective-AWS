// Phase 6A — AppShell.
//
// Wraps every authenticated page.  Provides:
//   - top navigation (desktop) on lg: and above
//   - mobile navigation on smaller screens
//   - the main content area with consistent spacing
// Children render inside <main role="main"> for assistive tech.

import type { ReactNode } from 'react'
import { MobileNavigation } from './MobileNavigation'
import { TopNavigation } from './TopNavigation'

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen bg-bg text-fg-primary">
      {/* Desktop + mobile header share the same DOM but only one is visible at a time via CSS */}
      <div className="hidden lg:block">
        <TopNavigation />
      </div>
      <MobileNavigation />
      <main role="main" className="mx-auto max-w-screen-2xl px-3 py-5 sm:px-5">
        {children}
      </main>
    </div>
  )
}
