// Phase 6C — ConversationList component tests.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { ConversationList } from '../components/ConversationList'
import type { Conversation } from '../types/ai'
import { ThemeProvider } from '../lib/theme'

function wrap(node: React.ReactNode) {
  return render(<ThemeProvider>{node}</ThemeProvider>)
}

beforeEach(() => {})
afterEach(() => {
  vi.restoreAllMocks()
})

const CONV: Conversation = {
  id: 1,
  user_id: 1,
  title: 'Cost Q1 analysis',
  is_archived: false,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-02T00:00:00Z',
  last_message_at: '2026-01-02T00:00:00Z',
}

describe('ConversationList', () => {
  it('shows the empty state when no conversations exist', () => {
    wrap(
      <ConversationList
        conversations={[]}
        loading={false}
        error={null}
        activeId={null}
        onSelect={() => undefined}
        onCreate={() => undefined}
      />,
    )
    expect(screen.getByText(/No conversations yet/i)).toBeInTheDocument()
    expect(screen.getByTestId('new-chat')).toBeInTheDocument()
  })

  it('renders skeleton rows during loading', () => {
    const { container } = wrap(
      <ConversationList
        conversations={[]}
        loading={true}
        error={null}
        activeId={null}
        onSelect={() => undefined}
        onCreate={() => undefined}
      />,
    )
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0)
  })

  it('renders a failure message distinct from the empty state', () => {
    wrap(
      <ConversationList
        conversations={[]}
        loading={false}
        error="HTTP 500"
        activeId={null}
        onSelect={() => undefined}
        onCreate={() => undefined}
      />,
    )
    expect(screen.getByRole('alert').textContent).toMatch(/Could not load conversations/)
    expect(screen.queryByText(/No conversations yet/i)).toBeNull()
  })

  it('renders rows and fires onSelect on click', () => {
    const onSelect = vi.fn()
    wrap(
      <ConversationList
        conversations={[CONV]}
        loading={false}
        error={null}
        activeId={null}
        onSelect={onSelect}
        onCreate={() => undefined}
      />,
    )
    const row = screen.getByTestId('conversation-row-1')
    fireEvent.click(row)
    expect(onSelect).toHaveBeenCalledWith(1)
  })

  it('marks the active row with aria-current', () => {
    wrap(
      <ConversationList
        conversations={[CONV]}
        loading={false}
        error={null}
        activeId={1}
        onSelect={() => undefined}
        onCreate={() => undefined}
      />,
    )
    expect(screen.getByTestId('conversation-row-1').getAttribute('aria-current')).toBe('true')
  })

  it('fires onCreate when the New chat button is pressed', () => {
    const onCreate = vi.fn()
    wrap(
      <ConversationList
        conversations={[]}
        loading={false}
        error={null}
        activeId={null}
        onSelect={() => undefined}
        onCreate={onCreate}
      />,
    )
    fireEvent.click(screen.getByTestId('new-chat'))
    expect(onCreate).toHaveBeenCalled()
  })
})
