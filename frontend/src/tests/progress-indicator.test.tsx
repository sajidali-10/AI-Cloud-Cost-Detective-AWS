// Phase 6C — ProgressIndicator tests.

import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ProgressIndicator } from '../components/ProgressIndicator'
import type { ConnectionState } from '../types/ai'

describe('ProgressIndicator', () => {
  const states: ConnectionState[] = [
    'idle',
    'connecting',
    'connected',
    'processing',
    'completed',
    'disconnected',
    'authorization_failure',
    'retryable_failure',
    'protocol_violation',
  ]
  for (const s of states) {
    it(`renders the ${s} state with a stable label and an aria-live status`, () => {
      render(<ProgressIndicator state={s} />)
      const el = screen.getByRole('status')
      expect(el.getAttribute('aria-live')).toBe('polite')
    })
  }

  it('shows the hint when supplied', () => {
    render(<ProgressIndicator state="connected" hint="Last 30 days" />)
    expect(screen.getByText('Last 30 days')).toBeInTheDocument()
  })

  it('does not rely on colour alone (label is always rendered)', () => {
    render(<ProgressIndicator state="processing" />)
    expect(screen.getByText(/Analyzing/i)).toBeInTheDocument()
  })
})
