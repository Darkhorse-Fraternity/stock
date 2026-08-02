from __future__ import annotations

from copy import deepcopy
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from .market_adapters import get_market_adapter
from .markets import market_date, order_session_date
from .parameters import record_paper_session
from .performance import upsert_recommendation_history
from .portfolio_engine.ledger import JsonLedgerStore
from .portfolio_engine.contracts import DecisionBatch
from .portfolio_engine.service import PortfolioEngine
from .recommendation import RecommendationPlan, recommendation_tracking_entries
from .reports import append_performance_link, format_recommendation_snapshot
from .runtime import assert_strategy_runnable
from .us_data_providers import strategy_us_data_source
from .utils import beijing_now, number


def _average_change(rows: list[dict]) -> float | None:
    values = [number(row.get("percent"), default=float("nan")) for row in rows]
    finite = [value for value in values if value == value]
    return round(sum(finite) / len(finite), 6) if finite else None


def _save_state(target: Path, payload: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def save_daily_selection(
    path: str | Path,
    plan: RecommendationPlan,
    *,
    now: datetime | None = None,
    strategy: dict | None = None,
    benchmark_fetcher: Callable | None = None,
    history_path: str | Path | None = None,
    portfolio_path: str | Path | None = None,
    portfolio_engine: PortfolioEngine | None = None,
) -> list[str]:
    if type(plan) is not RecommendationPlan:
        raise TypeError("plan must be RecommendationPlan")
    if strategy and strategy.get("id"):
        assert_strategy_runnable(strategy, execution_kind="scheduled", mode="report")
    strategy_id = (strategy or {}).get("id")
    portfolio_required = bool(strategy_id) and bool(
        (strategy or {}).get("portfolio", {}).get("enabled", True)
    )
    decision = plan.portfolio_decision
    if portfolio_required:
        if type(decision) is not DecisionBatch:
            raise ValueError("recommendation plan is missing portfolio_decision")
        if decision.strategy_id != strategy_id:
            raise ValueError("portfolio_decision strategy identity mismatch")
        if decision.strategy_revision != (strategy or {}).get("revision"):
            raise ValueError("portfolio_decision strategy revision mismatch")
    entries = recommendation_tracking_entries(plan)
    symbols = [item["symbol"] for item in entries]
    market_regime = deepcopy(plan.market_regime)
    if not symbols and not (strategy or {}).get("id"):
        return []
    current = beijing_now(now or plan.generated_at)
    benchmark_change = None
    benchmark_error = None
    if benchmark_fetcher is not None:
        try:
            rows, benchmark_error = benchmark_fetcher(plan.board_code, board_name=plan.board_name)
            benchmark_change = _average_change(rows)
        except Exception as exc:
            benchmark_error = str(exc)
    target = Path(path).expanduser()
    lifecycle = (strategy or {}).get("lifecycle", {})
    payload = {
        "version": 2,
        "trade_date": order_session_date(current, plan.market).isoformat(),
        "generated_at": current.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "symbols": symbols,
        "recommendations": entries,
        "strategy_id": (strategy or {}).get("id"),
        "strategy_name": (strategy or {}).get("name"),
        "strategy_revision": (strategy or {}).get("revision", 1),
        "strategy_stage": lifecycle.get("stage", "draft"),
        "signal_model": (strategy or {}).get("signal", {}).get("model"),
        "signal_data_cutoff": (strategy or {}).get("signal", {}).get("data_cutoff"),
        "board_code": plan.board_code,
        "board_name": plan.board_name,
        "market": plan.market,
        "us_data_source": (
            strategy_us_data_source(strategy)
            if plan.market == "us"
            else None
        ),
        "actual_data_sources": list(plan.sources),
        "benchmark_mode": "board_constituent_equal_weight",
        "benchmark_initial_change_pct": benchmark_change,
        "benchmark_error": benchmark_error,
        "market_regime": market_regime,
        "data_quality": deepcopy(plan.data_quality),
    }
    if portfolio_required:
        engine = portfolio_engine or PortfolioEngine(
            ledger_store=JsonLedgerStore(portfolio_path)
        )
        account = engine.commit(decision)
        payload["portfolio_account_id"] = account.id
        payload["portfolio_decision_run_key"] = decision.run_key
        payload["portfolio_intent_ids"] = [intent.id for intent in decision.intents]
        payload["portfolio_event_ids"] = [event.id for event in decision.events]
    _save_state(target, payload)
    if payload["strategy_id"] and payload["strategy_stage"] == "paper":
        try:
            record_paper_session(payload["strategy_id"], payload["trade_date"])
        except Exception as exc:
            payload["paper_session_error"] = str(exc)[:500]
            _save_state(target, payload)
    if history_path is not None:
        try:
            upsert_recommendation_history(payload, path=history_path, now=current)
        except Exception as exc:
            payload["history_archive_error"] = str(exc)[:500]
            _save_state(target, payload)
    return symbols


def load_daily_selection_state(path: str | Path, *, now: datetime | None = None) -> dict | None:
    target = Path(path).expanduser()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("trade_date") != market_date(
        beijing_now(now),
        payload.get("market") or "cn",
    ).isoformat():
        return None
    return payload if isinstance(payload, dict) else None


def load_daily_selection(path: str | Path, *, now: datetime | None = None) -> list[str]:
    payload = load_daily_selection_state(path, now=now)
    if not payload:
        return []
    symbols = payload.get("symbols") or []
    return [
        str(symbol)
        for symbol in symbols
        if re.fullmatch(r"(?:\d{6}|[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)?)", str(symbol))
    ]


def _return_since(entry_price: float, current_price: float) -> float | None:
    return (current_price / entry_price - 1) * 100 if entry_price > 0 and current_price > 0 else None


def _benchmark_since(initial_change: float | None, current_change: float | None) -> float | None:
    if initial_change is None or current_change is None:
        return None
    initial_ratio = 1 + initial_change / 100
    current_ratio = 1 + current_change / 100
    return (current_ratio / initial_ratio - 1) * 100 if initial_ratio > 0 else None


def generate_saved_tracking_report(
    *,
    state_path: str | Path,
    now: datetime | None = None,
    quote_fetcher: Callable | None = None,
    benchmark_fetcher: Callable | None = None,
    history_path: str | Path | None = None,
    performance_url: str = "",
) -> str:
    current = beijing_now(now)
    state = load_daily_selection_state(state_path, now=current)
    symbols = load_daily_selection(state_path, now=current)
    if not state or not symbols:
        return "⚠️ 今日尚无可跟踪的推荐股票，跳过本次盘中报告。"

    entries = [{"symbol": symbol} for symbol in symbols]
    market = state.get("market") or "cn"
    adapter = get_market_adapter(market)
    profile = adapter.profile
    try:
        if quote_fetcher is not None:
            rows, error = quote_fetcher(entries)
        else:
            rows, error = adapter.fetch_watchlist(
                entries,
                data_source_policy=state.get("us_data_source"),
            )
    except Exception as exc:
        rows, error = [], str(exc)
    rows = adapter.constrain_watchlist(rows, entries)
    by_symbol = {str(row.get("symbol")): row for row in rows}
    ordered = [by_symbol[symbol] for symbol in symbols if symbol in by_symbol]
    if not ordered:
        return append_performance_link("\n".join(
            [
                "⚠️ **推荐股盘中行情不可用**",
                (
                    f"策略：{state.get('strategy_name') or state.get('strategy_id') or '未命名策略'}"
                    f" · v{state.get('strategy_revision', 1)}"
                ),
                "数据源状态：暂不可用",
                "本次不发布成交量和涨跌幅，避免使用失效数据。",
            ]
        ), performance_url)

    initial_benchmark = state.get("benchmark_initial_change_pct")
    current_benchmark = None
    benchmark_error = None
    if initial_benchmark is not None:
        benchmark_client = benchmark_fetcher or adapter.benchmark_fetcher()
        try:
            benchmark_rows, benchmark_error = benchmark_client(
                state.get("board_code") or "BK0800",
                board_name=state.get("board_name") or "人工智能",
            )
            current_benchmark = _average_change(benchmark_rows)
        except Exception as exc:
            benchmark_error = str(exc)
    benchmark_return = _benchmark_since(initial_benchmark, current_benchmark)
    initial_entries = {item.get("symbol"): item for item in state.get("recommendations") or []}
    detail_lines = ["📐 **推荐后与板块相对表现**"]
    updated_entries = []
    for row in ordered:
        symbol = str(row.get("symbol"))
        saved = dict(initial_entries.get(symbol) or {"symbol": symbol, "name": row.get("name") or symbol})
        price = number(row.get("price"))
        volume = number(row.get("volume"))
        entry_price = number(saved.get("entry_price"))
        observed_max = max(number(saved.get("max_observed_price")), price)
        previous_min = number(saved.get("min_observed_price"), default=price) or price
        observed_min = min(previous_min, price)
        since = _return_since(entry_price, price)
        excess = since - benchmark_return if since is not None and benchmark_return is not None else None
        current_drawdown = (price / observed_max - 1) * 100 if observed_max > 0 else None
        previous_drawdown = number(saved.get("maximum_sampled_drawdown_pct"))
        drawdown = min(previous_drawdown, current_drawdown) if current_drawdown is not None else previous_drawdown
        previous_volume = saved.get("last_volume")
        volume_delta = volume - number(previous_volume) if previous_volume is not None else None
        since_text = f"{since:+.2f}%" if since is not None else "缺少入选价"
        excess_text = f"{excess:+.2f}%" if excess is not None else "基准暂缺"
        drawdown_text = f"{drawdown:.2f}%" if drawdown is not None else "-"
        volume_text = (
            f"，较上次 +{max(0.0, volume_delta):.0f} {profile.volume_unit}"
            if volume_delta is not None
            else ""
        )
        detail_lines.append(
            f"- {row.get('name') or symbol} ({symbol})：推荐后 {since_text}，相对{state.get('board_name') or '板块'} {excess_text}，"
            f"采样最大回撤 {drawdown_text}{volume_text}"
        )
        saved.update(
            {
                "name": row.get("name") or saved.get("name") or symbol,
                "max_observed_price": observed_max,
                "min_observed_price": observed_min,
                "maximum_sampled_drawdown_pct": drawdown,
                "last_price": price,
                "last_volume": volume,
                "last_change_pct": number(row.get("percent")),
                "last_turnover": number(row.get("turnover")),
                "return_since_recommendation_pct": since,
                "benchmark_return_since_recommendation_pct": benchmark_return,
                "excess_return_pct": excess,
                "last_updated_at": current.strftime("%Y-%m-%d %H:%M:%S %Z"),
            }
        )
        updated_entries.append(saved)
    updated_symbols = {item.get("symbol") for item in updated_entries}
    updated_entries.extend(initial_entries[symbol] for symbol in symbols if symbol not in updated_symbols and symbol in initial_entries)
    state["recommendations"] = updated_entries
    state["last_tracking_at"] = current.strftime("%Y-%m-%d %H:%M:%S %Z")
    state["benchmark_current_change_pct"] = current_benchmark
    state["benchmark_error"] = benchmark_error
    _save_state(Path(state_path).expanduser(), state)
    if history_path is not None:
        try:
            upsert_recommendation_history(state, path=history_path, now=current)
        except Exception as exc:
            state["history_archive_error"] = str(exc)[:500]
            _save_state(Path(state_path).expanduser(), state)

    snapshot = format_recommendation_snapshot(
        ordered,
        limit=len(symbols),
        generated_at=current.strftime("%m月%d日 %H:%M"),
        market=market,
    )
    mode_label = "🧪 模拟盘" if state.get("strategy_stage") != "live" else "✅ 已批准实盘"
    benchmark_line = (
        f"板块成分等权近似：{benchmark_return:+.2f}%（从推荐时刻起）"
        if benchmark_return is not None
        else "板块基准暂不可用"
    )
    return append_performance_link("\n".join(
        [
            f"📊 **推荐股盘中情况报告** ({current.strftime('%Y 年%m月%d日')})",
            (
                f"策略：{state.get('strategy_name') or state.get('strategy_id') or '未命名策略'}"
                f" · v{state.get('strategy_revision', 1)}"
            ),
            f"策略模式：{mode_label}",
            "",
            snapshot,
            "",
            benchmark_line,
            *detail_lines,
            "",
            "仅跟踪今日 08:00 基于前一交易日收盘数据生成的策略持仓，不重新选股。",
            "仅供策略验证，不构成投资建议。",
        ]
    ), performance_url)
