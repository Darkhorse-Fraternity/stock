from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Callable, Iterable

from .config import DEFAULT_BOARD_CODE, DEFAULT_BOARD_NAME
from .data_sources import fallback_quotes, fetch_board_quotes, fetch_sina_fallback_quotes, fetch_watchlist_quotes
from .selection import analyze, attach_ignition_signals, filter_candidates, price_position, select_agent_candidates
from .universe import constrain_to_watchlist, filter_rows_by_sector, normalize_sector_filters, normalize_watchlist
from .utils import beijing_now, number


def collect_analyzed_candidates(
    *,
    now: datetime | None = None,
    board_code: str = DEFAULT_BOARD_CODE,
    board_name: str = DEFAULT_BOARD_NAME,
    board_fetcher: Callable | None = None,
    fallback_fetcher: Callable | None = None,
    watchlist: str | Iterable[object] | None = None,
    sector_filters: str | Iterable[object] | None = None,
    watchlist_fetcher: Callable | None = None,
) -> tuple[datetime, list[dict], str | None]:
    report_time = beijing_now(now)
    watchlist_entries = normalize_watchlist(watchlist)
    sectors = normalize_sector_filters(sector_filters)

    if watchlist_entries:
        fetcher = watchlist_fetcher or fetch_watchlist_quotes
        try:
            rows, error = fetcher(watchlist_entries)
        except Exception as exc:
            rows, error = [], str(exc)
        rows = constrain_to_watchlist(rows, watchlist_entries)
        filtered = filter_candidates(rows, sector_filters=sectors)
        if not filtered and not error:
            if sectors:
                error = f"自选股池中没有匹配板块过滤（{'、'.join(sectors)}）的有效候选"
            else:
                error = "自选股池中没有有效候选"
        analyses = [analyze(row) for row in filtered]
        analyses.sort(key=lambda item: (item["score"], number(item.get("percent"))), reverse=True)
        return report_time, analyses, error

    fetcher = board_fetcher or fetch_board_quotes
    try:
        rows, error = fetcher(board_code, board_name=board_name)
    except Exception as exc:
        rows, error = [], str(exc)

    sector_rows = filter_rows_by_sector(rows, sectors)
    if rows and sectors and not sector_rows:
        return report_time, [], f"行情中没有匹配板块过滤（{'、'.join(sectors)}）的股票"

    filtered = filter_candidates(sector_rows)
    if not filtered:
        fallback = fallback_fetcher or fetch_sina_fallback_quotes
        try:
            fallback_rows, fallback_error = fallback(board_name=board_name)
        except Exception as exc:
            fallback_rows, fallback_error = [], str(exc)
        filtered = filter_candidates(fallback_rows, sector_filters=sectors)
        if filtered:
            error = None
        else:
            error = error or fallback_error or "未获得有效行情"
            filtered = filter_rows_by_sector(fallback_quotes(report_time, error), sectors)

    analyses = [analyze(row) for row in filtered]
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
    watchlist: str | Iterable[object] | None = None,
    sector_filters: str | Iterable[object] | None = None,
    watchlist_fetcher: Callable | None = None,
    enable_tick: bool = False,
    tick_fetcher: Callable | None = None,
    tick_limit: int = 2,
) -> str:
    watchlist_entries = normalize_watchlist(watchlist)
    sectors = normalize_sector_filters(sector_filters)
    report_time, analyses, error = collect_analyzed_candidates(
        now=now,
        board_code=board_code,
        board_name=board_name,
        board_fetcher=board_fetcher,
        fallback_fetcher=fallback_fetcher,
        watchlist=watchlist_entries,
        sector_filters=sectors,
        watchlist_fetcher=watchlist_fetcher,
    )
    candidates = select_agent_candidates(analyses, candidate_limit)
    if enable_tick:
        attach_ignition_signals(candidates, tick_fetcher=tick_fetcher, tick_limit=tick_limit)
    payload = {
        "generated_at": report_time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "universe_type": "watchlist" if watchlist_entries else "board",
        "watchlist_size": len(watchlist_entries),
        "sector_filters": sectors,
        "board_code": board_code,
        "board_name": board_name,
        "source": sorted({item.get("source") for item in candidates if item.get("source")}),
        "fetch_error": error,
        "candidate_count": len(candidates),
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
                "amplitude": number(item.get("amplitude")),
                "float_market_cap_cny": number(item.get("float_market_cap")),
                "source": item.get("source"),
                "price_position": price_position(item),
                "ignition_signal": item.get("ignition_signal"),
                "signal_score": item["score"],
                "risk_hint": item["risk_level"],
                "machine_reasons": item["reasons"][:5],
            }
            for item in candidates
        ],
    }
    universe_label = "自选股池" if watchlist_entries else f"{board_name}板块"
    filter_label = f"，板块过滤为 {'、'.join(sectors)}" if sectors else ""
    return "\n".join(
        [
            f"下面是北京时间 {report_time.strftime('%Y-%m-%d %H:%M')} 拉取的 A 股 {universe_label}候选数据{filter_label}。",
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
            "3. 对涨幅超过 7% 的股票，默认按高风险处理，除非有充分理由。",
            "4. 硬规则：涨幅超过 7% 的股票不得建议中仓或重仓，只能建议观望或轻仓试错。",
            "5. 如果所有强信号股票都已大涨，优先输出“今日不建议追高”。",
            "6. 输出包含：推荐排序、理由、风险、建议仓位、免责声明。",
            "7. 每只推荐股票必须带 6 位股票代码，便于后续每小时跟踪成交量和涨跌幅。",
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
