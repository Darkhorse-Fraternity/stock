from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Callable, Iterable

from .config import (
    DEFAULT_BOARD_CODE,
    DEFAULT_BOARD_NAME,
    DEFAULT_TIMEOUT_SECONDS,
    EASTMONEY_FALLBACK_URL,
    EASTMONEY_URL,
    NASDAQ_100_URL,
    STATIC_FALLBACK,
)
from .markets import CN_MARKET, US_MARKET, normalize_market
from .universe import normalize_stock_symbol, normalize_watchlist
from .utils import number


def fetch_board_quotes(
    board_code: str = DEFAULT_BOARD_CODE,
    *,
    board_name: str = DEFAULT_BOARD_NAME,
    limit: int = 1000,
    page_size: int = 50,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    retries: int | None = None,
    urlopen_func: Callable | None = None,
    _base_url: str | None = None,
) -> tuple[list[dict], str | None]:
    opener = urlopen_func or urllib.request.urlopen
    quotes = []
    last_error = None
    expected_total: int | None = None
    base_url = _base_url or os.getenv("STOCK_AGENT_EASTMONEY_URL", EASTMONEY_URL).strip() or EASTMONEY_URL
    pages = max(1, (limit + page_size - 1) // page_size)
    retry_count = max(0, int(os.getenv("STOCK_AGENT_SOURCE_RETRIES", "2") if retries is None else retries))

    for page in range(1, pages + 1):
        params = urllib.parse.urlencode(
            {
                "pn": str(page),
                "pz": str(page_size),
                "po": "1",
                "np": "1",
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": "2",
                "invt": "2",
                "fid": "f3",
                "fs": f"b:{board_code}",
                "fields": "f12,f14,f2,f3,f4,f5,f6,f7,f8,f9,f10,f15,f16,f17,f18,f20,f21,f23",
            },
            safe=":,",
        )
        request = urllib.request.Request(
            f"{base_url}?{params}",
            headers={
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://quote.eastmoney.com/",
                "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
            },
        )

        try:
            with opener(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
            if urlopen_func is not None:
                last_error = str(exc)
                break
            try:
                curl_result = subprocess.run(
                    [
                        "curl",
                        "--fail",
                        "--silent",
                        "--show-error",
                        "--max-time",
                        str(timeout),
                        "--retry",
                        str(retry_count),
                        "--retry-delay",
                        "1",
                        "--retry-all-errors",
                        "-H",
                        "Accept: application/json,text/plain,*/*",
                        "-H",
                        "Referer: https://quote.eastmoney.com/",
                        "-H",
                        "User-Agent: Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
                        request.full_url,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=max(timeout + 2, timeout * (retry_count + 1) + retry_count),
                )
                if curl_result.returncode != 0:
                    last_error = (curl_result.stderr or str(exc)).strip() or str(exc)
                    break
                payload = json.loads(curl_result.stdout)
            except (OSError, TimeoutError, subprocess.SubprocessError, json.JSONDecodeError) as curl_exc:
                last_error = str(curl_exc) or str(exc)
                break

        data = payload.get("data") or {}
        if expected_total is None and number(data.get("total")) > 0:
            expected_total = int(number(data.get("total")))
        rows = data.get("diff") or []
        if not rows:
            break
        for row in rows:
            symbol = str(row.get("f12") or "")
            if not symbol:
                continue
            quotes.append(
                {
                    "symbol": symbol,
                    "name": row.get("f14") or symbol,
                    "sector": board_name,
                    "price": number(row.get("f2")),
                    "percent": number(row.get("f3")),
                    "change": number(row.get("f4")),
                    "volume": int(number(row.get("f5"))),
                    "turnover": number(row.get("f6")),
                    "turnover_rate": number(row.get("f8")),
                    "pe": number(row.get("f9")),
                    "pb": number(row.get("f23")),
                    "volume_ratio": number(row.get("f10")),
                    "amplitude": number(row.get("f7", row.get("f10"))),
                    "high": number(row.get("f15")),
                    "low": number(row.get("f16")),
                    "open": number(row.get("f17")),
                    "prev_close": number(row.get("f18")),
                    "total_market_cap": number(row.get("f20")),
                    "float_market_cap": number(row.get("f21")),
                    "source": f"东方财富 {board_name}({board_code})",
                }
            )
            if len(quotes) >= limit:
                break
        if len(quotes) >= limit:
            break
        if expected_total is not None and len(quotes) >= min(limit, expected_total):
            break

    if last_error and urlopen_func is None and base_url == EASTMONEY_URL:
        fallback_url = (
            os.getenv("STOCK_AGENT_EASTMONEY_FALLBACK_URL", EASTMONEY_FALLBACK_URL).strip()
            or EASTMONEY_FALLBACK_URL
        )
        if fallback_url != base_url:
            return fetch_board_quotes(
                board_code,
                board_name=board_name,
                limit=limit,
                page_size=page_size,
                timeout=timeout,
                retries=retries,
                _base_url=fallback_url,
            )
    if not quotes:
        return [], last_error or "Eastmoney returned no board rows"
    if expected_total is not None:
        if expected_total > limit:
            last_error = f"板块共有 {expected_total} 只，配置上限 {limit} 只，拒绝保存截断股票池"
        elif len(quotes) < expected_total:
            last_error = last_error or f"板块行情只返回 {len(quotes)}/{expected_total} 只"
    quotes.sort(key=lambda item: str(item.get("symbol") or ""))
    return quotes, last_error


def sina_quote_symbol(symbol: str) -> str:
    code = str(symbol).strip()
    if code.startswith("6"):
        return f"sh{code}"
    return f"sz{code}"


def fetch_sina_fallback_quotes(
    *,
    symbols: Iterable[object] | None = None,
    board_name: str = DEFAULT_BOARD_NAME,
    source_label: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    urlopen_func: Callable | None = None,
) -> tuple[list[dict], str | None]:
    entries = list(STATIC_FALLBACK if symbols is None else symbols)
    metadata: dict[str, dict] = {}
    quote_symbols = []
    for item in entries:
        configured = dict(item) if isinstance(item, dict) else {}
        raw_symbol = configured.get("symbol") if configured else item
        try:
            symbol = normalize_stock_symbol(raw_symbol)
        except ValueError:
            continue
        metadata[symbol] = configured
        quote_symbols.append(sina_quote_symbol(symbol))
    quote_symbols = [item for item in quote_symbols if re.match(r"^(sh|sz)\d{6}$", item)]
    if not quote_symbols:
        return [], "No Sina quote symbols configured"

    opener = urlopen_func or urllib.request.urlopen
    quotes = []
    errors: list[str] = []
    source = source_label or f"新浪财经实时行情（{board_name}备用股池）"
    batch_size = max(1, int(os.getenv("STOCK_AGENT_SINA_QUOTE_BATCH_SIZE", "80")))
    for offset in range(0, len(quote_symbols), batch_size):
        batch = quote_symbols[offset : offset + batch_size]
        params = urllib.parse.urlencode({"list": ",".join(batch)}, safe=",")
        request = urllib.request.Request(
            f"https://hq.sinajs.cn/?{params}",
            headers={
                "Accept": "*/*",
                "Referer": "https://finance.sina.com.cn/",
                "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
            },
        )
        try:
            with opener(request, timeout=timeout) as response:
                text = response.read().decode("gb18030", errors="replace")
        except (OSError, TimeoutError, socket.timeout) as exc:
            errors.append(str(exc))
            continue

        for match in re.finditer(r'var hq_str_(sh|sz)(\d{6})="(.*?)";', text, flags=re.S):
            symbol = match.group(2)
            fields = match.group(3).split(",")
            if len(fields) < 10:
                continue
            configured = metadata.get(symbol) or {}
            name = str(configured.get("name") or fields[0].strip() or symbol)
            sector = str(configured.get("sector") or board_name)
            sectors = configured.get("sectors") or ([sector] if sector else [])
            open_price = number(fields[1])
            prev_close = number(fields[2])
            price = number(fields[3])
            high = number(fields[4])
            low = number(fields[5])
            shares = number(fields[8])
            turnover = number(fields[9])
            if price <= 0 or turnover <= 0:
                continue
            change = price - prev_close if prev_close > 0 else 0.0
            percent = (change / prev_close * 100) if prev_close > 0 else 0.0
            amplitude = ((high - low) / prev_close * 100) if prev_close > 0 and high > 0 and low > 0 else 0.0
            quote_time = ""
            if len(fields) > 31:
                quote_time = f"{fields[30]} {fields[31]}".strip()
            quotes.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "sector": sector,
                    "sectors": sectors,
                    "price": round(price, 2),
                    "percent": round(percent, 2),
                    "change": round(change, 2),
                    "volume": int(shares / 100),
                    "turnover": round(turnover, 2),
                    "turnover_rate": 0.0,
                    "volume_ratio": 0.0,
                    "pe": 0.0,
                    "pb": 0.0,
                    "amplitude": round(amplitude, 2),
                    "high": round(high, 2),
                    "low": round(low, 2),
                    "open": round(open_price, 2),
                    "prev_close": round(prev_close, 2),
                    "source": source,
                    "time": quote_time,
                }
            )

    if not quotes:
        return [], "；".join(errors) or "Sina returned no usable quote rows"
    quotes.sort(key=lambda item: str(item.get("symbol") or ""))
    error = f"新浪分批行情部分失败：{'；'.join(sorted(set(errors)))}" if errors else None
    return quotes, error


def _market_number(value: object) -> float:
    text = str(value or "").strip().replace(",", "").replace("$", "").replace("%", "")
    return number(text)


def fetch_nasdaq100_quotes(
    universe_code: str = "NASDAQ100",
    *,
    board_name: str = "纳斯达克100",
    limit: int = 1000,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    urlopen_func: Callable | None = None,
) -> tuple[list[dict], str | None]:
    """Fetch the current Nasdaq-100 membership from Nasdaq's public endpoint."""

    if str(universe_code).strip().upper() not in {"NASDAQ100", "NDX"}:
        return [], f"美股首版仅支持 NASDAQ100 动态成分，收到 {universe_code!r}"
    request = urllib.request.Request(
        NASDAQ_100_URL,
        headers={
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.nasdaq.com/market-activity/quotes/nasdaq-ndx-index",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        },
    )
    opener = urlopen_func or urllib.request.urlopen
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
        return [], str(exc)
    data = ((payload.get("data") or {}).get("data") or {})
    rows = data.get("rows") or []
    quotes: list[dict] = []
    for raw in rows[: max(1, int(limit))]:
        try:
            symbol = normalize_stock_symbol(raw.get("symbol"), US_MARKET)
        except ValueError:
            continue
        price = _market_number(raw.get("lastSalePrice"))
        percent = _market_number(raw.get("percentageChange"))
        change = _market_number(raw.get("netChange"))
        if str(raw.get("deltaIndicator") or "").strip().lower() == "down":
            percent = -abs(percent)
            change = -abs(change)
        market_cap = _market_number(raw.get("marketCap"))
        quotes.append(
            {
                "symbol": symbol,
                "name": str(raw.get("companyName") or symbol),
                "sector": board_name,
                "sectors": [board_name],
                "price": price,
                "percent": percent,
                "change": change,
                "volume": 0,
                "turnover": 0.0,
                "turnover_rate": 0.0,
                "volume_ratio": 0.0,
                "pe": 0.0,
                "pb": 0.0,
                "amplitude": 0.0,
                "high": 0.0,
                "low": 0.0,
                "open": 0.0,
                "prev_close": price - change if price > 0 else 0.0,
                "total_market_cap": market_cap,
                "float_market_cap": market_cap,
                "market": US_MARKET,
                "currency": "USD",
                "source": f"Nasdaq {board_name}动态成分",
            }
        )
    quotes.sort(key=lambda item: item["symbol"])
    return (quotes, None) if quotes else ([], "Nasdaq returned no usable Nasdaq-100 rows")


def fetch_sina_us_quotes(
    *,
    symbols: Iterable[object],
    board_name: str = "纳斯达克100",
    source_label: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    urlopen_func: Callable | None = None,
) -> tuple[list[dict], str | None]:
    entries = normalize_watchlist(symbols, market=US_MARKET)
    metadata = {item["symbol"]: item for item in entries}
    if not metadata:
        return [], "美股代码列表为空"
    opener = urlopen_func or urllib.request.urlopen
    source = source_label or "新浪财经美股实时行情"
    batch_size = max(1, int(os.getenv("STOCK_AGENT_SINA_US_QUOTE_BATCH_SIZE", "80")))
    quotes: list[dict] = []
    errors: list[str] = []
    tickers = list(metadata)
    for offset in range(0, len(tickers), batch_size):
        batch = tickers[offset : offset + batch_size]
        quote_symbols = [f"gb_{symbol.lower().replace('.', '$')}" for symbol in batch]
        params = urllib.parse.urlencode({"list": ",".join(quote_symbols)}, safe=",")
        request = urllib.request.Request(
            f"https://hq.sinajs.cn/?{params}",
            headers={
                "Accept": "*/*",
                "Referer": "https://finance.sina.com.cn/stock/usstock/",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            },
        )
        try:
            with opener(request, timeout=timeout) as response:
                text = response.read().decode("gb18030", errors="replace")
        except (OSError, TimeoutError, socket.timeout) as exc:
            errors.append(str(exc))
            continue
        for match in re.finditer(r'var hq_str_gb_([a-z0-9_$.-]+)="(.*?)";', text, flags=re.I | re.S):
            raw_symbol = match.group(1).replace("$", ".")
            try:
                symbol = normalize_stock_symbol(raw_symbol, US_MARKET)
            except ValueError:
                continue
            fields = match.group(2).split(",")
            if len(fields) < 15:
                continue
            configured = metadata.get(symbol) or {}
            price = number(fields[1])
            percent = number(fields[2])
            change = number(fields[4])
            open_price = number(fields[5])
            high = number(fields[6])
            low = number(fields[7])
            volume = int(
                number(fields[27])
                if len(fields) > 27 and number(fields[27]) > 0
                else number(fields[10])
            )
            market_cap = number(fields[12])
            pe = number(fields[14])
            prev_close = number(fields[26]) if len(fields) > 26 else price - change
            turnover = (
                number(fields[30])
                if len(fields) > 30 and number(fields[30]) > 0
                else price * volume
            )
            if price <= 0 or volume <= 0:
                continue
            sector = str(configured.get("sector") or board_name)
            sectors = configured.get("sectors") or ([sector] if sector else [])
            amplitude = ((high - low) / prev_close * 100) if prev_close > 0 and high > 0 and low > 0 else 0.0
            quotes.append(
                {
                    "symbol": symbol,
                    "name": str(configured.get("name") or fields[0].strip() or symbol),
                    "sector": sector,
                    "sectors": sectors,
                    "price": round(price, 4),
                    "percent": round(percent, 4),
                    "change": round(change, 4),
                    "volume": volume,
                    "turnover": round(turnover, 2),
                    "turnover_rate": 0.0,
                    "volume_ratio": 0.0,
                    "pe": round(pe, 4),
                    "pb": 0.0,
                    "amplitude": round(amplitude, 4),
                    "high": round(high, 4),
                    "low": round(low, 4),
                    "open": round(open_price, 4),
                    "prev_close": round(prev_close, 4),
                    "total_market_cap": market_cap,
                    "float_market_cap": market_cap,
                    "market": US_MARKET,
                    "currency": "USD",
                    "source": source,
                    "time": fields[3],
                }
            )
    quotes.sort(key=lambda item: item["symbol"])
    if not quotes:
        return [], "；".join(errors) or "Sina returned no usable US quote rows"
    error = f"新浪美股分批行情部分失败：{'；'.join(sorted(set(errors)))}" if errors else None
    return quotes, error


def fetch_watchlist_quotes(
    watchlist: Iterable[object],
    *,
    market: object = CN_MARKET,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    urlopen_func: Callable | None = None,
) -> tuple[list[dict], str | None]:
    normalized_market = normalize_market(market)
    entries = normalize_watchlist(watchlist, market=normalized_market)
    if not entries:
        return [], "自选股池为空"
    if normalized_market == US_MARKET:
        from .us_data_providers import get_us_market_data_provider

        return get_us_market_data_provider().fetch_quotes(
            symbols=entries,
            board_name="未分类",
            timeout=timeout,
            urlopen_func=urlopen_func,
        )
    return fetch_sina_fallback_quotes(
        symbols=entries,
        board_name="未分类",
        source_label="新浪财经实时行情（自选股池）",
        timeout=timeout,
        urlopen_func=urlopen_func,
    )


def akshare_symbol(symbol: str) -> str:
    code = str(symbol)
    if code.startswith("6"):
        return f"sh{code}"
    return f"sz{code}"


def fetch_tick_rows(symbol: str) -> list[dict]:
    import akshare as ak

    df = ak.stock_zh_a_tick_tx_js(symbol=akshare_symbol(symbol))
    rows = []
    for _, row in df.iterrows():
        rows.append(
            {
                "time": row.get("成交时间"),
                "price": row.get("成交价格"),
                "volume": row.get("成交量"),
            }
        )
    return rows


def fallback_quotes(now: datetime, reason: str) -> list[dict]:
    rows = []
    for index, stock in enumerate(STATIC_FALLBACK):
        percent = round(0.6 - index * 0.08, 2)
        base_price = float(stock["base_price"])
        price = round(base_price * (1 + percent / 100), 2)
        rows.append(
            {
                **stock,
                "price": price,
                "percent": percent,
                "change": round(price - base_price, 2),
                "volume": 0,
                "turnover": 0.0,
                "turnover_rate": 0.0,
                "volume_ratio": 0.0,
                "pe": 0.0,
                "pb": 0.0,
                "amplitude": 0.0,
                "source": f"估算兜底（实时接口失败：{reason}）",
                "time": now.strftime("%Y-%m-%d %H:%M"),
            }
        )
    return rows
