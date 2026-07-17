from __future__ import annotations

import os
from pathlib import Path

from .config import DEFAULT_BOARD_CODE, DEFAULT_BOARD_NAME, DEFAULT_LLM_TIMEOUT_SECONDS
from .context import generate_agent_context
from .reports import generate_ai_report, generate_report
from .schedule import parse_publish_hours, should_publish_now
from .tracking import generate_saved_tracking_report, save_daily_selection
from .universe import normalize_sector_filters, parse_watchlist


def main() -> None:
    publish_hours = parse_publish_hours(os.getenv("STOCK_AGENT_PUBLISH_HOURS", ""))
    schedule_guard = os.getenv("STOCK_AGENT_SCHEDULE_GUARD", "0").strip().lower() in {"1", "true", "yes"}
    if schedule_guard and not should_publish_now(publish_hours=publish_hours):
        return

    mode = os.getenv("STOCK_AGENT_MODE", "report").strip().lower()
    board_code = os.getenv("STOCK_AGENT_BOARD_CODE", DEFAULT_BOARD_CODE)
    board_name = os.getenv("STOCK_AGENT_BOARD_NAME", DEFAULT_BOARD_NAME)
    state_path = os.getenv("STOCK_AGENT_STATE_PATH", "")
    watchlist = parse_watchlist(os.getenv("STOCK_AGENT_WATCHLIST", ""))
    sector_filters = normalize_sector_filters(
        os.getenv("STOCK_AGENT_SECTOR_FILTERS") or os.getenv("STOCK_AGENT_SECTORS", "")
    )
    universe_options = {"watchlist": watchlist, "sector_filters": sector_filters}
    if mode == "track":
        report = generate_saved_tracking_report(
            state_path=state_path or "/tmp/stock-agent-daily-selection.json",
        )
    elif mode == "data":
        report = generate_agent_context(
            board_code=board_code,
            board_name=board_name,
            candidate_limit=int(os.getenv("STOCK_AGENT_CANDIDATE_LIMIT", "8")),
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
            tracking_limit=int(os.getenv("STOCK_AGENT_TRACKING_LIMIT", "3")),
            **universe_options,
        )
    else:
        report = generate_report(
            board_code=board_code,
            board_name=board_name,
            top_n=int(os.getenv("STOCK_AGENT_TOP_N", "3")),
            **universe_options,
        )
    if state_path and mode in {"ai", "report"}:
        save_daily_selection(state_path, report)
    output = os.getenv("STOCK_AGENT_OUTPUT", "/data/stock_recommendation.md")
    if output:
        path = Path(output).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
    print(report)
