from __future__ import annotations

import json
import os
import re
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import MAX_FLOAT_MARKET_CAP, MIN_FLOAT_MARKET_CAP
from .markets import (
    CN_MARKET,
    US_MARKET,
    market_profile,
    parameter_applicable,
    strategy_market,
)


GROUPS = [
    {"id": "universe", "label": "股票范围", "description": "市场、板块与基础排除条件"},
    {"id": "market", "label": "行情与流动性", "description": "价格、涨跌、成交和市值"},
    {"id": "technical", "label": "技术面", "description": "趋势、动量、波动与点火信号"},
    {"id": "valuation", "label": "估值", "description": "相对估值与现金流估值"},
    {"id": "growth", "label": "成长", "description": "收入、利润和每股收益增速"},
    {"id": "quality", "label": "盈利质量", "description": "回报率、利润率、现金流和偿债"},
    {"id": "income", "label": "分红", "description": "股息水平、持续性和派息负担"},
    {"id": "flow_event", "label": "资金与事件", "description": "资金流、业绩预告和重要事件"},
    {"id": "risk", "label": "风险控制", "description": "追高、停牌、解禁和治理风险"},
]


def _parameter(
    parameter_id: str,
    group: str,
    label: str,
    *,
    kind: str = "number",
    unit: str = "",
    operator: str = "min",
    default: Any = 0,
    active: bool = False,
    status: str = "planned",
    description: str = "",
    options: list[dict] | None = None,
    step: float | int = 0.01,
    scale: float | int = 1,
) -> dict:
    return {
        "id": parameter_id,
        "group": group,
        "label": label,
        "kind": kind,
        "unit": unit,
        "operator": operator,
        "default": default,
        "default_enabled": active,
        "status": status,
        "selected": True,
        "description": description,
        "options": options or [],
        "step": step,
        "scale": scale,
    }


PARAMETER_CATALOG = [
    _parameter("market", "universe", "证券市场", kind="choice", operator="equals", default="cn", active=True, status="live", options=[{"value": "cn", "label": "A股"}, {"value": "us", "label": "美股"}], description="决定代码格式、行情与历史数据源、交易时区、币种及模拟撮合规则"),
    _parameter("board_code", "universe", "板块代码", kind="text", operator="equals", default="BK0800", active=True, status="live", description="当前东方财富概念板块代码"),
    _parameter("board_name", "universe", "板块名称", kind="text", operator="equals", default="人工智能", active=True, status="live", description="默认覆盖科技 AI 方向的人工智能概念板块"),
    _parameter("watchlist", "universe", "自选股池", kind="tags", operator="in", default=[], status="live", description="A 股填写 6 位代码；美股填写 ticker（如 AAPL、MSFT）；启用后仅在自选股池中筛选"),
    _parameter("sector_filters", "universe", "板块过滤", kind="tags", operator="in", default=[], status="live", description="按股票的板块或概念标签过滤，支持多个关键词"),
    _parameter("stock_prefixes", "universe", "股票代码范围", kind="multi", operator="in", default=["0", "3", "6"], active=True, status="live", options=[{"value": "0", "label": "深市主板"}, {"value": "3", "label": "创业板"}, {"value": "6", "label": "沪市/科创板"}, {"value": "8", "label": "北交所"}], description="按证券代码前缀限制市场"),
    _parameter("exclude_st", "universe", "排除 ST/*ST", kind="boolean", operator="equals", default=True, active=True, status="live", description="剔除特别处理股票"),
    _parameter("exclude_suspended", "universe", "排除停牌/无成交", kind="boolean", operator="equals", default=True, active=True, status="live", description="剔除价格或成交额为零的证券"),
    _parameter("listed_days_min", "universe", "最少上市天数", unit="天", default=60, status="derived", step=1, description="由前复权日线首个交易日至今计算"),
    _parameter("industries", "universe", "行业范围", kind="tags", operator="in", default=[], status="planned", description="按申万或证监会行业选择"),
    _parameter("concepts", "universe", "概念范围", kind="tags", operator="in", default=["人工智能"], status="planned", description="支持多个概念板块的并集或交集"),

    _parameter("price_min", "market", "最低股价", unit="元", default=0.01, active=True, status="live", description="过滤无效报价和过低价格"),
    _parameter("price_max", "market", "最高股价", unit="元", operator="max", default=500, status="live", description="限制单股绝对价格"),
    _parameter("change_pct_min", "market", "最低涨跌幅", unit="%", default=-20, status="live", description="当日涨跌幅下限"),
    _parameter("change_pct_max", "market", "最高涨跌幅", unit="%", operator="max", default=7, status="live", description="用于限制追高"),
    _parameter("turnover_rate_min", "market", "最低换手率", unit="%", default=2, status="live", description="衡量交易活跃程度"),
    _parameter("turnover_rate_max", "market", "最高换手率", unit="%", operator="max", default=20, status="live", description="过滤过度换手"),
    _parameter("turnover_min", "market", "最低成交额", unit="亿元", default=20_000_000, status="live", scale=100_000_000, description="确保基础流动性"),
    _parameter("volume_ratio_min", "market", "最低量比", unit="倍", default=1.2, status="live", description="当前成交活跃度相对近期均值"),
    _parameter("amplitude_max", "market", "最高振幅", unit="%", operator="max", default=15, status="live", description="控制日内波动风险"),
    _parameter("float_market_cap_min", "market", "最低流通市值", unit="亿元", default=MIN_FLOAT_MARKET_CAP, status="live", scale=100_000_000, description="需要市值数据时可启用 20 亿元下限"),
    _parameter("float_market_cap_max", "market", "最高流通市值", unit="亿元", operator="max", default=MAX_FLOAT_MARKET_CAP, status="live", scale=100_000_000, description="需要市值数据时可启用 100 亿元上限"),
    _parameter("total_market_cap_min", "market", "最低总市值", unit="亿元", default=2_000_000_000, status="live", scale=100_000_000, description="公司整体规模下限"),
    _parameter("total_market_cap_max", "market", "最高总市值", unit="亿元", operator="max", default=50_000_000_000, status="live", scale=100_000_000, description="公司整体规模上限"),

    _parameter("above_open", "technical", "价格位于开盘价上方", kind="boolean", operator="equals", default=True, status="derived", description="盘中价格强于开盘价"),
    _parameter("above_prev_close", "technical", "价格位于昨收上方", kind="boolean", operator="equals", default=True, status="derived", description="价格保持红盘"),
    _parameter("above_vwap", "technical", "价格位于均价线上方", kind="boolean", operator="equals", default=True, status="derived", description="价格强于当日成交均价"),
    _parameter("ma5_above_ma20", "technical", "MA5 高于 MA20", kind="boolean", operator="equals", default=True, status="derived", description="前复权日线计算短期均线多头排列"),
    _parameter("ma20_above_ma60", "technical", "MA20 高于 MA60", kind="boolean", operator="equals", default=True, status="derived", description="前复权日线计算中期趋势"),
    _parameter("price_above_ma20", "technical", "价格站上 MA20", kind="boolean", operator="equals", default=True, status="derived", description="最新前复权收盘价位于 MA20 上方"),
    _parameter("rsi_min", "technical", "RSI 下限", default=45, status="derived", description="14 日 Wilder RSI 下限"),
    _parameter("rsi_max", "technical", "RSI 上限", operator="max", default=75, status="derived", description="14 日 Wilder RSI 上限"),
    _parameter("macd_bullish", "technical", "MACD 多头", kind="boolean", operator="equals", default=True, status="derived", description="EMA(12)-EMA(26) 高于 9 日信号线"),
    _parameter("breakout_20d", "technical", "突破 20 日高点", kind="boolean", operator="equals", default=True, status="derived", description="最新收盘价不低于此前 20 日最高收盘价"),
    _parameter("distance_52w_high_max", "technical", "距 52 周高点", unit="%", operator="max", default=15, status="derived", description="距最近 252 个交易日最高价的百分比"),
    _parameter("volatility_20d_max", "technical", "20 日波动率上限", unit="%", operator="max", default=35, status="derived", description="20 日对数收益率年化波动率"),
    _parameter("ignition_price_10s_min", "technical", "10 秒点火涨幅", unit="%", default=2, status="derived", description="逐笔成交短时价格加速阈值"),
    _parameter("ignition_volume_ratio_min", "technical", "10 秒点火量比", unit="倍", default=8, status="derived", description="最近 10 秒相对前两分钟的成交量放大"),

    _parameter("pe_min", "valuation", "市盈率下限", unit="倍", default=0, status="live", description="可排除亏损公司"),
    _parameter("pe_max", "valuation", "市盈率上限", unit="倍", operator="max", default=60, status="live", description="滚动或动态市盈率上限"),
    _parameter("pb_max", "valuation", "市净率上限", unit="倍", operator="max", default=6, status="live", description="东方财富板块行情市净率"),
    _parameter("ps_max", "valuation", "市销率上限", unit="倍", operator="max", default=8, status="planned", description="市值相对营业收入"),
    _parameter("peg_max", "valuation", "PEG 上限", unit="倍", operator="max", default=2, status="planned", description="估值相对盈利增长"),
    _parameter("ev_ebitda_max", "valuation", "EV/EBITDA 上限", unit="倍", operator="max", default=20, status="planned", description="考虑资本结构后的企业价值倍数"),
    _parameter("fcf_yield_min", "valuation", "自由现金流收益率", unit="%", default=2, status="derived", description="最新报告自由现金流相对当前总市值"),

    _parameter("revenue_growth_min", "growth", "营收增速", unit="%", default=10, status="live", description="最新报告营业收入同比增长"),
    _parameter("profit_growth_min", "growth", "净利润增速", unit="%", default=10, status="live", description="最新报告归母净利润同比增长"),
    _parameter("eps_growth_min", "growth", "每股收益增速", unit="%", default=10, status="live", description="最新报告基本每股收益同比增长"),
    _parameter("growth_years_min", "growth", "连续增长年数", unit="年", default=2, status="planned", step=1, description="减少单季异常影响"),

    _parameter("roe_min", "quality", "净资产收益率 ROE", unit="%", default=10, status="live", description="最新报告加权净资产收益率"),
    _parameter("roa_min", "quality", "总资产收益率 ROA", unit="%", default=5, status="live", description="最新报告总资产净利率"),
    _parameter("roic_min", "quality", "投入资本回报率 ROIC", unit="%", default=8, status="live", description="最新报告投入资本回报率"),
    _parameter("gross_margin_min", "quality", "毛利率", unit="%", default=20, status="live", description="最新报告销售毛利率"),
    _parameter("net_margin_min", "quality", "净利率", unit="%", default=5, status="live", description="最新报告销售净利率"),
    _parameter("operating_cashflow_positive", "quality", "经营现金流为正", kind="boolean", operator="equals", default=True, status="live", description="最新报告每股经营现金流大于零"),
    _parameter("free_cashflow_positive", "quality", "自由现金流为正", kind="boolean", operator="equals", default=True, status="live", description="最新报告自由现金流大于零"),
    _parameter("debt_ratio_max", "quality", "资产负债率上限", unit="%", operator="max", default=65, status="live", description="最新报告资产负债率"),
    _parameter("current_ratio_min", "quality", "流动比率", unit="倍", default=1.2, status="live", description="最新报告流动比率"),
    _parameter("cash_to_debt_min", "quality", "现金/有息负债", unit="倍", default=0.5, status="planned", description="现金对债务的覆盖"),

    _parameter("dividend_yield_min", "income", "股息率", unit="%", default=2, status="planned", description="年度现金分红相对股价"),
    _parameter("payout_ratio_min", "income", "最低派息率", unit="%", default=20, status="planned", description="利润用于分红的最低比例"),
    _parameter("payout_ratio_max", "income", "最高派息率", unit="%", operator="max", default=80, status="planned", description="避免不可持续的过度派息"),
    _parameter("dividend_years_min", "income", "连续分红年数", unit="年", default=3, status="planned", step=1, description="分红持续性"),

    _parameter("main_inflow_min", "flow_event", "主力净流入", unit="万元", default=0, status="planned", description="主力资金净流入下限"),
    _parameter("northbound_inflow_min", "flow_event", "北向资金净流入", unit="万元", default=0, status="planned", description="陆股通资金变化"),
    _parameter("institutional_holding_growth", "flow_event", "机构持仓增长", kind="boolean", operator="equals", default=True, status="planned", description="机构持股比例环比提高"),
    _parameter("earnings_forecast", "flow_event", "业绩预告方向", kind="choice", operator="in", default="positive", status="planned", options=[{"value": "positive", "label": "预增/扭亏"}, {"value": "not_negative", "label": "非预减"}, {"value": "any", "label": "不限"}], description="业绩预告的方向性筛选"),
    _parameter("analyst_rating_min", "flow_event", "分析师评级下限", kind="choice", operator="min", default="buy", status="planned", options=[{"value": "buy", "label": "买入"}, {"value": "outperform", "label": "增持"}, {"value": "hold", "label": "持有"}], description="卖方一致预期"),

    _parameter("chase_risk_pct", "risk", "追高风险阈值", unit="%", operator="max", default=7, active=True, status="live", description="超过后只能观望或轻仓试错"),
    _parameter("exclude_limit_up", "risk", "排除涨停股", kind="boolean", operator="equals", default=False, status="live", description="避免无法成交或开板风险"),
    _parameter("pledge_ratio_max", "risk", "股权质押比例上限", unit="%", operator="max", default=30, status="planned", description="控制大股东质押风险"),
    _parameter("unlock_30d_max", "risk", "30 日解禁比例上限", unit="%", operator="max", default=10, status="planned", description="控制短期解禁冲击"),
    _parameter("exclude_abnormal_audit", "risk", "排除非标审计意见", kind="boolean", operator="equals", default=True, status="planned", description="排除财报审计异常"),
    _parameter("exclude_regulatory_action", "risk", "排除近期监管处罚", kind="boolean", operator="equals", default=True, status="planned", description="控制公司治理与合规风险"),
]

PARAMETERS_BY_ID = {item["id"]: item for item in PARAMETER_CATALOG}
DELIVERY_CHANNELS = {"feishu", "telegram", "discord", "signal", "origin", "local"}
DELIVERY_FREQUENCIES = {"daily", "weekdays"}
STRATEGY_STAGES = {"draft", "backtesting", "paper", "live", "paused", "archived"}
LOCKED_STRATEGY_STAGES = {"live", "archived"}
STRATEGY_STORE_VERSION = 5


class StrategyLifecycleError(ValueError):
    pass


def strategy_config_path() -> Path:
    return Path(os.getenv("STOCK_AGENT_CONFIG", "data/strategy_config.json")).expanduser()


def _environment_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def default_report_delivery() -> dict:
    channel = os.getenv("STOCK_AGENT_DEFAULT_DELIVERY_CHANNEL", "feishu").strip().lower()
    frequency = os.getenv("STOCK_AGENT_DEFAULT_DELIVERY_FREQUENCY", "weekdays").strip().lower()
    return {
        "enabled": os.getenv("STOCK_AGENT_DEFAULT_DELIVERY_ENABLED", "0") == "1",
        "channel": channel if channel in DELIVERY_CHANNELS else "feishu",
        "target": os.getenv("STOCK_AGENT_DEFAULT_DELIVERY_TARGET", "")[:200],
        "hour": min(23, max(0, _environment_int("STOCK_AGENT_DEFAULT_DELIVERY_HOUR", 8))),
        "minute": min(59, max(0, _environment_int("STOCK_AGENT_DEFAULT_DELIVERY_MINUTE", 0))),
        "frequency": frequency if frequency in DELIVERY_FREQUENCIES else "weekdays",
        "push_on_empty": True,
        "push_on_error": True,
    }


def normalize_report_delivery(value: object) -> dict:
    normalized = default_report_delivery()
    if not isinstance(value, dict):
        return normalized
    channel = str(value.get("channel") or normalized["channel"]).strip().lower()
    frequency = str(value.get("frequency") or normalized["frequency"]).strip().lower()
    try:
        hour = int(value.get("hour", normalized["hour"]))
        minute = int(value.get("minute", normalized["minute"]))
    except (TypeError, ValueError):
        hour, minute = normalized["hour"], normalized["minute"]
    normalized.update(
        {
            "enabled": bool(value.get("enabled", normalized["enabled"])),
            "channel": channel if channel in DELIVERY_CHANNELS else "feishu",
            "target": str(value.get("target") or "").strip()[:200],
            "hour": min(23, max(0, hour)),
            "minute": min(59, max(0, minute)),
            "frequency": frequency if frequency in DELIVERY_FREQUENCIES else "daily",
            "push_on_empty": bool(value.get("push_on_empty", True)),
            "push_on_error": bool(value.get("push_on_error", True)),
        }
    )
    return normalized


def default_strategy_lifecycle() -> dict:
    return {
        "stage": "draft",
        "stage_updated_at": None,
        "approved_at": None,
        "approved_by": None,
        "paper_sessions": 0,
        "paper_started_at": None,
        "paper_dates": [],
    }


def normalize_strategy_lifecycle(value: object) -> dict:
    normalized = default_strategy_lifecycle()
    if not isinstance(value, dict):
        return normalized
    try:
        paper_sessions = max(0, int(value.get("paper_sessions") or 0))
    except (TypeError, ValueError):
        paper_sessions = 0
    stage = str(value.get("stage") or "draft").strip().lower()
    normalized.update(
        {
            "stage": stage if stage in STRATEGY_STAGES else "draft",
            "stage_updated_at": value.get("stage_updated_at"),
            "approved_at": value.get("approved_at"),
            "approved_by": str(value.get("approved_by") or "").strip()[:120] or None,
            "paper_sessions": paper_sessions,
            "paper_started_at": value.get("paper_started_at"),
            "paper_dates": [str(item)[:10] for item in value.get("paper_dates", []) if str(item).strip()][-252:]
            if isinstance(value.get("paper_dates"), list)
            else [],
        }
    )
    return normalized


def default_signal_config() -> dict:
    return {
        "model": "factor_rank_v1",
        "run_time": "08:00",
        "data_cutoff": "previous_trading_day_close",
        "minimum_history_rows": 61,
        "max_hot_candidates": 2,
        "factor_weights": {
            "momentum20": 1.0,
            "momentum60": 1.0,
            "trend": 1.0,
            "volume_ratio": 1.0,
            "inverse_volatility": 1.0,
            "drawdown": 1.0,
        },
    }


def normalize_signal_config(value: object) -> dict:
    normalized = default_signal_config()
    if not isinstance(value, dict):
        return normalized
    run_time = str(value.get("run_time") or normalized["run_time"]).strip()
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", run_time):
        normalized["run_time"] = run_time
    for field, minimum, maximum in (
        ("minimum_history_rows", 61, 2520),
        ("max_hot_candidates", 0, 10),
    ):
        try:
            normalized[field] = min(maximum, max(minimum, int(value.get(field, normalized[field]))))
        except (TypeError, ValueError):
            pass
    weights = value.get("factor_weights")
    if isinstance(weights, dict):
        for factor in normalized["factor_weights"]:
            try:
                normalized["factor_weights"][factor] = max(0.0, float(weights.get(factor, normalized["factor_weights"][factor])))
            except (TypeError, ValueError):
                pass
    if not any(normalized["factor_weights"].values()):
        normalized["factor_weights"] = default_signal_config()["factor_weights"]
    return normalized


def default_allocation_config() -> dict:
    return {
        "model": "trend_breadth_v1",
        "enabled": True,
        "minimum_universe_size": 1,
        "breadth_threshold_pct": 50.0,
        "risk_on_min_signals": 3,
        "neutral_min_signals": 2,
        "risk_on_exposure_pct": 100.0,
        "neutral_exposure_pct": 40.0,
        "risk_off_exposure_pct": 0.0,
        "unknown_exposure_pct": 0.0,
        "minimum_candidate_momentum20_pct": 0.0,
        "minimum_candidate_trend": 1.0,
        "exit_on_risk_off": True,
        "rebalance_to_target_exposure": True,
    }


def normalize_allocation_config(value: object) -> dict:
    normalized = default_allocation_config()
    if not isinstance(value, dict):
        return normalized
    normalized["enabled"] = bool(value.get("enabled", normalized["enabled"]))
    normalized["exit_on_risk_off"] = bool(value.get("exit_on_risk_off", normalized["exit_on_risk_off"]))
    normalized["rebalance_to_target_exposure"] = bool(
        value.get("rebalance_to_target_exposure", normalized["rebalance_to_target_exposure"])
    )
    for field, minimum, maximum in (
        ("minimum_universe_size", 1, 5000),
        ("risk_on_min_signals", 1, 3),
        ("neutral_min_signals", 1, 3),
    ):
        try:
            normalized[field] = min(maximum, max(minimum, int(value.get(field, normalized[field]))))
        except (TypeError, ValueError):
            pass
    for field, minimum, maximum in (
        ("breadth_threshold_pct", 0.0, 100.0),
        ("risk_on_exposure_pct", 0.0, 100.0),
        ("neutral_exposure_pct", 0.0, 100.0),
        ("risk_off_exposure_pct", 0.0, 100.0),
        ("unknown_exposure_pct", 0.0, 100.0),
        ("minimum_candidate_momentum20_pct", -100.0, 1000.0),
        ("minimum_candidate_trend", 0.0, 2.0),
    ):
        try:
            normalized[field] = min(maximum, max(minimum, float(value.get(field, normalized[field]))))
        except (TypeError, ValueError):
            pass
    normalized["risk_on_min_signals"] = max(normalized["neutral_min_signals"], normalized["risk_on_min_signals"])
    normalized["risk_on_exposure_pct"] = max(normalized["neutral_exposure_pct"], normalized["risk_on_exposure_pct"])
    normalized["neutral_exposure_pct"] = max(normalized["risk_off_exposure_pct"], normalized["neutral_exposure_pct"])
    return normalized


def default_validation_config() -> dict:
    return {
        "signal_time": "08:00",
        "holding_period_days": 3,
        "lookback_days": 60,
        "history_days_min": 756,
        "train_days": 504,
        "validation_days": 63,
        "test_days": 63,
        "gap_days": 5,
        "top_n": 3,
        "transaction_cost_bps": 10.0,
        "slippage_bps": 10.0,
        "minimum_oos_events": 200,
        "minimum_oos_months": 12,
        "minimum_positive_fold_ratio": 0.6,
        "minimum_dsr_probability": 0.95,
        "maximum_drawdown_pct": 15.0,
        "minimum_paper_sessions": 40,
        "last_backtest": None,
        "approval_gate": {"passed": False, "checks": [], "evaluated_at": None},
    }


def normalize_validation_config(value: object) -> dict:
    normalized = default_validation_config()
    if not isinstance(value, dict):
        return normalized
    integer_fields = {
        "holding_period_days": (1, 20),
        "lookback_days": (20, 252),
        "history_days_min": (252, 2520),
        "train_days": (126, 2520),
        "validation_days": (21, 504),
        "test_days": (21, 504),
        "gap_days": (0, 60),
        "top_n": (1, 20),
        "minimum_oos_events": (20, 100000),
        "minimum_oos_months": (3, 120),
        "minimum_paper_sessions": (5, 252),
    }
    float_fields = {
        "transaction_cost_bps": (0.0, 200.0),
        "slippage_bps": (0.0, 200.0),
        "minimum_positive_fold_ratio": (0.0, 1.0),
        "minimum_dsr_probability": (0.0, 1.0),
        "maximum_drawdown_pct": (1.0, 100.0),
    }
    for field, (minimum, maximum) in integer_fields.items():
        try:
            normalized[field] = min(maximum, max(minimum, int(value.get(field, normalized[field]))))
        except (TypeError, ValueError):
            pass
    for field, (minimum, maximum) in float_fields.items():
        try:
            normalized[field] = min(maximum, max(minimum, float(value.get(field, normalized[field]))))
        except (TypeError, ValueError):
            pass
    signal_time = str(value.get("signal_time") or normalized["signal_time"]).strip()
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", signal_time):
        normalized["signal_time"] = signal_time
    if isinstance(value.get("last_backtest"), dict):
        normalized["last_backtest"] = deepcopy(value["last_backtest"])
    if isinstance(value.get("approval_gate"), dict):
        gate = value["approval_gate"]
        normalized["approval_gate"] = {
            "passed": bool(gate.get("passed", False)),
            "checks": deepcopy(gate.get("checks") if isinstance(gate.get("checks"), list) else []),
            "evaluated_at": gate.get("evaluated_at"),
        }
    return normalized


def default_portfolio_config() -> dict:
    return {
        "enabled": True,
        "initial_cash": 1_000_000.0,
        "max_positions": 10,
        "target_weight_pct": 10.0,
        "stop_loss_pct": 8.0,
        "trailing_activation_pct": 10.0,
        "trailing_drawdown_pct": 5.0,
        "signal_invalid_days": 5,
        "replacement_score_delta": 0.10,
        "replacement_cost_multiple": 3.0,
        "warning_drawdown_pct": 12.0,
        "derisk_drawdown_pct": 14.0,
        "halt_drawdown_pct": 15.0,
        "warning_max_exposure_pct": 70.0,
        "commission_rate_pct": 0.03,
        "minimum_commission_cny": 5.0,
        "stamp_duty_rate_pct": 0.05,
        "transfer_fee_rate_pct": 0.001,
        "slippage_bps": 10.0,
        "max_bar_participation_pct": 5.0,
        "benchmark_symbol": "000300",
        "benchmark_name": "沪深 300 全收益",
    }


def normalize_portfolio_config(value: object) -> dict:
    normalized = default_portfolio_config()
    if not isinstance(value, dict):
        return normalized
    normalized["enabled"] = bool(value.get("enabled", normalized["enabled"]))
    integer_fields = {
        "max_positions": (1, 10),
        "signal_invalid_days": (1, 20),
    }
    float_fields = {
        "initial_cash": (10_000.0, 1_000_000_000.0),
        "target_weight_pct": (0.1, 10.0),
        "stop_loss_pct": (0.1, 50.0),
        "trailing_activation_pct": (0.1, 100.0),
        "trailing_drawdown_pct": (0.1, 50.0),
        "replacement_score_delta": (0.0, 1.0),
        "replacement_cost_multiple": (0.0, 20.0),
        "warning_drawdown_pct": (0.1, 99.0),
        "derisk_drawdown_pct": (0.1, 99.0),
        "halt_drawdown_pct": (0.1, 100.0),
        "warning_max_exposure_pct": (0.0, 100.0),
        "commission_rate_pct": (0.0, 5.0),
        "minimum_commission_cny": (0.0, 1_000.0),
        "stamp_duty_rate_pct": (0.0, 5.0),
        "transfer_fee_rate_pct": (0.0, 1.0),
        "slippage_bps": (0.0, 500.0),
        "max_bar_participation_pct": (0.1, 100.0),
    }
    for field, (minimum, maximum) in integer_fields.items():
        try:
            normalized[field] = min(maximum, max(minimum, int(value.get(field, normalized[field]))))
        except (TypeError, ValueError):
            pass
    for field, (minimum, maximum) in float_fields.items():
        try:
            normalized[field] = min(maximum, max(minimum, float(value.get(field, normalized[field]))))
        except (TypeError, ValueError):
            pass
    warning = normalized["warning_drawdown_pct"]
    normalized["derisk_drawdown_pct"] = max(warning, normalized["derisk_drawdown_pct"])
    normalized["halt_drawdown_pct"] = max(normalized["derisk_drawdown_pct"], normalized["halt_drawdown_pct"])
    normalized["benchmark_symbol"] = str(value.get("benchmark_symbol") or normalized["benchmark_symbol"]).strip()[:32]
    normalized["benchmark_name"] = str(value.get("benchmark_name") or normalized["benchmark_name"]).strip()[:80]
    return normalized


def default_strategy_config() -> dict:
    return {
        "version": STRATEGY_STORE_VERSION,
        "id": None,
        "revision": 1,
        "parent_strategy_id": None,
        "name": "科技 AI 板块短线筛选",
        "description": "默认覆盖人工智能板块，可叠加自选股池与板块过滤。",
        "created_at": None,
        "updated_at": None,
        "lifecycle": default_strategy_lifecycle(),
        "signal": default_signal_config(),
        "allocation": default_allocation_config(),
        "validation": default_validation_config(),
        "portfolio": default_portfolio_config(),
        "delivery": default_report_delivery(),
        "parameters": {
            item["id"]: {"enabled": item["default_enabled"], "value": deepcopy(item["default"])}
            for item in PARAMETER_CATALOG
        },
    }


def normalize_strategy_config(config: dict | None) -> dict:
    normalized = default_strategy_config()
    if not isinstance(config, dict):
        return normalized
    if isinstance(config.get("name"), str) and config["name"].strip():
        normalized["name"] = config["name"].strip()[:80]
    strategy_id = str(config.get("id") or "").strip()
    normalized["id"] = strategy_id[:80] or None
    try:
        normalized["revision"] = max(1, int(config.get("revision") or 1))
    except (TypeError, ValueError):
        normalized["revision"] = 1
    parent_strategy_id = str(config.get("parent_strategy_id") or "").strip()
    normalized["parent_strategy_id"] = parent_strategy_id[:80] or None
    normalized["description"] = str(config.get("description") or "").strip()[:500]
    normalized["created_at"] = config.get("created_at")
    normalized["updated_at"] = config.get("updated_at")
    normalized["lifecycle"] = normalize_strategy_lifecycle(config.get("lifecycle"))
    normalized["signal"] = normalize_signal_config(config.get("signal"))
    normalized["allocation"] = normalize_allocation_config(config.get("allocation"))
    normalized["validation"] = normalize_validation_config(config.get("validation"))
    normalized["portfolio"] = normalize_portfolio_config(config.get("portfolio"))
    normalized["validation"]["signal_time"] = normalized["signal"]["run_time"]
    normalized["validation"]["maximum_drawdown_pct"] = min(
        normalized["validation"]["maximum_drawdown_pct"],
        normalized["portfolio"]["halt_drawdown_pct"],
    )
    normalized["delivery"] = normalize_report_delivery(config.get("delivery"))
    provided = config.get("parameters")
    if not isinstance(provided, dict):
        return normalized
    for parameter_id, state in provided.items():
        definition = PARAMETERS_BY_ID.get(parameter_id)
        if not definition or not isinstance(state, dict):
            continue
        normalized["parameters"][parameter_id] = {
            "enabled": bool(state.get("enabled", False)),
            "value": _normalize_value(definition, state.get("value", definition["default"])),
        }
    return normalized


def _normalize_value(definition: dict, value: Any) -> Any:
    kind = definition["kind"]
    if kind == "boolean":
        return bool(value)
    if kind in {"text", "choice"}:
        return str(value).strip()[:200]
    if kind in {"multi", "tags"}:
        if not isinstance(value, list):
            return deepcopy(definition["default"])
        return [str(item).strip()[:60] for item in value if str(item).strip()][:30]
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(definition["default"])
    if definition.get("step") == 1:
        return int(number)
    return number


def default_strategy_store() -> dict:
    return {"version": STRATEGY_STORE_VERSION, "active_strategy_id": None, "strategies": []}


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize_strategy_store(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        raise StrategyLifecycleError("策略配置必须是对象")
    if payload.get("version") != STRATEGY_STORE_VERSION:
        raise StrategyLifecycleError(
            f"不支持的策略配置版本，仅接受 version={STRATEGY_STORE_VERSION}"
        )
    if not isinstance(payload.get("strategies"), list):
        raise StrategyLifecycleError("策略配置缺少 strategies 列表")

    configured_active_id = payload.get("active_strategy_id")
    strategies = []
    used_ids = set()
    for item in payload["strategies"]:
        if not isinstance(item, dict):
            raise StrategyLifecycleError("strategies 只能包含策略对象")
        if item.get("version") != STRATEGY_STORE_VERSION:
            raise StrategyLifecycleError(
                f"策略版本不受支持，仅接受 version={STRATEGY_STORE_VERSION}"
            )
        strategy_id = item.get("id")
        if not isinstance(strategy_id, str) or not strategy_id.strip():
            raise StrategyLifecycleError("策略必须包含非空 id")
        if strategy_id != strategy_id.strip():
            raise StrategyLifecycleError("策略 id 不能包含首尾空白")
        if strategy_id in used_ids:
            raise StrategyLifecycleError(f"策略 id 重复: {strategy_id}")
        required_sections = (
            "lifecycle",
            "signal",
            "allocation",
            "validation",
            "portfolio",
            "delivery",
            "parameters",
        )
        missing_sections = [
            section for section in required_sections if not isinstance(item.get(section), dict)
        ]
        if missing_sections:
            raise StrategyLifecycleError(
                f"策略 {strategy_id} 缺少配置段: {', '.join(missing_sections)}"
            )
        strategy = normalize_strategy_config(item)
        strategy["id"] = strategy_id
        used_ids.add(strategy_id)
        strategies.append(strategy)
    if configured_active_id is not None:
        if not isinstance(configured_active_id, str) or configured_active_id not in used_ids:
            raise StrategyLifecycleError("active_strategy_id 必须指向现有策略")
    return {
        "version": STRATEGY_STORE_VERSION,
        "active_strategy_id": configured_active_id,
        "strategies": strategies,
    }


def load_strategy_store(path: str | Path | None = None) -> dict:
    config_path = Path(path) if path is not None else strategy_config_path()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default_strategy_store()
    except json.JSONDecodeError as exc:
        raise StrategyLifecycleError(f"策略配置 JSON 无法解析: {exc.msg}") from exc
    return _normalize_strategy_store(payload)


def save_strategy_store(store: dict, path: str | Path | None = None) -> dict:
    config_path = Path(path) if path is not None else strategy_config_path()
    normalized = _normalize_strategy_store(store)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(config_path)
    return normalized


def load_strategy_config(path: str | Path | None = None, strategy_id: str | None = None) -> dict:
    store = load_strategy_store(path)
    target_id = strategy_id or store["active_strategy_id"]
    for strategy in store["strategies"]:
        if strategy["id"] == target_id:
            return deepcopy(strategy)
    return default_strategy_config()


def find_strategy_config(strategy_id: str, path: str | Path | None = None) -> dict | None:
    target_id = str(strategy_id or "").strip()
    if not target_id:
        return None
    store = load_strategy_store(path)
    for strategy in store["strategies"]:
        if strategy["id"] == target_id:
            return deepcopy(strategy)
    return None


def save_strategy_config(config: dict, path: str | Path | None = None, strategy_id: str | None = None) -> dict:
    store = load_strategy_store(path)
    target_id = strategy_id or config.get("id") or store["active_strategy_id"]
    normalized = normalize_strategy_config(config)
    normalized["updated_at"] = _timestamp()
    for index, existing in enumerate(store["strategies"]):
        if existing["id"] != target_id:
            continue
        changed_model = normalized["signal"] != existing.get("signal") or normalized["allocation"] != existing.get("allocation") or normalized["parameters"] != existing["parameters"] or normalized["portfolio"] != existing.get("portfolio") or any(
            normalized["validation"].get(key) != existing["validation"].get(key)
            for key in default_validation_config()
            if key not in {"last_backtest", "approval_gate"}
        )
        existing_stage = existing.get("lifecycle", {}).get("stage", "draft")
        if changed_model and existing_stage in LOCKED_STRATEGY_STAGES:
            raise StrategyLifecycleError("已上线或已归档策略不可直接修改，请创建新版本")
        normalized["id"] = existing["id"]
        normalized["revision"] = existing.get("revision", 1)
        normalized["parent_strategy_id"] = existing.get("parent_strategy_id")
        normalized["created_at"] = existing.get("created_at") or normalized["updated_at"]
        normalized["lifecycle"] = deepcopy(existing.get("lifecycle") or default_strategy_lifecycle())
        if changed_model:
            normalized["lifecycle"].update(
                {
                    "stage": "draft",
                    "stage_updated_at": normalized["updated_at"],
                    "approved_at": None,
                    "approved_by": None,
                    "paper_sessions": 0,
                    "paper_started_at": None,
                    "paper_dates": [],
                }
            )
            normalized["validation"]["last_backtest"] = None
            normalized["validation"]["approval_gate"] = {"passed": False, "checks": [], "evaluated_at": None}
        else:
            normalized["validation"]["last_backtest"] = deepcopy(existing["validation"].get("last_backtest"))
            normalized["validation"]["approval_gate"] = deepcopy(existing["validation"].get("approval_gate"))
        store["strategies"][index] = normalized
        save_strategy_store(store, path)
        return deepcopy(normalized)

    normalized["id"] = str(target_id or uuid.uuid4().hex)
    normalized["created_at"] = normalized.get("created_at") or normalized["updated_at"]
    store["strategies"].append(normalized)
    if not store["active_strategy_id"]:
        store["active_strategy_id"] = normalized["id"]
    save_strategy_store(store, path)
    return deepcopy(normalized)


def create_strategy(
    name: str,
    *,
    description: str = "",
    path: str | Path | None = None,
    activate: bool | None = None,
) -> dict:
    store = load_strategy_store(path)
    now = _timestamp()
    strategy = default_strategy_config()
    strategy.update(
        {
            "id": uuid.uuid4().hex,
            "name": str(name or "新策略").strip()[:80] or "新策略",
            "description": str(description or "").strip()[:500],
            "created_at": now,
            "updated_at": now,
        }
    )
    store["strategies"].append(strategy)
    if activate is True or (activate is None and store["active_strategy_id"] is None):
        store["active_strategy_id"] = strategy["id"]
    save_strategy_store(store, path)
    return deepcopy(strategy)


def activate_strategy(strategy_id: str, *, path: str | Path | None = None) -> dict:
    store = load_strategy_store(path)
    if strategy_id not in {item["id"] for item in store["strategies"]}:
        raise KeyError(f"strategy not found: {strategy_id}")
    store["active_strategy_id"] = strategy_id
    return save_strategy_store(store, path)


def deactivate_strategy(strategy_id: str, *, path: str | Path | None = None) -> dict:
    store = load_strategy_store(path)
    if strategy_id not in {item["id"] for item in store["strategies"]}:
        raise KeyError(f"strategy not found: {strategy_id}")
    if store["active_strategy_id"] == strategy_id:
        store["active_strategy_id"] = None
    return save_strategy_store(store, path)


def transition_strategy_stage(
    strategy_id: str,
    stage: str,
    *,
    approved_by: str = "",
    path: str | Path | None = None,
) -> dict:
    target_stage = str(stage or "").strip().lower()
    if target_stage not in STRATEGY_STAGES:
        raise StrategyLifecycleError(f"不支持的策略阶段: {stage}")
    store = load_strategy_store(path)
    strategy = next((item for item in store["strategies"] if item["id"] == strategy_id), None)
    if strategy is None:
        raise KeyError(strategy_id)
    current_stage = strategy["lifecycle"]["stage"]
    allowed = {
        "draft": {"draft", "backtesting", "paused", "archived"},
        "backtesting": {"draft", "paper", "paused", "archived"},
        "paper": {"draft", "backtesting", "live", "paused", "archived"},
        "live": {"paused", "archived"},
        "paused": {"draft", "backtesting", "paper", "live", "archived"},
        "archived": {"archived"},
    }
    if target_stage not in allowed[current_stage]:
        raise StrategyLifecycleError(f"策略不能从 {current_stage} 直接进入 {target_stage}")
    if target_stage == "live":
        gate = strategy["validation"].get("approval_gate") or {}
        required_sessions = strategy["validation"]["minimum_paper_sessions"]
        if not gate.get("passed"):
            raise StrategyLifecycleError("样本外回测尚未通过上线门禁")
        if strategy["lifecycle"].get("paper_sessions", 0) < required_sessions:
            raise StrategyLifecycleError(f"模拟盘不足 {required_sessions} 个交易日")
    now = _timestamp()
    lifecycle = strategy["lifecycle"]
    lifecycle["stage"] = target_stage
    lifecycle["stage_updated_at"] = now
    if target_stage == "paper" and not lifecycle.get("paper_started_at"):
        lifecycle["paper_started_at"] = now
    if target_stage == "live":
        lifecycle["approved_at"] = now
        lifecycle["approved_by"] = str(approved_by or "manual").strip()[:120]
    elif target_stage in {"draft", "backtesting"}:
        lifecycle["approved_at"] = None
        lifecycle["approved_by"] = None
    strategy["updated_at"] = now
    if target_stage == "archived" and store["active_strategy_id"] == strategy_id:
        store["active_strategy_id"] = None
    save_strategy_store(store, path)
    return deepcopy(strategy)


def record_backtest_evaluation(
    strategy_id: str,
    result: dict,
    *,
    path: str | Path | None = None,
) -> dict:
    store = load_strategy_store(path)
    strategy = next((item for item in store["strategies"] if item["id"] == strategy_id), None)
    if strategy is None:
        raise KeyError(strategy_id)
    now = _timestamp()
    summary = deepcopy(result)
    gate = deepcopy(summary.get("approval_gate") or {"passed": False, "checks": []})
    gate["evaluated_at"] = gate.get("evaluated_at") or now
    strategy["validation"]["last_backtest"] = summary
    strategy["validation"]["approval_gate"] = gate
    lifecycle = strategy["lifecycle"]
    if lifecycle["stage"] in {"draft", "backtesting"}:
        lifecycle.update(
            {
                "stage": "paper",
                "stage_updated_at": now,
                "paper_started_at": lifecycle.get("paper_started_at") or now,
            }
        )
    strategy["updated_at"] = now
    save_strategy_store(store, path)
    return deepcopy(strategy)


def record_paper_session(strategy_id: str, trade_date: str, *, path: str | Path | None = None) -> dict:
    store = load_strategy_store(path)
    strategy = next((item for item in store["strategies"] if item["id"] == strategy_id), None)
    if strategy is None:
        raise KeyError(strategy_id)
    if strategy["lifecycle"]["stage"] != "paper":
        return deepcopy(strategy)
    normalized_date = str(trade_date or "").strip()[:10]
    dates = strategy["lifecycle"].setdefault("paper_dates", [])
    if normalized_date and normalized_date not in dates:
        dates.append(normalized_date)
        strategy["lifecycle"]["paper_dates"] = dates[-252:]
        strategy["lifecycle"]["paper_sessions"] = len(strategy["lifecycle"]["paper_dates"])
        strategy["updated_at"] = _timestamp()
        save_strategy_store(store, path)
    return deepcopy(strategy)


def create_strategy_revision(strategy_id: str, *, path: str | Path | None = None) -> dict:
    store = load_strategy_store(path)
    source = next((item for item in store["strategies"] if item["id"] == strategy_id), None)
    if source is None:
        raise KeyError(strategy_id)
    now = _timestamp()
    revision = deepcopy(source)
    revision.update(
        {
            "id": uuid.uuid4().hex,
            "revision": int(source.get("revision", 1)) + 1,
            "parent_strategy_id": source["id"],
            "name": f"{source['name']} v{int(source.get('revision', 1)) + 1}"[:80],
            "created_at": now,
            "updated_at": now,
            "lifecycle": default_strategy_lifecycle(),
        }
    )
    revision["validation"]["last_backtest"] = None
    revision["validation"]["approval_gate"] = {"passed": False, "checks": [], "evaluated_at": None}
    revision["delivery"]["enabled"] = False
    store["strategies"].append(revision)
    save_strategy_store(store, path)
    return deepcopy(revision)


def duplicate_strategy(strategy_id: str, *, path: str | Path | None = None) -> dict:
    store = load_strategy_store(path)
    source = next((item for item in store["strategies"] if item["id"] == strategy_id), None)
    if source is None:
        raise KeyError(f"strategy not found: {strategy_id}")
    now = _timestamp()
    copied = deepcopy(source)
    copied.update(
        {
            "id": uuid.uuid4().hex,
            "revision": 1,
            "parent_strategy_id": None,
            "name": f"{source['name']} - 副本"[:80],
            "created_at": now,
            "updated_at": now,
            "lifecycle": default_strategy_lifecycle(),
        }
    )
    copied["validation"]["last_backtest"] = None
    copied["validation"]["approval_gate"] = {"passed": False, "checks": [], "evaluated_at": None}
    copied["delivery"]["enabled"] = False
    store["strategies"].append(copied)
    save_strategy_store(store, path)
    return deepcopy(copied)


def delete_strategy(strategy_id: str, *, path: str | Path | None = None) -> dict:
    store = load_strategy_store(path)
    remaining = [item for item in store["strategies"] if item["id"] != strategy_id]
    if len(remaining) == len(store["strategies"]):
        raise KeyError(f"strategy not found: {strategy_id}")
    store["strategies"] = remaining
    if store["active_strategy_id"] == strategy_id:
        store["active_strategy_id"] = remaining[0]["id"] if remaining else None
    return save_strategy_store(store, path)


def strategy_library_payload(path: str | Path | None = None) -> dict:
    store = load_strategy_store(path)
    summaries = []
    for strategy in store["strategies"]:
        active_parameters = sum(1 for state in strategy["parameters"].values() if state.get("enabled"))
        profile = market_profile(strategy_market(strategy))
        summaries.append(
            {
                "id": strategy["id"],
                "name": strategy["name"],
                "description": strategy.get("description") or "",
                "created_at": strategy.get("created_at"),
                "updated_at": strategy.get("updated_at"),
                "revision": strategy.get("revision", 1),
                "parent_strategy_id": strategy.get("parent_strategy_id"),
                "lifecycle": deepcopy(strategy["lifecycle"]),
                "approval_gate": deepcopy(strategy["validation"].get("approval_gate")),
                "last_backtest": deepcopy(strategy["validation"].get("last_backtest")),
                "minimum_paper_sessions": strategy["validation"].get("minimum_paper_sessions", 40),
                "signal": deepcopy(strategy["signal"]),
                "allocation": deepcopy(strategy["allocation"]),
                "active_parameters": active_parameters,
                "is_active": strategy["id"] == store["active_strategy_id"],
                "delivery": deepcopy(strategy["delivery"]),
                "market": {
                    "code": profile.code,
                    "label": profile.label,
                    "currency": profile.currency,
                    "currency_symbol": profile.currency_symbol,
                },
            }
        )
    return {"active_strategy_id": store["active_strategy_id"], "strategies": summaries}


def parameter_value(config: dict, parameter_id: str, default: Any = None) -> Any:
    state = config.get("parameters", {}).get(parameter_id, {})
    if not state.get("enabled"):
        return default
    return state.get("value", default)


def chase_risk_threshold(config: dict | None = None) -> float:
    current = config or load_strategy_config()
    try:
        return float(parameter_value(current, "chase_risk_pct", 7))
    except (TypeError, ValueError):
        return 7.0


def _number(text: str) -> float:
    return float(text.replace(",", ""))


def convert_strategy_text(text: str) -> dict:
    content = str(text or "").strip()
    updates: dict[str, dict] = {}

    def set_value(parameter_id: str, value: Any, reason: str) -> None:
        updates[parameter_id] = {"id": parameter_id, "enabled": True, "value": value, "reason": reason}

    market_match = re.search(r"美股|美国股票|纳斯达克|NASDAQ", content, flags=re.I)
    if market_match:
        set_value("market", US_MARKET, market_match.group(0))
    else:
        market_match = re.search(r"A股|沪深|中国股票", content, flags=re.I)
        if market_match:
            set_value("market", CN_MARKET, market_match.group(0))

    range_patterns = [
        (r"流通市值[^\d]*(\d+(?:\.\d+)?)\s*(?:到|至|[-~—])\s*(\d+(?:\.\d+)?)\s*亿", "float_market_cap_min", "float_market_cap_max", 100_000_000),
        (r"总市值[^\d]*(\d+(?:\.\d+)?)\s*(?:到|至|[-~—])\s*(\d+(?:\.\d+)?)\s*亿", "total_market_cap_min", "total_market_cap_max", 100_000_000),
        (r"RSI[^\d]*(\d+(?:\.\d+)?)\s*(?:到|至|[-~—])\s*(\d+(?:\.\d+)?)", "rsi_min", "rsi_max", 1),
    ]
    for pattern, minimum_id, maximum_id, multiplier in range_patterns:
        match = re.search(pattern, content, flags=re.I)
        if match:
            set_value(minimum_id, _number(match.group(1)) * multiplier, match.group(0))
            set_value(maximum_id, _number(match.group(2)) * multiplier, match.group(0))

    scalar_patterns = [
        (r"(?:涨幅|涨跌幅)(?:至少|不低于|大于等于|>=?)\s*(\d+(?:\.\d+)?)\s*%?", "change_pct_min"),
        (r"(?:涨幅|涨跌幅)(?:不超过|低于|小于等于|<=?)\s*(\d+(?:\.\d+)?)\s*%?", "change_pct_max"),
        (r"换手率(?:至少|不低于|大于等于|>=?)\s*(\d+(?:\.\d+)?)\s*%?", "turnover_rate_min"),
        (r"换手率(?:不超过|低于|小于等于|<=?)\s*(\d+(?:\.\d+)?)\s*%?", "turnover_rate_max"),
        (r"(?:PE|市盈率)(?:不超过|低于|小于等于|<=?)[^\d]*(\d+(?:\.\d+)?)", "pe_max"),
        (r"(?:PB|市净率)(?:不超过|低于|小于等于|<=?)[^\d]*(\d+(?:\.\d+)?)", "pb_max"),
        (r"(?:ROE|净资产收益率)(?:至少|不低于|大于等于|>=?)[^\d]*(\d+(?:\.\d+)?)\s*%?", "roe_min"),
        (r"(?:营收|营业收入)(?:同比)?(?:增速|增长)(?:至少|不低于|大于等于|>=?)[^\d]*(\d+(?:\.\d+)?)\s*%?", "revenue_growth_min"),
        (r"(?:净利润)(?:同比)?(?:增速|增长)(?:至少|不低于|大于等于|>=?)[^\d]*(\d+(?:\.\d+)?)\s*%?", "profit_growth_min"),
        (r"资产负债率(?:不超过|低于|小于等于|<=?)[^\d]*(\d+(?:\.\d+)?)\s*%?", "debt_ratio_max"),
        (r"股息率(?:至少|不低于|大于等于|>=?)[^\d]*(\d+(?:\.\d+)?)\s*%?", "dividend_yield_min"),
        (r"量比(?:至少|不低于|大于等于|>=?)[^\d]*(\d+(?:\.\d+)?)", "volume_ratio_min"),
    ]
    for pattern, parameter_id in scalar_patterns:
        match = re.search(pattern, content, flags=re.I)
        if match:
            set_value(parameter_id, _number(match.group(1)), match.group(0))

    boolean_keywords = [
        ("exclude_st", ["排除ST", "剔除ST", "不要ST"]),
        ("exclude_suspended", ["排除停牌", "剔除停牌"]),
        ("above_open", ["站上开盘价", "高于开盘价"]),
        ("above_prev_close", ["红盘", "高于昨收"]),
        ("above_vwap", ["站上均价线", "高于均价线", "均价线上方"]),
        ("ma5_above_ma20", ["MA5高于MA20", "5日线上穿20日线", "五日线上穿二十日线"]),
        ("ma20_above_ma60", ["MA20高于MA60", "20日线上穿60日线"]),
        ("macd_bullish", ["MACD金叉", "MACD多头"]),
        ("breakout_20d", ["突破20日高点", "突破二十日高点"]),
        ("operating_cashflow_positive", ["经营现金流为正", "正经营现金流"]),
        ("free_cashflow_positive", ["自由现金流为正", "正自由现金流"]),
        ("exclude_abnormal_audit", ["排除非标审计", "审计意见正常"]),
    ]
    compact = re.sub(r"\s+", "", content).upper()
    for parameter_id, keywords in boolean_keywords:
        if any(re.sub(r"\s+", "", keyword).upper() in compact for keyword in keywords):
            set_value(parameter_id, True, next(keyword for keyword in keywords if re.sub(r"\s+", "", keyword).upper() in compact))

    recognized = [item["reason"] for item in updates.values()]
    return {
        "strategy": content,
        "updates": list(updates.values()),
        "recognized_count": len(updates),
        "recognized_phrases": recognized,
        "message": "已生成参数草案" if updates else "未识别到明确阈值，请补充数字或条件词",
    }


def catalog_payload(config: dict | None = None) -> dict:
    current = normalize_strategy_config(config or load_strategy_config())
    market = strategy_market(current)
    profile = market_profile(market)
    parameters = []
    for definition in PARAMETER_CATALOG:
        item = deepcopy(definition)
        item.update(current["parameters"][item["id"]])
        item["applicable"] = parameter_applicable(item["id"], market)
        if item["id"] in {"price_min", "price_max"}:
            item["unit"] = profile.currency_symbol
        elif item["id"] == "turnover_min":
            item["unit"] = "亿美元" if market == US_MARKET else "亿元"
        elif item["id"] in {
            "float_market_cap_min",
            "float_market_cap_max",
            "total_market_cap_min",
            "total_market_cap_max",
        }:
            item["unit"] = "亿美元" if market == US_MARKET else "亿元"
        elif item["id"] == "board_code":
            item["label"] = "股票池代码"
            item["description"] = (
                "当前支持 NASDAQ100/NDX 动态成分"
                if market == US_MARKET
                else definition["description"]
            )
        item["effective"] = (
            item["applicable"]
            and item["enabled"]
            and item["status"] in {"live", "derived"}
        )
        parameters.append(item)
    return {
        "groups": deepcopy(GROUPS),
        "config": current,
        "parameters": parameters,
        "market": {
            "code": market,
            "label": profile.label,
            "timezone": str(profile.timezone),
            "currency": profile.currency,
            "currency_symbol": profile.currency_symbol,
            "lot_size": profile.lot_size,
        },
    }
