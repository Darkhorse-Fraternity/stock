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

from .data_sources import fetch_board_quotes, fetch_watchlist_quotes
from .enrichment import fetch_daily_history
from .parameters import (
    find_strategy_config,
    parameter_value,
    record_backtest_evaluation,
    strategy_config_path,
    transition_strategy_stage,
)
from .universe import normalize_watchlist
from .utils import number


BACKTEST_LOCK = threading.Lock()
ACTIVE_BACKTEST_IDS: set[str] = set()
MAX_BACKTESTS = 30


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
        }
    return dict(sorted(normalized.items()))


def _feature(history: list[dict]) -> dict | None:
    if len(history) < 61:
        return None
    closes = [row["close"] for row in history]
    volumes = [row["volume"] for row in history]
    latest = closes[-1]
    momentum20 = latest / closes[-21] - 1 if closes[-21] > 0 else 0.0
    momentum60 = latest / closes[-61] - 1 if closes[-61] > 0 else 0.0
    ma5 = statistics.fmean(closes[-5:])
    ma20 = statistics.fmean(closes[-20:])
    ma60 = statistics.fmean(closes[-60:])
    returns = [current / previous - 1 for previous, current in zip(closes[-21:-1], closes[-20:]) if previous > 0]
    volatility = statistics.pstdev(returns) * math.sqrt(252) if len(returns) >= 2 else 0.0
    peak = max(closes[-60:])
    drawdown = latest / peak - 1 if peak > 0 else 0.0
    previous_volume = [value for value in volumes[-21:-1] if value > 0]
    volume_ratio = volumes[-1] / statistics.fmean(previous_volume) if previous_volume and volumes[-1] > 0 else 1.0
    return {
        "momentum20": momentum20,
        "momentum60": momentum60,
        "trend": (1.0 if ma5 >= ma20 else 0.0) + (1.0 if ma20 >= ma60 else 0.0),
        "volume_ratio": min(volume_ratio, 5.0),
        "inverse_volatility": -volatility,
        "drawdown": drawdown,
    }


def _percentile_ranks(values: list[tuple[str, float]]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values, key=lambda item: (item[1], item[0]))
    denominator = max(1, len(ordered) - 1)
    return {symbol: index / denominator for index, (symbol, _) in enumerate(ordered)}


def _score_features(features: dict[str, dict]) -> list[tuple[str, float]]:
    fields = ["momentum20", "momentum60", "trend", "volume_ratio", "inverse_volatility", "drawdown"]
    ranks = {field: _percentile_ranks([(symbol, item[field]) for symbol, item in features.items()]) for field in fields}
    return sorted(
        ((symbol, statistics.fmean(ranks[field][symbol] for field in fields)) for symbol in features),
        key=lambda item: (item[1], item[0]),
        reverse=True,
    )


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
    add("point_in_time", bool(metadata.get("point_in_time_complete")), bool(metadata.get("point_in_time_complete")), True, "历史时点成分与财报数据完整")
    add("strategy_parity", bool(metadata.get("strategy_parity_complete")), bool(metadata.get("strategy_parity_complete")), True, "回测信号与线上策略逻辑一致")
    return {"passed": all(item["passed"] for item in checks), "checks": checks, "evaluated_at": _timestamp()}


def run_walk_forward_backtest(dataset: dict, strategy: dict) -> dict:
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
    normal_cost = 2 * (float(validation["transaction_cost_bps"]) + float(validation["slippage_bps"])) / 10000
    stressed_cost = normal_cost * 2
    events = []
    folds = []

    for fold_index, window in enumerate(windows, 1):
        fold_events = []
        for signal_day in window["test_dates"]:
            signal_index = date_index[signal_day]
            if signal_index < lookback or signal_index + holding >= len(all_dates):
                continue
            entry_day = all_dates[signal_index + 1]
            exit_day = all_dates[signal_index + holding]
            features = {}
            for symbol, history in panel.items():
                available_dates = [day for day in all_dates[: signal_index + 1] if day in history]
                if len(available_dates) < max(61, lookback):
                    continue
                if entry_day not in history or exit_day not in history:
                    continue
                item = _feature([history[day] for day in available_dates[-max(61, lookback) :]])
                if item:
                    features[symbol] = item
            selected = _score_features(features)[:top_n]
            if not selected:
                continue
            benchmark_return = _benchmark_return(benchmark, panel, entry_day, exit_day)
            stock_returns = []
            symbols = []
            for symbol, score in selected:
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
    approval_gate = evaluate_approval_gate(metrics, validation, metadata)
    return {
        "status": "succeeded",
        "strategy_id": strategy.get("id"),
        "strategy_revision": strategy.get("revision", 1),
        "generated_at": _timestamp(),
        "method": "rolling_walk_forward_fixed_factor_rank",
        "execution": {
            "signal_time": validation["signal_time"],
            "entry": "next_trading_day_open_or_supplied_entry_price",
            "holding_period_days": holding,
            "transaction_cost_bps": validation["transaction_cost_bps"],
            "slippage_bps": validation["slippage_bps"],
        },
        "metadata": metadata,
        "warnings": deepcopy(metadata.get("warnings") or []),
        "metrics": metrics,
        "folds": folds,
        "sample_events": events[:20],
        "approval_gate": approval_gate,
    }


def load_current_universe_dataset(strategy: dict) -> dict:
    watchlist = normalize_watchlist(parameter_value(strategy, "watchlist", []))
    if watchlist:
        rows, error = fetch_watchlist_quotes(watchlist)
    else:
        board_code = str(parameter_value(strategy, "board_code", "BK0800"))
        board_name = str(parameter_value(strategy, "board_name", "人工智能"))
        rows, error = fetch_board_quotes(board_code, board_name=board_name)
    if not rows:
        raise BacktestDataError(error or "无法获取当前股票范围")
    maximum = max(3, int(os.getenv("STOCK_AGENT_BACKTEST_UNIVERSE_LIMIT", "30")))
    symbols = []
    for row in rows:
        symbol = str(row.get("symbol") or "")
        name = str(row.get("name") or "")
        if symbol.startswith(("0", "3", "6")) and "ST" not in name.upper() and symbol not in symbols:
            symbols.append(symbol)
        if len(symbols) >= maximum:
            break
    panel: dict[str, list[dict]] = {}
    errors = []
    configured_workers = max(1, int(os.getenv("STOCK_AGENT_HISTORY_FETCH_WORKERS", "2")))
    with ThreadPoolExecutor(max_workers=min(configured_workers, len(symbols)), thread_name_prefix="stock-backtest") as executor:
        futures = {executor.submit(fetch_daily_history, symbol): symbol for symbol in symbols}
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
            "benchmark_mode": "current_universe_equal_weight",
            "point_in_time_complete": False,
            "strategy_parity_complete": False,
            "parameter_trials": 1,
            "warnings": [
                "当前自动加载器使用现有成分股，存在幸存者偏差，只能用于探索和模拟盘，不能通过实盘门禁。",
                "固定因子回测尚未覆盖线上全部实时筛选参数，需接入历史时点特征后才能通过策略一致性门禁。",
                *errors[:10],
            ],
        },
    }


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
    thread = threading.Thread(target=_execute_backtest, args=(item["id"], strategy, data_loader or load_current_universe_dataset), kwargs={"path": path, "config_path": config_path}, daemon=True)
    thread.start()
    return deepcopy(item)
