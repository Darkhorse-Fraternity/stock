from __future__ import annotations

import json
import math
import os
import statistics
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from statistics import NormalDist
from typing import Callable, Iterable

from .market_adapters import get_market_adapter
from .markets import strategy_market
from .parameters import (
    find_strategy_config,
    parameter_value,
    record_backtest_evaluation,
    strategy_config_path,
    transition_strategy_stage,
)
from .portfolio_backtest import normalize_universe_snapshots, replay_portfolio_fold
from .signal_engine import (
    SIGNAL_MODEL_ID,
    extract_signal_features,
    rank_signal_rows,
    score_feature_map,
    select_ranked_signals,
    signal_contract,
)
from .utils import number


BACKTEST_LOCK = threading.Lock()
ACTIVE_BACKTEST_IDS: set[str] = set()
MAX_BACKTESTS = 30
BACKTEST_DATASET_CONTRACT_VERSION = 2


class BacktestInProgressError(RuntimeError):
    pass


class BacktestDataError(ValueError):
    pass


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def backtests_path() -> Path:
    configured = os.getenv("STOCK_AGENT_BACKTESTS_PATH", "").strip()
    return Path(configured).expanduser() if configured else strategy_config_path().with_name("strategy_backtests.json")


def _date_value(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _normalize_history(rows: Iterable[dict]) -> dict[date, dict]:
    normalized: dict[date, dict] = {}
    for row in rows:
        day = _date_value(row.get("date", row.get("日期")))
        close = number(row.get("close", row.get("收盘")))
        if day is None or close <= 0:
            continue
        open_price = number(row.get("open", row.get("开盘")), default=close) or close
        normalized[day] = {
            "date": day,
            "open": open_price,
            "close": close,
            "high": number(row.get("high", row.get("最高")), default=close) or close,
            "low": number(row.get("low", row.get("最低")), default=close) or close,
            "volume": number(row.get("volume", row.get("成交量"))),
            "turnover": number(row.get("turnover", row.get("成交额"))),
            "name": row.get("name") or row.get("名称"),
            "upper_limit": row.get("upper_limit", row.get("涨停价")),
            "lower_limit": row.get("lower_limit", row.get("跌停价")),
            "open_volume": row.get("open_volume"),
            "close_volume": row.get("close_volume"),
            "entry_price": row.get("entry_price"),
            "exit_price": row.get("exit_price"),
        }
    return dict(sorted(normalized.items()))


def _feature(history: list[dict]) -> dict | None:
    return extract_signal_features(history)


def _score_features(features: dict[str, dict], strategy: dict | None = None) -> list[tuple[str, float]]:
    return score_feature_map(features, strategy=strategy)


def walk_forward_windows(dates: list[date], validation: dict) -> list[dict]:
    train_days = int(validation.get("train_days", 504))
    validation_days = int(validation.get("validation_days", 63))
    test_days = int(validation.get("test_days", 63))
    gap_days = int(validation.get("gap_days", 5))
    windows = []
    offset = 0
    while offset + train_days + validation_days + gap_days + test_days <= len(dates):
        train_end = offset + train_days
        validation_end = train_end + validation_days
        test_start = validation_end + gap_days
        test_end = test_start + test_days
        windows.append(
            {
                "train_start": dates[offset],
                "train_end": dates[train_end - 1],
                "validation_start": dates[train_end],
                "validation_end": dates[validation_end - 1],
                "test_start": dates[test_start],
                "test_end": dates[test_end - 1],
                "test_dates": dates[test_start:test_end],
            }
        )
        offset += test_days
    return windows


def _benchmark_return(
    benchmark: dict[date, dict] | None,
    panel: dict[str, dict[date, dict]],
    entry_day: date,
    exit_day: date,
) -> float:
    if benchmark and entry_day in benchmark and exit_day in benchmark:
        entry = benchmark[entry_day]["open"]
        exit_price = benchmark[exit_day]["close"]
        return exit_price / entry - 1 if entry > 0 else 0.0
    values = []
    for history in panel.values():
        if entry_day not in history or exit_day not in history:
            continue
        entry = history[entry_day]["open"]
        if entry > 0:
            values.append(history[exit_day]["close"] / entry - 1)
    return statistics.fmean(values) if values else 0.0


def _maximum_drawdown(returns: list[float]) -> float:
    equity = peak = 1.0
    maximum = 0.0
    for item in returns:
        equity *= 1 + item
        peak = max(peak, equity)
        maximum = min(maximum, equity / peak - 1)
    return maximum


def _skewness(values: list[float]) -> float:
    if len(values) < 3:
        return 0.0
    mean = statistics.fmean(values)
    deviation = statistics.pstdev(values)
    return statistics.fmean(((value - mean) / deviation) ** 3 for value in values) if deviation > 0 else 0.0


def _kurtosis(values: list[float]) -> float:
    if len(values) < 4:
        return 3.0
    mean = statistics.fmean(values)
    deviation = statistics.pstdev(values)
    return statistics.fmean(((value - mean) / deviation) ** 4 for value in values) if deviation > 0 else 3.0


def deflated_sharpe_probability(returns: list[float], *, parameter_trials: int = 1) -> float:
    if len(returns) < 3:
        return 0.0
    deviation = statistics.pstdev(returns)
    if deviation <= 0:
        return 1.0 if statistics.fmean(returns) > 0 else 0.0
    observed = statistics.fmean(returns) / deviation
    trials = max(1, int(parameter_trials))
    if trials <= 1:
        expected_max = 0.0
    else:
        normal = NormalDist()
        first = normal.inv_cdf(max(1e-9, 1 - 1 / trials))
        second = normal.inv_cdf(max(1e-9, 1 - 1 / (trials * math.e)))
        expected_max = ((1 - 0.5772156649) * first + 0.5772156649 * second) / math.sqrt(max(1, len(returns)))
    denominator = math.sqrt(
        max(1e-12, 1 - _skewness(returns) * observed + ((_kurtosis(returns) - 1) / 4) * observed * observed)
    )
    z_score = (observed - expected_max) * math.sqrt(len(returns) - 1) / denominator
    return NormalDist().cdf(z_score)


def evaluate_approval_gate(metrics: dict, validation: dict, metadata: dict) -> dict:
    checks = []

    def add(check_id: str, passed: bool, actual, required, message: str) -> None:
        checks.append({"id": check_id, "passed": bool(passed), "actual": actual, "required": required, "message": message})

    add("history", metrics["history_days"] >= validation["history_days_min"], metrics["history_days"], validation["history_days_min"], "历史数据长度")
    add("oos_events", metrics["oos_events"] >= validation["minimum_oos_events"], metrics["oos_events"], validation["minimum_oos_events"], "样本外事件数")
    add("oos_months", metrics["oos_months"] >= validation["minimum_oos_months"], metrics["oos_months"], validation["minimum_oos_months"], "样本外覆盖月份")
    add("positive_folds", metrics["positive_fold_ratio"] >= validation["minimum_positive_fold_ratio"], metrics["positive_fold_ratio"], validation["minimum_positive_fold_ratio"], "正超额收益窗口比例")
    add("net_excess", metrics["mean_excess_return_pct"] > 0, metrics["mean_excess_return_pct"], "> 0", "扣费后平均超额收益")
    add("cost_stress", metrics["stressed_mean_excess_return_pct"] > 0, metrics["stressed_mean_excess_return_pct"], "> 0", "双倍成本压力测试")
    add("drawdown", abs(metrics["maximum_drawdown_pct"]) <= validation["maximum_drawdown_pct"], metrics["maximum_drawdown_pct"], validation["maximum_drawdown_pct"], "最大回撤")
    add("dsr", metrics["dsr_probability"] >= validation["minimum_dsr_probability"], metrics["dsr_probability"], validation["minimum_dsr_probability"], "Deflated Sharpe 概率")
    add("point_in_time", bool(metadata.get("point_in_time_complete")), bool(metadata.get("point_in_time_complete")), True, "历史时点股票池覆盖完整")
    add("benchmark", bool(metadata.get("benchmark_complete")), bool(metadata.get("benchmark_complete")), True, "独立基准行情覆盖完整")
    strategy_parity = bool(metadata.get("strategy_parity_complete")) and metadata.get("signal_model") == SIGNAL_MODEL_ID
    add("strategy_parity", strategy_parity, strategy_parity, True, "回测信号与线上信号逻辑一致")
    add("execution_parity", bool(metadata.get("execution_parity_complete")), bool(metadata.get("execution_parity_complete")), True, "回测复用线上组合执行 Pipeline")
    add("execution_data", bool(metadata.get("execution_data_complete")), bool(metadata.get("execution_data_complete")), True, "历史成交量、涨跌停与执行时点数据完整")
    add("corporate_actions", bool(metadata.get("corporate_actions_complete")), bool(metadata.get("corporate_actions_complete")), True, "分红送转等公司行动处理完整")
    return {"passed": all(item["passed"] for item in checks), "checks": checks, "evaluated_at": _timestamp()}


def _run_signal_event_backtest(dataset: dict, strategy: dict) -> dict:
    validation = strategy["validation"]
    panel = {str(symbol): _normalize_history(rows) for symbol, rows in (dataset.get("panel") or {}).items()}
    panel = {symbol: rows for symbol, rows in panel.items() if rows}
    if not panel:
        raise BacktestDataError("没有可用的历史行情")
    benchmark = _normalize_history(dataset.get("benchmark") or []) or None
    all_dates = sorted({day for history in panel.values() for day in history})
    windows = walk_forward_windows(all_dates, validation)
    if not windows:
        raise BacktestDataError("历史长度不足以形成滚动训练、验证和测试窗口")

    date_index = {day: index for index, day in enumerate(all_dates)}
    holding = int(validation["holding_period_days"])
    lookback = int(validation["lookback_days"])
    top_n = int(validation["top_n"])
    minimum_history = max(61, int(strategy.get("signal", {}).get("minimum_history_rows", 61)), lookback)
    normal_cost = 2 * (float(validation["transaction_cost_bps"]) + float(validation["slippage_bps"])) / 10000
    stressed_cost = normal_cost * 2
    events = []
    folds = []

    for fold_index, window in enumerate(windows, 1):
        fold_events = []
        for signal_day in window["test_dates"]:
            signal_index = date_index[signal_day]
            if signal_index < minimum_history or signal_index + holding - 1 >= len(all_dates):
                continue
            entry_day = signal_day
            exit_day = all_dates[signal_index + holding - 1]
            features = {}
            for symbol, history in panel.items():
                available_dates = [day for day in all_dates[:signal_index] if day in history]
                if len(available_dates) < minimum_history:
                    continue
                if entry_day not in history or exit_day not in history:
                    continue
                item = extract_signal_features([history[day] for day in available_dates[-minimum_history:]], minimum_rows=minimum_history)
                if item:
                    features[symbol] = item
            ranked = rank_signal_rows(
                [
                    {
                        "symbol": symbol,
                        "percent": item.get("latest_return", 0.0) * 100,
                        "signal_features": item,
                    }
                    for symbol, item in features.items()
                ],
                strategy=strategy,
            )
            selected = select_ranked_signals(ranked, top_n, strategy=strategy)
            if not selected:
                continue
            benchmark_return = _benchmark_return(benchmark, panel, entry_day, exit_day)
            stock_returns = []
            symbols = []
            for selected_row in selected:
                symbol = selected_row["symbol"]
                score = selected_row["score"]
                entry_price = panel[symbol][entry_day]["open"]
                exit_price = panel[symbol][exit_day]["close"]
                if entry_price <= 0:
                    continue
                stock_returns.append(exit_price / entry_price - 1)
                symbols.append({"symbol": symbol, "score": round(score, 4)})
            if not stock_returns:
                continue
            gross = statistics.fmean(stock_returns)
            event = {
                "fold": fold_index,
                "signal_date": signal_day.isoformat(),
                "entry_date": entry_day.isoformat(),
                "exit_date": exit_day.isoformat(),
                "symbols": symbols,
                "gross_return": gross,
                "net_return": gross - normal_cost,
                "stressed_net_return": gross - stressed_cost,
                "benchmark_return": benchmark_return,
                "excess_return": gross - normal_cost - benchmark_return,
                "stressed_excess_return": gross - stressed_cost - benchmark_return,
            }
            events.append(event)
            fold_events.append(event)
        fold_excess = [item["excess_return"] for item in fold_events]
        folds.append(
            {
                "fold": fold_index,
                "test_start": window["test_start"].isoformat(),
                "test_end": window["test_end"].isoformat(),
                "events": len(fold_events),
                "mean_excess_return_pct": round(statistics.fmean(fold_excess) * 100, 4) if fold_excess else 0.0,
            }
        )

    if not events:
        raise BacktestDataError("样本外窗口没有产生可评估事件")
    net_returns = [item["net_return"] for item in events]
    excess_returns = [item["excess_return"] for item in events]
    stressed_excess = [item["stressed_excess_return"] for item in events]
    positive_folds = [fold for fold in folds if fold["events"] and fold["mean_excess_return_pct"] > 0]
    active_folds = [fold for fold in folds if fold["events"]]
    first_day = _date_value(events[0]["signal_date"])
    last_day = _date_value(events[-1]["signal_date"])
    parameter_trials = int(dataset.get("metadata", {}).get("parameter_trials") or 1)
    metrics = {
        "history_days": len(all_dates),
        "oos_events": len(events),
        "oos_months": round(((last_day - first_day).days + 1) / 30.4375, 2) if first_day and last_day else 0.0,
        "folds": len(active_folds),
        "positive_fold_ratio": round(len(positive_folds) / len(active_folds), 4) if active_folds else 0.0,
        "mean_net_return_pct": round(statistics.fmean(net_returns) * 100, 4),
        "mean_excess_return_pct": round(statistics.fmean(excess_returns) * 100, 4),
        "stressed_mean_excess_return_pct": round(statistics.fmean(stressed_excess) * 100, 4),
        "win_rate": round(sum(1 for item in net_returns if item > 0) / len(net_returns), 4),
        "maximum_drawdown_pct": round(_maximum_drawdown(net_returns) * 100, 4),
        "dsr_probability": round(deflated_sharpe_probability(excess_returns, parameter_trials=parameter_trials), 6),
        "parameter_trials": parameter_trials,
    }
    metadata = deepcopy(dataset.get("metadata") or {})
    metadata["dataset_contract_version"] = BACKTEST_DATASET_CONTRACT_VERSION
    metadata["signal_model"] = SIGNAL_MODEL_ID
    metadata["allocation_model"] = strategy.get("allocation", {}).get("model", "trend_breadth_v1")
    approval_gate = evaluate_approval_gate(metrics, validation, metadata)
    return {
        "status": "succeeded",
        "strategy_id": strategy.get("id"),
        "strategy_revision": strategy.get("revision", 1),
        "generated_at": _timestamp(),
        "method": "rolling_walk_forward_factor_rank_v1",
        "execution": {
            "signal_time": strategy.get("signal", {}).get("run_time", validation["signal_time"]),
            "data_cutoff": "previous_trading_day_close",
            "allocation_model": strategy.get("allocation", {}).get("model", "trend_breadth_v1"),
            "entry": "signal_day_open_or_supplied_entry_price",
            "holding_period_days": holding,
            "transaction_cost_bps": validation["transaction_cost_bps"],
            "slippage_bps": validation["slippage_bps"],
        },
        "metadata": {**metadata, "signal_contract": signal_contract(strategy)},
        "warnings": deepcopy(metadata.get("warnings") or []),
        "metrics": metrics,
        "folds": folds,
        "sample_events": events[:20],
        "approval_gate": approval_gate,
    }


def run_walk_forward_backtest(dataset: dict, strategy: dict) -> dict:
    validation = strategy["validation"]
    panel = {str(symbol): _normalize_history(rows) for symbol, rows in (dataset.get("panel") or {}).items()}
    panel = {symbol: rows for symbol, rows in panel.items() if rows}
    if not panel:
        raise BacktestDataError("没有可用的历史行情")
    benchmark = _normalize_history(dataset.get("benchmark") or []) or None
    all_dates = sorted({day for history in panel.values() for day in history})
    evaluation = dataset.get("evaluation_period") if isinstance(dataset.get("evaluation_period"), dict) else None
    if evaluation:
        evaluation_start = _date_value(evaluation.get("start"))
        evaluation_end = _date_value(evaluation.get("end"))
        if evaluation_start is None or evaluation_end is None or evaluation_end < evaluation_start:
            raise BacktestDataError("evaluation_period 必须包含有效的 start 和 end")
        test_dates = [day for day in all_dates if evaluation_start <= day <= evaluation_end]
        windows = (
            [
                {
                    "test_start": test_dates[0],
                    "test_end": test_dates[-1],
                    "test_dates": test_dates,
                }
            ]
            if test_dates
            else []
        )
        method = "point_in_time_holdout_portfolio_pipeline_v1"
    else:
        windows = walk_forward_windows(all_dates, validation)
        method = "rolling_walk_forward_portfolio_pipeline_v1"
    if not windows:
        raise BacktestDataError("历史长度不足以形成评估窗口")

    minimum_history = max(
        61,
        int(strategy.get("signal", {}).get("minimum_history_rows", 61)),
        int(validation["lookback_days"]),
    )
    top_n = min(int(validation["top_n"]), int(strategy.get("portfolio", {}).get("max_positions", 10)))
    universe_snapshots = normalize_universe_snapshots(dataset.get("universe_by_date"))
    folds = []
    normal_results = []
    stressed_results = []
    sample_events = []

    for fold_index, window in enumerate(windows, 1):
        normal = replay_portfolio_fold(
            panel,
            benchmark,
            window["test_dates"],
            all_dates,
            strategy,
            universe_snapshots=universe_snapshots,
            minimum_history=minimum_history,
            top_n=top_n,
            cost_multiplier=1.0,
            execution_price_mode=str(dataset.get("metadata", {}).get("execution_price_mode") or "daily_open_close_proxy"),
        )
        stressed = replay_portfolio_fold(
            panel,
            benchmark,
            window["test_dates"],
            all_dates,
            strategy,
            universe_snapshots=universe_snapshots,
            minimum_history=minimum_history,
            top_n=top_n,
            cost_multiplier=2.0,
            execution_price_mode=str(dataset.get("metadata", {}).get("execution_price_mode") or "daily_open_close_proxy"),
        )
        if not normal["days"]:
            continue
        normal_results.append(normal)
        stressed_results.append(stressed)
        excess = normal["fold_return"] - normal["benchmark_return"]
        folds.append(
            {
                "fold": fold_index,
                "test_start": normal["days"][0]["signal_date"],
                "test_end": normal["days"][-1]["signal_date"],
                "events": len(normal["days"]),
                "portfolio_return_pct": round(normal["fold_return"] * 100, 4),
                "benchmark_return_pct": round(normal["benchmark_return"] * 100, 4),
                "mean_excess_return_pct": round(excess * 100, 4),
                "maximum_drawdown_pct": round(normal["maximum_drawdown"] * 100, 4),
                "closed_trades": normal["closed_trades"],
            }
        )
        for day_result in normal["days"]:
            if len(sample_events) < 20:
                sample_events.append({"fold": fold_index, **deepcopy(day_result)})

    if not normal_results:
        raise BacktestDataError("样本外窗口没有产生可评估的组合净值")
    normal_days = [day for result in normal_results for day in result["days"]]
    stressed_days = [day for result in stressed_results for day in result["days"]]
    daily_returns = [day["daily_return"] for day in normal_days]
    daily_excess = [day["excess_return"] for day in normal_days]
    stressed_excess = [
        stressed["daily_return"] - normal["benchmark_return"]
        for normal, stressed in zip(normal_days, stressed_days)
    ]
    positive_folds = [fold for fold in folds if fold["mean_excess_return_pct"] > 0]
    first_day = _date_value(normal_days[0]["signal_date"])
    last_day = _date_value(normal_days[-1]["signal_date"])
    parameter_trials = int(dataset.get("metadata", {}).get("parameter_trials") or 1)
    cumulative_return = math.prod(1 + result["fold_return"] for result in normal_results) - 1
    benchmark_cumulative = math.prod(1 + result["benchmark_return"] for result in normal_results) - 1
    metrics = {
        "history_days": len(all_dates),
        "oos_events": len(normal_days),
        "oos_months": round(((last_day - first_day).days + 1) / 30.4375, 2) if first_day and last_day else 0.0,
        "folds": len(folds),
        "positive_fold_ratio": round(len(positive_folds) / len(folds), 4),
        "mean_net_return_pct": round(statistics.fmean(daily_returns) * 100, 4),
        "mean_excess_return_pct": round(statistics.fmean(daily_excess) * 100, 4),
        "stressed_mean_excess_return_pct": round(statistics.fmean(stressed_excess) * 100, 4),
        "win_rate": round(sum(1 for value in daily_returns if value > 0) / len(daily_returns), 4),
        "maximum_drawdown_pct": round(min(result["maximum_drawdown"] for result in normal_results) * 100, 4),
        "dsr_probability": round(deflated_sharpe_probability(daily_excess, parameter_trials=parameter_trials), 6),
        "cumulative_return_pct": round(cumulative_return * 100, 4),
        "benchmark_cumulative_return_pct": round(benchmark_cumulative * 100, 4),
        "closed_trades": sum(result["closed_trades"] for result in normal_results),
        "maximum_positions_observed": max(day["positions"] for day in normal_days),
        "parameter_trials": parameter_trials,
    }

    metadata = deepcopy(dataset.get("metadata") or {})
    metadata["dataset_contract_version"] = BACKTEST_DATASET_CONTRACT_VERSION
    metadata["signal_model"] = SIGNAL_MODEL_ID
    metadata["point_in_time_complete"] = bool(metadata.get("point_in_time_complete")) and all(
        result["coverage_complete"] for result in normal_results
    )
    benchmark_days = set(benchmark or {})
    required_benchmark_days = {
        _date_value(value)
        for day in normal_days
        for value in (day["signal_date"], day["cutoff_date"])
    }
    metadata["benchmark_complete"] = (
        bool(metadata.get("benchmark_complete"))
        and bool(benchmark)
        and all(day in benchmark_days for day in required_benchmark_days if day is not None)
    )
    metadata["execution_parity_complete"] = True
    metadata["execution_data_complete"] = bool(metadata.get("execution_data_complete")) and all(
        result["execution_data_coverage_complete"] for result in normal_results
    )
    approval_gate = evaluate_approval_gate(metrics, validation, metadata)
    return {
        "status": "succeeded",
        "strategy_id": strategy.get("id"),
        "strategy_revision": strategy.get("revision", 1),
        "generated_at": _timestamp(),
        "method": method,
        "execution": {
            "signal_time": strategy.get("signal", {}).get("run_time", validation["signal_time"]),
            "data_cutoff": "previous_trading_day_close",
            "entry": "signal_day_open_with_t_plus_one",
            "exit": "shared_portfolio_pipeline",
            "valuation": "daily_liquidation_nav",
            "cost_model": "commission_stamp_transfer_slippage",
            "stress_cost_multiplier": 2.0,
        },
        "metadata": {**metadata, "signal_contract": signal_contract(strategy)},
        "warnings": deepcopy(metadata.get("warnings") or []),
        "metrics": metrics,
        "folds": folds,
        "sample_events": sample_events,
        "approval_gate": approval_gate,
    }


def load_current_universe_dataset(strategy: dict) -> dict:
    market = strategy_market(strategy)
    adapter = get_market_adapter(market)
    watchlist = adapter.normalize_watchlist(parameter_value(strategy, "watchlist", []))
    if watchlist:
        rows, error = adapter.fetch_watchlist(watchlist, strategy=strategy)
    else:
        board_code, board_name = adapter.resolve_universe(strategy)
        batch = adapter.fetch_universe(
            strategy,
            code=board_code,
            name=board_name,
        )
        rows, error = list(batch.rows), batch.error
    if not rows:
        raise BacktestDataError(error or "无法获取当前股票范围")
    maximum = max(3, int(os.getenv("STOCK_AGENT_BACKTEST_UNIVERSE_LIMIT", "30")))
    symbols = []
    for row in rows:
        symbol = str(row.get("symbol") or "")
        name = str(row.get("name") or "")
        if (
            symbol
            and (not adapter.profile.uses_code_prefixes or symbol.startswith(("0", "3", "6")))
            and (not adapter.profile.uses_special_treatment_labels or ("ST" not in name.upper() and "退" not in name))
            and symbol not in symbols
        ):
            symbols.append(symbol)
        if len(symbols) >= maximum:
            break
    panel: dict[str, list[dict]] = {}
    errors = []
    configured_workers = max(1, int(os.getenv("STOCK_AGENT_HISTORY_FETCH_WORKERS", "2")))
    with ThreadPoolExecutor(max_workers=min(configured_workers, len(symbols)), thread_name_prefix="stock-backtest") as executor:
        futures = {
            executor.submit(
                adapter.fetch_history,
                symbol,
                strategy=strategy,
            ): symbol
            for symbol in symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                history = future.result()
                if history:
                    panel[symbol] = history
            except Exception as exc:
                errors.append(f"{symbol}: {exc}")
    if len(panel) < 3:
        raise BacktestDataError("历史行情可用股票不足 3 只")
    return {
        "panel": panel,
        "benchmark": [],
        "metadata": {
            "universe_mode": "current_watchlist" if watchlist else "current_board_constituents",
            "market": market,
            "benchmark_mode": "current_universe_equal_weight",
            "point_in_time_complete": False,
            "benchmark_complete": False,
            "strategy_parity_complete": True,
            "execution_data_complete": False,
            "corporate_actions_complete": False,
            "signal_model": SIGNAL_MODEL_ID,
            "parameter_trials": 1,
            "warnings": [
                "当前自动加载器使用现有成分股，存在幸存者偏差，只能用于探索和模拟盘，不能通过实盘门禁。",
                "回测已复用线上组合 Pipeline；当前日线数据不含完整执行时点成交量与涨跌停状态，不能通过实盘门禁。",
                "当前加载器未处理分红送转等公司行动，不能通过实盘门禁。",
                "当前没有独立基准行情，等权股票池仅作探索对照，不能通过实盘门禁。",
                *errors[:10],
            ],
        },
    }


def load_backtest_dataset_file(path: str | Path) -> dict:
    target = Path(path).expanduser()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BacktestDataError(f"无法读取回测数据集：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise BacktestDataError(f"回测数据集不是有效 JSON：{exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("panel"), dict):
        raise BacktestDataError("回测数据集必须包含 panel 股票历史映射")
    dataset = deepcopy(payload)
    dataset.setdefault("benchmark", [])
    dataset.setdefault("universe_by_date", {})
    metadata = dataset.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        raise BacktestDataError("回测数据集 metadata 必须是对象")
    metadata.setdefault("source", "local_point_in_time_dataset")
    return dataset


def load_backtest_dataset(strategy: dict) -> dict:
    configured = os.getenv("STOCK_AGENT_BACKTEST_DATASET_PATH", "").strip()
    if configured:
        return load_backtest_dataset_file(configured)
    return load_current_universe_dataset(strategy)


def _load_backtests_unlocked(path: str | Path | None = None) -> list[dict]:
    target = Path(path) if path is not None else backtests_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def _save_backtests_unlocked(results: list[dict], path: str | Path | None = None) -> None:
    target = Path(path) if path is not None else backtests_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(results[:MAX_BACKTESTS], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def list_backtests(strategy_id: str, *, path: str | Path | None = None) -> list[dict]:
    with BACKTEST_LOCK:
        items = [item for item in _load_backtests_unlocked(path) if item.get("strategy_id") == strategy_id]
    return [{key: deepcopy(item.get(key)) for key in ["id", "strategy_id", "strategy_revision", "status", "created_at", "started_at", "completed_at", "metrics", "approval_gate", "error"]} for item in items]


def get_backtest(backtest_id: str, *, path: str | Path | None = None) -> dict | None:
    with BACKTEST_LOCK:
        item = next((result for result in _load_backtests_unlocked(path) if result.get("id") == backtest_id), None)
    return deepcopy(item) if item else None


def _update_backtest(backtest_id: str, patch: dict, *, path: str | Path | None = None) -> dict:
    with BACKTEST_LOCK:
        items = _load_backtests_unlocked(path)
        for item in items:
            if item.get("id") == backtest_id:
                item.update(patch)
                _save_backtests_unlocked(items, path)
                return deepcopy(item)
    raise KeyError(backtest_id)


def _execute_backtest(
    backtest_id: str,
    strategy: dict,
    loader: Callable[[dict], dict],
    *,
    path: str | Path | None = None,
    config_path: str | Path | None = None,
) -> None:
    started = datetime.now().astimezone()
    _update_backtest(backtest_id, {"status": "running", "started_at": started.isoformat(timespec="seconds")}, path=path)
    try:
        result = run_walk_forward_backtest(loader(strategy), strategy)
        completed = datetime.now().astimezone()
        result.update({"id": backtest_id, "created_at": get_backtest(backtest_id, path=path)["created_at"], "started_at": started.isoformat(timespec="seconds"), "completed_at": completed.isoformat(timespec="seconds"), "duration_seconds": round((completed - started).total_seconds(), 1), "error": None})
        record_backtest_evaluation(strategy["id"], result, path=config_path)
        _update_backtest(backtest_id, result, path=path)
    except Exception as exc:
        completed = datetime.now().astimezone()
        _update_backtest(backtest_id, {"status": "failed", "completed_at": completed.isoformat(timespec="seconds"), "duration_seconds": round((completed - started).total_seconds(), 1), "error": str(exc)[:4000]}, path=path)
        try:
            transition_strategy_stage(strategy["id"], "draft", path=config_path)
        except Exception:
            pass
    finally:
        with BACKTEST_LOCK:
            ACTIVE_BACKTEST_IDS.discard(backtest_id)


def start_backtest(
    strategy_id: str,
    *,
    path: str | Path | None = None,
    config_path: str | Path | None = None,
    data_loader: Callable[[dict], dict] | None = None,
) -> dict:
    strategy = find_strategy_config(strategy_id, path=config_path)
    if strategy is None:
        raise KeyError(strategy_id)
    backtest_id = uuid.uuid4().hex
    with BACKTEST_LOCK:
        if ACTIVE_BACKTEST_IDS:
            raise BacktestInProgressError("已有回测正在执行，请等待完成")
        ACTIVE_BACKTEST_IDS.add(backtest_id)
    try:
        transition_strategy_stage(strategy_id, "backtesting", path=config_path)
        strategy = find_strategy_config(strategy_id, path=config_path)
    except Exception:
        with BACKTEST_LOCK:
            ACTIVE_BACKTEST_IDS.discard(backtest_id)
        raise
    with BACKTEST_LOCK:
        items = _load_backtests_unlocked(path)
        item = {
            "id": backtest_id,
            "strategy_id": strategy["id"],
            "strategy_revision": strategy.get("revision", 1),
            "status": "queued",
            "created_at": _timestamp(),
            "started_at": None,
            "completed_at": None,
            "duration_seconds": None,
            "metrics": None,
            "approval_gate": None,
            "error": None,
        }
        items.insert(0, item)
        _save_backtests_unlocked(items, path)
    thread = threading.Thread(target=_execute_backtest, args=(item["id"], strategy, data_loader or load_backtest_dataset), kwargs={"path": path, "config_path": config_path}, daemon=True)
    thread.start()
    return deepcopy(item)
