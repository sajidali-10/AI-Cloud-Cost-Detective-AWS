// Phase 6C — Composer behaviour tests.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Composer } from '../components/Composer'
import { ThemeProvider } from '../lib/theme'
import { __resetApiForTests } from '../lib/api'

function renderComposer(props: Partial<React.ComponentProps<typeof Composer>> = {}) {
  const onSubmit = vi.fn()
  const utils = render(
    <ThemeProvider>
      <Composer
        onSubmit={onSubmit}
        disabled={false}
        inflight={false}
        statusLabel="Ready"
        {...props}
      />
    </ThemeProvider>,
  )
  return { onSubmit, utils }
}

beforeEach(() => {
  __resetApiForTests()
})
afterEach(() => {
  vi.restoreAllMocks()
})

describe('Composer', () => {
  it('renders a labelled textarea', () => {
    renderComposer()
    expect(screen.getByLabelText(/ask about your aws environment/i)).toBeInTheDocument()
    expect(screen.getByTestId('composer-send')).toBeDisabled()
  })

  it('Enter submits, Shift+Enter inserts newline', async () => {
    const user = userEvent.setup()
    const { onSubmit } = renderComposer()
    const ta = screen.getByTestId('composer-textarea')
    await user.type(ta, 'Why did EC2 spend rise?')
    await user.keyboard('{Enter}')
    expect(onSubmit).toHaveBeenCalledWith('Why did EC2 spend rise?')
  })

  it('Shift+Enter inserts a newline without submitting', async () => {
    const user = userEvent.setup()
    const { onSubmit } = renderComposer()
    const ta = screen.getByTestId('composer-textarea') as HTMLTextAreaElement
    await user.click(ta)
    await user.keyboard('line one')
    await user.keyboard('{Shift>}{Enter}{/Shift}')
    await user.keyboard('line two')
    expect(ta.value).toContain('\n')
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('disables the Send button while inflight', () => {
    renderComposer({ inflight: true })
    expect(screen.getByTestId('composer-send')).toBeDisabled()
    expect(screen.getByTestId('composer-textarea')).toBeDisabled()
  })

  it('disables submit on empty input', async () => {
    const user = userEvent.setup()
    const { onSubmit } = renderComposer()
    const ta = screen.getByTestId('composer-textarea')
    await user.type(ta, '   ')
    expect(screen.getByTestId('composer-send')).toBeDisabled()
    fireEvent.keyDown(ta, { key: 'Enter' })
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('disables submit when question length exceeds the limit', () => {
    const { onSubmit } = renderComposer()
    const ta = screen.getByTestId('composer-textarea') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'x'.repeat(2001) } })
    expect(screen.getByTestId('composer-send')).toBeDisabled()
    fireEvent.keyDown(ta, { key: 'Enter' })
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('clears the textarea after submit', async () => {
    const user = userEvent.setup()
    const { onSubmit } = renderComposer()
    const ta = screen.getByTestId('composer-textarea') as HTMLTextAreaElement
    await user.type(ta, 'hello')
    await user.keyboard('{Enter}')
    expect(onSubmit).toHaveBeenCalled()
    await waitFor(() => expect(ta.value).toBe(''))
  })

  it('shows the counter and marks over-length with danger tone', () => {
    renderComposer()
    const ta = screen.getByTestId('composer-textarea') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'a'.repeat(2001) } })
    expect(screen.getByTestId('composer-counter').className).toContain('text-danger')
  })

  it('respects a custom statusLabel via aria-describedby', () => {
    renderComposer({ statusLabel: 'Analyzing AWS data' })
    const ta = screen.getByTestId('composer-textarea')
    expect(ta.getAttribute('aria-describedby')).toBe('composer-status')
    expect(screen.getByTestId('composer-status').textContent).toContain('Analyzing')
  })
})
