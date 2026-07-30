from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Callable, Iterable

from .markets import US_MARKET, is_market_open
from .universe import normalize_stock_symbol, normalize_watchlist
from .utils import number


DEFAULT_ALPACA_DATA_URL = "https://data.alpaca.markets"
_PROVIDER_LABELS = {"alpaca": "Alpaca", "sina": "新浪"}
US_DATA_SOURCE_AUTO = "auto"
US_DATA_SOURCE_ALPACA = "alpaca"
US_DATA_SOURCE_SINA = "sina"
US_DATA_SOURCE_POLICIES = {
    US_DATA_SOURCE_AUTO,
    US_DATA_SOURCE_ALPACA,
    US_DATA_SOURCE_SINA,
}


class UsMarketDataProvider(ABC):
    """Vendor contract used by the US market adapter."""

    provider_id: str

    @abstractmethod
    def fetch_quotes(
        self,
        *,
        symbols: Iterable[object],
        board_name: str = "纳斯达克100",
        source_label: str | None = None,
        timeout: int | float | None = None,
        urlopen_func: Callable | None = None,
    ) -> tuple[list[dict], str | None]:
        raise NotImplementedError

    @abstractmethod
    def fetch_daily_history(self, symbol: str) -> list[dict]:
        raise NotImplementedError


def _credential(primary: str, alias: str) -> str:
    return str(os.getenv(primary) or os.getenv(alias) or "").strip()


def _alpaca_credentials() -> tuple[str, str]:
    return (
        _credential("STOCK_AGENT_ALPACA_API_KEY_ID", "APCA_API_KEY_ID"),
        _credential("STOCK_AGENT_ALPACA_API_SECRET_KEY", "APCA_API_SECRET_KEY"),
    )


def _iso_timestamp(value: object) -> str:
    return str(value or "").strip()


def _timestamp(value: object) -> datetime | None:
    text = _iso_timestamp(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _provider_label(provider: object, default: str) -> str:
    provider_id = str(getattr(provider, "provider_id", default)).strip() or default
    return _PROVIDER_LABELS.get(provider_id, provider_id)


class AlpacaMarketDataClient:
    """Small dependency-free Alpaca Market Data API client."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str | None = None,
        feed: str | None = None,
        timeout: int | float | None = None,
        urlopen_func: Callable | None = None,
        now_func: Callable[[], datetime] | None = None,
    ):
        configured_key, configured_secret = _alpaca_credentials()
        self.api_key = str(api_key if api_key is not None else configured_key).strip()
        self.api_secret = str(api_secret if api_secret is not None else configured_secret).strip()
        self.base_url = (
            str(
                base_url
                if base_url is not None
                else os.getenv("STOCK_AGENT_ALPACA_DATA_URL", DEFAULT_ALPACA_DATA_URL)
            )
            .strip()
            .rstrip("/")
            or DEFAULT_ALPACA_DATA_URL
        )
        self.feed = str(
            feed if feed is not None else os.getenv("STOCK_AGENT_ALPACA_FEED", "iex")
        ).strip().lower() or "iex"
        self.timeout = float(
            timeout
            if timeout is not None
            else os.getenv("STOCK_AGENT_ALPACA_TIMEOUT_SECONDS", "8")
        )
        self.urlopen_func = urlopen_func
        self.now_func = now_func or (lambda: datetime.now(timezone.utc))

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _request_json(self, path: str, params: dict[str, object]) -> dict:
        if not self.configured:
            raise RuntimeError(
                "Alpaca 凭证未配置，请设置 STOCK_AGENT_ALPACA_API_KEY_ID "
                "和 STOCK_AGENT_ALPACA_API_SECRET_KEY"
            )
        query = urllib.parse.urlencode(
            {
                key: value
                for key, value in params.items()
                if value is not None and str(value) != ""
            },
            safe=",.:+-",
        )
        request = urllib.request.Request(
            f"{self.base_url}{path}?{query}",
            headers={
                "Accept": "application/json",
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.api_secret,
                "User-Agent": "stock-agent/0.1",
            },
        )
        opener = self.urlopen_func or urllib.request.urlopen
        try:
            with opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("message")
            except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                detail = None
            raise RuntimeError(f"Alpaca HTTP {exc.code}: {detail or exc.reason}") from exc
        except (OSError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Alpaca 请求失败：{exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Alpaca 返回了无效 JSON")
        if payload.get("message") and not (
            "snapshots" in payload or "bars" in payload
        ):
            raise RuntimeError(f"Alpaca 返回错误：{payload['message']}")
        return payload

    def fetch_quotes(
        self,
        *,
        symbols: Iterable[object],
        board_name: str = "纳斯达克100",
    ) -> list[dict]:
        entries = normalize_watchlist(symbols, market=US_MARKET)
        metadata = {item["symbol"]: item for item in entries}
        if not metadata:
            return []
        batch_size = max(
            1,
            int(os.getenv("STOCK_AGENT_ALPACA_QUOTE_BATCH_SIZE", "100")),
        )
        rows: list[dict] = []
        stale_symbols: list[str] = []
        tickers = list(metadata)
        for offset in range(0, len(tickers), batch_size):
            batch = tickers[offset : offset + batch_size]
            payload = self._request_json(
                "/v2/stocks/snapshots",
                {"symbols": ",".join(batch), "feed": self.feed},
            )
            snapshots = payload.get("snapshots") or {}
            if not isinstance(snapshots, dict):
                continue
            for raw_symbol, raw_snapshot in snapshots.items():
                try:
                    symbol = normalize_stock_symbol(raw_symbol, US_MARKET)
                except ValueError:
                    continue
                if symbol not in metadata or not isinstance(raw_snapshot, dict):
                    continue
                latest_trade = raw_snapshot.get("latestTrade") or {}
                minute_bar = raw_snapshot.get("minuteBar") or {}
                daily_bar = raw_snapshot.get("dailyBar") or {}
                previous_bar = raw_snapshot.get("prevDailyBar") or {}
                quote_time = (
                    latest_trade.get("t")
                    or minute_bar.get("t")
                    or daily_bar.get("t")
                )
                observed_at = _timestamp(quote_time)
                current = self.now_func()
                if current.tzinfo is None:
                    current = current.replace(tzinfo=timezone.utc)
                current = current.astimezone(timezone.utc)
                maximum_age_seconds = (
                    float(
                        os.getenv(
                            "STOCK_AGENT_ALPACA_MAX_QUOTE_AGE_SECONDS",
                            "900",
                        )
                    )
                    if is_market_open(current, US_MARKET)
                    else float(
                        os.getenv(
                            "STOCK_AGENT_ALPACA_MAX_CLOSED_QUOTE_AGE_DAYS",
                            "5",
                        )
                    )
                    * 86_400
                )
                age_seconds = (
                    (current - observed_at).total_seconds()
                    if observed_at is not None
                    else maximum_age_seconds + 1
                )
                if age_seconds < -60 or age_seconds > maximum_age_seconds:
                    stale_symbols.append(symbol)
                    continue
                price = number(latest_trade.get("p"))
                if price <= 0:
                    price = number(minute_bar.get("c"))
                if price <= 0:
                    price = number(daily_bar.get("c"))
                if price <= 0:
                    price = number(previous_bar.get("c"))
                previous_close = number(previous_bar.get("c"))
                if price <= 0:
                    continue
                change = price - previous_close if previous_close > 0 else 0.0
                percent = change / previous_close * 100 if previous_close > 0 else 0.0
                volume = max(0, int(number(daily_bar.get("v"))))
                high = number(daily_bar.get("h"), price)
                low = number(daily_bar.get("l"), price)
                open_price = number(daily_bar.get("o"), price)
                vwap = number(daily_bar.get("vw"), price)
                configured = metadata[symbol]
                sector = str(configured.get("sector") or board_name)
                sectors = configured.get("sectors") or ([sector] if sector else [])
                rows.append(
                    {
                        "symbol": symbol,
                        "name": str(configured.get("name") or symbol),
                        "sector": sector,
                        "sectors": sectors,
                        "price": round(price, 4),
                        "percent": round(percent, 4),
                        "change": round(change, 4),
                        "volume": volume,
                        "turnover": round(vwap * volume, 2),
                        "turnover_rate": 0.0,
                        "volume_ratio": 0.0,
                        "pe": 0.0,
                        "pb": 0.0,
                        "amplitude": round(
                            ((high - low) / previous_close * 100)
                            if previous_close > 0 and high > 0 and low > 0
                            else 0.0,
                            4,
                        ),
                        "high": round(high, 4),
                        "low": round(low, 4),
                        "open": round(open_price, 4),
                        "prev_close": round(previous_close, 4),
                        "total_market_cap": 0.0,
                        "float_market_cap": 0.0,
                        "market": US_MARKET,
                        "currency": "USD",
                        "source": f"Alpaca {self.feed.upper()} 美股行情",
                        "time": _iso_timestamp(quote_time),
                    }
                )
        rows.sort(key=lambda item: item["symbol"])
        if stale_symbols and not rows:
            raise RuntimeError(
                "Alpaca 行情时间戳过期或缺失："
                + "、".join(sorted(set(stale_symbols))[:10])
            )
        return rows

    def fetch_daily_history(self, symbol: str) -> list[dict]:
        normalized = normalize_stock_symbol(symbol, US_MARKET)
        history_feed = str(
            os.getenv("STOCK_AGENT_ALPACA_HISTORY_FEED", self.feed)
        ).strip().lower() or self.feed
        start = str(
            os.getenv("STOCK_AGENT_ALPACA_HISTORY_START", "2016-01-01T00:00:00Z")
        ).strip()
        end = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        maximum_pages = max(
            1,
            int(os.getenv("STOCK_AGENT_ALPACA_HISTORY_MAX_PAGES", "10")),
        )
        page_token: str | None = None
        rows_by_date: dict[str, dict] = {}
        for _ in range(maximum_pages):
            payload = self._request_json(
                f"/v2/stocks/{urllib.parse.quote(normalized, safe='')}/bars",
                {
                    "timeframe": "1Day",
                    "start": start,
                    "end": end,
                    "limit": 10_000,
                    "adjustment": "all",
                    "feed": history_feed,
                    "page_token": page_token,
                },
            )
            bars = payload.get("bars") or []
            if not isinstance(bars, list):
                raise RuntimeError("Alpaca 日线响应缺少 bars")
            for bar in bars:
                if not isinstance(bar, dict):
                    continue
                trade_date = str(bar.get("t") or "")[:10]
                close = number(bar.get("c"))
                if not trade_date or close <= 0:
                    continue
                rows_by_date[trade_date] = {
                    "date": trade_date,
                    "open": number(bar.get("o"), close),
                    "close": close,
                    "high": number(bar.get("h"), close),
                    "low": number(bar.get("l"), close),
                    "volume": max(0, int(number(bar.get("v")))),
                    "turnover": round(
                        number(bar.get("vw"), close)
                        * max(0, int(number(bar.get("v")))),
                        2,
                    ),
                    "source": f"Alpaca {history_feed.upper()} 美股日线",
                }
            page_token = str(payload.get("next_page_token") or "").strip() or None
            if not page_token:
                break
        rows = [rows_by_date[item] for item in sorted(rows_by_date)]
        if not rows:
            raise RuntimeError(f"Alpaca 未返回 {normalized} 的有效日线")
        return rows


class AlpacaUsMarketDataProvider(UsMarketDataProvider):
    provider_id = "alpaca"

    def __init__(self, client: AlpacaMarketDataClient | None = None):
        self.client = client or AlpacaMarketDataClient()

    def fetch_quotes(
        self,
        *,
        symbols: Iterable[object],
        board_name: str = "纳斯达克100",
        source_label: str | None = None,
        timeout: int | float | None = None,
        urlopen_func: Callable | None = None,
    ) -> tuple[list[dict], str | None]:
        client = self.client
        if timeout is not None or urlopen_func is not None:
            client = AlpacaMarketDataClient(
                api_key=client.api_key,
                api_secret=client.api_secret,
                base_url=client.base_url,
                feed=client.feed,
                timeout=timeout if timeout is not None else client.timeout,
                urlopen_func=urlopen_func or client.urlopen_func,
                now_func=client.now_func,
            )
        try:
            rows = client.fetch_quotes(symbols=symbols, board_name=board_name)
        except Exception as exc:
            return [], str(exc)
        return (rows, None) if rows else ([], "Alpaca 未返回有效美股行情")

    def fetch_daily_history(self, symbol: str) -> list[dict]:
        return self.client.fetch_daily_history(symbol)


class SinaUsMarketDataProvider(UsMarketDataProvider):
    provider_id = "sina"

    def fetch_quotes(
        self,
        *,
        symbols: Iterable[object],
        board_name: str = "纳斯达克100",
        source_label: str | None = None,
        timeout: int | float | None = None,
        urlopen_func: Callable | None = None,
    ) -> tuple[list[dict], str | None]:
        from .data_sources import fetch_sina_us_quotes

        return fetch_sina_us_quotes(
            symbols=symbols,
            board_name=board_name,
            source_label=source_label or "新浪财经美股实时行情（降级源）",
            timeout=int(timeout) if timeout is not None else 8,
            urlopen_func=urlopen_func,
        )

    def fetch_daily_history(self, symbol: str) -> list[dict]:
        from .enrichment import _download_sina_us_daily_history

        return _download_sina_us_daily_history(symbol)


class FailoverUsMarketDataProvider(UsMarketDataProvider):
    provider_id = "failover"

    def __init__(
        self,
        primary: UsMarketDataProvider,
        fallback: UsMarketDataProvider,
        *,
        minimum_quote_coverage_ratio: float | None = None,
    ):
        self.primary = primary
        self.fallback = fallback
        configured_ratio = float(
            os.getenv("STOCK_AGENT_MIN_QUOTE_COVERAGE_RATIO", "0.8")
        )
        self.minimum_quote_coverage_ratio = min(
            1.0,
            max(
                0.0,
                float(
                    minimum_quote_coverage_ratio
                    if minimum_quote_coverage_ratio is not None
                    else configured_ratio
                ),
            ),
        )

    def fetch_quotes(
        self,
        *,
        symbols: Iterable[object],
        board_name: str = "纳斯达克100",
        source_label: str | None = None,
        timeout: int | float | None = None,
        urlopen_func: Callable | None = None,
    ) -> tuple[list[dict], str | None]:
        entries = normalize_watchlist(symbols, market=US_MARKET)
        expected = len(entries)
        primary_rows, primary_error = self.primary.fetch_quotes(
            symbols=entries,
            board_name=board_name,
            timeout=timeout,
            urlopen_func=urlopen_func,
        )
        primary_coverage = len(primary_rows) / expected if expected else 0.0
        primary_name = _provider_label(self.primary, "primary")
        fallback_name = _provider_label(self.fallback, "fallback")
        if primary_rows and primary_coverage >= self.minimum_quote_coverage_ratio:
            coverage_error = (
                f"{primary_name} 行情覆盖 {len(primary_rows)}/{expected}"
                if len(primary_rows) < expected
                else None
            )
            return primary_rows, primary_error or coverage_error

        fallback_rows, fallback_error = self.fallback.fetch_quotes(
            symbols=entries,
            board_name=board_name,
            timeout=timeout,
            urlopen_func=urlopen_func,
        )
        if fallback_rows:
            reason = primary_error or (
                f"{primary_name} 行情覆盖不足：{len(primary_rows)}/{expected}"
            )
            return (
                fallback_rows,
                f"{primary_name} 主源不可用，已降级至 {fallback_name}：{reason}",
            )
        errors = [item for item in (primary_error, fallback_error) if item]
        return [], "；".join(errors) or "美股主备行情源均未返回有效数据"

    def fetch_daily_history(self, symbol: str) -> list[dict]:
        try:
            rows = self.primary.fetch_daily_history(symbol)
            if rows:
                return rows
        except Exception as primary_error:
            try:
                rows = self.fallback.fetch_daily_history(symbol)
                if rows:
                    return rows
            except Exception as fallback_error:
                raise RuntimeError(
                    "美股主备日线均不可用："
                    f"{primary_error}；{fallback_error}"
                ) from fallback_error
            raise RuntimeError(f"美股主日线不可用：{primary_error}") from primary_error
        raise RuntimeError(f"{symbol} 美股日线主源返回空数据")


def normalize_us_data_source_policy(value: object = None) -> str:
    policy = str(value or US_DATA_SOURCE_AUTO).strip().lower()
    if policy not in US_DATA_SOURCE_POLICIES:
        raise ValueError(f"不支持的美股数据源策略：{policy}")
    return policy


def strategy_us_data_source(strategy: dict | None) -> str:
    state = (strategy or {}).get("parameters", {}).get("us_data_source", {})
    value = state.get("value") if isinstance(state, dict) else state
    return normalize_us_data_source_policy(value)


def get_us_market_data_provider(
    policy: object | None = None,
) -> UsMarketDataProvider:
    if policy is not None:
        selected = normalize_us_data_source_policy(policy)
        if selected == US_DATA_SOURCE_ALPACA:
            return AlpacaUsMarketDataProvider()
        if selected == US_DATA_SOURCE_SINA:
            return SinaUsMarketDataProvider()
        return FailoverUsMarketDataProvider(
            AlpacaUsMarketDataProvider(),
            SinaUsMarketDataProvider(),
        )

    primary_name = str(
        os.getenv("STOCK_AGENT_US_DATA_PRIMARY", "alpaca")
    ).strip().lower()
    fallback_name = str(
        os.getenv("STOCK_AGENT_US_DATA_FALLBACK", "sina")
    ).strip().lower()
    providers: dict[str, UsMarketDataProvider] = {
        "alpaca": AlpacaUsMarketDataProvider(),
        "sina": SinaUsMarketDataProvider(),
    }
    if primary_name not in providers:
        raise ValueError(f"不支持的美股主数据源：{primary_name}")
    primary = providers[primary_name]
    if not fallback_name or fallback_name == primary_name:
        return primary
    if fallback_name not in providers:
        raise ValueError(f"不支持的美股降级数据源：{fallback_name}")
    return FailoverUsMarketDataProvider(primary, providers[fallback_name])


def us_market_data_status(policy: object | None = None) -> dict:
    api_key, api_secret = _alpaca_credentials()
    alpaca_configured = bool(api_key and api_secret)
    if policy is None:
        primary = str(
            os.getenv("STOCK_AGENT_US_DATA_PRIMARY", "alpaca")
        ).strip().lower()
        fallback = str(
            os.getenv("STOCK_AGENT_US_DATA_FALLBACK", "sina")
        ).strip().lower()
        selected_policy = (
            US_DATA_SOURCE_AUTO
            if primary == US_DATA_SOURCE_ALPACA
            and fallback == US_DATA_SOURCE_SINA
            else primary
        )
    else:
        selected_policy = normalize_us_data_source_policy(policy)
        primary = (
            US_DATA_SOURCE_SINA
            if selected_policy == US_DATA_SOURCE_SINA
            else US_DATA_SOURCE_ALPACA
        )
        fallback = (
            US_DATA_SOURCE_SINA
            if selected_policy == US_DATA_SOURCE_AUTO
            else ""
        )
    if selected_policy == US_DATA_SOURCE_SINA:
        mode = "primary_ready"
        effective_source = US_DATA_SOURCE_SINA
    elif alpaca_configured:
        mode = "primary_ready"
        effective_source = US_DATA_SOURCE_ALPACA
    elif selected_policy == US_DATA_SOURCE_AUTO:
        mode = "degraded_fallback"
        effective_source = US_DATA_SOURCE_SINA
    else:
        mode = "unavailable"
        effective_source = "unavailable"
    return {
        "selected_policy": selected_policy,
        "primary": primary,
        "fallback": fallback,
        "effective_source": effective_source,
        "mode": mode,
        "alpaca_configured": alpaca_configured,
        "providers": [
            {
                "id": US_DATA_SOURCE_ALPACA,
                "label": "Alpaca",
                "available": alpaca_configured,
                "plan": "Basic 免费套餐",
                "requires_credentials": True,
            },
            {
                "id": US_DATA_SOURCE_SINA,
                "label": "新浪财经",
                "available": True,
                "plan": "公开行情接口",
                "requires_credentials": False,
            },
        ],
        "alpaca_feed": str(
            os.getenv("STOCK_AGENT_ALPACA_FEED", "iex")
        ).strip().lower(),
        "alpaca_history_feed": str(
            os.getenv(
                "STOCK_AGENT_ALPACA_HISTORY_FEED",
                os.getenv("STOCK_AGENT_ALPACA_FEED", "iex"),
            )
        ).strip().lower(),
    }
