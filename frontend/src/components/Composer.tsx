// Phase 6C — Message composer.
//
//   * Multiline textarea.
//   * Enter → submit, Shift+Enter → newline.
//   * Disabled when the WebSocket is processing or when the question
//     is empty / exceeds MAX_QUESTION_LENGTH.
//   * Visible "Sending…" state with aria-busy.
//   * Never embeds system prompts or trusted evidence.

import { useCallback, useRef, useState, type KeyboardEvent } from 'react'
import { MAX_QUESTION_LENGTH } from '../types/ai'

export interface ComposerProps {
  onSubmit: (question: string) => void
  disabled: boolean
  placeholder?: string
  /** True while the server is processing a previous question. */
  inflight: boolean
  /** Optional connection-state label; surfaced via aria-describedby. */
  statusLabel?: string
  /** Reset counter — bumped by the parent when a previous answer arrives. */
  resetCounter?: number
}

export function Composer({
  onSubmit,
  disabled,
  inflight,
  placeholder = 'Ask about your AWS environment…',
  statusLabel,
  resetCounter,
}: ComposerProps) {
  const [value, setValue] = useState('')
  const taRef = useRef<HTMLTextAreaElement | null>(null)

  // Reset the textarea when the parent signals a new turn.
  // We don't reset on every render — only when resetCounter changes.
  // (Stored as state via a ref so we don't trigger re-renders.)
  const lastResetRef = useRef<number | undefined>(undefined)
  if (resetCounter !== undefined && resetCounter !== lastResetRef.current) {
    lastResetRef.current = resetCounter
    if (value !== '') {
      // Schedule on next microtask so we don't update state mid-render.
      queueMicrotask(() => setValue(''))
    }
  }

  const trimmed = value.trim()
  const tooLong = trimmed.length > MAX_QUESTION_LENGTH
  const canSubmit =
    !disabled &&
    !inflight &&
    !tooLong &&
    trimmed.length > 0

  const submit = useCallback(() => {
    if (!canSubmit) return
    onSubmit(trimmed)
    setValue('')
    // Refocus for fast follow-ups.
    queueMicrotask(() => taRef.current?.focus())
  }, [canSubmit, onSubmit, trimmed])

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      submit()
    }
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        submit()
      }}
      aria-label="Ask a question about your AWS environment"
      className="
        flex flex-col gap-2 rounded-md border border-border bg-surface
        p-3 shadow-card-sm
      "
      data-testid="composer"
    >
      <label htmlFor="composer-textarea" className="sr-only">
        Ask about your AWS environment
      </label>
      <textarea
        id="composer-textarea"
        ref={taRef}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder={placeholder}
        disabled={disabled || inflight}
        rows={2}
        aria-label="Ask about your AWS environment"
        aria-describedby={statusLabel ? 'composer-status' : undefined}
        aria-invalid={tooLong ? 'true' : 'false'}
        aria-busy={inflight ? 'true' : 'false'}
        maxLength={MAX_QUESTION_LENGTH + 200 /* allow buffer so user sees error */}
        data-testid="composer-textarea"
        className="
          w-full resize-y rounded-md border border-border bg-bg
          px-3 py-2 text-sm text-fg-primary
          placeholder:text-fg-muted
          focus:outline-none focus-visible:shadow-focus
          disabled:opacity-60
        "
      />
      <div className="flex items-center justify-between gap-2 text-xs">
        <div className="flex flex-col gap-0.5 text-fg-muted">
          <span id="composer-status" data-testid="composer-status">
            {statusLabel ?? 'Ready'}
          </span>
          <span
            className={tooLong ? 'text-danger' : 'text-fg-muted'}
            data-testid="composer-counter"
          >
            {trimmed.length}/{MAX_QUESTION_LENGTH}
          </span>
        </div>
        <button
          type="submit"
          disabled={!canSubmit}
          aria-disabled={!canSubmit}
          data-testid="composer-send"
          className="
            inline-flex items-center rounded-md border border-primary/40
            bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground
            hover:bg-primary-hover
            focus:outline-none focus-visible:shadow-focus
            disabled:cursor-not-allowed disabled:opacity-50
          "
        >
          {inflight ? 'Sending…' : 'Send'}
        </button>
      </div>
    </form>
  )
}
