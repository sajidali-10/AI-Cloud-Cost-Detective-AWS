// Phase 6B — Badge primitive tests.

import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { RecommendationSourceBadge, ConfidenceBadge } from '../components/RecommendationSourceBadge'
import { CapabilityStatusRow } from '../components/CapabilityStatusRow'
import { PartialWarning } from '../components/PartialWarning'
import { ThemeProvider } from '../lib/theme'

function wrap(node: React.ReactNode) {
  return render(<ThemeProvider>{node}</ThemeProvider>)
}

describe('RecommendationSourceBadge', () => {
  it('renders AWS-native source labels', () => {
    wrap(
      <div>
        <RecommendationSourceBadge source="AWS_COST_OPTIMIZATION_HUB" />
        <RecommendationSourceBadge source="AWS_COMPUTE_OPTIMIZER" />
        <RecommendationSourceBadge source="UNKNOWN" />
        <RecommendationSourceBadge source={null} />
      </div>,
    )
    expect(screen.getByText('AWS Cost Optimization Hub')).toBeInTheDocument()
    expect(screen.getByText('AWS Compute Optimizer')).toBeInTheDocument()
    expect(screen.getByText('Deterministic')).toBeInTheDocument()
    expect(screen.getByText('Unknown')).toBeInTheDocument()
  })
})

describe('ConfidenceBadge', () => {
  it('renders confidence labels', () => {
    wrap(
      <div>
        <ConfidenceBadge confidence="HIGH" />
        <ConfidenceBadge confidence="MEDIUM" />
        <ConfidenceBadge confidence="LOW" />
        <ConfidenceBadge confidence={null} />
      </div>,
    )
    expect(screen.getByText('High')).toBeInTheDocument()
    expect(screen.getByText('Medium')).toBeInTheDocument()
    expect(screen.getByText('Low')).toBeInTheDocument()
  })
})

describe('CapabilityStatusRow', () => {
  it('renders the capability status pill', () => {
    wrap(
      <CapabilityStatusRow
        label="Compute Optimizer"
        capability={{ status: 'INACTIVE', detail: null, last_checked_at: null, error_code: null }}
      />,
    )
    expect(screen.getByText('Compute Optimizer')).toBeInTheDocument()
    expect(screen.getByText('Inactive')).toBeInTheDocument()
  })

  it('handles missing capability', () => {
    wrap(<CapabilityStatusRow label="No capability" />)
    expect(screen.getByText('No capability')).toBeInTheDocument()
    expect(screen.getByText('Unknown')).toBeInTheDocument()
  })
})

describe('PartialWarning', () => {
  it('renders nothing when no warnings', () => {
    wrap(<PartialWarning warnings={[]} />)
    expect(screen.queryByTestId('partial-warning')).not.toBeInTheDocument()
  })

  it('renders warning lines', () => {
    wrap(
      <PartialWarning
        warnings={[
          { source: 'cloudwatch', code: 'AccessDenied', message: 'Access denied for EC2 metrics' },
        ]}
      />,
    )
    expect(screen.getByTestId('partial-warning')).toBeInTheDocument()
    expect(screen.getByText('cloudwatch')).toBeInTheDocument()
  })
})
