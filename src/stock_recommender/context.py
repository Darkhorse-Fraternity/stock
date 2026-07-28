from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from typing import Callable, Iterable

from .config import DEFAULT_BOARD_CODE, DEFAULT_BOARD_NAME
from .data_sources import fallback_quotes, fetch_board_quotes, fetch_sina_fallback_quotes, fetch_watchlist_quotes
from .enrichment import enrich_candidates
from .parameters import chase_risk_threshold, load_strategy_config, parameter_value
from .recommendation import RecommendationPlan, build_recommendation_plan
from .selection import analyze_candidates, attach_ignition_signals, filter_candidates, missing_required_parameter_data, price_position
from .universe import constrain_to_watchlist, normalize_sector_filters, normalize_watchlist
from .utils import beijing_now, number


def prepare_candidates(
    rows: list[dict],
    *,
    strategy: dict | None = None,
    history_fetcher: Callable | None = None,
    financial_fetcher: Callable | None = None,
    enrich_limit: int | None = None,
    sector_filters: str | Iterable[object] | None = None,
    signal_cutoff=None,
) -> tuple[list[dict], str | None]:
    current = strategy if strategy is not None else load_strategy_config()
    missing = missing_required_parameter_data(rows, current)
    if missing:
        return [], "行情缺少已启用参数数据：" + "、".join(missing)
    basic = filter_candidates(rows, current, include_enriched=False, sector_filters=sector_filters)
    enriched = enrich_candidates(
        basic,
        strategy=current,
        history_fetcher=history_fetcher,
        financial_fetcher=financial_fetcher,
        limit=enrich_limit,
        signal_cutoff=signal_cutoff,
    )
    filtered = filter_candidates(enriched, current, sector_filters=sector_filters)
    errors = [message for row in enriched for message in row.get("enrichment_errors", [])]
    enrichment_error = None
    if basic and not filtered and errors:
        enrichment_error = "扩展指标获取失败或缺失：" + "；".join(sorted(set(errors))[:3])
    return filtered, enrichment_error


def collect_analyzed_candidates(
    *,
    now: datetime | None = None,
    board_code: str = DEFAULT_BOARD_CODE,
    board_name: str = DEFAULT_BOARD_NAME,
    board_fetcher: Callable | None = None,
    fallback_fetcher: Callable | None = None,
    history_fetcher: Callable | None = None,
    financial_fetcher: Callable | None = None,
    enrich_limit: int | None = None,
    strategy: dict | None = None,
    watchlist: str | Iterable[object] | None = None,
    sector_filters: str | Iterable[object] | None = None,
    watchlist_fetcher: Callable | None = None,
) -> tuple[datetime, list[dict], str | None]:
    report_time = beijing_now(now)
    strategy = strategy or load_strategy_config()
    configured_watchlist = watchlist if watchlist is not None else parameter_value(strategy, "watchlist", [])
    configured_sectors = sector_filters if sector_filters is not None else parameter_value(strategy, "sector_filters", [])
    watchlist_entries = normalize_watchlist(configured_watchlist)
    sectors = normalize_sector_filters(configured_sectors)

    if watchlist_entries:
        fetcher = watchlist_fetcher or fetch_watchlist_quotes
        try:
            rows, error = fetcher(watchlist_entries)
        except Exception as exc:
            rows, error = [], str(exc)
        rows = constrain_to_watchlist(rows, watchlist_entries)
        filtered, enrichment_error = prepare_candidates(
            rows,
            strategy=strategy,
            history_fetcher=history_fetcher,
            financial_fetcher=financial_fetcher,
            enrich_limit=enrich_limit,
            sector_filters=sectors,
            signal_cutoff=report_time.date(),
        )
        if enrichment_error:
            error = enrichment_error
        if not filtered and not error:
            if sectors:
                error = f"自选股池中没有匹配板块过滤（{'、'.join(sectors)}）的有效候选"
            else:
                error = "自选股池中没有符合当前策略的候选"
        analyses = analyze_candidates(filtered, strategy=strategy)
        if filtered and not analyses and not error:
            error = "候选缺少 08:00 信号所需的前一交易日历史数据"
        return report_time, analyses, error

    fetcher = board_fetcher or fetch_board_quotes
    try:
        rows, error = fetcher(board_code, board_name=board_name)
    except Exception as exc:
        rows, error = [], str(exc)

    filtered, enrichment_error = prepare_candidates(
        rows,
        strategy=strategy,
        history_fetcher=history_fetcher,
        financial_fetcher=financial_fetcher,
        enrich_limit=enrich_limit,
        sector_filters=sectors,
        signal_cutoff=report_time.date(),
    )
    if enrichment_error:
        error = enrichment_error
    if not rows:
        fallback = fallback_fetcher or fetch_sina_fallback_quotes
        try:
            fallback_rows, fallback_error = fallback(board_name=board_name)
        except Exception as exc:
            fallback_rows, fallback_error = [], str(exc)
        filtered, enrichment_error = prepare_candidates(
            fallback_rows,
            strategy=strategy,
            history_fetcher=history_fetcher,
            financial_fetcher=financial_fetcher,
            enrich_limit=enrich_limit,
            sector_filters=sectors,
            signal_cutoff=report_time.date(),
        )
        if fallback_rows and not enrichment_error:
            error = None
        elif not fallback_rows:
            filtered = fallback_quotes(report_time, error or fallback_error or "未获得有效行情")
        elif enrichment_error:
            error = enrichment_error

    analyses = analyze_candidates(filtered, strategy=strategy)
    if filtered and not analyses and not error:
        error = "候选缺少 08:00 信号所需的前一交易日历史数据"
    return report_time, analyses, error


def collect_recommendation_plan(
    *,
    now: datetime | None = None,
    board_code: str = DEFAULT_BOARD_CODE,
    board_name: str = DEFAULT_BOARD_NAME,
    candidate_limit: int = 8,
    selection_limit: int = 3,
    board_fetcher: Callable | None = None,
    fallback_fetcher: Callable | None = None,
    history_fetcher: Callable | None = None,
    financial_fetcher: Callable | None = None,
    enrich_limit: int | None = None,
    strategy: dict | None = None,
    watchlist: str | Iterable[object] | None = None,
    sector_filters: str | Iterable[object] | None = None,
    watchlist_fetcher: Callable | None = None,
) -> RecommendationPlan:
    current = strategy if strategy is not None else load_strategy_config()
    configured_watchlist = watchlist if watchlist is not None else parameter_value(current, "watchlist", [])
    configured_sectors = sector_filters if sector_filters is not None else parameter_value(current, "sector_filters", [])
    watchlist_entries = normalize_watchlist(configured_watchlist)
    sectors = normalize_sector_filters(configured_sectors)
    report_time, analyses, error = collect_analyzed_candidates(
        now=now,
        board_code=board_code,
        board_name=board_name,
        board_fetcher=board_fetcher,
        fallback_fetcher=fallback_fetcher,
        history_fetcher=history_fetcher,
        financial_fetcher=financial_fetcher,
        enrich_limit=enrich_limit,
        strategy=current,
        watchlist=watchlist_entries,
        sector_filters=sectors,
        watchlist_fetcher=watchlist_fetcher,
    )
    plan = build_recommendation_plan(
        generated_at=report_time,
        analyses=analyses,
        strategy=current,
        board_code=board_code,
        board_name=board_name,
        watchlist_size=len(watchlist_entries),
        sector_filters=sectors,
        fetch_error=error,
        candidate_limit=candidate_limit,
        selection_limit=selection_limit,
    )
    return plan


def enrich_recommendation_plan_with_ticks(
    plan: RecommendationPlan,
    *,
    tick_fetcher: Callable | None = None,
    tick_limit: int = 2,
) -> RecommendationPlan:
    """Attach optional intraday tick commentary without changing admission."""

    candidates = [deepcopy(item) for item in plan.candidates]
    attach_ignition_signals(candidates, tick_fetcher=tick_fetcher, tick_limit=tick_limit)
    by_symbol = {str(item.get("symbol")): item for item in candidates}
    selected = [deepcopy(by_symbol.get(str(item.get("symbol")), item)) for item in plan.selected_candidates]
    return replace(plan, candidates=tuple(candidates), selected_candidates=tuple(selected))


def recommendation_context_payload(plan: RecommendationPlan) -> dict:
    return {
        "generated_at": plan.generated_at.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "universe_type": plan.universe_type,
        "watchlist_size": plan.watchlist_size,
        "sector_filters": list(plan.sector_filters),
        "board_code": plan.board_code,
        "board_name": plan.board_name,
        "source": list(plan.sources),
        "fetch_error": plan.fetch_error,
        "candidate_count": len(plan.candidates),
        "market_regime": deepcopy(plan.market_regime),
        "signal_contract": deepcopy(plan.signal_contract),
        "portfolio_candidates": [str(item.get("symbol")) for item in plan.selected_candidates],
        "candidates": [
            {
                "symbol": item["symbol"],
                "name": item["name"],
                "sector": item.get("sector") or "未分类",
                "sectors": item.get("sectors") or ([item.get("sector")] if item.get("sector") else []),
                "price": number(item.get("price")),
                "change_percent": number(item.get("percent")),
                "change_amount": number(item.get("change")),
                "turnover_rate": number(item.get("turnover_rate")),
                "volume_hands": number(item.get("volume")),
                "turnover_cny": number(item.get("turnover")),
                "pe": number(item.get("pe")),
                "pb": number(item.get("pb")),
                "amplitude": number(item.get("amplitude")),
                "float_market_cap_cny": number(item.get("float_market_cap")),
                "source": item.get("source"),
                "price_position": price_position(item),
                "ignition_signal": item.get("ignition_signal"),
                "technical": {
                    "ma5": item.get("ma5"),
                    "ma20": item.get("ma20"),
                    "ma60": item.get("ma60"),
                    "rsi": item.get("rsi"),
                    "macd": item.get("macd"),
                    "macd_signal": item.get("macd_signal"),
                    "breakout_20d": item.get("breakout_20d"),
                    "distance_52w_high": item.get("distance_52w_high"),
                    "volatility_20d": item.get("volatility_20d"),
                    "listed_days": item.get("listed_days"),
                },
                "fundamentals": {
                    "report_date": item.get("financial_report_date"),
                    "revenue_growth": item.get("revenue_growth"),
                    "profit_growth": item.get("profit_growth"),
                    "eps_growth": item.get("eps_growth"),
                    "roe": item.get("roe"),
                    "roa": item.get("roa"),
                    "roic": item.get("roic"),
                    "gross_margin": item.get("gross_margin"),
                    "net_margin": item.get("net_margin"),
                    "debt_ratio": item.get("debt_ratio"),
                    "current_ratio": item.get("current_ratio"),
                    "fcf_yield": item.get("fcf_yield"),
                },
                "signal_score": item["score"],
                "signal_features": item.get("signal_features") or {},
                "risk_hint": item["risk_level"],
                "machine_reasons": item["reasons"][:5],
            }
            for item in plan.candidates
        ],
    }


def generate_agent_context_from_plan(plan: RecommendationPlan, *, strategy: dict | None = None) -> str:
    current = strategy if strategy is not None else load_strategy_config()
    payload = recommendation_context_payload(plan)
    chase_threshold = chase_risk_threshold(current)
    threshold_text = f"{chase_threshold:g}%"
    universe_label = "自选股池" if plan.universe_type == "watchlist" else f"{plan.board_name}板块"
    filter_label = f"，板块过滤为 {'、'.join(plan.sector_filters)}" if plan.sector_filters else ""
    return "\n".join(
        [
            f"下面是北京时间 {plan.generated_at.strftime('%Y-%m-%d %H:%M')} 拉取的 A 股 {universe_label}候选数据{filter_label}。",
            "这些只是结构化行情和机器初筛信号，不是最终投资建议。",
            "",
            "MARKET_DATA_JSON:",
            "```json",
            json.dumps(payload, ensure_ascii=False, indent=2),
            "```",
            "",
            "请解释 factor_rank_v1 已确定的 portfolio_candidates；不得增删或替换股票：",
            "1. 逐只解释 portfolio_candidates，AI 只生成说明与风险提示，不参与入池决策。",
            "2. 必须区分短线热度、追高风险、流动性、估值风险。",
            "技术指标统一使用前复权日线；财务指标使用最新已披露报告期，二者时间口径不得混淆。",
            f"3. 对涨幅超过 {threshold_text} 的股票，默认按高风险处理，除非有充分理由。",
            f"4. 硬规则：涨幅超过 {threshold_text} 的股票不得建议中仓或重仓，只能建议观望或轻仓试错。",
            "5. 如果所有强信号股票都已大涨，优先输出“今日不建议追高”。",
            "6. 输出包含：推荐排序、理由、风险、建议仓位、免责声明。",
            "7. 每只推荐股票必须带 6 位股票代码，便于后续每小时跟踪成交量和涨跌幅。",
        ]
    )


def generate_agent_context(
    *,
    now: datetime | None = None,
    board_code: str = DEFAULT_BOARD_CODE,
    board_name: str = DEFAULT_BOARD_NAME,
    candidate_limit: int = 8,
    board_fetcher: Callable | None = None,
    fallback_fetcher: Callable | None = None,
    enable_tick: bool = False,
    tick_fetcher: Callable | None = None,
    tick_limit: int = 2,
    history_fetcher: Callable | None = None,
    financial_fetcher: Callable | None = None,
    enrich_limit: int | None = None,
    strategy: dict | None = None,
    watchlist: str | Iterable[object] | None = None,
    sector_filters: str | Iterable[object] | None = None,
    watchlist_fetcher: Callable | None = None,
) -> str:
    current = strategy or load_strategy_config()
    selection_limit = int(current.get("validation", {}).get("top_n", 3))
    plan = collect_recommendation_plan(
        now=now,
        board_code=board_code,
        board_name=board_name,
        candidate_limit=candidate_limit,
        selection_limit=selection_limit,
        board_fetcher=board_fetcher,
        fallback_fetcher=fallback_fetcher,
        history_fetcher=history_fetcher,
        financial_fetcher=financial_fetcher,
        enrich_limit=enrich_limit,
        strategy=current,
        watchlist=watchlist,
        sector_filters=sector_filters,
        watchlist_fetcher=watchlist_fetcher,
    )
    if enable_tick:
        plan = enrich_recommendation_plan_with_ticks(
            plan,
            tick_fetcher=tick_fetcher,
            tick_limit=tick_limit,
        )
    return generate_agent_context_from_plan(plan, strategy=current)


def extract_market_payload(context: str) -> dict | None:
    match = re.search(r"```json\n(.*?)\n```", context, flags=re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
