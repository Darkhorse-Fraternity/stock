from __future__ import annotations

import json
import os
import statistics
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from .market_adapters import get_market_adapter
from .utils import beijing_now, number


HISTORY_VERSION = 1
HISTORY_RETENTION_DAYS = 120


def recommendation_history_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    configured = os.getenv("STOCK_AGENT_HISTORY_PATH", "data/recommendation_history.json").strip()
    return Path(configured or "data/recommendation_history.json").expanduser()


def _date_value(value: object) -> date | None:
    try:
        return datetime.strptime(str(value or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _read_history(path: str | Path | None = None) -> dict:
    target = recommendation_history_path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": HISTORY_VERSION, "records": []}
    records = payload.get("records") if isinstance(payload, dict) else []
    return {
        "version": HISTORY_VERSION,
        "records": [item for item in records if isinstance(item, dict)] if isinstance(records, list) else [],
    }


def _write_history(payload: dict, path: str | Path | None = None) -> None:
    target = recommendation_history_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def upsert_recommendation_history(
    record: dict,
    *,
    path: str | Path | None = None,
    now: datetime | None = None,
) -> dict:
    current = beijing_now(now)
    trade_date = str(record.get("trade_date") or "")[:10]
    if _date_value(trade_date) is None:
        raise ValueError("推荐记录缺少有效交易日期")
    strategy_id = record.get("strategy_id")
    payload = _read_history(path)
    records = [
        item
        for item in payload["records"]
        if not (str(item.get("trade_date") or "")[:10] == trade_date and item.get("strategy_id") == strategy_id)
    ]
    saved = deepcopy(record)
    saved["archived_at"] = current.strftime("%Y-%m-%d %H:%M:%S %Z")
    records.append(saved)
    cutoff = current.date() - timedelta(days=HISTORY_RETENTION_DAYS - 1)
    records = [item for item in records if (_date_value(item.get("trade_date")) or date.min) >= cutoff]
    records.sort(key=lambda item: (str(item.get("trade_date") or ""), str(item.get("generated_at") or "")), reverse=True)
    payload = {"version": HISTORY_VERSION, "records": records}
    _write_history(payload, path)
    return deepcopy(payload)


def load_recommendation_history(
    *,
    days: int = 30,
    path: str | Path | None = None,
    now: datetime | None = None,
) -> list[dict]:
    current = beijing_now(now)
    window = min(90, max(1, int(days)))
    cutoff = current.date() - timedelta(days=window - 1)
    records = [
        item
        for item in _read_history(path)["records"]
        if (_date_value(item.get("trade_date")) or date.min) >= cutoff
        and (_date_value(item.get("trade_date")) or date.max) <= current.date()
    ]
    return deepcopy(records)


def _return_pct(entry_price: float, current_price: float) -> float | None:
    if entry_price <= 0 or current_price <= 0:
        return None
    return (current_price / entry_price - 1) * 100


def _rounded(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def build_recommendation_performance(
    *,
    days: int = 30,
    path: str | Path | None = None,
    now: datetime | None = None,
    quote_fetcher: Callable | None = None,
) -> dict:
    current = beijing_now(now)
    records = load_recommendation_history(days=days, path=path, now=current)
    entries_by_market: dict[str, dict[str, dict]] = {}
    for record in records:
        market = str(record.get("market") or "cn")
        adapter = get_market_adapter(market)
        for item in record.get("recommendations") or []:
            try:
                symbol = adapter.normalize_symbol(item.get("symbol"))
            except ValueError:
                continue
            entries_by_market.setdefault(adapter.market, {}).setdefault(
                symbol,
                {"symbol": symbol, "name": item.get("name") or symbol},
            )

    rows = []
    quote_error = None
    errors = []
    if quote_fetcher is not None:
        entries = [entry for group in entries_by_market.values() for entry in group.values()]
        try:
            rows, quote_error = quote_fetcher(entries)
            rows = [
                row
                for market, group in entries_by_market.items()
                for row in get_market_adapter(market).constrain_watchlist(rows, group.values())
            ]
        except Exception as exc:
            quote_error = str(exc)
    else:
        for market, entries in entries_by_market.items():
            adapter = get_market_adapter(market)
            try:
                fetched, error = adapter.fetch_watchlist(entries.values())
                rows.extend(adapter.constrain_watchlist(fetched, entries.values()))
                if error:
                    errors.append(f"{adapter.profile.label}: {error}")
            except Exception as exc:
                errors.append(f"{adapter.profile.label}: {exc}")
        quote_error = "；".join(errors) or None
    quotes = {str(row.get("symbol")): row for row in rows}

    events = []
    for record in records:
        trade_day = _date_value(record.get("trade_date"))
        for rank, item in enumerate(record.get("recommendations") or [], 1):
            symbol = str(item.get("symbol") or "")
            quote = quotes.get(symbol) or {}
            entry_price = number(item.get("entry_price"))
            live_price = number(quote.get("price"))
            stored_price = number(item.get("last_price"), default=entry_price)
            latest_price = live_price or stored_price or entry_price
            return_pct = _return_pct(entry_price, latest_price)
            events.append(
                {
                    "trade_date": record.get("trade_date"),
                    "generated_at": record.get("generated_at"),
                    "rank": rank,
                    "symbol": symbol,
                    "name": quote.get("name") or item.get("name") or symbol,
                    "entry_price": _rounded(entry_price),
                    "latest_price": _rounded(latest_price),
                    "return_pct": _rounded(return_pct),
                    "initial_change_pct": _rounded(number(item.get("initial_change_pct"))),
                    "latest_change_pct": _rounded(number(quote.get("percent"), default=number(item.get("last_change_pct")))),
                    "maximum_sampled_drawdown_pct": _rounded(
                        number(item.get("maximum_sampled_drawdown_pct"))
                        if item.get("maximum_sampled_drawdown_pct") is not None
                        else None
                    ),
                    "intraday_excess_pct": _rounded(
                        number(item.get("excess_return_pct")) if item.get("excess_return_pct") is not None else None
                    ),
                    "volume_hands": _rounded(number(quote.get("volume"), default=number(item.get("last_volume"))), 0),
                    "turnover_cny": _rounded(number(quote.get("turnover"), default=number(item.get("last_turnover"))), 0),
                    "days_held": (current.date() - trade_day).days if trade_day else None,
                    "strategy_id": record.get("strategy_id"),
                    "strategy_name": record.get("strategy_name") or "股票推荐策略",
                    "strategy_revision": record.get("strategy_revision", 1),
                    "strategy_stage": record.get("strategy_stage", "draft"),
                    "market": record.get("market") or "cn",
                    "last_tracking_at": item.get("last_updated_at") or record.get("last_tracking_at"),
                    "quote_status": "live" if live_price > 0 else "stored",
                }
            )
    events.sort(key=lambda item: (str(item.get("trade_date") or ""), -int(item.get("rank") or 0)), reverse=True)

    strategies_by_key: dict[tuple[str, int], dict] = {}
    for event in events:
        strategy_id = str(event.get("strategy_id") or "")
        revision = int(event.get("strategy_revision") or 1)
        key = (strategy_id or str(event.get("strategy_name") or "股票推荐策略"), revision)
        strategy = strategies_by_key.setdefault(
            key,
            {
                "id": strategy_id or None,
                "name": event.get("strategy_name") or "股票推荐策略",
                "revision": revision,
                "stage": event.get("strategy_stage") or "draft",
                "recommendations": 0,
                "first_recommended_date": event.get("trade_date"),
                "last_recommended_date": event.get("trade_date"),
            },
        )
        strategy["recommendations"] += 1
        strategy["first_recommended_date"] = min(
            str(strategy.get("first_recommended_date") or event.get("trade_date") or ""),
            str(event.get("trade_date") or ""),
        )
        strategy["last_recommended_date"] = max(
            str(strategy.get("last_recommended_date") or event.get("trade_date") or ""),
            str(event.get("trade_date") or ""),
        )
    strategies = sorted(
        strategies_by_key.values(),
        key=lambda item: (str(item.get("last_recommended_date") or ""), str(item.get("name") or "")),
        reverse=True,
    )

    stocks_by_symbol: dict[str, dict] = {}
    for event in sorted(events, key=lambda item: (str(item.get("trade_date") or ""), str(item.get("generated_at") or ""), int(item.get("rank") or 0))):
        symbol = event["symbol"]
        existing = stocks_by_symbol.get(symbol)
        if existing is None:
            existing = {
                "symbol": symbol,
                "name": event["name"],
                "first_recommended_date": event["trade_date"],
                "first_recommended_at": event["generated_at"],
                "first_recommendation_price": event["entry_price"],
                "latest_recommended_date": event["trade_date"],
                "current_price": event["latest_price"],
                "return_pct": None,
                "recommendation_count": 0,
                "maximum_sampled_drawdown_pct": event["maximum_sampled_drawdown_pct"],
                "volume_hands": event["volume_hands"],
                "turnover_cny": event["turnover_cny"],
                "latest_change_pct": event["latest_change_pct"],
                "strategy_name": event["strategy_name"],
                "strategy_stage": event["strategy_stage"],
                "quote_status": event["quote_status"],
            }
            stocks_by_symbol[symbol] = existing
        existing["recommendation_count"] += 1
        existing["name"] = event["name"]
        existing["latest_recommended_date"] = event["trade_date"]
        existing["current_price"] = event["latest_price"]
        existing["volume_hands"] = event["volume_hands"]
        existing["turnover_cny"] = event["turnover_cny"]
        existing["latest_change_pct"] = event["latest_change_pct"]
        existing["quote_status"] = event["quote_status"]
        drawdown = event["maximum_sampled_drawdown_pct"]
        if drawdown is not None:
            previous = existing["maximum_sampled_drawdown_pct"]
            existing["maximum_sampled_drawdown_pct"] = min(previous, drawdown) if previous is not None else drawdown

    stocks = list(stocks_by_symbol.values())
    for item in stocks:
        item["return_pct"] = _rounded(_return_pct(number(item["first_recommendation_price"]), number(item["current_price"])))
        item["successful"] = item["return_pct"] is not None and item["return_pct"] > 0
        first_day = _date_value(item["first_recommended_date"])
        item["days_since_first_recommendation"] = (current.date() - first_day).days if first_day else None
    stocks.sort(key=lambda item: (str(item.get("first_recommended_date") or ""), item.get("return_pct") or 0), reverse=True)

    stock_returns = [item["return_pct"] for item in stocks if item["return_pct"] is not None]
    successful_stocks = [item for item in stocks if item["successful"]]
    best = max((item for item in stocks if item["return_pct"] is not None), key=lambda item: item["return_pct"], default=None)
    worst = min((item for item in stocks if item["return_pct"] is not None), key=lambda item: item["return_pct"], default=None)
    daily = []
    for trade_date in sorted({str(item.get("trade_date")) for item in events if item.get("trade_date")}):
        values = [item["return_pct"] for item in events if item.get("trade_date") == trade_date and item["return_pct"] is not None]
        daily.append(
            {
                "trade_date": trade_date,
                "recommendations": sum(1 for item in events if item.get("trade_date") == trade_date),
                "average_return_pct": _rounded(statistics.fmean(values)) if values else None,
                "win_rate": _rounded(sum(1 for value in values if value > 0) / len(values) * 100) if values else None,
            }
        )

    window = min(90, max(1, int(days)))
    return {
        "window_days": window,
        "generated_at": current.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "data_start": min((item.get("trade_date") for item in events), default=None),
        "data_end": max((item.get("trade_date") for item in events), default=None),
        "quote_error": quote_error,
        "strategies": strategies,
        "summary": {
            "strategy_count": len(strategies),
            "recommendation_days": len({item.get("trade_date") for item in events}),
            "total_recommendations": len(events),
            "unique_stocks": len(stocks),
            "priced_recommendations": len([item for item in events if item["return_pct"] is not None]),
            "priced_stocks": len(stock_returns),
            "successful_stocks": len(successful_stocks),
            "average_return_pct": _rounded(statistics.fmean(stock_returns)) if stock_returns else None,
            "median_return_pct": _rounded(statistics.median(stock_returns)) if stock_returns else None,
            "success_rate": _rounded(len(successful_stocks) / len(stock_returns) * 100) if stock_returns else None,
            "win_rate": _rounded(len(successful_stocks) / len(stock_returns) * 100) if stock_returns else None,
            "best": deepcopy(best),
            "worst": deepcopy(worst),
        },
        "daily": daily,
        "stocks": stocks,
        "events": events,
    }
