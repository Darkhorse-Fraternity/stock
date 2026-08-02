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

function literal(value: unknown, allowed: readonly string[], path: string) {
  text(value, path)
  if (!allowed.includes(value as string)) throw new TypeError(`${path} is unsupported`)
}

function nullableLiteral(value: unknown, allowed: readonly string[], path: string) {
  if (value !== null) literal(value, allowed, path)
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

const markets = ["cn", "us"] as const
const exposureModes = ["LONG_ONLY", "LONG_LEVERAGED", "LONG_SHORT"] as const
const riskLevels = ["NORMAL", "MEDIUM", "WARNING", "DERISK", "DE_RISKING", "BREACHED", "MANUAL_HALT", "INSOLVENT_HALT", "REDUCE_ONLY", "MARGIN_CALL"] as const
const tradingModes = ["RUNNING", "ENTRY_BLOCKED", "EXIT_ONLY", "COVER_ONLY", "MANUAL_HALT", "INSOLVENT_HALT", "REDUCE_ONLY", "MARGIN_CALL"] as const
const positionSides = ["LONG", "SHORT"] as const
const positionModes = ["NORMAL", "COVER_ONLY"] as const
const borrowSources = ["strategy_estimate", "unavailable"] as const
const orderSides = ["BUY", "SELL"] as const
const orderStatuses = ["INTENDED", "PARTIAL", "FILLED", "CANCELLED", "EXPIRED"] as const
const positionEffects = ["OPEN", "INCREASE", "REDUCE", "CLOSE"] as const
const orderPurposes = ["ENTRY", "EXIT"] as const

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
  literal(payload.market, markets, "payload.market")

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
  literal(strategy.market, markets, "strategy.market")
  if (strategy.market !== payload.market) throw new TypeError("strategy.market mismatch")
  finite(strategy.revision, "strategy.revision")
  finite(strategy.initial_cash, "strategy.initial_cash")
  finite(strategy.max_positions, "strategy.max_positions")
  for (const field of ["signal_model", "signal_time", "signal_data_cutoff", "allocation_model", "benchmark_symbol", "benchmark_name"] as const) {
    nullableText(strategy[field], `strategy.${field}`)
  }
  nullableLiteral(strategy.risk_level, riskLevels, "strategy.risk_level")
  nullableLiteral(strategy.trading_mode, tradingModes, "strategy.trading_mode")
  nullableFinite(strategy.target_exposure_pct, "strategy.target_exposure_pct")
  if (strategy.market_regime !== null) record(strategy.market_regime, "strategy.market_regime")
  const exposurePolicy = record(strategy.exposure_policy, "strategy.exposure_policy")
  literal(exposurePolicy.mode, exposureModes, "strategy.exposure_policy.mode")
  const marginPolicy = record(strategy.margin_policy, "strategy.margin_policy")
  literal(marginPolicy.accrual_mode, ["DAILY"], "strategy.margin_policy.accrual_mode")
  record(strategy.short_policy, "strategy.short_policy")

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
    nullableLiteral(point.risk_level, riskLevels, `${path}.risk_level`)
    nullableLiteral(point.trading_mode, tradingModes, `${path}.trading_mode`)
  })
  list(payload.positions, "positions").forEach((item, index) => {
    const path = `positions[${index}]`
    const position = record(item, path)
    required(position, ["slot_id", "name", "symbol", "first_entry_price", "first_entry_at", "current_price", "day_change_pct", "return_pct", "unrealized_pnl", "weight_pct", "quantity", "sellable_quantity", "trailing_active", "signal_invalid_days", "exit_distance_pct", "market_value", "average_cost", "position_side", "side", "position_mode", "borrow_rate_pct", "borrow_rate_source", "borrow_rate_estimated", "margin_used"], path)
    for (const field of ["slot_id", "current_price", "return_pct", "unrealized_pnl", "weight_pct", "quantity", "market_value", "average_cost", "margin_used"] as const) finite(position[field], `${path}.${field}`)
    for (const field of ["first_entry_price", "day_change_pct", "sellable_quantity", "signal_invalid_days", "exit_distance_pct", "borrow_rate_pct"] as const) nullableFinite(position[field], `${path}.${field}`)
    nullableText(position.first_entry_at, `${path}.first_entry_at`)
    for (const field of ["name", "symbol", "position_side", "side", "position_mode", "borrow_rate_source"] as const) text(position[field], `${path}.${field}`)
    literal(position.position_side, positionSides, `${path}.position_side`)
    literal(position.side, positionSides, `${path}.side`)
    if (position.position_side !== position.side) throw new TypeError(`${path}.side mismatch`)
    literal(position.position_mode, positionModes, `${path}.position_mode`)
    literal(position.borrow_rate_source, borrowSources, `${path}.borrow_rate_source`)
    truth(position.trailing_active, `${path}.trailing_active`)
    truth(position.borrow_rate_estimated, `${path}.borrow_rate_estimated`)
    if ((position.first_entry_price === null) !== (position.first_entry_at === null)) {
      throw new TypeError(`${path} first_entry_price and first_entry_at must be present together`)
    }
    if (position.side === "LONG" && (
      position.borrow_rate_pct !== null
      || position.borrow_rate_source !== "unavailable"
      || position.borrow_rate_estimated !== false
    )) {
      throw new TypeError(`${path} LONG borrow contract is contradictory`)
    }
    if (position.side === "SHORT" && (
      position.borrow_rate_pct === null
      || number(position.borrow_rate_pct) < 0
      || position.borrow_rate_source !== "strategy_estimate"
      || position.borrow_rate_estimated !== true
    )) {
      throw new TypeError(`${path} SHORT borrow contract is contradictory`)
    }
  })
  list(payload.orders, "orders").forEach((item, index) => {
    const path = `orders[${index}]`
    const order = record(item, path)
    required(order, ["id", "side", "symbol", "name", "quantity", "filled_quantity", "status", "reason", "created_at", "updated_at", "filled_notional", "commission_charged", "fees_charged", "strategy_revision", "position_side", "position_effect", "key", "control_epoch", "purpose", "slot_id", "signal_price", "score", "reserved_cash", "valid_date", "valid_session_date", "cancel_reason", "replacement_candidate"], path)
    for (const field of ["id", "side", "symbol", "name", "status", "reason", "created_at", "updated_at", "position_side", "position_effect"] as const) text(order[field], `${path}.${field}`)
    for (const field of ["quantity", "filled_quantity", "filled_notional", "commission_charged", "fees_charged"] as const) finite(order[field], `${path}.${field}`)
    for (const field of ["strategy_revision", "control_epoch", "slot_id", "signal_price", "score", "reserved_cash"] as const) nullableFinite(order[field], `${path}.${field}`)
    for (const field of ["key", "purpose", "valid_date", "valid_session_date", "cancel_reason"] as const) nullableText(order[field], `${path}.${field}`)
    literal(order.side, orderSides, `${path}.side`)
    literal(order.status, orderStatuses, `${path}.status`)
    literal(order.position_side, positionSides, `${path}.position_side`)
    literal(order.position_effect, positionEffects, `${path}.position_effect`)
    nullableLiteral(order.purpose, orderPurposes, `${path}.purpose`)
    if (order.replacement_candidate !== null) record(order.replacement_candidate, `${path}.replacement_candidate`)
  })
  list(payload.closed_trades, "closed_trades").forEach((item, index) => {
    const path = `closed_trades[${index}]`
    const trade = record(item, path)
    required(trade, ["id", "name", "symbol", "entry_price", "exit_price", "quantity", "realized_pnl", "return_pct", "reason", "closed_at", "strategy_revision", "position_side"], path)
    for (const field of ["id", "name", "symbol", "reason", "closed_at", "position_side"] as const) text(trade[field], `${path}.${field}`)
    for (const field of ["entry_price", "exit_price", "quantity", "realized_pnl", "return_pct", "strategy_revision"] as const) finite(trade[field], `${path}.${field}`)
    literal(trade.position_side, positionSides, `${path}.position_side`)
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
  INTENDED: "待成交", CANCELLED: "已取消", EXPIRED: "已过期", NORMAL: "正常",
  WARNING: "回撤预警", DERISK: "强制降仓", DE_RISKING: "强制降仓", BREACHED: "人工暂停",
  RUNNING: "运行中", ENTRY_BLOCKED: "禁止开仓", EXIT_ONLY: "只减仓", MANUAL_HALT: "已暂停",
  INSOLVENT_HALT: "资不抵债暂停", REDUCE_ONLY: "仅减仓", BUY: "买入", SELL: "卖出",
  OPEN: "开仓", INCREASE: "加仓", REDUCE: "减仓", CLOSE: "平仓", ENTRY: "入场", EXIT: "退出",
  strategy_estimate: "策略估算", unavailable: "不可用", market_regime: "市场状态",
  absolute_momentum: "绝对动量",
}

export const escapeHtml = (value: unknown) => String(value ?? "").replace(
  /[&<'">]/g,
  (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]!,
)

const translate = (value: unknown) => translations[String(value ?? "")] ?? String(value ?? "—")
const number = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? value : 0
const percent = (value: unknown, digits = 2) => `${number(value) >= 0 ? "+" : ""}${number(value).toFixed(digits)}%`
const money = (value: unknown, symbol: string) => {
  const amount = number(value)
  return `${amount < 0 ? "-" : ""}${symbol}${Math.abs(amount).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}
const dateTime = (value: unknown) => String(value ?? "—").replace("T", " ").replace(/([+-]\d{2}:\d{2}|Z)$/, "")
const tone = (value: unknown) => number(value) > 0 ? "up" : number(value) < 0 ? "down" : ""

function navChart(rows: StrategyPerformancePayload["nav_history"], symbol: string) {
  if (!rows.length) return '<div class="empty">尚无净值记录</div>'
  const values = rows.map((point) => number(point.nav))
  const low = Math.min(...values)
  const high = Math.max(...values)
  const span = Math.max(high - low, 1)
  const width = 800
  const height = 180
  const padding = 18
  const points = values.map((value, index) => [
    padding + (width - padding * 2) * (rows.length === 1 ? 0.5 : index / (rows.length - 1)),
    padding + (height - padding * 2) * (1 - (value - low) / span),
  ])
  const line = points.map((point) => point.join(",")).join(" ")
  const area = `${padding},${height - padding} ${line} ${width - padding},${height - padding}`
  return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="策略净值曲线"><line x1="${padding}" y1="${height - padding}" x2="${width - padding}" y2="${height - padding}" stroke="#b9c8d6" stroke-dasharray="5 5"/><polygon points="${area}" fill="#2866ae18"/><polyline points="${line}" fill="none" stroke="#2866ae" stroke-width="3"/>${points.map((point, index) => `<circle cx="${point[0]}" cy="${point[1]}" r="${index === points.length - 1 ? 5 : 3}" fill="#c83e4c"><title>${escapeHtml(rows[index].at)} · ${escapeHtml(money(rows[index].nav, symbol))}</title></circle>`).join("")}<text x="${padding}" y="12" fill="#66768a" font-size="11">${escapeHtml(money(high, symbol))}</text><text x="${padding}" y="${height}" fill="#66768a" font-size="11">${escapeHtml(money(low, symbol))}</text></svg>`
}

function borrow(position: PortfolioPerformancePosition) {
  if (position.side !== "SHORT") return "无需借券"
  const rate = position.borrow_rate_pct === null ? "费率不可用" : `借券年化 ${number(position.borrow_rate_pct).toFixed(2)}%`
  const source = position.borrow_rate_estimated ? "策略估算" : translate(position.borrow_rate_source)
  return `${rate} · ${source}`
}

function positionRows(rows: PortfolioPerformancePosition[], symbol: string) {
  if (!rows.length) return '<div class="empty">当前空仓，等待符合策略条件的入场信号。</div>'
  return `<div class="table-wrap"><table><thead><tr><th>槽位 / 股票</th><th>首次成交</th><th>当前价</th><th>持仓收益</th><th>仓位 / 保证金</th><th>数量 / 可卖</th><th>风控 / 借券</th><th>退出距离</th></tr></thead><tbody>${rows.map((position) => `<tr data-row-kind="position"><td><strong>#${escapeHtml(position.slot_id)} ${escapeHtml(position.name)}</strong><span class="side ${position.side === "SHORT" ? "short" : "long"}">${escapeHtml(translate(position.side))}</span><div class="tiny">${escapeHtml(position.symbol)} · ${escapeHtml(translate(position.position_mode))}</div></td><td>${position.first_entry_price === null ? "—" : money(position.first_entry_price, symbol)}<div class="tiny">${escapeHtml(dateTime(position.first_entry_at))}</div></td><td>${money(position.current_price, symbol)}<div class="tiny">当日 ${position.day_change_pct === null ? "—" : percent(position.day_change_pct)}</div></td><td><span class="pill ${tone(position.return_pct)}">${percent(position.return_pct)}</span><div class="tiny">${money(position.unrealized_pnl, symbol)}</div></td><td>${number(position.weight_pct).toFixed(1)}%<div class="tiny">占用 ${money(position.margin_used, symbol)}</div></td><td>${escapeHtml(position.quantity)} / ${escapeHtml(position.sellable_quantity ?? "—")}</td><td>${position.trailing_active ? "追踪退出已激活" : "常规监控"}<div class="tiny">${escapeHtml(borrow(position))}</div><div class="tiny">信号失效 ${escapeHtml(position.signal_invalid_days ?? "—")} 日</div></td><td>${position.exit_distance_pct === null ? "—" : percent(position.exit_distance_pct)}</td></tr>`).join("")}</tbody></table></div>`
}

function orderRows(rows: PortfolioPerformanceOrder[], symbol: string) {
  if (!rows.length) return '<div class="empty">暂无订单记录。</div>'
  return `<div class="table-wrap"><table><thead><tr><th>时间</th><th>方向 / 股票</th><th>状态 / 原因</th><th>委托 / 成交 / 剩余</th><th>成交金额 / 费用</th><th>仓位语义</th><th>版本 / 有效期</th></tr></thead><tbody>${rows.map((order) => {
    const remaining = Math.max(0, number(order.quantity) - number(order.filled_quantity))
    return `<tr data-row-kind="order"><td>${escapeHtml(dateTime(order.updated_at))}<div class="tiny">创建 ${escapeHtml(dateTime(order.created_at))}</div></td><td><strong>${escapeHtml(order.side)} ${escapeHtml(order.name)}</strong><div class="tiny">${escapeHtml(order.symbol)}</div></td><td><span class="pill">${escapeHtml(translate(order.status))}</span><div class="tiny">${escapeHtml(order.reason)}</div>${order.cancel_reason ? `<div class="tiny">${escapeHtml(order.cancel_reason)}</div>` : ""}</td><td>${escapeHtml(order.quantity)} / ${escapeHtml(order.filled_quantity)} / ${escapeHtml(remaining)}</td><td>${money(order.filled_notional, symbol)}<div class="tiny">费用 ${money(order.fees_charged, symbol)} · 佣金 ${money(order.commission_charged, symbol)}</div></td><td>${escapeHtml(order.position_side)} / ${escapeHtml(order.position_effect)} / ${escapeHtml(order.purpose ?? "—")}<div class="tiny">${escapeHtml(translate(order.position_side))} · ${escapeHtml(translate(order.position_effect))} · ${escapeHtml(translate(order.purpose))}</div></td><td>v${escapeHtml(order.strategy_revision ?? "—")}<div class="tiny">${escapeHtml(order.valid_session_date ?? order.valid_date ?? "—")} · slot ${escapeHtml(order.slot_id ?? "—")}</div></td></tr>`
  }).join("")}</tbody></table></div>`
}

function tradeRows(rows: PortfolioPerformanceClosedTrade[], symbol: string) {
  if (!rows.length) return '<div class="empty">尚无已退出持仓。</div>'
  return `<div class="table-wrap"><table><thead><tr><th>股票 / 方向</th><th>入场 / 退出</th><th>数量</th><th>净收益（已含费用）</th><th>收益率</th><th>退出原因 / 时间</th><th>版本</th></tr></thead><tbody>${rows.map((trade) => `<tr data-row-kind="trade"><td><strong>${escapeHtml(trade.name)}</strong><div class="tiny">${escapeHtml(trade.symbol)} · ${escapeHtml(translate(trade.position_side))}</div></td><td>${money(trade.entry_price, symbol)} → ${money(trade.exit_price, symbol)}</td><td>${escapeHtml(trade.quantity)}</td><td class="${tone(trade.realized_pnl)}">${money(trade.realized_pnl, symbol)}<div class="tiny">净收益（已含费用）</div></td><td class="${tone(trade.return_pct)}">${percent(trade.return_pct)}</td><td>${escapeHtml(trade.reason)}<div class="tiny">${escapeHtml(dateTime(trade.closed_at))}</div></td><td>v${escapeHtml(trade.strategy_revision)}</td></tr>`).join("")}</tbody></table></div>`
}

function eventRows(rows: PortfolioPerformanceEvent[]) {
  if (!rows.length) return '<div class="empty">暂无策略事件。</div>'
  const riskTypes = new Set(["MARGIN_CALL", "COVER_ONLY", "RISK_CHANGED", "MANUAL_HALT", "INSOLVENT_HALT", "REDUCE_ONLY"])
  const highlight = new URLSearchParams(window.location.search).get("event") ?? ""
  return `<div class="events">${rows.map((event) => `<article id="event-${escapeHtml(event.id)}" class="event ${riskTypes.has(event.type) ? "risk" : ""} ${event.id === highlight ? "highlight" : ""}"><time>${escapeHtml(dateTime(event.occurred_at))}</time><div class="type">${escapeHtml(translate(event.type))}<span class="tiny">${escapeHtml(event.type)}</span></div><div><strong>${escapeHtml(event.message)}</strong><div class="tiny">策略版本 v${escapeHtml(event.strategy_revision ?? "—")} · ${escapeHtml(event.id)}${event.key ? ` · ${escapeHtml(event.key)}` : ""}</div><pre class="event-data">${escapeHtml(JSON.stringify(event.data, null, 2))}</pre></div></article>`).join("")}</div>`
}

function stageDiagnostics(rows: unknown) {
  if (!Array.isArray(rows)) return "暂无 Pipeline 诊断"
  return rows.map((raw) => {
    const row = raw && typeof raw === "object" ? raw as JsonRecord : {}
    const diagnostics = Array.isArray(row.diagnostics) && row.diagnostics[0] && typeof row.diagnostics[0] === "object" ? row.diagnostics[0] as JsonRecord : {}
    return `${translate(row.stage)}: ${Object.entries(diagnostics).map(([key, value]) => `${key}=${String(value)}`).join(", ")}`
  }).join(" · ") || "暂无 Pipeline 诊断"
}

function qualityDiagnostics(value: unknown) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "暂无数据覆盖诊断"
  const quality = value as JsonRecord
  if (!Object.keys(quality).length) return "暂无数据覆盖诊断"
  const count = (field: string, fallback = 0) => number(quality[field] ?? fallback)
  return `股票池 ${count("raw_count")} → 实时行情 ${count("quote_count", count("raw_count"))} → 基础过滤 ${count("basic_count")} → 历史特征 ${count("history_ready_count")} → 策略过滤 ${count("strategy_filtered_count")} → 动量准入 ${count("absolute_momentum_count")} → 最终 ${count("selected_count")} · ${quality.status === "READY" ? "可运行" : "已阻断"} · ${String(quality.source_mode ?? "未知数据源")}`
}

function bindTabs(root: HTMLElement, initialPanel = "positions") {
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
  const initialIndex = Math.max(0, panels.findIndex((panel) => panel.id === initialPanel))
  activate(initialIndex)
}

export function renderPerformance(root: HTMLElement, payload: StrategyPerformancePayload) {
  const summary = payload.summary
  const strategy = payload.strategy
  const runtime = payload.runtime
  const symbol = payload.currency_symbol
  const regime = strategy.market_regime && typeof strategy.market_regime === "object" ? strategy.market_regime as JsonRecord : {}
  const gross = number(summary.gross_exposure_pct)
  const net = number(summary.net_exposure_pct)
  const longShare = summary.nav ? number(summary.long_market_value) / number(summary.nav) * 100 : 0
  const highlight = new URLSearchParams(window.location.search).get("event") ?? ""
  const quoteError = payload.quote_error ? `<div role="alert" data-kind="quote-error" class="quote-error">行情：${escapeHtml(payload.quote_error)}</div>` : ""
  const costNote = payload.market === "us" ? "美股模拟盘按整股口径，已计滑点、融资与借券估算成本。" : "A 股模拟盘收益已计佣金、印花税、过户费与滑点。"
  root.innerHTML = `<header><div><div class="eyebrow">Strategy Portfolio Ledger</div><h1>${escapeHtml(strategy.name)}</h1><div class="sub">${escapeHtml(payload.market_label)} · 策略 ID ${escapeHtml(strategy.id)} · 版本 v${escapeHtml(strategy.revision)} · ${escapeHtml(strategy.stage)} · 基准 ${escapeHtml(strategy.benchmark_name ?? "—")}</div></div><div class="badge">${escapeHtml(translate(strategy.exposure_policy.mode))} · ${escapeHtml(translate(strategy.risk_level))} / ${escapeHtml(translate(strategy.trading_mode))}</div></header>
    <section class="runtime" data-section="runtime"><div><div class="eyebrow">Pipeline Runtime</div><strong>${runtime.last_successful_pipeline_at ? `最后成功：${escapeHtml(dateTime(runtime.last_successful_pipeline_at))}` : "尚无成功运行"}</strong><div class="tiny">Run ${escapeHtml(runtime.last_successful_pipeline_run_id ?? "—")}</div><div class="tiny">运行历史：${escapeHtml(runtime.availability.complete ? "完整" : runtime.availability.reason ?? "不完整")} · ${escapeHtml(runtime.availability.source)}</div></div><div class="runtime-detail">市场状态 ${escapeHtml(regime.label ?? regime.state ?? "—")}（${escapeHtml(regime.state ?? "—")}） · 目标仓位 ${number(regime.target_exposure_pct ?? strategy.target_exposure_pct).toFixed(0)}% · 本次准入 ${escapeHtml(runtime.last_pipeline_admitted ?? 0)} 只<br><span class="tiny">${escapeHtml(qualityDiagnostics(runtime.last_pipeline_data_quality))}<br>${escapeHtml(stageDiagnostics(runtime.last_pipeline_stages))}</span></div></section>
    <section class="metrics"><div class="metric"><label>策略累计收益</label><strong>${percent(summary.cumulative_return_pct)}</strong></div><div class="metric"><label>当前净值</label><strong>${money(summary.nav, symbol)}</strong></div><div class="metric"><label>最大回撤</label><strong>${summary.maximum_drawdown_pct === null ? "—" : percent(-summary.maximum_drawdown_pct)}</strong></div><div class="metric"><label>退出胜率</label><strong>${summary.win_rate_pct === null ? "—" : `${number(summary.win_rate_pct).toFixed(1)}%`}</strong></div></section>
    <section class="risk-ledger"><div class="risk-copy"><div class="eyebrow">Exposure Tape</div><h2>风险敞口台账</h2><p>多头资产、空头负债、保证金与持有成本使用同一估值快照。</p></div><div class="risk-cell"><label>多头市值</label><strong>${money(summary.long_market_value, symbol)}</strong><span class="tiny">占净值 ${longShare.toFixed(1)}%</span></div><div class="risk-cell"><label>空头负债</label><strong>${money(summary.short_liability, symbol)}</strong><span class="tiny">SHORT liability</span></div><div class="risk-cell"><label>总敞口</label><strong>${summary.gross_exposure_pct === null ? "—" : percent(gross)}</strong><span class="tiny">上限 ${number(strategy.exposure_policy.max_gross_exposure_pct).toFixed(0)}%</span></div><div class="risk-cell"><label>净敞口</label><strong>${summary.net_exposure_pct === null ? "—" : percent(net)}</strong><span class="tiny">多头减空头</span></div><div class="risk-cell"><label>保证金率</label><strong>${summary.margin_rate_pct === null ? "—" : percent(summary.margin_rate_pct)}</strong><span class="tiny">融资余额 ${money(summary.margin_loan, symbol)}</span></div><div class="risk-cell"><label>可用购买力</label><strong>${money(summary.buying_power, symbol)}</strong><span class="tiny">现金预留 ${money(summary.reserved_cash, symbol)}</span></div><div class="risk-cell"><label>累计融资成本</label><strong>${money(summary.financing_cost, symbol)}</strong><span class="tiny">融资余额 ${money(summary.margin_loan, symbol)}</span></div><div class="risk-cell"><label>累计借券成本</label><strong>${money(summary.borrow_cost, symbol)}</strong><span class="tiny">空头成本按来源标注</span></div></section>
    <section class="chart-area"><div class="chart-copy"><div class="eyebrow">Portfolio NAV</div><h2>策略净值轨迹</h2><p>从模拟账户启用起累计，不再限定 30 天。</p></div><div class="chart">${navChart(payload.nav_history, symbol)}<div class="tiny">更新：${escapeHtml(dateTime(payload.generated_at))}</div>${quoteError}</div></section>
    <nav class="tabs" role="tablist" aria-label="策略账本视图"><button id="positions-tab" role="tab" aria-controls="positions">当前持仓</button><button id="orders-tab" role="tab" aria-controls="orders">订单</button><button id="trades-tab" role="tab" aria-controls="trades">退出记录</button><button id="events-tab" role="tab" aria-controls="events">事件账本</button></nav>
    <section id="positions" role="tabpanel" aria-labelledby="positions-tab">${positionRows(payload.positions, symbol)}</section><section id="orders" role="tabpanel" aria-labelledby="orders-tab">${orderRows(payload.orders, symbol)}</section><section id="trades" role="tabpanel" aria-labelledby="trades-tab">${tradeRows(payload.closed_trades, symbol)}</section><section id="events" role="tabpanel" aria-labelledby="events-tab">${eventRows(payload.events)}</section><footer><span>${escapeHtml(costNote)}</span><span>仅用于策略验证，不构成投资建议。</span></footer>`
  bindTabs(root, highlight ? "events" : "positions")
  if (highlight) document.getElementById(`event-${highlight}`)?.scrollIntoView({ block: "center" })
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
