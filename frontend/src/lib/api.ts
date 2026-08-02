export type ParameterStatus = "live" | "derived" | "planned"
export type ParameterKind = "number" | "text" | "boolean" | "choice" | "multi" | "tags"

export interface Group {
  id: string
  label: string
  description: string
}

export interface Option {
  value: string
  label: string
  description?: string
  disabled?: boolean
}

export type UsDataSourcePolicy = "auto" | "alpaca" | "sina"

export interface UsMarketDataProviderStatus {
  id: "alpaca" | "sina"
  label: string
  available: boolean
  plan: string
  requires_credentials: boolean
}

export interface UsMarketDataStatus {
  selected_policy: UsDataSourcePolicy
  primary: "alpaca" | "sina"
  fallback: "sina" | ""
  effective_source: "alpaca" | "sina" | "unavailable"
  mode: "primary_ready" | "degraded_fallback" | "unavailable"
  alpaca_configured: boolean
  alpaca_feed: string
  alpaca_history_feed: string
  providers: UsMarketDataProviderStatus[]
}

export interface Parameter {
  id: string
  group: string
  label: string
  description: string
  kind: ParameterKind
  unit: string
  operator: "min" | "max" | "equals" | "in"
  status: ParameterStatus
  selected: boolean
  enabled: boolean
  effective: boolean
  value: unknown
  default: unknown
  options: Option[]
  step: number
  scale: number
  applicable?: boolean
}

export interface ReportDelivery {
  enabled: boolean
  channel: "feishu" | "telegram" | "discord" | "signal" | "origin" | "local"
  target: string
  hour: number
  minute: number
  frequency: "daily" | "weekdays"
  push_on_empty: boolean
  push_on_error: boolean
}

export interface PortfolioConfig {
  enabled: boolean
  initial_cash: number
  max_positions: number
  target_weight_pct: number
  stop_loss_pct: number
  trailing_activation_pct: number
  trailing_drawdown_pct: number
  signal_invalid_days: number
  replacement_score_delta: number
  replacement_cost_multiple: number
  warning_drawdown_pct: number
  derisk_drawdown_pct: number
  halt_drawdown_pct: number
  warning_max_exposure_pct: number
  commission_rate_pct: number
  minimum_commission_cny: number
  stamp_duty_rate_pct: number
  transfer_fee_rate_pct: number
  slippage_bps: number
  max_bar_participation_pct: number
  benchmark_symbol: string
  benchmark_name: string
}

export type ExposureMode = "LONG_ONLY" | "LONG_LEVERAGED" | "LONG_SHORT"

export interface ExposurePolicy {
  mode: ExposureMode
  max_positions: number
  max_gross_exposure_pct: number
  max_net_exposure_pct: number
  max_long_exposure_pct: number
  max_short_exposure_pct: number
  max_long_position_pct: number
  max_short_position_pct: number
}

export interface MarginPolicy {
  maintenance_margin_pct: number
  liquidation_buffer_pct: number
  financing_apr_pct: number
  accrual_mode: "DAILY"
}

export interface ShortPolicy {
  signal_model: string
  require_shortable: boolean
  require_easy_to_borrow: boolean
  estimated_borrow_apr_pct: number
  cost_stress_multiplier: number
  block_on_borrow_data_missing: boolean
  stop_loss_pct: number
  trailing_activation_pct: number
  trailing_rebound_pct: number
  event_blackout_sessions: number
  squeeze_rise_pct: number
  squeeze_volume_ratio: number
  maximum_volatility_20d_pct: number
}

export type PerformanceJsonValue =
  | string
  | number
  | boolean
  | null
  | PerformanceJsonValue[]
  | { readonly [key: string]: PerformanceJsonValue }

export interface PerformanceJsonObject {
  readonly [key: string]: PerformanceJsonValue
}

export interface PerformanceHistoryAvailability {
  complete: boolean
  source: string
  reason: string | null
}

export interface PerformanceHistoryStatus {
  nav: PerformanceHistoryAvailability
  lifecycle: PerformanceHistoryAvailability
}

export interface PortfolioPerformanceStrategy {
  id: string
  name: string
  revision: number
  stage: string
  market: "cn" | "us"
  market_label: string
  currency: string
  currency_symbol: string
  initial_cash: number
  max_positions: number
  signal_model: string | null
  signal_time: string | null
  signal_data_cutoff: string | null
  allocation_model: string | null
  benchmark_symbol: string | null
  benchmark_name: string | null
  market_regime: PerformanceJsonObject | null
  risk_level: string | null
  trading_mode: string | null
  target_exposure_pct: number | null
  exposure_policy: ExposurePolicy
  margin_policy: MarginPolicy
  short_policy: ShortPolicy
}

export interface PortfolioPerformanceSummary {
  initial_cash: number
  nav: number
  cash: number
  reserved_cash: number
  market_value: number
  long_market_value: number
  short_liability: number
  gross_exposure_pct: number | null
  net_exposure_pct: number | null
  margin_rate_pct: number | null
  buying_power: number
  margin_loan: number
  financing_cost: number
  borrow_cost: number
  cumulative_return_pct: number
  maximum_drawdown_pct: number | null
  realized_pnl: number | null
  unrealized_pnl: number
  position_count: number
  max_positions: number
  target_exposure_pct: number | null
  closed_trade_count: number | null
  win_rate_pct: number | null
}

export interface PortfolioPerformancePosition {
  slot_id: number
  name: string
  symbol: string
  first_entry_price: number | null
  first_entry_at: string | null
  current_price: number
  day_change_pct: number | null
  return_pct: number
  unrealized_pnl: number
  weight_pct: number
  quantity: number
  sellable_quantity: number | null
  trailing_active: boolean
  signal_invalid_days: number | null
  exit_distance_pct: number | null
  market_value: number
  average_cost: number
  position_side: "LONG" | "SHORT"
  side: "LONG" | "SHORT"
  position_mode: string
  borrow_rate_pct: number | null
  borrow_rate_source: "strategy_estimate" | "unavailable"
  borrow_rate_estimated: boolean
  margin_used: number
}

export interface PortfolioPerformanceRuntime {
  last_successful_pipeline_at: string | null
  last_successful_pipeline_run_id: string | null
  last_pipeline_admitted: number | null
  last_pipeline_stages: PerformanceJsonObject[] | null
  last_pipeline_market_regime: PerformanceJsonObject | null
  last_pipeline_data_quality: PerformanceJsonObject | null
  availability: PerformanceHistoryAvailability
}

export interface PortfolioPerformanceNavPoint {
  at: string
  nav: number
  cash: number
  market_value: number
  cumulative_return_pct: number
  drawdown_pct: number | null
  risk_level: string | null
  trading_mode: string | null
  source: string
}

export interface PortfolioPerformanceOrder {
  id: string
  side: "BUY" | "SELL"
  symbol: string
  name: string
  quantity: number
  filled_quantity: number
  status: "INTENDED" | "PARTIAL" | "FILLED" | "CANCELLED" | "EXPIRED"
  reason: string
  created_at: string
  updated_at: string
  filled_notional: number
  commission_charged: number
  fees_charged: number
  strategy_revision: number | null
  position_side: "LONG" | "SHORT"
  position_effect: "OPEN" | "INCREASE" | "REDUCE" | "CLOSE"
  key: string | null
  control_epoch: number | null
  purpose: "ENTRY" | "EXIT" | null
  slot_id: number | null
  signal_price: number | null
  score: number | null
  reserved_cash: number | null
  valid_date: string | null
  valid_session_date: string | null
  cancel_reason: string | null
  replacement_candidate: PerformanceJsonObject | null
}

export interface PortfolioPerformanceClosedTrade {
  id: string
  name: string
  symbol: string
  entry_price: number
  exit_price: number
  quantity: number
  realized_pnl: number
  return_pct: number
  reason: string
  closed_at: string
  strategy_revision: number
  position_side: "LONG" | "SHORT"
}

export interface PortfolioPerformanceEvent {
  id: string
  type: string
  occurred_at: string
  message: string
  strategy_revision: number | null
  key: string | null
  data: PerformanceJsonObject
}

export interface StrategyPerformancePayload {
  generated_at: string
  quote_error: string | null
  strategy: PortfolioPerformanceStrategy
  summary: PortfolioPerformanceSummary
  runtime: PortfolioPerformanceRuntime
  nav_history: PortfolioPerformanceNavPoint[]
  positions: PortfolioPerformancePosition[]
  orders: PortfolioPerformanceOrder[]
  closed_trades: PortfolioPerformanceClosedTrade[]
  events: PortfolioPerformanceEvent[]
  history_availability: PerformanceHistoryStatus
  market: "cn" | "us"
  market_label: string
  currency: string
  currency_symbol: string
  config: PerformanceJsonObject
  allocation: PerformanceJsonObject
}

export interface SignalConfig {
  model: "factor_rank_v1"
  run_time: string
  data_cutoff: "previous_trading_day_close"
  minimum_history_rows: number
  max_hot_candidates: number
  factor_weights: Record<string, number>
}

export interface AllocationConfig {
  model: "trend_breadth_v1"
  enabled: boolean
  minimum_universe_size: number
  breadth_threshold_pct: number
  risk_on_min_signals: number
  neutral_min_signals: number
  risk_on_exposure_pct: number
  neutral_exposure_pct: number
  risk_off_exposure_pct: number
  unknown_exposure_pct: number
  minimum_candidate_momentum20_pct: number
  minimum_candidate_trend: number
  exit_on_risk_off: boolean
  rebalance_to_target_exposure: boolean
}

export interface StrategyLifecycle {
  stage: "draft" | "backtesting" | "paper" | "live" | "paused" | "archived"
  paper_sessions: number
}

export interface DeliverySync {
  status: "synced" | "paused" | "unavailable" | "error"
  message: string
  job_id?: string
  schedule?: string
  deliver?: string
}

export interface StrategyConfig {
  version: number
  id: string | null
  name: string
  description: string
  created_at: string | null
  updated_at: string | null
  revision: number
  lifecycle: StrategyLifecycle
  signal: SignalConfig
  allocation: AllocationConfig
  delivery: ReportDelivery
  portfolio: PortfolioConfig
  exposure_policy: ExposurePolicy
  margin_policy: MarginPolicy
  short_policy: ShortPolicy
  parameters: Record<string, { enabled: boolean; value: unknown }>
}

export interface StrategySummary {
  id: string
  name: string
  description: string
  created_at: string | null
  updated_at: string | null
  revision: number
  lifecycle: StrategyLifecycle
  signal: SignalConfig
  allocation: AllocationConfig
  active_parameters: number
  is_active: boolean
  delivery: ReportDelivery
  market: {
    code: "cn" | "us"
    label: string
    currency: string
    currency_symbol: string
  }
  us_market_data?: UsMarketDataStatus | null
}

export interface StrategyLibrary {
  active_strategy_id: string | null
  strategies: StrategySummary[]
  delivery_sync?: DeliverySync
}

export interface ConfigPayload {
  groups: Group[]
  parameters: Parameter[]
  config: StrategyConfig
  delivery_sync?: DeliverySync
  market: {
    code: "cn" | "us"
    label: string
    timezone: string
    currency: string
    currency_symbol: string
    lot_size: number
  }
  us_market_data: UsMarketDataStatus
}

export interface StrategyUpdate {
  id: string
  enabled: boolean
  value: unknown
  reason: string
}

export interface StrategyDraft {
  strategy: string
  updates: StrategyUpdate[]
  recognized_count: number
  recognized_phrases: string[]
  message: string
}

export interface ChatMessage {
  role: "user" | "assistant"
  content: string
}

export interface StrategyChatResponse {
  status: "question" | "review" | "confirmed"
  message: string
  summary: string
  strategy_text: string
  draft: StrategyDraft | null
  provider: "ai" | "fallback"
}

export type StrategyRunStatus = "queued" | "running" | "succeeded" | "failed"

export interface StrategyRun {
  id: string
  strategy_id: string
  strategy_name: string
  status: StrategyRunStatus
  created_at: string
  started_at: string | null
  completed_at: string | null
  duration_seconds: number | null
  report?: string
  error: string | null
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(payload.error || `请求失败 (${response.status})`)
  return payload as T
}

export const convertStrategy = (strategy: string) => api<StrategyDraft>("/api/strategy/convert", { method: "POST", body: JSON.stringify({ strategy }) })
export const chatStrategy = (messages: ChatMessage[], strategyId: string) => api<StrategyChatResponse>("/api/strategy/chat", { method: "POST", body: JSON.stringify({ messages, strategy_id: strategyId }) })
export const getStrategies = () => api<StrategyLibrary>("/api/strategies")
export const getStrategy = (id: string) => api<ConfigPayload>(`/api/strategies/${id}`)
export const createStrategy = (name: string, description: string) => api<ConfigPayload>("/api/strategies", { method: "POST", body: JSON.stringify({ name, description }) })
export const saveStrategy = (id: string, config: StrategyConfig) => api<ConfigPayload>(`/api/strategies/${id}`, { method: "PUT", body: JSON.stringify(config) })
export const activateStrategy = (id: string) => api<StrategyLibrary>(`/api/strategies/${id}/activate`, { method: "POST", body: "{}" })
export const deactivateStrategy = (id: string) => api<StrategyLibrary>(`/api/strategies/${id}/deactivate`, { method: "POST", body: "{}" })
export const duplicateStrategy = (id: string) => api<ConfigPayload>(`/api/strategies/${id}/duplicate`, { method: "POST", body: "{}" })
export const deleteStrategy = (id: string) => api<StrategyLibrary>(`/api/strategies/${id}`, { method: "DELETE" })
export const resetStrategy = (id: string) => api<ConfigPayload>(`/api/strategies/${id}/reset`, { method: "POST", body: "{}" })
export const startStrategyRun = (id: string) => api<StrategyRun>(`/api/strategies/${id}/runs`, { method: "POST", body: "{}" })
export const getStrategyRuns = (id: string) => api<{ runs: StrategyRun[] }>(`/api/strategies/${id}/runs`)
export const getStrategyRun = (id: string) => api<StrategyRun>(`/api/runs/${id}`)
export const syncStrategyDelivery = (id: string) => api<{ delivery_sync: DeliverySync }>(`/api/strategies/${id}/sync-delivery`, { method: "POST", body: "{}" })
