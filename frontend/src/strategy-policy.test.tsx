import { act, useState } from "react"
import { createRoot, type Root } from "react-dom/client"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { ConfigPayload, ExposureMode, StrategyConfig } from "@/lib/api"
import { parseBoundedNumber, PolicyNumberInput, useStrategySaveFlow } from "@/strategy-policy"

function draft(mode: ExposureMode = "LONG_ONLY"): StrategyConfig {
  return {
    version: 6, id: "s1", name: "US", description: "", created_at: null, updated_at: null, revision: 1,
    lifecycle: { stage: "paper", paper_sessions: 0 },
    signal: { model: "factor_rank_v1", run_time: "09:30", data_cutoff: "previous_trading_day_close", minimum_history_rows: 20, max_hot_candidates: 10, factor_weights: {} },
    allocation: { model: "trend_breadth_v1", enabled: true, minimum_universe_size: 20, breadth_threshold_pct: 60, risk_on_min_signals: 3, neutral_min_signals: 2, risk_on_exposure_pct: 100, neutral_exposure_pct: 50, risk_off_exposure_pct: 0, unknown_exposure_pct: 0, minimum_candidate_momentum20_pct: 0, minimum_candidate_trend: 1, exit_on_risk_off: true, rebalance_to_target_exposure: true },
    delivery: { enabled: false, channel: "feishu", target: "", hour: 8, minute: 0, frequency: "weekdays", push_on_empty: false, push_on_error: true },
    portfolio: { enabled: true, initial_cash: 100000, max_positions: 10, target_weight_pct: 10, stop_loss_pct: 8, trailing_activation_pct: 10, trailing_drawdown_pct: 5, signal_invalid_days: 3, replacement_score_delta: 0.1, replacement_cost_multiple: 2, warning_drawdown_pct: 12, derisk_drawdown_pct: 14, halt_drawdown_pct: 15, warning_max_exposure_pct: 50, commission_rate_pct: 0, minimum_commission_cny: 0, stamp_duty_rate_pct: 0, transfer_fee_rate_pct: 0, slippage_bps: 5, max_bar_participation_pct: 5, benchmark_symbol: "SPY", benchmark_name: "S&P 500" },
    exposure_policy: { mode, max_positions: 10, max_gross_exposure_pct: 100, max_net_exposure_pct: 100, max_long_exposure_pct: 100, max_short_exposure_pct: 0, max_long_position_pct: 10, max_short_position_pct: 0 },
    margin_policy: { maintenance_margin_pct: 30, liquidation_buffer_pct: 10, financing_apr_pct: 6, accrual_mode: "DAILY" },
    short_policy: { signal_model: "inverse_rank_v1", require_shortable: true, require_easy_to_borrow: true, estimated_borrow_apr_pct: 3, cost_stress_multiplier: 2, block_on_borrow_data_missing: true, stop_loss_pct: 8, trailing_activation_pct: 10, trailing_rebound_pct: 5, event_blackout_sessions: 2, squeeze_rise_pct: 8, squeeze_volume_ratio: 2, maximum_volatility_20d_pct: 80 },
    parameters: {},
  }
}

function Harness({ market = "us", confirm = vi.fn(() => true), save, run = vi.fn() }: { market?: "cn" | "us"; confirm?: () => boolean; save: (payload: StrategyConfig) => Promise<ConfigPayload>; run?: () => void }) {
  const [config, setConfig] = useState(() => draft())
  const flow = useStrategySaveFlow({ config, market, confirm, save, onSaved: (payload) => setConfig(payload.config) })
  return <>
    <button onClick={() => setConfig((current) => ({ ...current, exposure_policy: { ...current.exposure_policy, mode: "LONG_SHORT" } }))}>short</button>
    <button onClick={() => setConfig((current) => ({ ...current, exposure_policy: { ...current.exposure_policy, mode: "LONG_ONLY" } }))}>long</button>
    <button onClick={() => void flow.save()}>save</button>
    <button onClick={() => void flow.beforeRun().then(run).catch(() => undefined)}>run</button>
    <output>{config.exposure_policy.mode}</output>
  </>
}

function response(config: StrategyConfig): ConfigPayload {
  return { groups: [], parameters: [], config, market: { code: "us", label: "美股", timezone: "America/New_York", currency: "USD", currency_symbol: "$", lot_size: 1 }, us_market_data: { selected_policy: "auto", primary: "sina", fallback: "", effective_source: "sina", mode: "primary_ready", alpaca_configured: false, alpaca_feed: "iex", alpaca_history_feed: "iex", providers: [] } }
}

describe("strategy policy save flow", () => {
  let root: Root

  beforeEach(() => {
    document.body.innerHTML = '<div id="root"></div>'
    root = createRoot(document.querySelector("#root")!)
  })

  afterEach(() => {
    act(() => root.unmount())
  })

  const button = (label: string) => [...document.querySelectorAll("button")].find((node) => node.textContent === label)!
  const click = async (label: string) => {
    await act(async () => {
      button(label).dispatchEvent(new MouseEvent("click", { bubbles: true }))
      await Promise.resolve()
    })
  }

  it("saves an accepted transition and updates its persisted baseline", async () => {
    const confirm = vi.fn(() => true)
    const save = vi.fn(async (payload: StrategyConfig) => response(payload))
    act(() => root.render(<Harness confirm={confirm} save={save} />))
    await click("short"); await click("save")
    expect(save).toHaveBeenCalledTimes(1); expect(confirm).toHaveBeenCalledTimes(1)
    await click("save")
    expect(save).toHaveBeenCalledTimes(2); expect(confirm).toHaveBeenCalledTimes(1)
  })

  it("does not save or run when transition confirmation is cancelled", async () => {
    const save = vi.fn(); const run = vi.fn()
    act(() => root.render(<Harness confirm={() => false} save={save} run={run} />))
    await click("short"); await click("run")
    expect(save).not.toHaveBeenCalled(); expect(run).not.toHaveBeenCalled()
  })

  it("forces non-US payloads to LONG_ONLY", async () => {
    const save = vi.fn(async (payload: StrategyConfig) => response(payload))
    act(() => root.render(<Harness market="cn" save={save} />))
    await click("short"); await click("save")
    expect(save.mock.calls[0][0].exposure_policy.mode).toBe("LONG_ONLY")
  })

  it("sends the latest draft and keeps an in-flight payload immutable", async () => {
    let release!: () => void
    const save = vi.fn((payload: StrategyConfig) => new Promise<ConfigPayload>((resolve) => { release = () => resolve(response(payload)) }))
    act(() => root.render(<Harness save={save} />))
    await click("short"); await click("save"); await click("long")
    expect(save.mock.calls[0][0].exposure_policy.mode).toBe("LONG_SHORT")
    await act(async () => release())
  })

  it("keeps empty and non-finite values invalid, then clamps a committed boundary", async () => {
    const onCommit = vi.fn()
    act(() => root.render(<PolicyNumberInput label="总敞口" value={100} minimum={0} maximum={150} step={1} onCommit={onCommit} />))
    const input = document.querySelector<HTMLInputElement>('input[aria-label="总敞口"]')!
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!
    act(() => {
      setValue.call(input, "")
      input.dispatchEvent(new Event("input", { bubbles: true }))
    })
    expect(document.querySelector('[role="alert"]')?.textContent).toContain("请输入有效数字")
    expect(onCommit).not.toHaveBeenCalled()
    expect(parseBoundedNumber("Infinity", 0, 150)).toEqual({ value: null, error: "请输入有效数字" })
    act(() => {
      setValue.call(input, "999")
      input.dispatchEvent(new Event("input", { bubbles: true }))
      input.dispatchEvent(new FocusEvent("focusout", { bubbles: true }))
    })
    expect(onCommit).toHaveBeenLastCalledWith(150)
  })
})
