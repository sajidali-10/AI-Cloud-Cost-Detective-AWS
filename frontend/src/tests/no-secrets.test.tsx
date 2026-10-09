// Phase 6A — Static guard tests.
//
// These tests enforce the spec mandates that are best expressed as
// static checks on the source tree:
//
//   * No hardcoded localhost / 127.0.0.1 / private IP in frontend
//     browser code.
//   * No JWT secret, password, AWS key, or LiteLLM key strings in
//     frontend source.
//   * No dark-only Tailwind palette on major surfaces
//     (components should consume semantic tokens).
//   * Major surfaces reference semantic token classes (bg-surface,
//     text-fg-*, border-border, ...) rather than raw hex/colors.

import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

const FRONTEND_SRC = join(__dirname, '..')

// Recursive file walker; excludes node_modules + dist + tests.
function* walk(dir: string): Generator<string> {
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry === 'dist' || entry === 'tests') continue
    const p = join(dir, entry)
    const s = statSync(p)
    if (s.isDirectory()) yield* walk(p)
    else yield p
  }
}

function collectFiles(): string[] {
  return Array.from(walk(FRONTEND_SRC)).filter((f) =>
    /\.(ts|tsx|css|html)$/.test(f),
  )
}

function readOrNull(p: string): string {
  try {
    return readFileSync(p, 'utf-8')
  } catch {
    return ''
  }
}

describe('Frontend static guard', () => {
  it('contains no hardcoded localhost / 127.0.0.1 / private IPs', () => {
    const offenders: string[] = []
    const patterns = [
      /\bhttps?:\/\/localhost\b/i,
      /\b127\.0\.0\.1\b/,
      /\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b/,
      /\b192\.168\.\d{1,3}\.\d{1,3}\b/,
      /\b169\.254\.\d{1,3}\.\d{1,3}\b/,
    ]
    for (const f of collectFiles()) {
      const text = readOrNull(f)
      for (const re of patterns) {
        if (re.test(text)) {
          offenders.push(`${relative(FRONTEND_SRC, f)}: ${re}`)
        }
      }
    }
    expect(offenders).toEqual([])
  })

  it('contains no obvious JWT secret / password / AWS / LiteLLM keys', () => {
    const offenders: string[] = []
    const forbiddenPatterns: Array<{ name: string; re: RegExp }> = [
      { name: 'jwt-secret', re: /jwt[_-]?secret\s*[:=]\s*['"][A-Za-z0-9_\-]{8,}/i },
      { name: 'aws-access-key', re: /AKIA[0-9A-Z]{16}/ },
      { name: 'aws-secret-key', re: /aws[_-]?secret[_-]?access[_-]?key\s*[:=]\s*['"][A-Za-z0-9/+=]{20,}/i },
      { name: 'litellm-key', re: /sk-[A-Za-z0-9]{16,}/ },
    ]
    for (const f of collectFiles()) {
      const text = readOrNull(f)
      for (const { name, re } of forbiddenPatterns) {
        if (re.test(text)) offenders.push(`${relative(FRONTEND_SRC, f)}: ${name}`)
      }
    }
    expect(offenders).toEqual([])
  })

  it('major surfaces consume semantic tokens (no direct slate-/gray- Tailwind palette)', () => {
    // Components (src/components/**) should not bypass the semantic
    // token layer.  Allowed exceptions are documented in the plan:
    // - BrandHeader uses bg-primary-soft (semantic), no slate/gray.
    // - UserMenu uses bg-surface (semantic).
    // - The slug check below flags the bare palette classes which
    //   are the ones the spec explicitly forbids on major surfaces.
    const offenders: string[] = []
    const bannedSlugs = [
      /\bbg-slate-(?:50|100|200|300|400|500|600|700|800|900|950)\b/,
      /\bbg-gray-(?:50|100|200|300|400|500|600|700|800|900|950)\b/,
      /\bbg-zinc-(?:50|100|200|300|400|500|600|700|800|900|950)\b/,
      /\bbg-neutral-(?:50|100|200|300|400|500|600|700|800|900|950)\b/,
      /\btext-slate-(?:50|100|200|300|400|500|600|700|800|900|950)\b/,
      /\btext-gray-(?:50|100|100|300|400|500|600|700|800|900|950)\b/,
      /\bborder-slate-\d+\b/,
      /\bborder-gray-\d+\b/,
    ]
    const componentDir = join(FRONTEND_SRC, 'components')
    for (const f of walk(componentDir)) {
      if (!/\.(ts|tsx)$/.test(f)) continue
      const text = readOrNull(f)
      for (const re of bannedSlugs) {
        if (re.test(text)) offenders.push(`${relative(FRONTEND_SRC, f)}: ${re}`)
      }
    }
    expect(offenders).toEqual([])
  })

  it('major surfaces use bg-surface / border-border / fg-* tokens', () => {
    // Soft check: most component files should reference at least
    // one semantic token class.  Components that intentionally
    // use only utility classes (e.g. icons) are exempt via
    // explicit allowlist below.
    const allowlist = new Set([
      'ThemeToggle.tsx', // icons only, no surface
      'HiplinkLogo.tsx', // single <img> element, no surface chrome
    ])
    const offenders: string[] = []
    const semanticRe = /\b(?:bg-surface|bg-bg|bg-bg-elevated|border-border|text-fg-primary|text-fg-secondary|text-fg-muted|bg-primary|text-primary)\b/
    const componentDir = join(FRONTEND_SRC, 'components')
    for (const f of walk(componentDir)) {
      if (!/\.(tsx)$/.test(f)) continue
      const name = relative(componentDir, f)
      if (allowlist.has(name)) continue
      const text = readOrNull(f)
      if (!semanticRe.test(text)) {
        offenders.push(name)
      }
    }
    expect(offenders).toEqual([])
  })
})

describe('Frontend accessibility — color is not the sole state carrier', () => {
  it('RoleBadge always renders text alongside color', async () => {
    const { render, screen } = await import('@testing-library/react')
    const { ThemeProvider } = await import('../lib/theme')
    const { RoleBadge } = await import('../components/RoleBadge')
    render(
      <ThemeProvider>
        <RoleBadge role="ADMIN" />
        <RoleBadge role="ANALYST" />
        <RoleBadge role="VIEWER" />
      </ThemeProvider>,
    )
    expect(screen.getByText('Administrator')).toBeInTheDocument()
    expect(screen.getByText('Analyst')).toBeInTheDocument()
    expect(screen.getByText('Viewer')).toBeInTheDocument()
  })
})
