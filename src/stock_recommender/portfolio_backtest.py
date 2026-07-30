from __future__ import annotations

import statistics
from bisect import bisect_left, bisect_right
from copy import deepcopy
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from .market_regime import evaluate_market_regime, filter_absolute_momentum
from .markets import market_profile, strategy_market
from .portfolio import create_portfolio_account, plan_daily_candidates, process_market_snapshot
from .signal_engine import extract_signal_features, rank_signal_rows, select_ranked_signals
from .utils import number


SHANGHAI = ZoneInfo("Asia/Shanghai")
FEE_FIELDS = (
    "commission_rate_pct",
    "minimum_commission_cny",
    "stamp_duty_rate_pct",
    "transfer_fee_rate_pct",
    "slippage_bps",
)


def normalize_universe_snapshots(value: object) -> dict[date, set[str]]:
    snapshots: dict[date, set[str]] = {}
    if isinstance(value, dict):
        entries = value.items()
    elif isinstance(value, list):
        entries = ((item.get("date"), item.get("symbols")) for item in value if isinstance(item, dict))
    else:
        entries = ()
    for raw_day, raw_symbols in entries:
        try:
            day = raw_day if isinstance(raw_day, date) else datetime.strptime(str(raw_day)[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if not isinstance(raw_symbols, (list, tuple, set)):
            continue
        symbols = {str(symbol).strip() for symbol in raw_symbols if str(symbol).strip()}
        if symbols:
            snapshots[day] = symbols
    return dict(sorted(snapshots.items()))


def universe_for_day(
    snapshots: dict[date, set[str]],
    cutoff: date,
    fallback: set[str],
    *,
    ordered_days: tuple[date, ...] | None = None,
) -> tuple[set[str], bool]:
    snapshot_days = ordered_days if ordered_days is not None else tuple(snapshots)
    index = bisect_right(snapshot_days, cutoff) - 1
    if index < 0:
        return set(fallback), False
    return set(snapshots[snapshot_days[index]]), True


def _replay_strategy(strategy: dict, cost_multiplier: float) -> dict:
    replay = deepcopy(strategy)
    replay["id"] = str(replay.get("id") or "isolated-backtest")
    replay["name"] = str(replay.get("name") or "隔离回测")
    replay.setdefault("lifecycle", {})["stage"] = "paper"
    replay.setdefault("portfolio", {})["enabled"] = True
    multiplier = max(0.0, float(cost_multiplier))
    for field in FEE_FIELDS:
        replay["portfolio"][field] = number(replay["portfolio"].get(field)) * multiplier
    return replay


def _quote(symbol: str, row: dict, *, phase: str, previous: dict | None = None) -> dict:
    price = (
        number(row.get("entry_price")) or number(row.get("open"))
        if phase == "open"
        else number(row.get("exit_price")) or number(row.get("close"))
    )
    previous_close = number((previous or {}).get("close"))
    percent = (price / previous_close - 1) * 100 if previous_close > 0 and price > 0 else 0.0
    quote = {
        "symbol": symbol,
        "name": str(row.get("name") or symbol),
        "price": price,
        "percent": percent,
        "volume": number(row.get("volume")),
        "turnover": number(row.get("turnover")),
        "bar_open": price,
        "bar_high": price if phase == "open" else number(row.get("high"), default=price),
        "bar_low": price if phase == "open" else number(row.get("low"), default=price),
    }
    for field in ("upper_limit", "lower_limit"):
        if number(row.get(field)) > 0:
            quote[field] = number(row.get(field))
    if row.get(f"{phase}_volume") is not None:
        quote["bar_volume"] = number(row.get(f"{phase}_volume"))
    return quote


def _liquidation_nav(account: dict) -> float:
    config = account.get("portfolio_config") or {}
    cash = number(account.get("cash"))
    slippage = number(config.get("slippage_bps")) / 10_000
    commission_rate = number(config.get("commission_rate_pct")) / 100
    transfer_rate = number(config.get("transfer_fee_rate_pct")) / 100
    stamp_rate = number(config.get("stamp_duty_rate_pct")) / 100
    minimum_commission = number(config.get("minimum_commission_cny"))
    for position in account.get("positions", {}).values():
        quantity = int(number(position.get("quantity")))
        price = number(position.get("current_price")) * (1 - slippage)
        notional = max(0.0, quantity * price)
        fees = max(minimum_commission, notional * commission_rate) + notional * (transfer_rate + stamp_rate)
        cash += max(0.0, notional - fees)
    return cash


def _benchmark_return(
    benchmark: dict[date, dict] | None,
    panel: dict[str, dict[date, dict]],
    previous_day: date,
    current_day: date,
    eligible: set[str],
) -> float:
    if benchmark and previous_day in benchmark and current_day in benchmark:
        previous = number(benchmark[previous_day].get("close"))
        current = number(benchmark[current_day].get("close"))
        return current / previous - 1 if previous > 0 and current > 0 else 0.0
    returns = []
    for symbol in eligible:
        history = panel.get(symbol) or {}
        if previous_day not in history or current_day not in history:
            continue
        previous = number(history[previous_day].get("close"))
        current = number(history[current_day].get("close"))
        if previous > 0 and current > 0:
            returns.append(current / previous - 1)
    return statistics.fmean(returns) if returns else 0.0


def replay_portfolio_fold(
    panel: dict[str, dict[date, dict]],
    benchmark: dict[date, dict] | None,
    test_dates: list[date],
    all_dates: list[date],
    strategy: dict,
    *,
    universe_snapshots: dict[date, set[str]] | None = None,
    minimum_history: int = 61,
    top_n: int = 3,
    cost_multiplier: float = 1.0,
    execution_price_mode: str = "daily_open_close_proxy",
) -> dict:
    if not test_dates:
        return {"days": [], "fold_return": 0.0, "benchmark_return": 0.0, "maximum_drawdown": 0.0, "coverage_complete": False, "execution_data_coverage_complete": False}
    replay_strategy = _replay_strategy(strategy, cost_multiplier)
    profile = market_profile(strategy_market(replay_strategy))
    first_time = datetime.combine(test_dates[0], time(8, 0), tzinfo=SHANGHAI)
    account = create_portfolio_account(replay_strategy, now=first_time)
    date_index = {day: index for index, day in enumerate(all_dates)}
    snapshots = universe_snapshots or {}
    snapshot_days = tuple(snapshots)
    fallback = set(panel)
    history_dates = {symbol: tuple(history) for symbol, history in panel.items()}
    initial_cash = number(account.get("initial_cash"))
    previous_nav = initial_cash
    benchmark_equity = 1.0
    peak_nav = initial_cash
    maximum_drawdown = 0.0
    coverage_complete = bool(snapshots)
    exact_execution_prices = execution_price_mode == "intraday_0935_1500"
    execution_data_coverage_complete = exact_execution_prices
    day_results = []

    for signal_day in test_dates:
        signal_index = date_index.get(signal_day, -1)
        if signal_index < minimum_history or signal_index <= 0:
            continue
        cutoff_day = all_dates[signal_index - 1]
        eligible, covered = universe_for_day(snapshots, cutoff_day, fallback, ordered_days=snapshot_days)
        coverage_complete = coverage_complete and covered
        signal_rows = []
        for symbol in sorted(eligible):
            history = panel.get(symbol) or {}
            symbol_dates = history_dates.get(symbol) or ()
            available_end = bisect_left(symbol_dates, signal_day)
            if available_end < minimum_history or cutoff_day not in history or signal_day not in history:
                continue
            available_dates = symbol_dates[available_end - minimum_history : available_end]
            features = extract_signal_features(
                [history[day] for day in available_dates],
                minimum_rows=minimum_history,
            )
            if not features:
                continue
            signal_rows.append(
                {
                    "symbol": symbol,
                    "name": str(history[cutoff_day].get("name") or symbol),
                    "price": number(history[cutoff_day].get("close")),
                    "percent": features.get("latest_return", 0.0) * 100,
                    "signal_features": features,
                }
            )
        ranked = rank_signal_rows(signal_rows, strategy=replay_strategy)
        market_regime = evaluate_market_regime(ranked, replay_strategy)
        eligible_signals = filter_absolute_momentum(ranked, replay_strategy, market_regime)
        selected = select_ranked_signals(eligible_signals, top_n, strategy=replay_strategy)
        morning = datetime.combine(signal_day, time(8, 0), tzinfo=SHANGHAI)
        account, morning_events = plan_daily_candidates(
            replay_strategy,
            selected,
            now=morning,
            account=account,
            market_regime=market_regime,
        )

        open_quotes = []
        close_quotes = []
        needed_symbols = set(account.get("positions", {})) | {
            str(order.get("symbol"))
            for order in account.get("orders", [])
            if order.get("status") in {"INTENDED", "ACCEPTED", "PARTIAL"}
        }
        for symbol in sorted(needed_symbols):
            history = panel.get(symbol) or {}
            row = history.get(signal_day)
            if not row:
                execution_data_coverage_complete = False
                continue
            required_execution_fields = [
                "entry_price",
                "exit_price",
                "open_volume",
                "close_volume",
            ]
            if profile.has_daily_price_limits:
                required_execution_fields.extend(["upper_limit", "lower_limit"])
            if any(row.get(field) is None for field in required_execution_fields):
                execution_data_coverage_complete = False
            previous = history.get(cutoff_day)
            open_quotes.append(_quote(symbol, row, phase="open", previous=previous))
            close_quotes.append(_quote(symbol, row, phase="close", previous=previous))

        account, _ = process_market_snapshot(
            replay_strategy,
            open_quotes,
            now=datetime.combine(signal_day, time(9, 35), tzinfo=profile.timezone),
            account=account,
        )
        account, events = process_market_snapshot(
            replay_strategy,
            close_quotes,
            now=datetime.combine(signal_day, profile.session_end, tzinfo=profile.timezone),
            account=account,
        )
        nav = _liquidation_nav(account)
        daily_return = nav / previous_nav - 1 if previous_nav > 0 else 0.0
        previous_nav = nav
        peak_nav = max(peak_nav, nav)
        drawdown = nav / peak_nav - 1 if peak_nav > 0 else 0.0
        maximum_drawdown = min(maximum_drawdown, drawdown)
        benchmark_daily = _benchmark_return(benchmark, panel, cutoff_day, signal_day, eligible)
        benchmark_equity *= 1 + benchmark_daily
        day_results.append(
            {
                "signal_date": signal_day.isoformat(),
                "cutoff_date": cutoff_day.isoformat(),
                "symbols": [item["symbol"] for item in selected],
                "market_regime": market_regime["state"],
                "market_regime_detail": deepcopy(market_regime),
                "target_exposure_pct": market_regime["target_exposure_pct"],
                "nav": round(nav, 4),
                "daily_return": daily_return,
                "benchmark_return": benchmark_daily,
                "excess_return": daily_return - benchmark_daily,
                "drawdown": drawdown,
                "positions": len(account.get("positions", {})),
                "actions": [event.get("type") for event in [*morning_events, *events]],
            }
        )

    final_nav = previous_nav
    return {
        "days": day_results,
        "fold_return": final_nav / initial_cash - 1 if initial_cash > 0 else 0.0,
        "benchmark_return": benchmark_equity - 1,
        "maximum_drawdown": maximum_drawdown,
        "coverage_complete": coverage_complete,
        "execution_data_coverage_complete": execution_data_coverage_complete,
        "closed_trades": len(account.get("closed_trades", [])),
        "final_positions": len(account.get("positions", {})),
    }
