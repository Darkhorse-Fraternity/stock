from __future__ import annotations

import os
from pathlib import Path

from .config import DEFAULT_BOARD_CODE, DEFAULT_BOARD_NAME, DEFAULT_LLM_TIMEOUT_SECONDS
from .context import generate_agent_context
from .delivery import should_deliver_report
from .parameters import find_strategy_config, load_strategy_config, parameter_value
from .reports import generate_ai_report, generate_report


def main() -> None:
    strategy_id = os.getenv("STOCK_AGENT_STRATEGY_ID", "").strip()
    strategy = find_strategy_config(strategy_id) if strategy_id else load_strategy_config()
    if strategy is None:
        raise ValueError(f"策略不存在: {strategy_id}")
    mode = os.getenv("STOCK_AGENT_MODE", "report").strip().lower()
    board_code = os.getenv("STOCK_AGENT_BOARD_CODE") or str(parameter_value(strategy, "board_code", DEFAULT_BOARD_CODE))
    board_name = os.getenv("STOCK_AGENT_BOARD_NAME") or str(parameter_value(strategy, "board_name", DEFAULT_BOARD_NAME))
    if mode == "data":
        report = generate_agent_context(
            board_code=board_code,
            board_name=board_name,
            candidate_limit=int(os.getenv("STOCK_AGENT_CANDIDATE_LIMIT", "8")),
            strategy=strategy,
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
        )
    else:
        report = generate_report(
            board_code=board_code,
            board_name=board_name,
            top_n=int(os.getenv("STOCK_AGENT_TOP_N", "3")),
            strategy=strategy,
        )
    output = os.getenv("STOCK_AGENT_OUTPUT", "/data/stock_recommendation.md")
    if output:
        path = Path(output).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
    if os.getenv("STOCK_AGENT_DELIVERY_RUN", "0") != "1" or should_deliver_report(report, strategy):
        print(report)


if __name__ == "__main__":
    main()
