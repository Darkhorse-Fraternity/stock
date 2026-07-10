from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Callable

from .config import DEFAULT_BOARD_CODE, DEFAULT_BOARD_NAME
from .data_sources import fallback_quotes, fetch_board_quotes, fetch_sina_fallback_quotes
from .enrichment import enrich_candidates
from .parameters import chase_risk_threshold, load_strategy_config
from .selection import analyze, attach_ignition_signals, filter_candidates, missing_required_parameter_data, price_position, select_agent_candidates
from .utils import beijing_now, number


def prepare_candidates(
    rows: list[dict],
    *,
    strategy: dict | None = None,
    history_fetcher: Callable | None = None,
    financial_fetcher: Callable | None = None,
    enrich_limit: int | None = None,
) -> tuple[list[dict], str | None]:
    current = strategy or load_strategy_config()
    missing = missing_required_parameter_data(rows, current)
    if missing:
        return [], "行情缺少已启用参数数据：" + "、".join(missing)
    basic = filter_candidates(rows, current, include_enriched=False)
    enriched = enrich_candidates(
        basic,
        strategy=current,
        history_fetcher=history_fetcher,
        financial_fetcher=financial_fetcher,
        limit=enrich_limit,
    )
    filtered = filter_candidates(enriched, current)
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
) -> tuple[datetime, list[dict], str | None]:
    report_time = beijing_now(now)
    fetcher = board_fetcher or fetch_board_quotes
    try:
        rows, error = fetcher(board_code, board_name=board_name)
    except Exception as exc:
        rows, error = [], str(exc)

    strategy = strategy or load_strategy_config()
    filtered, enrichment_error = prepare_candidates(
        rows,
        strategy=strategy,
        history_fetcher=history_fetcher,
        financial_fetcher=financial_fetcher,
        enrich_limit=enrich_limit,
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
        )
        if fallback_rows and not enrichment_error:
            error = None
        elif not fallback_rows:
            filtered = fallback_quotes(report_time, error or fallback_error or "未获得有效行情")
        elif enrichment_error:
            error = enrichment_error

    analyses = [analyze(row, strategy=strategy) for row in filtered]
    analyses.sort(key=lambda item: (item["score"], number(item.get("percent"))), reverse=True)
    return report_time, analyses, error


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
) -> str:
    report_time, analyses, error = collect_analyzed_candidates(
        now=now,
        board_code=board_code,
        board_name=board_name,
        board_fetcher=board_fetcher,
        fallback_fetcher=fallback_fetcher,
        history_fetcher=history_fetcher,
        financial_fetcher=financial_fetcher,
        enrich_limit=enrich_limit,
        strategy=strategy,
    )
    candidates = select_agent_candidates(analyses, candidate_limit, strategy=strategy)
    if enable_tick:
        attach_ignition_signals(candidates, tick_fetcher=tick_fetcher, tick_limit=tick_limit)
    payload = {
        "generated_at": report_time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "board_code": board_code,
        "board_name": board_name,
        "source": sorted({item["source"] for item in candidates}),
        "fetch_error": error,
        "candidate_count": len(candidates),
        "candidates": [
            {
                "symbol": item["symbol"],
                "name": item["name"],
                "price": number(item.get("price")),
                "change_percent": number(item.get("percent")),
                "change_amount": number(item.get("change")),
                "turnover_rate": number(item.get("turnover_rate")),
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
                "risk_hint": item["risk_level"],
                "machine_reasons": item["reasons"][:5],
            }
            for item in candidates
        ],
    }
    chase_threshold = chase_risk_threshold(strategy)
    threshold_text = f"{chase_threshold:g}%"
    return "\n".join(
        [
            f"下面是北京时间 {report_time.strftime('%Y-%m-%d %H:%M')} 拉取的 A 股 {board_name} 板块候选数据。",
            "这些只是结构化行情和机器初筛信号，不是最终投资建议。",
            "",
            "MARKET_DATA_JSON:",
            "```json",
            json.dumps(payload, ensure_ascii=False, indent=2),
            "```",
            "",
            "请基于以上数据做 AI agent 分析，给出最终推荐：",
            "1. 最多推荐 3 只股票；如果高风险过高，可以少于 3 只或建议观望。",
            "2. 必须区分短线热度、追高风险、流动性、估值风险。",
            "技术指标统一使用前复权日线；财务指标使用最新已披露报告期，二者时间口径不得混淆。",
            f"3. 对涨幅超过 {threshold_text} 的股票，默认按高风险处理，除非有充分理由。",
            f"4. 硬规则：涨幅超过 {threshold_text} 的股票不得建议中仓或重仓，只能建议观望或轻仓试错。",
            "5. 如果所有强信号股票都已大涨，优先输出“今日不建议追高”。",
            "6. 输出包含：推荐排序、理由、风险、建议仓位、免责声明。",
        ]
    )


def extract_market_payload(context: str) -> dict | None:
    match = re.search(r"```json\n(.*?)\n```", context, flags=re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
