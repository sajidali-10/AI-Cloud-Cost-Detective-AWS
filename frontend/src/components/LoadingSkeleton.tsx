// Phase 6A — LoadingSkeleton.
//
// Restrained loading placeholders.  Visually weighted like the
// real components so layout doesn't jump when data arrives.

import type { CSSProperties } from 'react'

export function LoadingSkeleton({
  className = '',
  style,
}: {
  className?: string
  style?: CSSProperties
}) {
  return (
    <div
      aria-hidden
      className={[
        'animate-pulse rounded-md bg-surface-2',
        className,
      ].join(' ')}
      style={style}
    />
  )
}

export function LoadingSkeletonRows({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-hidden>
      {Array.from({ length: rows }).map((_, i) => (
        <LoadingSkeleton key={i} className="h-9 w-full" />
      ))}
    </div>
  )
}
