from __future__ import annotations

import json
import re
import socket
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Callable, Iterable

from .config import DEFAULT_BOARD_CODE, DEFAULT_BOARD_NAME, DEFAULT_TIMEOUT_SECONDS, EASTMONEY_URL, STATIC_FALLBACK
from .utils import number


def fetch_board_quotes(
    board_code: str = DEFAULT_BOARD_CODE,
    *,
    board_name: str = DEFAULT_BOARD_NAME,
    limit: int = 50,
    page_size: int = 50,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    urlopen_func: Callable | None = None,
) -> tuple[list[dict], str | None]:
    opener = urlopen_func or urllib.request.urlopen
    quotes = []
    last_error = None
    pages = max(1, (limit + page_size - 1) // page_size)

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
            f"{EASTMONEY_URL}?{params}",
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
                        "10",
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
                    timeout=max(timeout + 2, timeout * 5),
                )
                if curl_result.returncode != 0:
                    last_error = (curl_result.stderr or str(exc)).strip() or str(exc)
                    break
                payload = json.loads(curl_result.stdout)
            except (OSError, TimeoutError, subprocess.SubprocessError, json.JSONDecodeError) as curl_exc:
                last_error = str(curl_exc) or str(exc)
                break

        rows = (payload.get("data") or {}).get("diff") or []
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

    if not quotes:
        return [], last_error or "Eastmoney returned no board rows"
    return quotes, None


def sina_quote_symbol(symbol: str) -> str:
    code = str(symbol).strip()
    if code.startswith("6"):
        return f"sh{code}"
    return f"sz{code}"


def fetch_sina_fallback_quotes(
    *,
    symbols: Iterable[str] | None = None,
    board_name: str = DEFAULT_BOARD_NAME,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    urlopen_func: Callable | None = None,
) -> tuple[list[dict], str | None]:
    quote_symbols = [sina_quote_symbol(item["symbol"] if isinstance(item, dict) else item) for item in (symbols or STATIC_FALLBACK)]
    quote_symbols = [item for item in quote_symbols if re.match(r"^(sh|sz)\d{6}$", item)]
    if not quote_symbols:
        return [], "No Sina quote symbols configured"

    params = urllib.parse.urlencode({"list": ",".join(quote_symbols)}, safe=",")
    request = urllib.request.Request(
        f"https://hq.sinajs.cn/?{params}",
        headers={
            "Accept": "*/*",
            "Referer": "https://finance.sina.com.cn/",
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        },
    )
    opener = urlopen_func or urllib.request.urlopen

    try:
        with opener(request, timeout=timeout) as response:
            text = response.read().decode("gb18030", errors="replace")
    except (OSError, TimeoutError, socket.timeout) as exc:
        return [], str(exc)

    quotes = []
    source = f"新浪财经实时行情（{board_name}备用股池）"
    for match in re.finditer(r'var hq_str_(sh|sz)(\d{6})="(.*?)";', text, flags=re.S):
        symbol = match.group(2)
        fields = match.group(3).split(",")
        if len(fields) < 10:
            continue
        name = fields[0].strip() or symbol
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
                "sector": board_name,
                "price": round(price, 2),
                "percent": round(percent, 2),
                "change": round(change, 2),
                "volume": int(shares / 100),
                "turnover": round(turnover, 2),
                "turnover_rate": 0.0,
                "volume_ratio": 0.0,
                "pe": 0.0,
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
        return [], "Sina returned no usable quote rows"
    return quotes, None


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
                "amplitude": 0.0,
                "source": f"估算兜底（实时接口失败：{reason}）",
                "time": now.strftime("%Y-%m-%d %H:%M"),
            }
        )
    return rows
