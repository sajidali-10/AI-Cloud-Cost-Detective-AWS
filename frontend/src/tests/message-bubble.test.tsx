// Phase 6C — MessageBubble rendering tests.

import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MessageBubble } from '../components/MessageBubble'
import type { ConversationMessage } from '../types/ai'
import { ThemeProvider } from '../lib/theme'

function wrap(node: React.ReactNode) {
  return render(<ThemeProvider>{node}</ThemeProvider>)
}

describe('MessageBubble', () => {
  it('renders a USER message with right alignment and the You label', () => {
    const m: ConversationMessage = {
      id: 1,
      conversation_id: 1,
      role: 'USER',
      content: 'Why is EC2 spend up?',
      created_at: '2026-01-01T00:00:00Z',
    }
    wrap(<MessageBubble message={m} />)
    expect(screen.getByTestId('message-user')).toBeInTheDocument()
    expect(screen.getByText('You')).toBeInTheDocument()
    expect(screen.getByText(/Why is EC2 spend up?/)).toBeInTheDocument()
  })

  it('renders an ASSISTANT message with markdown (heading + code)', () => {
    const m: ConversationMessage = {
      id: 2,
      conversation_id: 1,
      role: 'ASSISTANT',
      content: '## Summary\n\nEC2 is the driver.\n\n```\nUSD 1,234.56\n```',
      created_at: '2026-01-01T00:00:00Z',
      model_alias: 'cost-detective-free',
    }
    wrap(<MessageBubble message={m} />)
    expect(screen.getByTestId('message-assistant')).toBeInTheDocument()
    expect(screen.getByText(/Summary/)).toBeInTheDocument()
    expect(screen.getByText(/EC2 is the driver/)).toBeInTheDocument()
    expect(screen.getByText(/USD 1,234.56/)).toBeInTheDocument()
  })

  it('renders ASSISTANT warnings when present', () => {
    const m: ConversationMessage = {
      id: 3,
      conversation_id: 1,
      role: 'ASSISTANT',
      content: 'Answer',
      created_at: '2026-01-01T00:00:00Z',
      warnings: ['INSUFFICIENT_EVIDENCE'],
    }
    wrap(<MessageBubble message={m} />)
    expect(screen.getByTestId('message-warnings')).toBeInTheDocument()
    expect(screen.getByText(/INSUFFICIENT_EVIDENCE/)).toBeInTheDocument()
  })

  it('renders a SYSTEM_EVENT row as a neutral pill', () => {
    const m: ConversationMessage = {
      id: 4,
      conversation_id: 1,
      role: 'SYSTEM_EVENT',
      content: '',
      created_at: '2026-01-01T00:00:00Z',
      error_code: 'AI_UNAVAILABLE',
    }
    wrap(<MessageBubble message={m} />)
    expect(screen.getByTestId('message-system')).toBeInTheDocument()
    expect(screen.getByText('AI_UNAVAILABLE')).toBeInTheDocument()
  })

  it('escapes untrusted content (no raw HTML emission)', () => {
    const m: ConversationMessage = {
      id: 5,
      conversation_id: 1,
      role: 'USER',
      content: '<script>alert(1)</script>',
      created_at: '2026-01-01T00:00:00Z',
    }
    wrap(<MessageBubble message={m} />)
    expect(document.querySelector('script')).toBeNull()
    expect(screen.getByText(/<script>/)).toBeInTheDocument()
  })
})
