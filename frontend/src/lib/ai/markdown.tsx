// Phase 6C — Minimal markdown subset renderer.
//
// Hard rules:
//   * No raw HTML emission.  Everything is escaped before producing
//     React elements.  The output is plain text rendered through
//     normal JSX text nodes — React escapes by default.
//   * No links with arbitrary hrefs.  When a markdown link is
//     encountered we render the link TEXT only; the URL is shown
//     in a muted parenthetical (so the operator can see where the
//     citation points but the browser never navigates there).
//   * No images, no iframes, no embeds.
//   * Resource names, AWS tags, conversation history and user
//     questions are untrusted text — they flow through this
//     renderer and never receive `dangerouslySetInnerHTML`.
//
// Supported subset:
//   * ATX headings: # / ## / ### (larger levels collapse to h3)
//   * Unordered lists: lines starting with `-`, `*`, or `+`
//   * Ordered lists: lines starting with `1.` / `2.` / ...
//   * Paragraphs separated by blank lines
//   * Fenced code blocks: ``` ... ```
//   * Inline code: `code`
//   * Bold (**x**) and italic (*x* / _x_)
//   * Plain inline text

import type { ReactNode } from 'react'

// ---------------------------------------------------------------------------
// Escaping
// ---------------------------------------------------------------------------

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/**
 * Render an untrusted string safely.  We don't actually need HTML
 * because React text nodes are already escaped; this helper exists
 * only to document the invariant.
 */
function safeText(text: string): string {
  return text
}

// ---------------------------------------------------------------------------
// Inline tokenizer
// ---------------------------------------------------------------------------

interface InlineToken {
  kind: 'text' | 'code' | 'bold' | 'italic' | 'link'
  value: string
  /** For 'link', the textual label (already extracted). */
  label?: string
  /** For 'link', the raw URL. */
  url?: string
}

function tokenizeInline(input: string): InlineToken[] {
  const tokens: InlineToken[] = []
  let i = 0
  let buf = ''
  const flush = () => {
    if (buf.length > 0) {
      tokens.push({ kind: 'text', value: safeText(buf) })
      buf = ''
    }
  }
  while (i < input.length) {
    // Inline code: `...`
    if (input[i] === '`') {
      const end = input.indexOf('`', i + 1)
      if (end > i) {
        flush()
        tokens.push({ kind: 'code', value: safeText(input.slice(i + 1, end)) })
        i = end + 1
        continue
      }
    }
    // Link: [label](url)
    if (input[i] === '[') {
      const closeBracket = input.indexOf(']', i + 1)
      if (closeBracket > i && input[closeBracket + 1] === '(') {
        const closeParen = input.indexOf(')', closeBracket + 2)
        if (closeParen > closeBracket) {
          const label = input.slice(i + 1, closeBracket)
          const url = input.slice(closeBracket + 2, closeParen).trim()
          flush()
          tokens.push({ kind: 'link', value: '', label: safeText(label), url })
          i = closeParen + 1
          continue
        }
      }
    }
    // Bold: **x**
    if (input.startsWith('**', i)) {
      const end = input.indexOf('**', i + 2)
      if (end > i + 1) {
        flush()
        tokens.push({ kind: 'bold', value: safeText(input.slice(i + 2, end)) })
        i = end + 2
        continue
      }
    }
    // Italic: *x* or _x_
    if (input[i] === '*' && input[i + 1] !== '*') {
      const end = input.indexOf('*', i + 1)
      if (end > i) {
        flush()
        tokens.push({ kind: 'italic', value: safeText(input.slice(i + 1, end)) })
        i = end + 1
        continue
      }
    }
    if (input[i] === '_' && input[i + 1] !== '_') {
      const end = input.indexOf('_', i + 1)
      if (end > i) {
        flush()
        tokens.push({ kind: 'italic', value: safeText(input.slice(i + 1, end)) })
        i = end + 1
        continue
      }
    }
    buf += input[i]
    i += 1
  }
  flush()
  return tokens
}

function renderInline(tokens: InlineToken[]): ReactNode {
  return tokens.map((t, idx) => {
    switch (t.kind) {
      case 'text':
        return <span key={idx}>{t.value}</span>
      case 'code':
        return (
          <code
            key={idx}
            className="rounded bg-surface-2 px-1 py-0.5 font-mono text-[0.85em] text-fg-primary"
          >
            {t.value}
          </code>
        )
      case 'bold':
        return (
          <strong key={idx} className="font-semibold text-fg-primary">
            {t.value}
          </strong>
        )
      case 'italic':
        return (
          <em key={idx} className="italic text-fg-primary">
            {t.value}
          </em>
        )
      case 'link':
        return (
          <span
            key={idx}
            className="text-primary underline decoration-primary/40 underline-offset-2"
            data-md-link="true"
          >
            {t.label}
            {t.url ? (
              <span className="ml-1 text-fg-muted">({t.url})</span>
            ) : null}
          </span>
        )
      default:
        return null
    }
  })
}

// ---------------------------------------------------------------------------
// Block tokenizer
// ---------------------------------------------------------------------------

type Block =
  | { kind: 'heading'; level: 1 | 2 | 3; text: string }
  | { kind: 'paragraph'; text: string }
  | { kind: 'list'; ordered: boolean; items: string[] }
  | { kind: 'code'; lang: string; body: string }

export function parseMarkdown(input: string): Block[] {
  const lines = input.replace(/\r\n/g, '\n').split('\n')
  const blocks: Block[] = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]

    // Fenced code block
    if (line.startsWith('```')) {
      const lang = line.slice(3).trim()
      const body: string[] = []
      i += 1
      while (i < lines.length && !lines[i].startsWith('```')) {
        body.push(lines[i])
        i += 1
      }
      // skip closing fence if present
      if (i < lines.length && lines[i].startsWith('```')) i += 1
      blocks.push({ kind: 'code', lang, body: body.join('\n') })
      continue
    }

    // Heading
    const headingMatch = /^(#{1,3})\s+(.*)$/.exec(line)
    if (headingMatch) {
      blocks.push({
        kind: 'heading',
        level: headingMatch[1].length as 1 | 2 | 3,
        text: headingMatch[2],
      })
      i += 1
      continue
    }

    // Unordered list
    if (/^[-*+]\s+/.test(line)) {
      const items: string[] = []
      while (i < lines.length && /^[-*+]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^[-*+]\s+/, ''))
        i += 1
      }
      blocks.push({ kind: 'list', ordered: false, items })
      continue
    }

    // Ordered list
    if (/^\d+\.\s+/.test(line)) {
      const items: string[] = []
      while (i < lines.length && /^\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\d+\.\s+/, ''))
        i += 1
      }
      blocks.push({ kind: 'list', ordered: true, items })
      continue
    }

    // Blank line — skip
    if (line.trim() === '') {
      i += 1
      continue
    }

    // Paragraph: consume until blank / heading / list
    const para: string[] = [line]
    i += 1
    while (
      i < lines.length &&
      lines[i].trim() !== '' &&
      !/^(#{1,3})\s+/.test(lines[i]) &&
      !/^[-*+]\s+/.test(lines[i]) &&
      !/^\d+\.\s+/.test(lines[i]) &&
      !lines[i].startsWith('```')
    ) {
      para.push(lines[i])
      i += 1
    }
    blocks.push({ kind: 'paragraph', text: para.join(' ') })
  }
  return blocks
}

// ---------------------------------------------------------------------------
// Renderer
// ---------------------------------------------------------------------------

export function renderMarkdown(input: string): ReactNode {
  const blocks = parseMarkdown(input ?? '')
  return blocks.map((block, idx) => {
    switch (block.kind) {
      case 'heading': {
        const cls = 'mt-3 mb-1 font-semibold text-fg-primary'
        if (block.level === 1)
          return (
            <h3 key={idx} className={`${cls} text-base`}>
              {renderInline(tokenizeInline(block.text))}
            </h3>
          )
        if (block.level === 2)
          return (
            <h4 key={idx} className={`${cls} text-sm`}>
              {renderInline(tokenizeInline(block.text))}
            </h4>
          )
        return (
          <h5 key={idx} className={`${cls} text-sm`}>
            {renderInline(tokenizeInline(block.text))}
          </h5>
        )
      }
      case 'paragraph':
        return (
          <p key={idx} className="my-1.5 text-sm leading-6 text-fg-primary">
            {renderInline(tokenizeInline(block.text))}
          </p>
        )
      case 'list':
        if (block.ordered) {
          return (
            <ol
              key={idx}
              className="my-1.5 list-decimal pl-6 text-sm text-fg-primary"
            >
              {block.items.map((it, j) => (
                <li key={j}>{renderInline(tokenizeInline(it))}</li>
              ))}
            </ol>
          )
        }
        return (
          <ul key={idx} className="my-1.5 list-disc pl-6 text-sm text-fg-primary">
            {block.items.map((it, j) => (
              <li key={j}>{renderInline(tokenizeInline(it))}</li>
            ))}
          </ul>
        )
      case 'code':
        return (
          <pre
            key={idx}
            className="my-2 overflow-x-auto rounded-md border border-border bg-surface-2 p-3 font-mono text-xs text-fg-primary"
          >
            <code>{block.body}</code>
          </pre>
        )
      default:
        return null
    }
  })
}

// Exposed for tests that want to assert escape behaviour.
export const __markdownTestInternals = { escapeHtml, tokenizeInline, parseMarkdown }
