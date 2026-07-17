#!/usr/bin/env python3
from __future__ import annotations

from stock_recommender.config import (
    BEIJING_TZ,
    DEFAULT_BOARD_CODE,
    DEFAULT_BOARD_NAME,
    DEFAULT_LLM_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    EASTMONEY_URL,
    MAX_FLOAT_MARKET_CAP,
    MIN_FLOAT_MARKET_CAP,
    STATIC_FALLBACK,
)
from stock_recommender.context import collect_analyzed_candidates, extract_market_payload, generate_agent_context
from stock_recommender.data_sources import (
    akshare_symbol,
    fallback_quotes,
    fetch_board_quotes,
    fetch_sina_fallback_quotes,
    fetch_tick_rows,
    fetch_watchlist_quotes,
    sina_quote_symbol,
)
from stock_recommender.llm import call_llm_analysis
from stock_recommender.reports import (
    append_candidate_explanation,
    apply_risk_guard,
    build_conservative_report,
    candidate_action,
    format_cny,
    format_recommendation_snapshot,
    format_volume_hands,
    generate_ai_report,
    generate_report,
    ignition_signal_summary,
    market_sentiment_profile,
    price_position_summary,
    select_snapshot_candidates,
)
from stock_recommender.selection import (
    analyze,
    attach_ignition_signals,
    evaluate_tick_ignition,
    filter_candidates,
    price_position,
    select_agent_candidates,
    tick_seconds,
)
from stock_recommender.cli import main
from stock_recommender.schedule import DEFAULT_PUBLISH_HOURS, is_market_open, is_weekday, parse_publish_hours, should_publish_now
from stock_recommender.tracking import (
    TRACKING_HEADER,
    extract_recommended_symbols,
    generate_saved_tracking_report,
    load_daily_selection,
    save_daily_selection,
)
from stock_recommender.universe import (
    constrain_to_watchlist,
    filter_rows_by_sector,
    normalize_sector_filters,
    normalize_stock_symbol,
    normalize_watchlist,
    parse_watchlist,
    row_matches_sector,
    row_sector_tags,
)
from stock_recommender.utils import beijing_now, number

__all__ = [
    "BEIJING_TZ",
    "DEFAULT_BOARD_CODE",
    "DEFAULT_BOARD_NAME",
    "DEFAULT_LLM_TIMEOUT_SECONDS",
    "DEFAULT_PUBLISH_HOURS",
    "DEFAULT_TIMEOUT_SECONDS",
    "EASTMONEY_URL",
    "MAX_FLOAT_MARKET_CAP",
    "MIN_FLOAT_MARKET_CAP",
    "STATIC_FALLBACK",
    "TRACKING_HEADER",
    "akshare_symbol",
    "analyze",
    "append_candidate_explanation",
    "apply_risk_guard",
    "attach_ignition_signals",
    "beijing_now",
    "build_conservative_report",
    "call_llm_analysis",
    "candidate_action",
    "collect_analyzed_candidates",
    "evaluate_tick_ignition",
    "extract_market_payload",
    "fallback_quotes",
    "fetch_board_quotes",
    "fetch_sina_fallback_quotes",
    "fetch_tick_rows",
    "fetch_watchlist_quotes",
    "filter_candidates",
    "filter_rows_by_sector",
    "format_cny",
    "format_recommendation_snapshot",
    "format_volume_hands",
    "generate_saved_tracking_report",
    "generate_agent_context",
    "generate_ai_report",
    "generate_report",
    "ignition_signal_summary",
    "main",
    "market_sentiment_profile",
    "is_weekday",
    "is_market_open",
    "normalize_sector_filters",
    "normalize_stock_symbol",
    "normalize_watchlist",
    "number",
    "parse_watchlist",
    "parse_publish_hours",
    "price_position",
    "price_position_summary",
    "select_agent_candidates",
    "select_snapshot_candidates",
    "extract_recommended_symbols",
    "load_daily_selection",
    "save_daily_selection",
    "should_publish_now",
    "constrain_to_watchlist",
    "row_matches_sector",
    "row_sector_tags",
    "sina_quote_symbol",
    "tick_seconds",
]


if __name__ == "__main__":
    main()
