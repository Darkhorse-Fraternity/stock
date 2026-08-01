"""Runtime adapters that connect market data and the PortfolioEngine service."""

from __future__ import annotations

import hashlib
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .markets import strategy_market
from .portfolio_engine import PortfolioEngine, PortfolioSnapshot
from .portfolio_engine.borrow import BorrowSnapshot
from .portfolio_engine.contracts import AccountSnapshot, DecisionBatch, MarketSnapshot
from .portfolio_engine.ledger import JsonLedgerStore
from .us_data_providers import strategy_us_data_source


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _snapshot_id(market: str, occurred_at: datetime, symbols: tuple[str, ...]) -> str:
    material = "|".join((market, occurred_at.isoformat(), *symbols))
    return "market-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


class MarketAdapterQuoteProvider:
    def __init__(self, adapter: object, strategy: Mapping[str, Any]) -> None:
        self._adapter = adapter
        self._strategy = dict(strategy)

    def snapshot(
        self,
        symbols: tuple[str, ...],
        occurred_at: datetime,
    ) -> MarketSnapshot:
        rows, error = self._adapter.fetch_watchlist(
            ({"symbol": symbol} for symbol in symbols),
            strategy=self._strategy,
            data_source_policy=(
                strategy_us_data_source(self._strategy)
                if strategy_market(self._strategy) == "us"
                else None
            ),
        )
        by_symbol = {
            str(item.get("symbol") or ""): item
            for item in rows
            if isinstance(item, Mapping) and item.get("symbol")
        }
        quotes: dict[str, dict[str, float]] = {}
        for symbol in symbols:
            row = by_symbol.get(symbol)
            if row is None:
                continue
            price = _finite_number(row.get("price"))
            if price is None or price <= 0:
                continue
            quote: dict[str, float] = {"price": price}
            aliases = {
                "bar_open": ("bar_open", "open"),
                "bar_high": ("bar_high", "high"),
                "bar_low": ("bar_low", "low"),
                "bar_volume": ("bar_volume", "volume"),
            }
            for target, source_names in aliases.items():
                value = next(
                    (
                        candidate
                        for source in source_names
                        if (candidate := _finite_number(row.get(source))) is not None
                    ),
                    None,
                )
                if value is not None and value > 0:
                    quote[target] = value
            quotes[symbol] = quote
        if symbols and not quotes:
            raise RuntimeError(error or "portfolio quote snapshot is empty")
        market = strategy_market(self._strategy)
        return MarketSnapshot(
            id=_snapshot_id(market, occurred_at, symbols),
            occurred_at=occurred_at,
            quotes=quotes,
        )


class FailClosedBorrowProvider:
    def snapshot(
        self,
        symbols: tuple[str, ...],
        occurred_at: datetime,
    ) -> BorrowSnapshot:
        material = "|".join((occurred_at.isoformat(), *symbols))
        snapshot_id = "borrow-" + hashlib.sha256(
            material.encode("utf-8")
        ).hexdigest()[:24]
        return BorrowSnapshot.unavailable(snapshot_id)


class EmptyEventCalendarProvider:
    def sessions_until_events(
        self,
        symbols: tuple[str, ...],
        occurred_at: datetime,
    ) -> Mapping[str, int | None]:
        return {symbol: None for symbol in symbols}


def open_portfolio_runtime(
    strategy: Mapping[str, Any],
    *,
    path: str | Path,
    adapter: object,
    occurred_at: datetime,
) -> tuple[PortfolioEngine, AccountSnapshot]:
    """Open the strict ledger and bootstrap exactly one strategy account."""

    strategy_id = str(strategy.get("id") or "")
    if not strategy_id:
        raise ValueError("strategy.id is required for portfolio runtime")
    ledger = JsonLedgerStore(path)
    try:
        account = ledger.load(strategy_id)
    except KeyError:
        portfolio = strategy.get("portfolio")
        initial_cash = (
            _finite_number(portfolio.get("initial_cash"))
            if isinstance(portfolio, Mapping)
            else None
        )
        if initial_cash is None or initial_cash <= 0:
            raise ValueError("strategy portfolio.initial_cash must be positive")
        revision = strategy.get("revision")
        if type(revision) is not int or revision < 1:
            raise ValueError("strategy.revision must be positive")
        account = ledger.create_account(
            AccountSnapshot(
                id=f"account-{strategy_id}",
                strategy_id=strategy_id,
                strategy_revision=revision,
                occurred_at=occurred_at,
                available_cash=initial_cash,
                snapshot_id=f"bootstrap-{strategy_id}-r{revision}",
            )
        )
    engine = PortfolioEngine(
        quote_provider=MarketAdapterQuoteProvider(adapter, strategy),
        borrow_provider=FailClosedBorrowProvider(),
        calendar_provider=EmptyEventCalendarProvider(),
        ledger_store=ledger,
    )
    return engine, account


def process_portfolio_runtime(
    strategy: Mapping[str, Any],
    *,
    engine: PortfolioEngine,
    account: AccountSnapshot,
    occurred_at: datetime,
) -> tuple[DecisionBatch, PortfolioSnapshot]:
    request = engine.prepare_process_request(
        run_key=f"process:{strategy['id']}:{occurred_at.isoformat()}",
        strategy=strategy,
        account=account,
        occurred_at=occurred_at,
    )
    batch = engine.process_and_commit(request)
    snapshot = engine.performance(str(strategy["id"]), request.market)
    return batch, snapshot


def format_portfolio_snapshot(
    strategy: Mapping[str, Any],
    snapshot: PortfolioSnapshot,
    *,
    performance_url: str = "",
) -> str:
    exposure_policy = strategy.get("exposure_policy")
    max_positions = (
        exposure_policy.get("max_positions", 10)
        if isinstance(exposure_policy, Mapping)
        else 10
    )
    lines = [
        "📊 **策略持仓每小时报告**",
        f"策略：{strategy.get('name') or strategy.get('id')} · v{strategy.get('revision')}",
        (
            f"净值：{snapshot.metrics.equity:,.2f} · "
            f"可用现金：{snapshot.account.available_cash:,.2f} · "
            f"持仓：{len(snapshot.positions)}/{max_positions}"
        ),
    ]
    for held in snapshot.positions:
        lines.append(
            f"- {held.symbol} {held.side.value}：{held.quantity} 股 · "
            f"现价 {held.current_price:.2f}"
        )
    if not snapshot.positions:
        lines.append("- 当前空仓")
    if performance_url:
        lines.extend(("", f"策略表现：{performance_url}"))
    return "\n".join(lines)


def format_portfolio_actions(
    strategy: Mapping[str, Any],
    batch: DecisionBatch,
    *,
    performance_url: str = "",
) -> str:
    actions = [
        *(f"订单意图：{item.symbol} {item.order_side.value} {item.quantity}" for item in batch.intents),
        *(f"模拟成交：{item.symbol} {item.quantity} @ {item.price:.2f}" for item in batch.fills),
        *(f"事件：{item.type}" for item in batch.events),
    ]
    if not actions:
        return ""
    lines = [
        "⚠️ **策略组合动作通知**",
        f"策略：{strategy.get('name') or strategy.get('id')} · v{strategy.get('revision')}",
        *actions,
    ]
    if performance_url:
        lines.extend(("", f"策略表现：{performance_url}"))
    return "\n".join(lines)


__all__ = (
    "EmptyEventCalendarProvider",
    "FailClosedBorrowProvider",
    "MarketAdapterQuoteProvider",
    "format_portfolio_actions",
    "format_portfolio_snapshot",
    "open_portfolio_runtime",
    "process_portfolio_runtime",
)
