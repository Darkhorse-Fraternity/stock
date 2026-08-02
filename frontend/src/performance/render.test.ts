import { beforeEach, describe, expect, it, vi } from "vitest"

import type { StrategyPerformancePayload } from "@/lib/api"
import { parseStrategyPerformancePayload, renderPerformance } from "@/performance/render"
import adminContractFixture from "@/test-fixtures/admin-strategy-performance.json"

const fixture = {
  generated_at: "2026-08-02T13:00:00+00:00",
  quote_error: "Alpaca quote delayed <unsafe>",
  market: "us",
  market_label: "美股",
  currency: "USD",
  currency_symbol: "$",
  config: {
    enabled: true,
    initial_cash: 100000,
    max_positions: 10,
    target_weight_pct: 10,
    stop_loss_pct: 8,
    trailing_activation_pct: 10,
    trailing_drawdown_pct: 5,
    signal_invalid_days: 3,
    replacement_score_delta: 0.1,
    replacement_cost_multiple: 2,
    warning_drawdown_pct: 12,
    derisk_drawdown_pct: 14,
    halt_drawdown_pct: 15,
    warning_max_exposure_pct: 50,
    commission_rate_pct: 0,
    minimum_commission_cny: 0,
    stamp_duty_rate_pct: 0,
    transfer_fee_rate_pct: 0,
    slippage_bps: 5,
    max_bar_participation_pct: 5,
    benchmark_symbol: "SPY",
    benchmark_name: "S&P 500",
  },
  allocation: {
    model: "trend_breadth_v1",
    enabled: true,
    minimum_universe_size: 20,
    breadth_threshold_pct: 60,
    risk_on_min_signals: 3,
    neutral_min_signals: 2,
    risk_on_exposure_pct: 100,
    neutral_exposure_pct: 50,
    risk_off_exposure_pct: 0,
    unknown_exposure_pct: 0,
    minimum_candidate_momentum20_pct: 0,
    minimum_candidate_trend: 1,
    exit_on_risk_off: true,
    rebalance_to_target_exposure: true,
  },
  strategy: {
    id: "strategy-us",
    name: "AI Momentum <unsafe>",
    revision: 3,
    stage: "paper",
    market: "us",
    market_label: "美股",
    currency: "USD",
    currency_symbol: "$",
    initial_cash: 100000,
    max_positions: 10,
    signal_model: "factor_rank_v1",
    signal_time: "09:30",
    signal_data_cutoff: "previous_trading_day_close",
    allocation_model: "trend_breadth_v1",
    benchmark_symbol: "SPY",
    benchmark_name: "S&P 500",
    market_regime: { state: "RISK_ON", label: "强势", target_exposure_pct: 100 },
    risk_level: "NORMAL",
    trading_mode: "RUNNING",
    target_exposure_pct: 100,
    exposure_policy: { mode: "LONG_SHORT", max_positions: 10, max_gross_exposure_pct: 150, max_net_exposure_pct: 120, max_long_exposure_pct: 120, max_short_exposure_pct: 30, max_long_position_pct: 15, max_short_position_pct: 5 },
    margin_policy: { maintenance_margin_pct: 30, liquidation_buffer_pct: 10, financing_apr_pct: 6, accrual_mode: "DAILY" },
    short_policy: { signal_model: "inverse_rank_v1", require_shortable: true, require_easy_to_borrow: true, estimated_borrow_apr_pct: 3, cost_stress_multiplier: 2, block_on_borrow_data_missing: true, stop_loss_pct: 8, trailing_activation_pct: 10, trailing_rebound_pct: 5, event_blackout_sessions: 2, squeeze_rise_pct: 8, squeeze_volume_ratio: 2, maximum_volatility_20d_pct: 80 },
  },
  summary: { initial_cash: 100000, nav: 99500, cash: 40000, reserved_cash: 0, market_value: 59500, long_market_value: 70000, short_liability: 10500, gross_exposure_pct: 80.9, net_exposure_pct: 59.8, margin_rate_pct: 24, buying_power: 20000, margin_loan: 10000, financing_cost: 12.5, borrow_cost: 4.25, cumulative_return_pct: -0.5, maximum_drawdown_pct: 1.2, realized_pnl: -100, unrealized_pnl: -400, position_count: 2, max_positions: 10, target_exposure_pct: 100, closed_trade_count: 1, win_rate_pct: 0 },
  runtime: { last_successful_pipeline_at: "2026-08-02T12:55:00+00:00", last_successful_pipeline_run_id: "run-1", last_pipeline_admitted: 2, last_pipeline_stages: [{ stage: "signal", diagnostics: [{ accepted: 2 }] }], last_pipeline_market_regime: { state: "RISK_ON" }, last_pipeline_data_quality: { status: "READY", raw_count: 20, selected_count: 2 }, availability: { complete: true, source: "v2_ledger", reason: null } },
  nav_history: [{ at: "2026-08-02T12:00:00+00:00", nav: 100000, cash: 100000, market_value: 0, cumulative_return_pct: 0, drawdown_pct: 0, risk_level: "NORMAL", trading_mode: "RUNNING", source: "v2_ledger" }, { at: "2026-08-02T13:00:00+00:00", nav: 99500, cash: 40000, market_value: 59500, cumulative_return_pct: -0.5, drawdown_pct: 0.5, risk_level: "WARNING", trading_mode: "ENTRY_BLOCKED", source: "v2_ledger" }],
  positions: [{ slot_id: 1, name: "Apple", symbol: "AAPL", first_entry_price: 200, first_entry_at: "2026-08-01T13:30:00+00:00", current_price: 195, day_change_pct: -1, return_pct: -2.5, unrealized_pnl: -250, weight_pct: 10, quantity: 50, sellable_quantity: 40, trailing_active: true, signal_invalid_days: 2, exit_distance_pct: -5, market_value: 9750, average_cost: 200, position_side: "SHORT", side: "SHORT", position_mode: "COVER_ONLY", borrow_rate_pct: 3.25, borrow_rate_source: "strategy_estimate", borrow_rate_estimated: true, margin_used: 2925 }],
  orders: [{ id: "order-1", side: "BUY", symbol: "AAPL", name: "Apple", quantity: 50, filled_quantity: 50, status: "FILLED", reason: "ENTRY", created_at: "2026-08-01T13:30:00+00:00", updated_at: "2026-08-01T13:30:00+00:00", filled_notional: 10000, commission_charged: 0, fees_charged: 0, strategy_revision: 3, position_side: "LONG", position_effect: "OPEN", key: null, control_epoch: null, purpose: "ENTRY", slot_id: 1, signal_price: 200, score: 1, reserved_cash: 0, valid_date: "2026-08-01", valid_session_date: "2026-08-01", cancel_reason: null, replacement_candidate: null }],
  closed_trades: [{ id: "trade-1", name: "Tesla", symbol: "TSLA", entry_price: 300, exit_price: 295, quantity: 20, realized_pnl: -100, return_pct: -1.67, reason: "STOP_LOSS", closed_at: "2026-08-01T15:00:00+00:00", strategy_revision: 3, position_side: "LONG" }],
  events: [{ id: "event-1", type: "MARGIN_CALL", occurred_at: "2026-08-02T12:58:00+00:00", message: "unsafe <script>alert(1)</script>", strategy_revision: 3, key: "risk-1", data: { margin_rate_pct: 24, action: "COVER_ONLY" } }],
  history_availability: { nav: { complete: true, source: "v2_ledger", reason: null }, lifecycle: { complete: true, source: "v2_ledger", reason: null } },
} satisfies StrategyPerformancePayload

describe("strategy performance runtime", () => {
  beforeEach(() => {
    document.body.innerHTML = '<main id="app"></main>'
    history.replaceState({}, "", "/strategies/strategy-us/portfolio")
  })

  it("validates and renders the typed backend payload without injecting HTML", () => {
    const parsed = parseStrategyPerformancePayload(structuredClone(fixture))
    renderPerformance(document.querySelector("#app")!, parsed)
    expect(document.querySelector("h1")?.textContent).toBe("AI Momentum <unsafe>")
    expect(document.querySelector("script")).toBeNull()
    expect(document.body.textContent).toContain("-0.50%")
    expect(document.body.textContent).toContain("0.0%")
    for (const label of ["多头市值", "空头负债", "总敞口", "净敞口", "保证金率", "可用购买力", "累计融资成本", "累计借券成本"]) {
      expect(document.body.textContent).toContain(label)
    }
  })

  it("renders NAV, runtime diagnostics, quote errors, and full lifecycle tables", () => {
    renderPerformance(document.querySelector("#app")!, fixture)

    const chart = document.querySelector<SVGElement>('svg[role="img"][aria-label="策略净值曲线"]')
    expect(chart).not.toBeNull()
    expect(chart?.querySelectorAll("circle")).toHaveLength(2)
    expect(chart?.querySelector("title")?.textContent).toContain("$100,000.00")

    const runtime = document.querySelector<HTMLElement>('[data-section="runtime"]')
    expect(runtime?.textContent).toContain("强势（RISK_ON）")
    expect(runtime?.textContent).toContain("股票池 20")
    expect(runtime?.textContent).toContain("signal: accepted=2")
    expect(document.querySelector<HTMLElement>('[role="alert"][data-kind="quote-error"]')?.textContent).toContain("Alpaca quote delayed <unsafe>")

    const position = document.querySelector<HTMLTableRowElement>('tr[data-row-kind="position"]')
    for (const value of ["#1 Apple", "空头", "$200.00", "2026-08-01", "$195.00", "当日 -1.00%", "-2.50%", "-$250.00", "10.0%", "$2,925.00", "50 / 40", "追踪退出已激活", "借券年化 3.25% · 策略估算", "-5.00%", "仅允许回补"]) {
      expect(position?.textContent).toContain(value)
    }

    document.querySelector<HTMLButtonElement>("#orders-tab")!.click()
    const order = document.querySelector<HTMLTableRowElement>('tr[data-row-kind="order"]')
    for (const value of ["BUY", "Apple", "已成交", "ENTRY", "50 / 50 / 0", "$0.00", "LONG / OPEN / ENTRY", "v3", "2026-08-01"]) {
      expect(order?.textContent).toContain(value)
    }

    document.querySelector<HTMLButtonElement>("#trades-tab")!.click()
    const trade = document.querySelector<HTMLTableRowElement>('tr[data-row-kind="trade"]')
    for (const value of ["Tesla", "$300.00 → $295.00", "20", "-$100.00", "-1.67%", "STOP_LOSS", "v3", "净收益（已含费用）"]) {
      expect(trade?.textContent).toContain(value)
    }
  })

  it("deep-links to a risk event, exposes event data, and escapes its content", () => {
    history.replaceState({}, "", "/strategies/strategy-us/portfolio?event=event-1")
    const scrollIntoView = vi.fn()
    HTMLElement.prototype.scrollIntoView = scrollIntoView
    renderPerformance(document.querySelector("#app")!, fixture)

    const event = document.querySelector<HTMLElement>("#event-event-1")
    expect(document.querySelector("#events-tab")?.getAttribute("aria-selected")).toBe("true")
    expect(document.querySelector<HTMLElement>("#events")?.hidden).toBe(false)
    expect(event?.classList.contains("risk")).toBe(true)
    expect(event?.classList.contains("highlight")).toBe(true)
    expect(event?.textContent).toContain('"margin_rate_pct": 24')
    expect(event?.textContent).toContain("risk-1")
    expect(document.querySelector("script")).toBeNull()
    expect(scrollIntoView).toHaveBeenCalled()
  })

  it("consumes the exact admin serializer contract fixture", () => {
    const parsed = parseStrategyPerformancePayload(adminContractFixture)
    expect(parsed.orders[0].key).toBe("order-key-1")
    expect(parsed.positions[0].borrow_rate_source).toBe("strategy_estimate")
    expect(parsed.closed_trades[0].reason).toBe("STOP_LOSS")
    expect(parsed.events[0].data).toEqual({ margin_rate_pct: 24, action: "COVER_ONLY" })
    expect(parsed.runtime.last_pipeline_market_regime).toEqual({ state: "RISK_ON" })
  })

  it("rejects malformed payloads before rendering", () => {
    const malformed = structuredClone(fixture) as unknown as Record<string, unknown>
    malformed.summary = { nav: Number.NaN }
    expect(() => parseStrategyPerformancePayload(malformed)).toThrow(/summary/)
  })

  it.each([
    ["payload market", (value: Record<string, unknown>) => { value.market = "hk" }],
    ["strategy market mismatch", (value: Record<string, unknown>) => { (value.strategy as Record<string, unknown>).market = "cn" }],
    ["exposure mode", (value: Record<string, unknown>) => { ((value.strategy as Record<string, unknown>).exposure_policy as Record<string, unknown>).mode = "FLAT" }],
    ["risk level", (value: Record<string, unknown>) => { (value.strategy as Record<string, unknown>).risk_level = "UNKNOWN" }],
    ["trading mode", (value: Record<string, unknown>) => { (value.strategy as Record<string, unknown>).trading_mode = "UNKNOWN" }],
    ["position side", (value: Record<string, unknown>) => { ((value.positions as unknown[])[0] as Record<string, unknown>).side = "FLAT" }],
    ["position mode", (value: Record<string, unknown>) => { ((value.positions as unknown[])[0] as Record<string, unknown>).position_mode = "OPEN" }],
    ["borrow source", (value: Record<string, unknown>) => { ((value.positions as unknown[])[0] as Record<string, unknown>).borrow_rate_source = "guess" }],
    ["order side", (value: Record<string, unknown>) => { ((value.orders as unknown[])[0] as Record<string, unknown>).side = "HOLD" }],
    ["order status", (value: Record<string, unknown>) => { ((value.orders as unknown[])[0] as Record<string, unknown>).status = "UNKNOWN" }],
    ["order effect", (value: Record<string, unknown>) => { ((value.orders as unknown[])[0] as Record<string, unknown>).position_effect = "HEDGE" }],
    ["order purpose", (value: Record<string, unknown>) => { ((value.orders as unknown[])[0] as Record<string, unknown>).purpose = "REBALANCE" }],
    ["trade side", (value: Record<string, unknown>) => { ((value.closed_trades as unknown[])[0] as Record<string, unknown>).position_side = "FLAT" }],
  ])("rejects unsupported %s literals", (_label, mutate) => {
    const malformed = structuredClone(fixture) as unknown as Record<string, unknown>
    mutate(malformed)
    expect(() => parseStrategyPerformancePayload(malformed)).toThrow(/unsupported|mismatch/)
  })

  it("supports click and complete keyboard tab navigation", () => {
    renderPerformance(document.querySelector("#app")!, fixture)
    const tabs = [...document.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
    tabs[1].dispatchEvent(new MouseEvent("click", { bubbles: true }))
    expect(tabs[1].getAttribute("aria-selected")).toBe("true")
    expect(document.querySelector<HTMLElement>("#orders")?.hidden).toBe(false)
    tabs[1].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }))
    expect(document.activeElement).toBe(tabs[2])
    tabs[2].dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true }))
    expect(document.activeElement).toBe(tabs[3])
    tabs[3].dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true }))
    expect(document.activeElement).toBe(tabs[0])
    tabs[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }))
    expect(document.activeElement).toBe(tabs[3])
    expect(tabs.filter((tab) => tab.tabIndex === 0)).toEqual([tabs[3]])
  })

  it("fetches through the typed parser before rendering", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(fixture)))
    const { loadPerformance } = await import("@/performance/render")
    await loadPerformance("strategy-us", document.querySelector("#app")!, fetcher)
    expect(fetcher).toHaveBeenCalledWith("/api/strategies/strategy-us/portfolio", expect.anything())
    expect(document.body.textContent).toContain("Apple")
  })
})
