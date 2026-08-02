import type {
  PortfolioPerformanceClosedTrade,
  PortfolioPerformanceEvent,
  PortfolioPerformanceOrder,
  PortfolioPerformancePosition,
  StrategyPerformancePayload,
} from "@/lib/api"

type JsonRecord = Record<string, unknown>

function record(value: unknown, path: string): JsonRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${path} must be an object`)
  }
  return value as JsonRecord
}

function required(source: JsonRecord, fields: readonly string[], path: string) {
  for (const field of fields) {
    if (!(field in source)) throw new TypeError(`${path}.${field} is required`)
  }
}

function finite(value: unknown, path: string) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new TypeError(`${path} must be a finite number`)
  }
}

function text(value: unknown, path: string) {
  if (typeof value !== "string") throw new TypeError(`${path} must be a string`)
}

function nullableText(value: unknown, path: string) {
  if (value !== null) text(value, path)
}

function nullableFinite(value: unknown, path: string) {
  if (value !== null) finite(value, path)
}

function truth(value: unknown, path: string) {
  if (typeof value !== "boolean") throw new TypeError(`${path} must be a boolean`)
}

function list(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) throw new TypeError(`${path} must be an array`)
  return value
}

const payloadFields = [
  "generated_at", "quote_error", "strategy", "summary", "runtime", "nav_history",
  "positions", "orders", "closed_trades", "events", "history_availability", "market",
  "market_label", "currency", "currency_symbol", "config", "allocation",
] as const

const summaryNumberFields = [
  "initial_cash", "nav", "cash", "reserved_cash", "market_value", "long_market_value",
  "short_liability", "buying_power", "margin_loan", "financing_cost", "borrow_cost",
  "cumulative_return_pct", "unrealized_pnl", "position_count", "max_positions",
] as const

const summaryNullableNumberFields = [
  "gross_exposure_pct", "net_exposure_pct", "margin_rate_pct", "maximum_drawdown_pct",
  "realized_pnl", "target_exposure_pct", "closed_trade_count", "win_rate_pct",
] as const

function validateAvailability(value: unknown, path: string) {
  const availability = record(value, path)
  required(availability, ["complete", "source", "reason"], path)
  truth(availability.complete, `${path}.complete`)
  text(availability.source, `${path}.source`)
  nullableText(availability.reason, `${path}.reason`)
}

export function parseStrategyPerformancePayload(input: unknown): StrategyPerformancePayload {
  const payload = record(input, "payload")
  required(payload, payloadFields, "payload")
  text(payload.generated_at, "payload.generated_at")
  nullableText(payload.quote_error, "payload.quote_error")
  text(payload.market, "payload.market")
  if (payload.market !== "cn" && payload.market !== "us") {
    throw new TypeError("payload.market is unsupported")
  }

  const strategy = record(payload.strategy, "strategy")
  required(strategy, [
    "id", "name", "revision", "stage", "market", "market_label", "currency",
    "currency_symbol", "initial_cash", "max_positions", "signal_model", "signal_time",
    "signal_data_cutoff", "allocation_model", "benchmark_symbol", "benchmark_name",
    "market_regime", "risk_level", "trading_mode", "target_exposure_pct",
    "exposure_policy", "margin_policy", "short_policy",
  ], "strategy")
  for (const field of ["id", "name", "stage", "market", "market_label", "currency", "currency_symbol"] as const) {
    text(strategy[field], `strategy.${field}`)
  }
  finite(strategy.revision, "strategy.revision")
  finite(strategy.initial_cash, "strategy.initial_cash")
  finite(strategy.max_positions, "strategy.max_positions")
  for (const field of ["signal_model", "signal_time", "signal_data_cutoff", "allocation_model", "benchmark_symbol", "benchmark_name", "risk_level", "trading_mode"] as const) {
    nullableText(strategy[field], `strategy.${field}`)
  }
  nullableFinite(strategy.target_exposure_pct, "strategy.target_exposure_pct")
  if (strategy.market_regime !== null) record(strategy.market_regime, "strategy.market_regime")
  for (const field of ["exposure_policy", "margin_policy", "short_policy"] as const) record(strategy[field], `strategy.${field}`)

  const summary = record(payload.summary, "summary")
  required(summary, [...summaryNumberFields, ...summaryNullableNumberFields], "summary")
  for (const field of summaryNumberFields) finite(summary[field], `summary.${field}`)
  for (const field of summaryNullableNumberFields) nullableFinite(summary[field], `summary.${field}`)

  const runtime = record(payload.runtime, "runtime")
  required(runtime, ["last_successful_pipeline_at", "last_successful_pipeline_run_id", "last_pipeline_admitted", "last_pipeline_stages", "last_pipeline_market_regime", "last_pipeline_data_quality", "availability"], "runtime")
  nullableText(runtime.last_successful_pipeline_at, "runtime.last_successful_pipeline_at")
  nullableText(runtime.last_successful_pipeline_run_id, "runtime.last_successful_pipeline_run_id")
  nullableFinite(runtime.last_pipeline_admitted, "runtime.last_pipeline_admitted")
  if (runtime.last_pipeline_stages !== null) list(runtime.last_pipeline_stages, "runtime.last_pipeline_stages")
  for (const field of ["last_pipeline_market_regime", "last_pipeline_data_quality"] as const) {
    if (runtime[field] !== null) record(runtime[field], `runtime.${field}`)
  }
  validateAvailability(runtime.availability, "runtime.availability")

  const history = record(payload.history_availability, "history_availability")
  required(history, ["nav", "lifecycle"], "history_availability")
  validateAvailability(history.nav, "history_availability.nav")
  validateAvailability(history.lifecycle, "history_availability.lifecycle")
  record(payload.config, "config")
  record(payload.allocation, "allocation")

  list(payload.nav_history, "nav_history").forEach((item, index) => {
    const path = `nav_history[${index}]`
    const point = record(item, path)
    required(point, ["at", "nav", "cash", "market_value", "cumulative_return_pct", "drawdown_pct", "risk_level", "trading_mode", "source"], path)
    for (const field of ["at", "source"] as const) text(point[field], `${path}.${field}`)
    for (const field of ["nav", "cash", "market_value", "cumulative_return_pct"] as const) finite(point[field], `${path}.${field}`)
    nullableFinite(point.drawdown_pct, `${path}.drawdown_pct`)
    nullableText(point.risk_level, `${path}.risk_level`)
    nullableText(point.trading_mode, `${path}.trading_mode`)
  })
  list(payload.positions, "positions").forEach((item, index) => {
    const path = `positions[${index}]`
    const position = record(item, path)
    required(position, ["slot_id", "name", "symbol", "first_entry_price", "first_entry_at", "current_price", "day_change_pct", "return_pct", "unrealized_pnl", "weight_pct", "quantity", "sellable_quantity", "trailing_active", "signal_invalid_days", "exit_distance_pct", "market_value", "average_cost", "position_side", "side", "position_mode", "borrow_rate_pct", "borrow_rate_source", "borrow_rate_estimated", "margin_used"], path)
    for (const field of ["slot_id", "current_price", "return_pct", "unrealized_pnl", "weight_pct", "quantity", "market_value", "average_cost", "margin_used"] as const) finite(position[field], `${path}.${field}`)
    for (const field of ["first_entry_price", "day_change_pct", "sellable_quantity", "signal_invalid_days", "exit_distance_pct", "borrow_rate_pct"] as const) nullableFinite(position[field], `${path}.${field}`)
    nullableText(position.first_entry_at, `${path}.first_entry_at`)
    for (const field of ["name", "symbol", "position_side", "side", "position_mode", "borrow_rate_source"] as const) text(position[field], `${path}.${field}`)
    truth(position.trailing_active, `${path}.trailing_active`)
    truth(position.borrow_rate_estimated, `${path}.borrow_rate_estimated`)
  })
  list(payload.orders, "orders").forEach((item, index) => {
    const path = `orders[${index}]`
    const order = record(item, path)
    required(order, ["id", "side", "symbol", "name", "quantity", "filled_quantity", "status", "reason", "created_at", "updated_at", "filled_notional", "commission_charged", "fees_charged", "strategy_revision", "position_side", "position_effect", "key", "control_epoch", "purpose", "slot_id", "signal_price", "score", "reserved_cash", "valid_date", "valid_session_date", "cancel_reason", "replacement_candidate"], path)
    for (const field of ["id", "side", "symbol", "name", "status", "reason", "created_at", "updated_at", "position_side", "position_effect"] as const) text(order[field], `${path}.${field}`)
    for (const field of ["quantity", "filled_quantity", "filled_notional", "commission_charged", "fees_charged"] as const) finite(order[field], `${path}.${field}`)
    for (const field of ["strategy_revision", "control_epoch", "slot_id", "signal_price", "score", "reserved_cash"] as const) nullableFinite(order[field], `${path}.${field}`)
    for (const field of ["key", "purpose", "valid_date", "valid_session_date", "cancel_reason"] as const) nullableText(order[field], `${path}.${field}`)
    if (order.replacement_candidate !== null) record(order.replacement_candidate, `${path}.replacement_candidate`)
  })
  list(payload.closed_trades, "closed_trades").forEach((item, index) => {
    const path = `closed_trades[${index}]`
    const trade = record(item, path)
    required(trade, ["id", "name", "symbol", "entry_price", "exit_price", "quantity", "realized_pnl", "return_pct", "reason", "closed_at", "strategy_revision", "position_side"], path)
    for (const field of ["id", "name", "symbol", "reason", "closed_at", "position_side"] as const) text(trade[field], `${path}.${field}`)
    for (const field of ["entry_price", "exit_price", "quantity", "realized_pnl", "return_pct", "strategy_revision"] as const) finite(trade[field], `${path}.${field}`)
  })
  list(payload.events, "events").forEach((item, index) => {
    const path = `events[${index}]`
    const event = record(item, path)
    required(event, ["id", "type", "occurred_at", "message", "strategy_revision", "key", "data"], path)
    for (const field of ["id", "type", "occurred_at", "message"] as const) text(event[field], `${path}.${field}`)
    nullableFinite(event.strategy_revision, `${path}.strategy_revision`)
    nullableText(event.key, `${path}.key`)
    record(event.data, `${path}.data`)
  })
  return payload as unknown as StrategyPerformancePayload
}

const translations: Record<string, string> = {
  LONG: "多头", SHORT: "空头", LONG_ONLY: "只做多", LONG_LEVERAGED: "杠杆做多",
  LONG_SHORT: "多空组合", MARGIN_CALL: "保证金追缴", COVER_ONLY: "仅允许回补",
  RISK_CHANGED: "风险状态变化", FILLED: "已成交", PARTIAL: "部分成交",
  INTENDED: "待成交", CANCELLED: "已取消", EXPIRED: "已过期",
}

export const escapeHtml = (value: unknown) => String(value ?? "").replace(
  /[&<'">]/g,
  (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]!,
)

const translate = (value: unknown) => translations[String(value ?? "")] ?? String(value ?? "—")
const number = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? value : 0
const percent = (value: unknown, digits = 2) => `${number(value) >= 0 ? "+" : ""}${number(value).toFixed(digits)}%`
const money = (value: unknown, symbol: string) => `${symbol}${number(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

function positionRows(rows: PortfolioPerformancePosition[], symbol: string) {
  if (!rows.length) return '<div class="empty">当前空仓，等待符合策略条件的入场信号。</div>'
  return `<div class="table-wrap"><table><thead><tr><th>股票</th><th>方向</th><th>当前价</th><th>持仓收益</th><th>数量</th><th>保证金 / 借券</th></tr></thead><tbody>${rows.map((position) => `<tr><td><strong>${escapeHtml(position.name)}</strong><div class="tiny">${escapeHtml(position.symbol)}</div></td><td><span class="side ${position.side === "SHORT" ? "short" : "long"}">${escapeHtml(translate(position.side))}</span></td><td>${money(position.current_price, symbol)}</td><td>${percent(position.return_pct)}</td><td>${escapeHtml(position.quantity)}</td><td>${money(position.margin_used, symbol)}<span class="tiny">${position.borrow_rate_pct === null ? "借券费率不可用" : `借券年化 ${number(position.borrow_rate_pct).toFixed(2)}% · ${position.borrow_rate_estimated ? "策略估算" : escapeHtml(position.borrow_rate_source)}`}</span></td></tr>`).join("")}</tbody></table></div>`
}

function orderRows(rows: PortfolioPerformanceOrder[], symbol: string) {
  if (!rows.length) return '<div class="empty">暂无订单记录。</div>'
  return `<div class="table-wrap"><table><tbody>${rows.map((order) => `<tr><td>${escapeHtml(order.updated_at)}</td><td>${escapeHtml(order.name)} · ${escapeHtml(order.symbol)}</td><td>${escapeHtml(translate(order.status))}</td><td>${money(order.filled_notional, symbol)}</td></tr>`).join("")}</tbody></table></div>`
}

function tradeRows(rows: PortfolioPerformanceClosedTrade[], symbol: string) {
  if (!rows.length) return '<div class="empty">尚无已退出持仓。</div>'
  return `<div class="table-wrap"><table><tbody>${rows.map((trade) => `<tr><td>${escapeHtml(trade.name)} · ${escapeHtml(trade.symbol)}</td><td>${money(trade.realized_pnl, symbol)}</td><td>${percent(trade.return_pct)}</td><td>${escapeHtml(trade.reason)}</td></tr>`).join("")}</tbody></table></div>`
}

function eventRows(rows: PortfolioPerformanceEvent[]) {
  if (!rows.length) return '<div class="empty">暂无策略事件。</div>'
  return `<div class="events">${rows.map((event) => `<article class="event"><time>${escapeHtml(event.occurred_at)}</time><div class="type">${escapeHtml(translate(event.type))}<span class="tiny">${escapeHtml(event.type)}</span></div><strong>${escapeHtml(event.message)}</strong></article>`).join("")}</div>`
}

function bindTabs(root: HTMLElement) {
  const tabs = [...root.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
  const panels = [...root.querySelectorAll<HTMLElement>('[role="tabpanel"]')]
  const activate = (index: number, focus = false) => {
    const normalized = (index + tabs.length) % tabs.length
    tabs.forEach((tab, tabIndex) => {
      const active = tabIndex === normalized
      tab.setAttribute("aria-selected", String(active))
      tab.tabIndex = active ? 0 : -1
    })
    panels.forEach((panel, panelIndex) => { panel.hidden = panelIndex !== normalized })
    if (focus) tabs[normalized].focus()
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => activate(index))
    tab.addEventListener("keydown", (event) => {
      const target = event.key === "ArrowRight" ? index + 1
        : event.key === "ArrowLeft" ? index - 1
          : event.key === "Home" ? 0
            : event.key === "End" ? tabs.length - 1
              : null
      if (target === null) return
      event.preventDefault()
      activate(target, true)
    })
  })
  activate(0)
}

export function renderPerformance(root: HTMLElement, payload: StrategyPerformancePayload) {
  const summary = payload.summary
  const strategy = payload.strategy
  const runtime = payload.runtime
  const symbol = payload.currency_symbol
  root.innerHTML = `<header><div><div class="eyebrow">Strategy Portfolio Ledger</div><h1>${escapeHtml(strategy.name)}</h1><div class="sub">${escapeHtml(payload.market_label)} · 策略 ID ${escapeHtml(strategy.id)} · 版本 v${escapeHtml(strategy.revision)}</div></div><div class="badge">${escapeHtml(translate(strategy.exposure_policy.mode))}</div></header>
    <section class="runtime"><div><div class="eyebrow">Pipeline Runtime</div><strong>${runtime.last_successful_pipeline_at ? escapeHtml(runtime.last_successful_pipeline_at) : "尚无成功运行"}</strong></div><div class="runtime-detail">Run ${escapeHtml(runtime.last_successful_pipeline_run_id ?? "—")} · 本次准入 ${escapeHtml(runtime.last_pipeline_admitted ?? 0)} 只</div></section>
    <section class="metrics"><div class="metric"><label>策略累计收益</label><strong>${percent(summary.cumulative_return_pct)}</strong></div><div class="metric"><label>当前净值</label><strong>${money(summary.nav, symbol)}</strong></div><div class="metric"><label>最大回撤</label><strong>${summary.maximum_drawdown_pct === null ? "—" : percent(-summary.maximum_drawdown_pct)}</strong></div><div class="metric"><label>退出胜率</label><strong>${summary.win_rate_pct === null ? "—" : `${number(summary.win_rate_pct).toFixed(1)}%`}</strong></div></section>
    <section class="risk-ledger"><div class="risk-copy"><div class="eyebrow">Exposure Tape</div><h2>风险敞口台账</h2></div><div class="risk-cell"><label>多头市值</label><strong>${money(summary.long_market_value, symbol)}</strong></div><div class="risk-cell"><label>空头负债</label><strong>${money(summary.short_liability, symbol)}</strong></div><div class="risk-cell"><label>总敞口</label><strong>${summary.gross_exposure_pct === null ? "—" : percent(summary.gross_exposure_pct)}</strong></div><div class="risk-cell"><label>净敞口</label><strong>${summary.net_exposure_pct === null ? "—" : percent(summary.net_exposure_pct)}</strong></div><div class="risk-cell"><label>保证金率</label><strong>${summary.margin_rate_pct === null ? "—" : percent(summary.margin_rate_pct)}</strong></div><div class="risk-cell"><label>可用购买力</label><strong>${money(summary.buying_power, symbol)}</strong></div><div class="risk-cell"><label>累计融资成本</label><strong>${money(summary.financing_cost, symbol)}</strong></div><div class="risk-cell"><label>累计借券成本</label><strong>${money(summary.borrow_cost, symbol)}</strong></div></section>
    <nav class="tabs" role="tablist" aria-label="策略账本视图"><button id="positions-tab" role="tab" aria-controls="positions">当前持仓</button><button id="orders-tab" role="tab" aria-controls="orders">订单</button><button id="trades-tab" role="tab" aria-controls="trades">退出记录</button><button id="events-tab" role="tab" aria-controls="events">事件账本</button></nav>
    <section id="positions" role="tabpanel" aria-labelledby="positions-tab">${positionRows(payload.positions, symbol)}</section><section id="orders" role="tabpanel" aria-labelledby="orders-tab">${orderRows(payload.orders, symbol)}</section><section id="trades" role="tabpanel" aria-labelledby="trades-tab">${tradeRows(payload.closed_trades, symbol)}</section><section id="events" role="tabpanel" aria-labelledby="events-tab">${eventRows(payload.events)}</section>`
  bindTabs(root)
}

export async function loadPerformance(
  strategyId: string,
  root: HTMLElement,
  fetcher: typeof fetch = fetch,
) {
  const response = await fetcher(`/api/strategies/${encodeURIComponent(strategyId)}/portfolio`, {
    headers: { Accept: "application/json" },
  })
  const body: unknown = await response.json()
  if (!response.ok) {
    const error = body && typeof body === "object" && "error" in body ? String((body as JsonRecord).error) : `HTTP ${response.status}`
    throw new Error(error)
  }
  const payload = parseStrategyPerformancePayload(body)
  renderPerformance(root, payload)
  return payload
}
