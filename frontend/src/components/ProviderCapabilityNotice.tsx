// Phase 7C.2A — capability-aware empty state.
//
// When the operator selects Azure in Phase 7C.2A the Costs /
// Resources / Optimization surfaces are NOT yet implemented.
// The UI must:
//   * never call AWS endpoints with an Azure context;
//   * never show AWS data under the Azure selection;
//   * never show fake zero values;
//   * never silently switch back to AWS.
//
// This component renders a professional, consistent
// capability-aware state for any provider whose capability is
// UNSUPPORTED or NOT_CONFIGURED.

import { SectionCard } from './SectionCard'

export interface ProviderCapabilityNoticeProps {
  provider: 'AWS' | 'AZURE' | 'RACKSPACE'
  feature: 'Costs' | 'Resources' | 'Optimization'
  phase?: string
  message?: string
}

function featureCopy(
  provider: string,
  feature: 'Costs' | 'Resources' | 'Optimization',
  phase?: string,
): { title: string; body: string } {
  if (provider === 'AZURE') {
    if (feature === 'Costs') {
      return {
        title: 'Azure Costs',
        body:
          'Azure Cost Management integration will be enabled in ' +
          (phase || 'Phase 7C.2B') +
          '. ' +
          'Authentication and subscription access are connected.',
      }
    }
    if (feature === 'Resources') {
      return {
        title: 'Azure Resources',
        body:
          'Azure Resource Graph inventory will be enabled in ' +
          (phase || 'Phase 7C.2C') +
          '. ' +
          'Subscription access is verified; resource discovery is next.',
      }
    }
    return {
      title: 'Azure Optimization',
      body:
        'Azure Advisor recommendations will be enabled in ' +
        (phase || 'Phase 7C.2D') +
        '. ' +
        'Authentication and subscription access are connected.',
    }
  }
  return {
    title: `${provider} ${feature}`,
    body: `${provider} ${feature.toLowerCase()} integration is not yet implemented.`,
  }
}

export function ProviderCapabilityNotice({
  provider,
  feature,
  phase,
  message,
}: ProviderCapabilityNoticeProps) {
  const copy = featureCopy(provider, feature, phase)
  return (
    <SectionCard
      title={copy.title}
      data-testid={`${provider.toLowerCase()}-${feature.toLowerCase()}-notice`}
    >
      <div className="flex flex-col gap-3 py-2" data-testid="provider-capability-notice">
        <p className="text-sm text-fg-secondary">{message || copy.body}</p>
        <div className="rounded-md border border-info/30 bg-info-soft px-3 py-2 text-xs text-info">
          {provider === 'AZURE'
            ? 'Authentication and subscription access are connected. ' +
              'This page will activate automatically once the next phase ships.'
            : `${provider} ${feature.toLowerCase()} integration is reserved for a future release.`}
        </div>
      </div>
    </SectionCard>
  )
}

export default ProviderCapabilityNotice
