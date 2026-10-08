// Phase 6B — Region filter.
//
// Populated from observed cost-by-region and resource-inventory
// data.  "All / account-level" is the default; it preserves the
// existing backend semantics where account-level cost includes
// global services.

import { useFinopsPeriod } from '../lib/finops/period'
import { FilterSelect } from './FilterBar'

export interface RegionOption {
  value: string
  label: string
}

export interface RegionFilterProps {
  options: RegionOption[]
  disabled?: boolean
  testId?: string
}

export function RegionFilter({ options, disabled = false, testId = 'region-filter' }: RegionFilterProps) {
  const { region, setRegion } = useFinopsPeriod()
  return (
    <span className="inline-flex items-center rounded-md border border-border bg-surface text-fg-secondary">
      <FilterSelect
        label="Region"
        value={region}
        options={options}
        onChange={setRegion}
        disabled={disabled}
        {...(testId ? { testId } : {})}
      />
    </span>
  )
}

/**
 * Build the dropdown options from observed regions.  Always prepend
 * "All / account-level" so the user can return to the global view.
 * "global" / "no_region" entries are relabelled to "Global / No Region"
 * rather than dropped silently (per spec).
 */
export function buildRegionOptions(
  regions: Iterable<string | null | undefined>,
  options: { current?: string; includeAll?: boolean } = {},
): RegionOption[] {
  const seen = new Set<string>()
  const out: RegionOption[] = []
  if (options.includeAll !== false) {
    out.push({ value: 'all', label: 'All / account-level' })
  }
  for (const r of regions) {
    if (!r) continue
    if (seen.has(r)) continue
    seen.add(r)
    out.push({ value: r, label: humanRegionLabel(r) })
  }
  if (options.current && options.current !== 'all' && !seen.has(options.current)) {
    out.push({ value: options.current, label: humanRegionLabel(options.current) })
  }
  return out
}

function humanRegionLabel(region: string): string {
  if (region === 'global' || region === 'no_region') return 'Global / No Region'
  return region
}
