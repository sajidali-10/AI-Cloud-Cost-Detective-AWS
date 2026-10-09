// Phase 6C — EvidenceCard rendering tests.

import { describe, expect, it } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { EvidenceCard } from '../components/EvidenceCard'
import { ThemeProvider } from '../lib/theme'

function wrap(node: React.ReactNode) {
  return render(<ThemeProvider>{node}</ThemeProvider>)
}

describe('EvidenceCard', () => {
  it('renders a preview row and toggles the full key/value list', () => {
    wrap(
      <EvidenceCard
        index={0}
        citation={{ service: 'EC2', region: 'us-east-1', account_id: '123456789012' }}
      />,
    )
    expect(screen.getByText('Evidence 1')).toBeInTheDocument()
    const btn = screen.getByRole('button')
    expect(btn.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(btn)
    expect(btn.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByText('service')).toBeInTheDocument()
    expect(screen.getByText('EC2')).toBeInTheDocument()
  })

  it('does not invent keys or values that were not on the wire', () => {
    wrap(<EvidenceCard index={2} citation={{ service: 'S3' }} />)
    expect(screen.getByText(/service: S3/)).toBeInTheDocument()
  })

  it('escapes untrusted key/value content', () => {
    wrap(
      <EvidenceCard
        index={0}
        citation={{ note: '<script>alert(1)</script>' }}
      />,
    )
    fireEvent.click(screen.getByRole('button'))
    expect(document.querySelector('script')).toBeNull()
    expect(screen.getByText('<script>alert(1)</script>')).toBeInTheDocument()
  })

  it('renders an em-dash when a value is null', () => {
    wrap(<EvidenceCard index={0} citation={{ service: 'EC2', account_id: null }} />)
    fireEvent.click(screen.getByRole('button'))
    expect(screen.getByText('—')).toBeInTheDocument()
  })
})
