from __future__ import annotations

import json
import math
import os
import re
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable, Iterable

from .config import DEFAULT_BOARD_CODE, DEFAULT_BOARD_NAME
from .enrichment import enrich_candidates
from .market_adapters import get_market_adapter
from .markets import CN_MARKET, market_profile, order_session_date, strategy_market
from .parameters import chase_risk_threshold, load_strategy_config, parameter_value
from .recommendation import RecommendationPlan, build_recommendation_plan
from .selection import analyze_candidates, attach_ignition_signals, filter_candidates, missing_required_parameter_data, price_position
from .universe import normalize_sector_filters
from .universe_provider import BoardUniverseProvider, Nasdaq100UniverseProvider, UniverseQuoteBatch
from .utils import beijing_now, number


@dataclass(frozen=True, slots=True)
class CandidatePreparation:
    filtered: tuple[dict, ...]
    history_ready: tuple[dict, ...]
    raw_count: int
    basic_count: int
    enriched_count: int
    history_ready_count: int
    strategy_filtered_count: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateCollection:
    generated_at: datetime
    analyses: tuple[dict, ...]
    market_analyses: tuple[dict, ...]
    fetch_error: str | None
    data_quality: dict


def prepare_candidates(
    rows: list[dict],
    *,
    strategy: dict | None = None,
    history_fetcher: Callable | None = None,
    financial_fetcher: Callable | None = None,
    enrich_limit: int | None = None,
    sector_filters: str | Iterable[object] | None = None,
    signal_cutoff=None,
    market: object = CN_MARKET,
) -> CandidatePreparation:
    current = strategy if strategy is not None else load_strategy_config()
    missing = missing_required_parameter_data(rows, current, market=market)
    if missing:
        return CandidatePreparation(
            filtered=(),
            history_ready=(),
            raw_count=len(rows),
            basic_count=0,
            enriched_count=0,
            history_ready_count=0,
            strategy_filtered_count=0,
            error="行情缺少已启用参数数据：" + "、".join(missing),
        )
    basic = filter_candidates(
        rows,
        current,
        include_enriched=False,
        sector_filters=sector_filters,
        market=market,
    )
    enriched = enrich_candidates(
        basic,
        strategy=current,
        history_fetcher=history_fetcher,
        financial_fetcher=financial_fetcher,
        limit=enrich_limit,
        signal_cutoff=signal_cutoff,
        market=market,
    )
    history_ready = [row for row in enriched if isinstance(row.get("signal_features"), dict)]
    filtered = filter_candidates(
        enriched,
        current,
        sector_filters=sector_filters,
        market=market,
    )
    errors = [message for row in enriched for message in row.get("enrichment_errors", [])]
    enrichment_error = None
    if basic and not history_ready and errors:
        enrichment_error = "扩展指标获取失败或缺失：" + "；".join(sorted(set(errors))[:3])
    return CandidatePreparation(
        filtered=tuple(filtered),
        history_ready=tuple(history_ready),
        raw_count=len(rows),
        basic_count=len(basic),
        enriched_count=len(enriched),
        history_ready_count=len(history_ready),
        strategy_filtered_count=len(filtered),
        error=enrichment_error,
    )


def _coverage_thresholds(universe_type: str) -> tuple[int, float]:
    if universe_type == "watchlist":
        minimum = int(os.getenv("STOCK_AGENT_MIN_WATCHLIST_HISTORY_COUNT", "1"))
        ratio = float(os.getenv("STOCK_AGENT_MIN_WATCHLIST_HISTORY_COVERAGE_RATIO", "0.7"))
    else:
        minimum = int(os.getenv("STOCK_AGENT_MIN_BOARD_HISTORY_COUNT", "30"))
        ratio = float(os.getenv("STOCK_AGENT_MIN_BOARD_HISTORY_COVERAGE_RATIO", "0.7"))
    return max(1, minimum), min(1.0, max(0.0, ratio))


def _data_quality(
    preparation: CandidatePreparation,
    *,
    universe_type: str,
    source_diagnostics: dict,
    analyzed_count: int,
    market_analyzed_count: int,
) -> dict:
    minimum_count, minimum_ratio = _coverage_thresholds(universe_type)
    if source_diagnostics.get("source_mode") == "injected_primary":
        minimum_count = 1
    snapshot_count = int(source_diagnostics.get("snapshot_count") or 0)
    universe_count = max(preparation.raw_count, snapshot_count)
    quote_coverage_ratio = preparation.raw_count / universe_count if universe_count else 0.0
    minimum_quote_ratio = min(
        1.0,
        max(0.0, float(os.getenv("STOCK_AGENT_MIN_QUOTE_COVERAGE_RATIO", "0.8"))),
    )
    ratio_required = math.ceil(preparation.basic_count * minimum_ratio)
    if universe_type == "watchlist":
        required_count = min(preparation.basic_count, max(minimum_count, ratio_required))
    else:
        required_count = max(minimum_count, ratio_required)
    coverage_ratio = (
        preparation.history_ready_count / preparation.basic_count
        if preparation.basic_count
        else 0.0
    )
    ready = (
        preparation.error is None
        and preparation.basic_count > 0
        and quote_coverage_ratio >= minimum_quote_ratio
        and preparation.history_ready_count >= required_count
    )
    if ready:
        reason = (
            f"历史特征覆盖 {preparation.history_ready_count}/{preparation.basic_count} "
            f"（{coverage_ratio * 100:.1f}%）"
        )
    elif preparation.error:
        reason = preparation.error
    elif universe_count and quote_coverage_ratio < minimum_quote_ratio:
        reason = (
            f"实时行情覆盖不足：{preparation.raw_count}/{universe_count} "
            f"（{quote_coverage_ratio * 100:.1f}%），至少需要 {minimum_quote_ratio * 100:.0f}%"
        )
    elif preparation.basic_count <= 0:
        reason = "基础过滤后没有可评估股票"
    else:
        reason = (
            f"历史特征覆盖不足：{preparation.history_ready_count}/{preparation.basic_count} "
            f"（{coverage_ratio * 100:.1f}%），至少需要 {required_count} 只"
        )
    return {
        "schema_version": 1,
        "status": "READY" if ready else "BLOCKED",
        "reason": reason,
        "universe_type": universe_type,
        "source_mode": source_diagnostics.get("source_mode") or universe_type,
        "primary_error": source_diagnostics.get("primary_error"),
        "quote_error": source_diagnostics.get("quote_error"),
        "snapshot_count": snapshot_count,
        "snapshot_fetched_at": source_diagnostics.get("snapshot_fetched_at"),
        "raw_count": universe_count,
        "quote_count": preparation.raw_count,
        "basic_count": preparation.basic_count,
        "enriched_count": preparation.enriched_count,
        "history_ready_count": preparation.history_ready_count,
        "strategy_filtered_count": preparation.strategy_filtered_count,
        "market_analyzed_count": market_analyzed_count,
        "analyzed_count": analyzed_count,
        "minimum_history_count": minimum_count,
        "minimum_history_coverage_ratio": minimum_ratio,
        "required_history_count": required_count,
        "history_coverage_ratio": round(coverage_ratio, 6),
        "minimum_quote_coverage_ratio": minimum_quote_ratio,
        "quote_coverage_ratio": round(quote_coverage_ratio, 6),
    }


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
    universe_provider: BoardUniverseProvider | Nasdaq100UniverseProvider | None = None,
) -> CandidateCollection:
    report_time = beijing_now(now)
    strategy = strategy or load_strategy_config()
    market = strategy_market(strategy)
    adapter = get_market_adapter(market)
    history_client = history_fetcher or (
        lambda symbol: adapter.fetch_history(symbol, strategy=strategy)
    )
    signal_cutoff = order_session_date(report_time, market)
    board_code, board_name = adapter.resolve_universe(
        strategy,
        code=board_code,
        name=board_name,
    )
    configured_watchlist = watchlist if watchlist is not None else parameter_value(strategy, "watchlist", [])
    configured_sectors = sector_filters if sector_filters is not None else parameter_value(strategy, "sector_filters", [])
    watchlist_entries = adapter.normalize_watchlist(configured_watchlist)
    sectors = normalize_sector_filters(configured_sectors)

    if watchlist_entries:
        try:
            rows, error = adapter.fetch_watchlist(
                watchlist_entries,
                fetcher=watchlist_fetcher,
                strategy=strategy,
            )
        except Exception as exc:
            rows, error = [], str(exc)
        rows = adapter.constrain_watchlist(rows, watchlist_entries)
        prepared = prepare_candidates(
            rows,
            strategy=strategy,
            history_fetcher=history_client,
            financial_fetcher=financial_fetcher,
            enrich_limit=enrich_limit,
            sector_filters=sectors,
            signal_cutoff=signal_cutoff,
            market=market,
        )
        if prepared.error:
            error = prepared.error
        if not prepared.filtered and not error:
            if sectors:
                error = f"自选股池中没有匹配板块过滤（{'、'.join(sectors)}）的有效候选"
            else:
                error = "自选股池中没有符合当前策略的候选"
        analyses = analyze_candidates(prepared.filtered, strategy=strategy)
        market_analyses = analyze_candidates(prepared.history_ready, strategy=strategy)
        if prepared.filtered and not analyses and not error:
            error = "候选缺少 08:00 信号所需的前一交易日历史数据"
        quality = _data_quality(
            prepared,
            universe_type="watchlist",
            source_diagnostics={"source_mode": "watchlist"},
            analyzed_count=len(analyses),
            market_analyzed_count=len(market_analyses),
        )
        if quality["status"] == "BLOCKED":
            error = error or quality["reason"]
        return CandidateCollection(
            generated_at=report_time,
            analyses=tuple(analyses),
            market_analyses=tuple(market_analyses),
            fetch_error=error,
            data_quality=quality,
        )

    if universe_provider is not None:
        batch = universe_provider.fetch(board_code, board_name=board_name, now=report_time)
    elif board_fetcher is not None:
        try:
            rows, error = board_fetcher(board_code, board_name=board_name)
        except Exception as exc:
            rows, error = [], str(exc)
        batch = UniverseQuoteBatch(
            rows=tuple(rows),
            mode="injected_primary" if rows else "unavailable",
            board_code=str(board_code),
            board_name=str(board_name),
            primary_error=error,
            quote_error=(
                "注入数据源没有完整板块快照，禁止使用固定兜底名单"
                if not rows and fallback_fetcher is not None
                else None
            ),
            market=market,
        )
    else:
        batch = adapter.fetch_universe(
            strategy,
            code=board_code,
            name=board_name,
            now=report_time,
        )

    prepared = prepare_candidates(
        list(batch.rows),
        strategy=strategy,
        history_fetcher=history_client,
        financial_fetcher=financial_fetcher,
        enrich_limit=enrich_limit,
        sector_filters=sectors,
        signal_cutoff=signal_cutoff,
        market=market,
    )
    error = batch.error or prepared.error
    analyses = analyze_candidates(prepared.filtered, strategy=strategy)
    market_analyses = analyze_candidates(prepared.history_ready, strategy=strategy)
    if prepared.filtered and not analyses and not error:
        error = "候选缺少 08:00 信号所需的前一交易日历史数据"
    quality = _data_quality(
        prepared,
        universe_type="board",
        source_diagnostics=batch.diagnostics(),
        analyzed_count=len(analyses),
        market_analyzed_count=len(market_analyses),
    )
    if quality["status"] == "BLOCKED":
        error = error or quality["reason"]
    return CandidateCollection(
        generated_at=report_time,
        analyses=tuple(analyses),
        market_analyses=tuple(market_analyses),
        fetch_error=error,
        data_quality=quality,
    )


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
    universe_provider: BoardUniverseProvider | Nasdaq100UniverseProvider | None = None,
) -> RecommendationPlan:
    current = strategy if strategy is not None else load_strategy_config()
    market = strategy_market(current)
    adapter = get_market_adapter(market)
    board_code, board_name = adapter.resolve_universe(
        current,
        code=board_code,
        name=board_name,
    )
    configured_watchlist = watchlist if watchlist is not None else parameter_value(current, "watchlist", [])
    configured_sectors = sector_filters if sector_filters is not None else parameter_value(current, "sector_filters", [])
    watchlist_entries = adapter.normalize_watchlist(configured_watchlist)
    sectors = normalize_sector_filters(configured_sectors)
    collection = collect_analyzed_candidates(
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
        universe_provider=universe_provider,
    )
    plan = build_recommendation_plan(
        generated_at=collection.generated_at,
        analyses=collection.analyses,
        market_analyses=collection.market_analyses,
        strategy=current,
        board_code=board_code,
        board_name=board_name,
        watchlist_size=len(watchlist_entries),
        sector_filters=sectors,
        fetch_error=collection.fetch_error,
        data_quality=collection.data_quality,
        candidate_limit=candidate_limit,
        selection_limit=selection_limit,
        market=market,
    )
    return plan


def enrich_recommendation_plan_with_ticks(
    plan: RecommendationPlan,
    *,
    tick_fetcher: Callable | None = None,
    tick_limit: int = 2,
) -> RecommendationPlan:
    """Attach optional intraday tick commentary without changing admission."""

    if not market_profile(plan.market).supports_tick_ignition:
        return plan
    candidates = [deepcopy(item) for item in plan.candidates]
    attach_ignition_signals(candidates, tick_fetcher=tick_fetcher, tick_limit=tick_limit)
    by_symbol = {str(item.get("symbol")): item for item in candidates}
    selected = [deepcopy(by_symbol.get(str(item.get("symbol")), item)) for item in plan.selected_candidates]
    return replace(plan, candidates=tuple(candidates), selected_candidates=tuple(selected))


def recommendation_context_payload(plan: RecommendationPlan) -> dict:
    profile = market_profile(plan.market)
    return {
        "generated_at": plan.generated_at.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "universe_type": plan.universe_type,
        "watchlist_size": plan.watchlist_size,
        "sector_filters": list(plan.sector_filters),
        "board_code": plan.board_code,
        "board_name": plan.board_name,
        "market": plan.market,
        "market_label": profile.label,
        "currency": profile.currency,
        "currency_symbol": profile.currency_symbol,
        "source": list(plan.sources),
        "fetch_error": plan.fetch_error,
        "candidate_count": len(plan.candidates),
        "data_quality": deepcopy(plan.data_quality),
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
                "price_position": price_position(item, market=plan.market),
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
    profile = market_profile(plan.market)
    chase_threshold = chase_risk_threshold(current)
    threshold_text = f"{chase_threshold:g}%"
    universe_label = "自选股池" if plan.universe_type == "watchlist" else f"{plan.board_name}股票池"
    filter_label = f"，板块过滤为 {'、'.join(plan.sector_filters)}" if plan.sector_filters else ""
    data_scope_instruction = (
        "技术指标统一使用前复权日线；财务指标使用最新已披露报告期，二者时间口径不得混淆。"
        if profile.code == CN_MARKET
        else "技术指标使用美股日线；未接入的财务指标已标记为不适用，不得虚构。"
    )
    execution_instruction = (
        "5. 可解释已接入的 10 秒逐笔量价点火，但不得改变已确定的股票列表。"
        if profile.supports_tick_ignition
        else "5. 当前市场未接入逐笔点火数据，不得虚构相关成交或盘口判断。"
    )
    return "\n".join(
        [
            f"下面是北京时间 {plan.generated_at.strftime('%Y-%m-%d %H:%M')} 拉取的{profile.label} {universe_label}候选数据{filter_label}。",
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
            data_scope_instruction,
            f"3. 对涨幅超过 {threshold_text} 的股票，默认按高风险处理，除非有充分理由。",
            f"4. 硬规则：涨幅超过 {threshold_text} 的股票不得建议中仓或重仓，只能建议观望或轻仓试错。",
            execution_instruction,
            "6. 如果所有强信号股票都已大涨，优先输出“今日不建议追高”。",
            "7. 输出包含：推荐排序、理由、风险、建议仓位、免责声明。",
            "8. 每只推荐股票必须带标准证券代码，便于后续在对应市场开市期间跟踪成交量和涨跌幅。",
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
    universe_provider: BoardUniverseProvider | Nasdaq100UniverseProvider | None = None,
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
        universe_provider=universe_provider,
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
