// Phase 6B — Typed FinOps surface.
//
// These interfaces mirror the backend Pydantic schemas exactly.  Do
// NOT add fields the backend does not emit, and do NOT widen the
// types of fields that come back as numeric strings — Cost Explorer
// returns `Decimal` values serialized as strings to preserve
// precision, so the wire type is `string | null`.  Presentation code
// converts via `formatCurrencyDecimal` in `lib/format.ts`.

// ---------------------------------------------------------------------------
// Identity (GET /api/aws/identity)
// ---------------------------------------------------------------------------

export interface AwsIdentity {
  account: string
  arn: string
  user_id: string
  region: string | null
}

// ---------------------------------------------------------------------------
// Cost Explorer (GET /api/aws/costs?days=)
// ---------------------------------------------------------------------------

export type CacheStatus = 'HIT' | 'MISS' | 'REFRESHED'

export interface CostPeriod {
  start: string // ISO date YYYY-MM-DD
  end: string
  days: number
}

export interface DailyCostPoint {
  date: string
  amount: string // Decimal serialized as string
  unit: string
}

export interface ServiceCost {
  service: string
  amount: string
  unit: string
}

export interface RegionCost {
  region: string
  amount: string
  unit: string
}

export interface CostReport {
  account_id: string
  period: CostPeriod
  previous_period: CostPeriod
  currency: string
  total_cost: string
  previous_period_cost: string
  change_amount: string
  change_percent: string | null // null when previous period is zero
  estimated: boolean
  daily_trend: DailyCostPoint[]
  by_service: ServiceCost[]
  by_region: RegionCost[]
  source: 'AWS_COST_EXPLORER'
}

export interface CostReportResponse {
  report: CostReport
  cache_status: CacheStatus
  cached_at: string | null
  expires_at: string | null
}

// ---------------------------------------------------------------------------
// Phase 1 resources (GET /api/aws/resources?region=)
// ---------------------------------------------------------------------------

export type ServiceStatus = 'ok' | 'denied' | 'error'

export interface ServiceResult<T = Record<string, unknown>> {
  service: string
  status: ServiceStatus
  items: T[]
  error_code: string | null
}

export interface Ec2Instance {
  instance_id: string
  state: string | null
  instance_type: string | null
  region: string | null
  tags: Record<string, string>
}
export interface EbsVolume {
  volume_id: string
  size_gb: number | null
  state: string | null
  region: string | null
  attachments: number | null
  availability_zone: string | null
  tags: Record<string, string>
}
export interface ElasticIp {
  public_ip: string
  allocation_id: string | null
  region: string | null
  association_id: string | null
  instance_id: string | null
  network_interface_id: string | null
  private_ip_address: string | null
  tags: Record<string, string>
}
export interface NatGateway {
  nat_gateway_id: string
  state: string | null
  region: string | null
  tags: Record<string, string>
}
export interface LoadBalancerV2 {
  arn: string
  name: string
  type: 'application' | 'network' | 'gateway'
  region: string | null
  tags: Record<string, string>
}
export interface RdsInstance {
  db_instance_identifier: string
  db_instance_class: string | null
  engine: string | null
  status: string | null
  region: string | null
  tags: Record<string, string>
}
export interface LambdaFunction {
  function_name: string
  function_arn: string | null
  runtime: string | null
  region: string | null
  tags: Record<string, string>
}
export interface S3Bucket {
  name: string
  creation_date: string | null
  region: string | null
  tags: Record<string, string>
}

export interface ResourcesResponse {
  region: string | null
  services: {
    ec2?: ServiceResult<Ec2Instance>
    ebs?: ServiceResult<EbsVolume>
    eip?: ServiceResult<ElasticIp>
    nat?: ServiceResult<NatGateway>
    elbv2?: ServiceResult<LoadBalancerV2>
    rds?: ServiceResult<RdsInstance>
    lambda?: ServiceResult<LambdaFunction>
    s3?: ServiceResult<S3Bucket>
  }
  enrichment?: Record<string, unknown>
}

// ---------------------------------------------------------------------------
// Utilization (POST /api/aws/utilization)
// ---------------------------------------------------------------------------

export type DataQuality = 'high' | 'medium' | 'low' | 'no_data'

export interface UtilizationDatapoint {
  timestamp: string
  value: string // Decimal serialized as string
}

export interface UtilizationMetricSeries {
  namespace: string
  metric_name: string
  statistic: string
  unit: string
  datapoints: UtilizationDatapoint[]
  data_quality: DataQuality
}

export interface UtilizationWarning {
  source: string
  service: string
  resource_id: string | null
  code: string
  message: string
}

export interface ResourceUtilization {
  resource_id: string
  resource_type: string
  service: string
  region: string
  lookback_days: number
  metrics: UtilizationMetricSeries[]
  data_quality: DataQuality
  warnings: UtilizationWarning[]
}

export interface UtilizationResponse {
  region: string
  lookback_days: number
  resources: ResourceUtilization[]
  warnings: UtilizationWarning[]
}

export interface UtilizationRequestBody {
  region: string
  lookback_days: number
  resource_types?: string[]
}

// ---------------------------------------------------------------------------
// Optimization (Phase 3)
// ---------------------------------------------------------------------------

export type CapabilityStatus =
  | 'ACTIVE'
  | 'INACTIVE'
  | 'PENDING'
  | 'FAILED'
  | 'ACCESS_DENIED'
  | 'UNAVAILABLE'
  | 'NOT_ENROLLED'
  | 'AVAILABLE'

export type OptimizationStatus = 'SUCCESS' | 'PARTIAL_SUCCESS' | 'FAILED'

export type SavingsSource =
  | 'AWS_COST_OPTIMIZATION_HUB'
  | 'AWS_COMPUTE_OPTIMIZER'
  | 'CALCULATED'
  | 'UNKNOWN'

export type OptimizationConfidence = 'HIGH' | 'MEDIUM' | 'LOW'

export type ResourceTypeSlug =
  | 'EC2'
  | 'EBS_VOLUME'
  | 'LAMBDA_FUNCTION'
  | 'RDS_DB_INSTANCE'
  | 'ELASTIC_IP'
  | 'NAT_GATEWAY'
  | 'LOAD_BALANCER'

export type RecommendationAction =
  | 'RIGHTSIZE'
  | 'DELETE_UNUSED'
  | 'RELEASE_UNUSED'
  | 'STOP_IDLE'
  | 'REVIEW_DELETE_UNATTACHED_EBS'
  | 'REVIEW_RELEASE_UNUSED_EIP'
  | 'REVIEW_LOW_UTILIZATION_EC2'
  | 'REVIEW_IDLE_NAT_GATEWAY'
  | 'REVIEW_IDLE_LOAD_BALANCER'
  | 'REVIEW_LOW_UTILIZATION_RDS'

export interface ServiceCapability {
  status: CapabilityStatus
  detail: string | null
  last_checked_at: string | null
  error_code: string | null
}

export interface CapabilitiesResponse {
  region: string
  account_id: string | null
  compute_optimizer: ServiceCapability
  cost_optimization_hub: ServiceCapability
  deterministic_engine: ServiceCapability
  supported_resource_types: ResourceTypeSlug[]
  supported_lookback_days: number[]
  warnings: OptimizationNotice[]
}

export interface RecommendationEvidence {
  source: SavingsSource
  confidence: OptimizationConfidence
  data: Record<string, unknown>
  reason_codes: string[]
}

export interface Recommendation {
  recommendation_id: string
  resource_id: string
  resource_arn: string | null
  resource_type: ResourceTypeSlug
  region: string
  account_id: string | null
  action: RecommendationAction
  title: string
  finding: string
  current_configuration: Record<string, unknown>
  recommended_configuration: Record<string, unknown>
  estimated_monthly_savings: string | null // Decimal string or null
  currency: string
  savings_percentage: string | null
  savings_source: SavingsSource
  primary_source: SavingsSource
  sources: SavingsSource[]
  confidence: OptimizationConfidence
  data_quality: string
  reason_codes: string[]
  restart_needed: boolean | null
  rollback_possible: boolean | null
  evidence: RecommendationEvidence[]
  aws_recommendation_ids: string[]
  detected_at: string | null
}

export interface OptimizationNotice {
  source: string
  code: string
  message: string
  region: string | null
}

export interface RecommendationsResponse {
  region: string
  account_id: string | null
  days: number
  status: OptimizationStatus
  count: number
  recommendations: Recommendation[]
  warnings: OptimizationNotice[]
}

export interface SummaryByCategory {
  key: string
  count: number
  estimated_monthly_savings: string | null
  currency: string
}

export interface OptimizationSummaryResponse {
  region: string
  account_id: string | null
  days: number
  status: OptimizationStatus
  total_recommendations: number
  total_estimated_monthly_savings: string | null
  currency: string
  by_resource_type: SummaryByCategory[]
  by_action: SummaryByCategory[]
  by_source: SummaryByCategory[]
  by_confidence: SummaryByCategory[]
  recommendations_without_savings: number
  warnings: OptimizationNotice[]
}

// ---------------------------------------------------------------------------
// Cross-cutting UI types
// ---------------------------------------------------------------------------

/** LOOKBACK_DAYS allowed by the backend (Phase 2/3 validation). */
export const ALLOWED_LOOKBACK_DAYS = [7, 30, 60, 90] as const
export type LookbackDays = (typeof ALLOWED_LOOKBACK_DAYS)[number]

/** Result envelope shared by every data hook in lib/finops/*. */
export interface FinopsQuery<T> {
  data: T | null
  loading: boolean
  error: Error | null
  warnings: OptimizationNotice[] | UtilizationWarning[]
  /** Wall-clock timestamp when the data was last refreshed. */
  refreshedAt: Date | null
  /** Force a refetch; resets the cache key. */
  refresh: () => void
}
