// Phase 6C — Message bubble.
//
// Renders a USER, ASSISTANT or SYSTEM_EVENT message with the right
// alignment and tone.  Never injects HTML — every text value flows
// through the markdown subset renderer (which itself emits React
// text nodes only).

import type { ConversationMessage } from '../types/ai'
import { renderMarkdown } from '../lib/ai/markdown'
import { formatLocalTime } from '../lib/format'

export interface MessageBubbleProps {
  message: ConversationMessage
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const time = formatLocalTime(message.created_at)
  if (message.role === 'SYSTEM_EVENT') {
    return (
      <div
        role="status"
        data-testid="message-system"
        className="
          mx-auto my-2 max-w-md rounded-md border border-border bg-surface-2
          px-3 py-2 text-center text-xs text-fg-muted
        "
      >
        <span className="font-mono">{message.error_code ?? 'SYSTEM_EVENT'}</span>
        <span className="ml-2">{time}</span>
      </div>
    )
  }
  const isUser = message.role === 'USER'
  const wrapperCls = isUser ? 'justify-end' : 'justify-start'
  const bubbleCls = isUser
    ? 'bg-primary-soft text-fg-primary border-primary/30'
    : 'bg-surface text-fg-primary border-border'
  return (
    <div
      data-testid={`message-${isUser ? 'user' : 'assistant'}`}
      className={`flex w-full ${wrapperCls}`}
    >
      <article
        className={`
          max-w-[80%] rounded-lg border px-3 py-2 text-sm shadow-card-sm
          ${bubbleCls}
        `}
      >
        <header className="mb-1 flex items-center gap-2 text-xs text-fg-muted">
          <span className="font-medium">
            {isUser ? 'You' : 'AI Cost Analyst'}
          </span>
          <time dateTime={message.created_at}>{time}</time>
          {message.role === 'ASSISTANT' && message.model_alias ? (
            <span className="font-mono text-[0.7rem] text-fg-muted">
              · {message.model_alias}
            </span>
          ) : null}
        </header>
        <div className="leading-6">
          {renderMarkdown(message.content || '')}
        </div>
        {message.role === 'ASSISTANT' &&
        message.warnings &&
        message.warnings.length > 0 ? (
          <ul
            className="mt-2 space-y-0.5 border-t border-border pt-2 text-xs text-warning"
            data-testid="message-warnings"
          >
            {message.warnings.map((w, idx) => (
              <li key={idx}>· {w}</li>
            ))}
          </ul>
        ) : null}
      </article>
    </div>
  )
}
