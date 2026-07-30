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
