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
    sina_quote_symbol,
)
from stock_recommender.llm import call_llm_analysis
from stock_recommender.reports import (
    append_candidate_explanation,
    apply_risk_guard,
    build_conservative_report,
    candidate_action,
    format_cny,
    generate_ai_report,
    generate_report,
    ignition_signal_summary,
    market_sentiment_profile,
    price_position_summary,
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
from stock_recommender.utils import beijing_now, number

__all__ = [
    "BEIJING_TZ",
    "DEFAULT_BOARD_CODE",
    "DEFAULT_BOARD_NAME",
    "DEFAULT_LLM_TIMEOUT_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "EASTMONEY_URL",
    "MAX_FLOAT_MARKET_CAP",
    "MIN_FLOAT_MARKET_CAP",
    "STATIC_FALLBACK",
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
    "filter_candidates",
    "format_cny",
    "generate_agent_context",
    "generate_ai_report",
    "generate_report",
    "ignition_signal_summary",
    "main",
    "market_sentiment_profile",
    "number",
    "price_position",
    "price_position_summary",
    "select_agent_candidates",
    "sina_quote_symbol",
    "tick_seconds",
]


if __name__ == "__main__":
    main()
