from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from .config import DEFAULT_BOARD_CODE, DEFAULT_BOARD_NAME, DEFAULT_LLM_TIMEOUT_SECONDS
from .context import generate_agent_context
from .data_sources import fetch_board_quotes
from .delivery import should_deliver_report
from .parameters import find_strategy_config, load_strategy_config, parameter_value
from .portfolio import format_action_notifications, format_portfolio_summary, monitor_portfolio
from .reports import append_performance_link, generate_ai_report_result, generate_report_result
from .runtime import assert_strategy_runnable
from .schedule import parse_publish_hours, should_publish_now
from .tracking import save_daily_selection
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
    execution_kind = os.getenv("STOCK_AGENT_EXECUTION_KIND", "scheduled").strip().lower()
    assert_strategy_runnable(strategy, execution_kind=execution_kind, mode=mode)
    board_code = os.getenv("STOCK_AGENT_BOARD_CODE") or str(parameter_value(strategy, "board_code", DEFAULT_BOARD_CODE))
    board_name = os.getenv("STOCK_AGENT_BOARD_NAME") or str(parameter_value(strategy, "board_name", DEFAULT_BOARD_NAME))
    state_path = os.getenv("STOCK_AGENT_STATE_PATH", "/tmp/stock-agent-daily-selection.json")
    history_path = os.getenv("STOCK_AGENT_HISTORY_PATH", "data/recommendation_history.json")
    portfolio_path = os.getenv("STOCK_AGENT_PORTFOLIO_PATH", "data/strategy_portfolios.json")
    public_url = os.getenv("STOCK_AGENT_PUBLIC_URL", "").strip().rstrip("/")
    performance_url = (
        f"{public_url}/strategies/{quote(str(strategy['id']), safe='')}/portfolio"
        if public_url and strategy.get("id")
        else ""
    )
    watchlist_raw = os.getenv("STOCK_AGENT_WATCHLIST")
    watchlist = parse_watchlist(watchlist_raw) if watchlist_raw is not None else parameter_value(strategy, "watchlist", [])
    sector_raw = os.getenv("STOCK_AGENT_SECTOR_FILTERS") or os.getenv("STOCK_AGENT_SECTORS")
    sector_filters = normalize_sector_filters(
        sector_raw if sector_raw is not None else parameter_value(strategy, "sector_filters", [])
    )
    universe_options = {"watchlist": watchlist, "sector_filters": sector_filters}
    recommendation = None
    if mode == "track":
        account, _, quote_error = monitor_portfolio(strategy, path=portfolio_path)
        report = format_portfolio_summary(account, performance_url=performance_url, quote_error=quote_error)
    elif mode == "risk":
        account, events, _ = monitor_portfolio(strategy, path=portfolio_path)
        report = format_action_notifications(account, events, performance_url=performance_url) if events else ""
    elif mode == "data":
        report = generate_agent_context(
            board_code=board_code,
            board_name=board_name,
            candidate_limit=int(os.getenv("STOCK_AGENT_CANDIDATE_LIMIT", "8")),
            strategy=strategy,
            **universe_options,
        )
    elif mode == "ai":
        recommendation = generate_ai_report_result(
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
        report = recommendation.report
    else:
        recommendation = generate_report_result(
            board_code=board_code,
            board_name=board_name,
            top_n=int(os.getenv("STOCK_AGENT_TOP_N", "3")),
            strategy=strategy,
            **universe_options,
        )
        report = recommendation.report
    if mode in {"ai", "report"} and execution_kind == "scheduled":
        if recommendation is None:
            raise RuntimeError("推荐模式未生成结构化推荐计划")
        save_daily_selection(
            state_path,
            recommendation.plan,
            strategy=strategy,
            benchmark_fetcher=fetch_board_quotes if strategy.get("id") else None,
            history_path=history_path,
            portfolio_path=portfolio_path,
        )
    if mode in {"ai", "report"}:
        report = append_performance_link(report, performance_url)
    if not report:
        return
    output = os.getenv("STOCK_AGENT_OUTPUT", "/data/stock_recommendation.md")
    if output:
        path = Path(output).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
    delivery_allowed = execution_kind == "scheduled" and (mode in {"track", "risk"} or should_deliver_report(report, strategy))
    if os.getenv("STOCK_AGENT_DELIVERY_RUN", "0") != "1" or delivery_allowed:
        print(report)


if __name__ == "__main__":
    main()
