// Phase 7A — AI status / provider badge safety tests.

import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import React from 'react'

import { AiProviderBadge } from '../components/AiProviderBadge'
import type { AIStatusResponse } from '../types/ai'

function renderBadge(status: AIStatusResponse | null) {
  return render(<AiProviderBadge status={status} />)
}

describe('AiProviderBadge — Phase 7A', () => {
  it('renders OK state with model alias', () => {
    renderBadge({
      status: 'OK',
      ai_enabled: true,
      litellm_reachable: true,
      provider_configured: true,
      model_alias: 'cost-analyst',
      provider_model: 'openai/gpt-4o-mini',
      litellm_base_url: 'http://litellm:4000',
      timeout_seconds: 30,
      max_output_tokens: 800,
      context_limits: {},
      required_env_var: null,
      message: null,
    })
    expect(screen.getByText(/AI ready · cost-analyst/i)).toBeTruthy()
  })

  it('surfaces the required env var when provider is not configured', () => {
    renderBadge({
      status: 'DISABLED',
      ai_enabled: true,
      litellm_reachable: false,
      provider_configured: false,
      model_alias: 'cost-analyst',
      provider_model: null,
      litellm_base_url: 'http://litellm:4000',
      timeout_seconds: 30,
      max_output_tokens: 800,
      context_limits: {},
      required_env_var: 'LITELLM_API_KEY',
      message: 'Waiting on provider credential.',
    })
    expect(screen.getByText(/AI waiting on LITELLM_API_KEY/i)).toBeTruthy()
  })

  it('renders AI disabled when ai_enabled=false', () => {
    renderBadge({
      status: 'DISABLED',
      ai_enabled: false,
      litellm_reachable: false,
      provider_configured: false,
      model_alias: 'cost-analyst',
      provider_model: null,
      litellm_base_url: 'http://litellm:4000',
      timeout_seconds: 30,
      max_output_tokens: 800,
      context_limits: {},
      required_env_var: null,
      message: null,
    })
    expect(screen.getByText(/AI disabled/i)).toBeTruthy()
  })

  it('never renders credential fragments', () => {
    const { container } = renderBadge({
      status: 'OK',
      ai_enabled: true,
      litellm_reachable: true,
      provider_configured: true,
      model_alias: 'cost-analyst',
      provider_model: 'openai/gpt-4o-mini',
      litellm_base_url: 'http://litellm:4000',
      timeout_seconds: 30,
      max_output_tokens: 800,
      context_limits: {},
      required_env_var: null,
      message: null,
    })
    const html = container.innerHTML
    expect(html).not.toMatch(/sk-[a-z0-9]{20,}/i)
    expect(html).not.toMatch(/Bearer\s+[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/)
  })
})
