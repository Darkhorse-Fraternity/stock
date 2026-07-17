from __future__ import annotations

import os
from pathlib import Path

from .config import DEFAULT_BOARD_CODE, DEFAULT_BOARD_NAME, DEFAULT_LLM_TIMEOUT_SECONDS
from .context import generate_agent_context
from .data_sources import fetch_board_quotes
from .delivery import should_deliver_report
from .parameters import find_strategy_config, load_strategy_config, parameter_value
from .reports import generate_ai_report, generate_report
from .schedule import parse_publish_hours, should_publish_now
from .tracking import TRACKING_HEADER, generate_saved_tracking_report, save_daily_selection
from .universe import normalize_sector_filters, parse_watchlist


def main() -> None:
    publish_hours = parse_publish_hours(os.getenv("STOCK_AGENT_PUBLISH_HOURS", ""))
    schedule_guard = os.getenv("STOCK_AGENT_SCHEDULE_GUARD", "0").strip().lower() in {"1", "true", "yes"}
    if schedule_guard and not should_publish_now(publish_hours=publish_hours):
        return

    strategy_id = os.getenv("STOCK_AGENT_STRATEGY_ID", "").strip()
    strategy = find_strategy_config(strategy_id) if strategy_id else load_strategy_config()
    if strategy is None:
        raise ValueError(f"策略不存在: {strategy_id}")
    mode = os.getenv("STOCK_AGENT_MODE", "report").strip().lower()
    board_code = os.getenv("STOCK_AGENT_BOARD_CODE") or str(parameter_value(strategy, "board_code", DEFAULT_BOARD_CODE))
    board_name = os.getenv("STOCK_AGENT_BOARD_NAME") or str(parameter_value(strategy, "board_name", DEFAULT_BOARD_NAME))
    state_path = os.getenv("STOCK_AGENT_STATE_PATH", "/tmp/stock-agent-daily-selection.json")
    watchlist_raw = os.getenv("STOCK_AGENT_WATCHLIST")
    watchlist = parse_watchlist(watchlist_raw) if watchlist_raw is not None else parameter_value(strategy, "watchlist", [])
    sector_raw = os.getenv("STOCK_AGENT_SECTOR_FILTERS") or os.getenv("STOCK_AGENT_SECTORS")
    sector_filters = normalize_sector_filters(
        sector_raw if sector_raw is not None else parameter_value(strategy, "sector_filters", [])
    )
    universe_options = {"watchlist": watchlist, "sector_filters": sector_filters}
    if mode == "track":
        report = generate_saved_tracking_report(state_path=state_path)
    elif mode == "data":
        report = generate_agent_context(
            board_code=board_code,
            board_name=board_name,
            candidate_limit=int(os.getenv("STOCK_AGENT_CANDIDATE_LIMIT", "8")),
            strategy=strategy,
            **universe_options,
        )
    elif mode == "ai":
        report = generate_ai_report(
            board_code=board_code,
            board_name=board_name,
            candidate_limit=int(os.getenv("STOCK_AGENT_CANDIDATE_LIMIT", "5")),
            llm_base_url=os.getenv("STOCK_AGENT_LLM_BASE_URL", ""),
            llm_model=os.getenv("STOCK_AGENT_LLM_MODEL", "Flux_AI/Flux_AI:latest"),
            llm_api_key=os.getenv("STOCK_AGENT_LLM_API_KEY", "ollama"),
            llm_timeout=int(os.getenv("STOCK_AGENT_LLM_TIMEOUT", str(DEFAULT_LLM_TIMEOUT_SECONDS))),
            enable_tick=os.getenv("STOCK_AGENT_ENABLE_TICK", "0") == "1",
            tick_limit=int(os.getenv("STOCK_AGENT_TICK_LIMIT", "2")),
            strategy=strategy,
            tracking_limit=int(os.getenv("STOCK_AGENT_TRACKING_LIMIT", "3")),
            **universe_options,
        )
    else:
        report = generate_report(
            board_code=board_code,
            board_name=board_name,
            top_n=int(os.getenv("STOCK_AGENT_TOP_N", "3")),
            strategy=strategy,
            **universe_options,
        )
    if mode in {"ai", "report"}:
        save_daily_selection(
            state_path,
            report,
            strategy=strategy,
            board_code=board_code,
            board_name=board_name,
            benchmark_fetcher=fetch_board_quotes if strategy.get("id") else None,
        )
    output = os.getenv("STOCK_AGENT_OUTPUT", "/data/stock_recommendation.md")
    if output:
        path = Path(output).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
    delivery_allowed = TRACKING_HEADER in report if mode == "track" else should_deliver_report(report, strategy)
    if os.getenv("STOCK_AGENT_DELIVERY_RUN", "0") != "1" or delivery_allowed:
        print(report)


if __name__ == "__main__":
    main()
