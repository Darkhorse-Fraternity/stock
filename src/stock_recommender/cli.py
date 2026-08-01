from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import quote

from .config import DEFAULT_BOARD_CODE, DEFAULT_BOARD_NAME, DEFAULT_LLM_TIMEOUT_SECONDS
from .context import collect_recommendation_plan, generate_agent_context
from .delivery import should_deliver_report
from .market_adapters import get_market_adapter
from .parameters import find_strategy_config, load_strategy_config, parameter_value
from .markets import strategy_market
from .portfolio_runtime import (
    format_portfolio_actions,
    format_portfolio_snapshot,
    open_portfolio_runtime,
    process_portfolio_runtime,
)
from .reports import append_performance_link, render_ai_report_result, render_report_result
from .runtime import assert_strategy_runnable
from .schedule import (
    parse_publish_hours,
    should_publish_at_market_open,
    should_publish_now,
)
from .tracking import save_daily_selection
from .universe import normalize_sector_filters
from .utils import beijing_now


def _persist_scheduled_plan(
    plan,
    *,
    execution_kind: str,
    strategy: dict,
    state_path: str,
    history_path: str,
    portfolio_path: str,
    portfolio_engine=None,
) -> None:
    if execution_kind != "scheduled":
        return
    save_daily_selection(
        state_path,
        plan,
        strategy=strategy,
        benchmark_fetcher=(
            get_market_adapter(strategy_market(strategy)).benchmark_fetcher()
            if strategy.get("id")
            else None
        ),
        history_path=history_path,
        portfolio_path=portfolio_path,
        portfolio_engine=portfolio_engine,
    )


def main() -> None:
    run_started = time.monotonic()
    publish_hours = parse_publish_hours(os.getenv("STOCK_AGENT_PUBLISH_HOURS", ""))
    schedule_guard = os.getenv("STOCK_AGENT_SCHEDULE_GUARD", "0").strip().lower() in {"1", "true", "yes"}
    strategy_id = os.getenv("STOCK_AGENT_STRATEGY_ID", "").strip()
    strategy = find_strategy_config(strategy_id) if strategy_id else load_strategy_config()
    if strategy is None:
        raise ValueError(f"策略不存在: {strategy_id}")
    market = strategy_market(strategy)
    if schedule_guard and not should_publish_now(
        publish_hours=publish_hours,
        market=market,
    ):
        return
    mode = os.getenv("STOCK_AGENT_MODE", "report").strip().lower()
    execution_kind = os.getenv("STOCK_AGENT_EXECUTION_KIND", "scheduled").strip().lower()
    automatic_market_open_guard = (
        execution_kind == "scheduled"
        and mode in {"report", "ai"}
        and str(
            strategy.get("delivery", {}).get("schedule_mode", "fixed")
        ).strip().lower()
        == "market_open"
    )
    if automatic_market_open_guard and not should_publish_at_market_open(
        market=market
    ):
        return

    assert_strategy_runnable(strategy, execution_kind=execution_kind, mode=mode)
    adapter = get_market_adapter(market)
    board_code, board_name = adapter.resolve_universe(
        strategy,
        code=os.getenv("STOCK_AGENT_BOARD_CODE")
        or str(parameter_value(strategy, "board_code", DEFAULT_BOARD_CODE)),
        name=os.getenv("STOCK_AGENT_BOARD_NAME")
        or str(parameter_value(strategy, "board_name", DEFAULT_BOARD_NAME)),
    )
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
    watchlist = (
        adapter.normalize_watchlist(watchlist_raw)
        if watchlist_raw is not None
        else parameter_value(strategy, "watchlist", [])
    )
    sector_raw = os.getenv("STOCK_AGENT_SECTOR_FILTERS") or os.getenv("STOCK_AGENT_SECTORS")
    sector_filters = normalize_sector_filters(
        sector_raw if sector_raw is not None else parameter_value(strategy, "sector_filters", [])
    )
    universe_options = {"watchlist": watchlist, "sector_filters": sector_filters}
    portfolio_engine = None
    portfolio_account = None
    portfolio_enabled = bool(strategy.get("id")) and bool(
        strategy.get("portfolio", {}).get("enabled", True)
    )
    if portfolio_enabled and mode in {"report", "ai", "track", "risk"}:
        portfolio_engine, portfolio_account = open_portfolio_runtime(
            strategy,
            path=portfolio_path,
            adapter=adapter,
            occurred_at=beijing_now(),
        )
    if mode == "track":
        batch, snapshot = process_portfolio_runtime(
            strategy,
            engine=portfolio_engine,
            account=portfolio_account,
            occurred_at=beijing_now(),
        )
        report = format_portfolio_snapshot(
            strategy,
            snapshot,
            performance_url=performance_url,
        )
    elif mode == "risk":
        batch, _ = process_portfolio_runtime(
            strategy,
            engine=portfolio_engine,
            account=portfolio_account,
            occurred_at=beijing_now(),
        )
        report = format_portfolio_actions(
            strategy,
            batch,
            performance_url=performance_url,
        )
    elif mode == "data":
        report = generate_agent_context(
            board_code=board_code,
            board_name=board_name,
            candidate_limit=int(os.getenv("STOCK_AGENT_CANDIDATE_LIMIT", "8")),
            strategy=strategy,
            portfolio_engine=portfolio_engine,
            portfolio_account=portfolio_account,
            **universe_options,
        )
    elif mode == "ai":
        candidate_limit = int(os.getenv("STOCK_AGENT_CANDIDATE_LIMIT", "5"))
        tracking_limit = int(os.getenv("STOCK_AGENT_TRACKING_LIMIT", "3"))
        selection_limit = int(strategy.get("validation", {}).get("top_n", min(3, candidate_limit)))
        plan = collect_recommendation_plan(
            board_code=board_code,
            board_name=board_name,
            candidate_limit=candidate_limit,
            selection_limit=selection_limit,
            strategy=strategy,
            **universe_options,
        )
        _persist_scheduled_plan(
            plan,
            execution_kind=execution_kind,
            strategy=strategy,
            state_path=state_path,
            history_path=history_path,
            portfolio_path=portfolio_path,
            portfolio_engine=portfolio_engine,
        )
        configured_llm_timeout = int(
            os.getenv("STOCK_AGENT_LLM_TIMEOUT", str(DEFAULT_LLM_TIMEOUT_SECONDS))
        )
        run_budget = max(1.0, float(os.getenv("STOCK_AGENT_RUN_BUDGET_SECONDS", "100")))
        remaining_budget = max(1, int(run_budget - (time.monotonic() - run_started)))
        recommendation = render_ai_report_result(
            plan,
            tracking_limit=tracking_limit,
            llm_base_url=os.getenv("STOCK_AGENT_LLM_BASE_URL", ""),
            llm_model=os.getenv("STOCK_AGENT_LLM_MODEL", "Flux_AI/Flux_AI:latest"),
            llm_api_key=os.getenv("STOCK_AGENT_LLM_API_KEY", "ollama"),
            llm_timeout=min(configured_llm_timeout, remaining_budget),
            strategy=strategy,
            enable_tick=(
                execution_kind != "scheduled"
                and os.getenv("STOCK_AGENT_ENABLE_TICK", "0") == "1"
            ),
            tick_limit=int(os.getenv("STOCK_AGENT_TICK_LIMIT", "2")),
        )
        report = recommendation.report
    else:
        top_n = int(os.getenv("STOCK_AGENT_TOP_N", "3"))
        plan = collect_recommendation_plan(
            board_code=board_code,
            board_name=board_name,
            candidate_limit=top_n,
            selection_limit=top_n,
            strategy=strategy,
            portfolio_engine=portfolio_engine,
            portfolio_account=portfolio_account,
            **universe_options,
        )
        _persist_scheduled_plan(
            plan,
            execution_kind=execution_kind,
            strategy=strategy,
            state_path=state_path,
            history_path=history_path,
            portfolio_path=portfolio_path,
            portfolio_engine=portfolio_engine,
        )
        recommendation = render_report_result(plan, strategy=strategy)
        report = recommendation.report
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
