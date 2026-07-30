from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from copy import deepcopy
from datetime import datetime
from typing import Callable

from . import enrichment
from .data_sources import fetch_board_quotes, fetch_nasdaq100_quotes, fetch_watchlist_quotes
from .markets import (
    CN_MARKET,
    US_MARKET,
    MarketProfile,
    market_profile,
    normalize_market,
    strategy_universe,
)
from .universe import constrain_to_watchlist, normalize_stock_symbol, normalize_watchlist
from .universe_provider import BoardUniverseProvider, Nasdaq100UniverseProvider, UniverseQuoteBatch
from .us_data_providers import get_us_market_data_provider


class MarketAdapter(ABC):
    """Market-specific I/O and execution conventions.

    Selection, recommendation, portfolio and reporting code consume this
    contract instead of importing a market data vendor directly.
    """

    market: str

    @property
    def profile(self) -> MarketProfile:
        return market_profile(self.market)

    def normalize_symbol(self, value: object) -> str:
        return normalize_stock_symbol(value, self.market)

    def normalize_watchlist(self, entries: str | Iterable[object] | None) -> list[dict]:
        return normalize_watchlist(entries, market=self.market)

    def constrain_watchlist(
        self,
        rows: Iterable[dict],
        entries: str | Iterable[object],
    ) -> list[dict]:
        return constrain_to_watchlist(rows, entries, market=self.market)

    def resolve_universe(
        self,
        strategy: dict | None,
        *,
        code: object | None = None,
        name: object | None = None,
    ) -> tuple[str, str]:
        return strategy_universe(
            strategy,
            board_code=code,
            board_name=name,
            market=self.market,
        )

    def fetch_watchlist(
        self,
        entries: Iterable[object],
        *,
        fetcher: Callable | None = None,
    ) -> tuple[list[dict], str | None]:
        if fetcher is not None:
            return fetcher(entries)
        return fetch_watchlist_quotes(entries, market=self.market)

    def fetch_history(self, symbol: str, **kwargs) -> list[dict]:
        return enrichment.fetch_daily_history(symbol, market=self.market, **kwargs)

    def execution_config(self, config: dict) -> dict:
        return deepcopy(config)

    @abstractmethod
    def benchmark_fetcher(self) -> Callable:
        raise NotImplementedError

    def fetch_secondary_history(
        self,
        symbol: str,
        *,
        now: datetime,
    ) -> list[dict] | None:
        return None

    def secondary_history_fetcher(self) -> Callable | None:
        return None

    @abstractmethod
    def universe_provider(self) -> BoardUniverseProvider | Nasdaq100UniverseProvider:
        raise NotImplementedError

    def fetch_universe(
        self,
        strategy: dict | None,
        *,
        code: object | None = None,
        name: object | None = None,
        now: datetime | None = None,
        provider: BoardUniverseProvider | Nasdaq100UniverseProvider | None = None,
    ) -> UniverseQuoteBatch:
        universe_code, universe_name = self.resolve_universe(
            strategy,
            code=code,
            name=name,
        )
        return (provider or self.universe_provider()).fetch(
            universe_code,
            board_name=universe_name,
            now=now,
        )


class AShareMarketAdapter(MarketAdapter):
    market = CN_MARKET

    def universe_provider(self) -> BoardUniverseProvider:
        return BoardUniverseProvider()

    def benchmark_fetcher(self) -> Callable:
        return fetch_board_quotes

    def fetch_secondary_history(
        self,
        symbol: str,
        *,
        now: datetime,
    ) -> list[dict]:
        return enrichment.download_cn_sina_daily_history(symbol, now=now)

    def secondary_history_fetcher(self) -> Callable:
        return self.fetch_secondary_history


class UsStockMarketAdapter(MarketAdapter):
    market = US_MARKET

    def universe_provider(self) -> Nasdaq100UniverseProvider:
        return Nasdaq100UniverseProvider(
            quote_fetcher=get_us_market_data_provider().fetch_quotes,
        )

    def benchmark_fetcher(self) -> Callable:
        return fetch_nasdaq100_quotes

    def fetch_watchlist(
        self,
        entries: Iterable[object],
        *,
        fetcher: Callable | None = None,
    ) -> tuple[list[dict], str | None]:
        if fetcher is not None:
            return fetcher(entries)
        return get_us_market_data_provider().fetch_quotes(
            symbols=entries,
            board_name="未分类",
        )

    def fetch_history(self, symbol: str, **kwargs) -> list[dict]:
        provider = get_us_market_data_provider()
        return enrichment.fetch_daily_history(
            symbol,
            market=self.market,
            downloader=provider.fetch_daily_history,
            **kwargs,
        )

    def execution_config(self, config: dict) -> dict:
        effective = deepcopy(config)
        # Commission-free whole-share simulation. Slippage and participation
        # limits remain active; regulatory per-share fees can be added behind
        # this adapter without changing the portfolio engine.
        effective.update(
            {
                "commission_rate_pct": 0.0,
                "minimum_commission_cny": 0.0,
                "stamp_duty_rate_pct": 0.0,
                "transfer_fee_rate_pct": 0.0,
                "benchmark_symbol": "NDX",
                "benchmark_name": "纳斯达克100",
            }
        )
        return effective


_ADAPTERS: dict[str, MarketAdapter] = {
    CN_MARKET: AShareMarketAdapter(),
    US_MARKET: UsStockMarketAdapter(),
}


def get_market_adapter(market: object = CN_MARKET) -> MarketAdapter:
    return _ADAPTERS[normalize_market(market)]
