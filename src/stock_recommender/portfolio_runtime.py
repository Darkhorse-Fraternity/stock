"""Runtime adapters that connect market data and the PortfolioEngine service."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .markets import market_profile, strategy_market
from .portfolio_engine import PortfolioEngine
from .portfolio_engine.borrow import BorrowSnapshot
from .portfolio_engine.contracts import (
    AccountSnapshot,
    DecisionBatch,
    MarketSnapshot,
    PerformanceProjectionRequest,
    PortfolioPerformanceLedgerView,
    PerformanceStrategySource,
    RevisionTransition,
    StrategyPerformanceProjection,
)
from .portfolio_engine.ledger import (
    InMemoryLedgerStore,
    portfolio_ledger_path,
)
from .portfolio_store import open_portfolio_store
from .us_data_providers import strategy_us_data_source


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _utc_datetime(value: object, field_name: str = "occurred_at") -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _strategy_revision_time(strategy: Mapping[str, Any]) -> datetime:
    raw = strategy.get("updated_at")
    if type(raw) is not str or not raw:
        raise ValueError(
            "strategy.updated_at must be an explicit timezone-aware revision timestamp"
        )
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            "strategy.updated_at must be an ISO timezone-aware revision timestamp"
        ) from exc
    return _utc_datetime(parsed, "strategy.updated_at")


def _snapshot_id(
    market: str,
    occurred_at: datetime,
    quotes: Mapping[str, Mapping[str, float]],
) -> str:
    material = json.dumps(
        {
            "market": market,
            "occurred_at": occurred_at.isoformat(),
            "quotes": quotes,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
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
        snapshot, _ = self.snapshot_with_warning(symbols, occurred_at)
        return snapshot

    def snapshot_with_warning(
        self,
        symbols: tuple[str, ...],
        occurred_at: datetime,
    ) -> tuple[MarketSnapshot, str | None]:
        captured_at = _utc_datetime(occurred_at)
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
                "percent": ("percent", "change_pct"),
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
                if value is not None and (target == "percent" or value > 0):
                    quote[target] = value
            quotes[symbol] = quote
        if symbols and not quotes:
            raise RuntimeError(error or "portfolio quote snapshot is empty")
        market = strategy_market(self._strategy)
        snapshot = MarketSnapshot(
            id=_snapshot_id(market, captured_at, quotes),
            occurred_at=captured_at,
            quotes=quotes,
        )
        warning = None if error is None else str(error)
        return snapshot, warning


class FailClosedBorrowProvider:
    def snapshot(
        self,
        symbols: tuple[str, ...],
        occurred_at: datetime,
    ) -> BorrowSnapshot:
        captured_at = _utc_datetime(occurred_at)
        material = "|".join((captured_at.isoformat(), *symbols))
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
        _utc_datetime(occurred_at)
        return {symbol: None for symbol in symbols}


def open_portfolio_runtime(
    strategy: Mapping[str, Any],
    *,
    path: str | Path,
    adapter: object,
    occurred_at: datetime,
) -> tuple[PortfolioEngine, AccountSnapshot]:
    """Open the strict ledger and bootstrap exactly one strategy account."""

    captured_at = _utc_datetime(occurred_at)
    strategy_id = str(strategy.get("id") or "")
    if not strategy_id:
        raise ValueError("strategy.id is required for portfolio runtime")
    revision = strategy.get("revision")
    if type(revision) is not int or revision < 1:
        raise ValueError("strategy.revision must be positive")
    ledger = open_portfolio_store(path)
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
        account = ledger.create_account(
            AccountSnapshot(
                id=f"account-{strategy_id}",
                strategy_id=strategy_id,
                strategy_revision=revision,
                occurred_at=captured_at,
                available_cash=initial_cash,
                snapshot_id=f"bootstrap-{strategy_id}-r{revision}",
            )
        )
    if account.strategy_revision > revision:
        raise ValueError(
            "ledger account uses a newer strategy revision; downgrade is forbidden"
        )
    if account.strategy_revision < revision:
        source_revision = account.strategy_revision
        revision_time = _strategy_revision_time(strategy)
        transition_material = (
            f"{strategy_id}|{account.snapshot_id}|{source_revision}|{revision}"
        )
        account = ledger.transition_revision(
            RevisionTransition(
                id="revision-transition-"
                + hashlib.sha256(transition_material.encode("utf-8")).hexdigest()[:24],
                strategy_id=strategy_id,
                expected_snapshot_id=account.snapshot_id,
                from_revision=source_revision,
                to_revision=revision,
                occurred_at=revision_time,
            )
        )
    engine = PortfolioEngine(
        quote_provider=MarketAdapterQuoteProvider(adapter, strategy),
        borrow_provider=FailClosedBorrowProvider(),
        calendar_provider=EmptyEventCalendarProvider(),
        ledger_store=ledger,
    )
    return engine, account


def _symbol_names(strategy: Mapping[str, Any]) -> dict[str, str]:
    parameters = strategy.get("parameters")
    watchlist = (
        parameters.get("watchlist", {}).get("value", ())
        if isinstance(parameters, Mapping)
        and isinstance(parameters.get("watchlist"), Mapping)
        else ()
    )
    result: dict[str, str] = {}
    if isinstance(watchlist, (tuple, list)):
        for item in watchlist:
            if isinstance(item, Mapping):
                symbol = str(item.get("symbol") or "")
                if symbol:
                    result[symbol] = str(item.get("name") or symbol)
            elif isinstance(item, str) and item:
                result[item] = item
    return result


def _performance_source(strategy: Mapping[str, Any]) -> PerformanceStrategySource:
    strategy_id = str(strategy.get("id") or "")
    revision = strategy.get("revision")
    portfolio = strategy.get("portfolio")
    allocation = strategy.get("allocation")
    exposure = strategy.get("exposure_policy")
    margin = strategy.get("margin_policy")
    short = strategy.get("short_policy")
    lifecycle = strategy.get("lifecycle")
    signal = strategy.get("signal")
    if not strategy_id:
        raise ValueError("strategy.id is required for portfolio performance")
    if type(revision) is not int or revision < 1:
        raise ValueError("strategy.revision must be positive")
    if not isinstance(portfolio, Mapping):
        raise ValueError("strategy.portfolio must be an object")
    initial_cash = _finite_number(portfolio.get("initial_cash"))
    if initial_cash is None or initial_cash <= 0:
        raise ValueError("strategy portfolio.initial_cash must be positive")
    max_positions_value = (
        exposure.get("max_positions")
        if isinstance(exposure, Mapping) and exposure.get("max_positions") is not None
        else portfolio.get("max_positions", 10)
    )
    if type(max_positions_value) is not int or max_positions_value <= 0:
        raise ValueError("strategy max_positions must be a positive integer")
    market = strategy_market(strategy)
    profile = market_profile(market)
    market_regime = strategy.get("market_regime")
    return PerformanceStrategySource(
        id=strategy_id,
        name=str(strategy.get("name") or strategy_id),
        revision=revision,
        stage=str(lifecycle.get("stage", "draft")) if isinstance(lifecycle, Mapping) else "draft",
        market=market,
        market_label=profile.label,
        currency=profile.currency,
        currency_symbol=profile.currency_symbol,
        initial_cash=initial_cash,
        max_positions=max_positions_value,
        signal_model=(
            str(signal.get("model"))
            if isinstance(signal, Mapping) and signal.get("model")
            else None
        ),
        signal_time=(
            str(signal.get("run_time"))
            if isinstance(signal, Mapping) and signal.get("run_time")
            else None
        ),
        signal_data_cutoff=(
            str(signal.get("data_cutoff"))
            if isinstance(signal, Mapping) and signal.get("data_cutoff")
            else None
        ),
        allocation_model=(
            str(allocation.get("model"))
            if isinstance(allocation, Mapping) and allocation.get("model")
            else None
        ),
        benchmark_symbol=(
            str(portfolio.get("benchmark_symbol"))
            if portfolio.get("benchmark_symbol")
            else None
        ),
        benchmark_name=(
            str(portfolio.get("benchmark_name"))
            if portfolio.get("benchmark_name")
            else None
        ),
        market_regime=market_regime if isinstance(market_regime, Mapping) else None,
        risk_level=(
            str(strategy.get("risk_level")) if strategy.get("risk_level") else None
        ),
        trading_mode=(
            str(exposure.get("mode"))
            if isinstance(exposure, Mapping) and exposure.get("mode")
            else None
        ),
        target_exposure_pct=(
            _finite_number(market_regime.get("target_exposure_pct"))
            if isinstance(market_regime, Mapping)
            else None
        ),
        exposure_policy=exposure if isinstance(exposure, Mapping) else {},
        margin_policy=margin if isinstance(margin, Mapping) else {},
        short_policy=short if isinstance(short, Mapping) else {},
        config=portfolio,
        allocation=allocation if isinstance(allocation, Mapping) else {},
        symbol_names=_symbol_names(strategy),
    )


def project_strategy_performance(
    strategy: Mapping[str, Any],
    *,
    path: str | Path | None = None,
    adapter: object,
    occurred_at: datetime,
) -> StrategyPerformanceProjection:
    """Project a read-only typed performance view with persisted quote fallback."""

    captured_at = _utc_datetime(occurred_at)
    source = _performance_source(strategy)
    strategy_id = source.id
    persistent_store = open_portfolio_store(path)
    try:
        ledger_view = persistent_store.load_performance_view(strategy_id)
        store = persistent_store
    except KeyError:
        revision = strategy.get("revision")
        if type(revision) is not int or revision < 1:
            raise ValueError("strategy.revision must be positive")
        store = InMemoryLedgerStore(portfolio_ledger_path(path))
        account = store.create_account(
            AccountSnapshot(
                id=f"account-{strategy_id}",
                strategy_id=strategy_id,
                strategy_revision=revision,
                occurred_at=captured_at,
                available_cash=source.initial_cash,
                snapshot_id=f"projection-{strategy_id}-r{revision}",
            )
        )
        ledger_view = PortfolioPerformanceLedgerView(account=account)
    account = ledger_view.account
    symbols = tuple(held.symbol for held in account.positions)
    quote_error = None
    valuation_source = "live_quote"
    if symbols:
        try:
            live_market, adapter_warning = MarketAdapterQuoteProvider(
                adapter,
                strategy,
            ).snapshot_with_warning(
                symbols,
                captured_at,
            )
            quotes = {symbol: dict(value) for symbol, value in live_market.quotes.items()}
            missing = [symbol for symbol in symbols if symbol not in quotes]
            warnings = [adapter_warning] if adapter_warning else []
            if missing:
                warnings.append("missing live quotes: " + ", ".join(missing))
                valuation_source = "mixed_live_and_persisted_quotes"
            elif adapter_warning:
                valuation_source = "degraded_live_quote"
            quote_error = "; ".join(warnings) or None
            market_id = live_market.id
        except Exception as exc:
            quotes = {}
            missing = list(symbols)
            quote_error = str(exc)
            valuation_source = "persisted_quote_fallback"
            market_id = ""
        positions_by_symbol = {item.symbol: item for item in account.positions}
        for symbol in missing:
            held = positions_by_symbol[symbol]
            quotes[symbol] = {"price": held.current_price or held.average_cost}
        market_name = strategy_market(strategy)
        market = MarketSnapshot(
            id=market_id or _snapshot_id(market_name, captured_at, quotes),
            occurred_at=captured_at,
            quotes=quotes,
        )
    else:
        market_name = strategy_market(strategy)
        market = MarketSnapshot(
            id=_snapshot_id(market_name, captured_at, {}),
            occurred_at=captured_at,
            quotes={},
        )
    return PortfolioEngine(ledger_store=store).performance_projection(
        PerformanceProjectionRequest(
            strategy=source,
            market=market,
            generated_at=captured_at,
            valuation_source=valuation_source,
            quote_error=quote_error,
        ),
        ledger_view=ledger_view,
    )


def process_portfolio_runtime(
    strategy: Mapping[str, Any],
    *,
    engine: PortfolioEngine,
    account: AccountSnapshot,
    occurred_at: datetime,
) -> tuple[DecisionBatch, PortfolioSnapshot]:
    captured_at = _utc_datetime(occurred_at)
    request = engine.prepare_process_request(
        run_key=f"process:{strategy['id']}:{captured_at.isoformat()}",
        strategy=strategy,
        account=account,
        occurred_at=captured_at,
    )
    batch = engine.process_and_commit(request)
    snapshot = engine.performance(str(strategy["id"]), request.market)
    return batch, snapshot


__all__ = (
    "EmptyEventCalendarProvider",
    "FailClosedBorrowProvider",
    "MarketAdapterQuoteProvider",
    "open_portfolio_runtime",
    "project_strategy_performance",
    "process_portfolio_runtime",
)
