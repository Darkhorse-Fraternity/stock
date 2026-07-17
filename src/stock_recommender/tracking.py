from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from .data_sources import fetch_watchlist_quotes
from .reports import format_recommendation_snapshot
from .universe import constrain_to_watchlist
from .utils import beijing_now


TRACKING_HEADER = "📈 **推荐股每小时成交与涨跌跟踪**"


def extract_recommended_symbols(report: str) -> list[str]:
    if TRACKING_HEADER not in report:
        return []
    section = report.split(TRACKING_HEADER, 1)[1]
    symbols: list[str] = []
    for symbol in re.findall(r"^\s*- .*?\((\d{6})\)：", section, flags=re.M):
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols


def save_daily_selection(path: str | Path, report: str, *, now: datetime | None = None) -> list[str]:
    symbols = extract_recommended_symbols(report)
    if not symbols:
        return []
    current = beijing_now(now)
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "trade_date": current.strftime("%Y-%m-%d"),
        "generated_at": current.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "symbols": symbols,
    }
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return symbols


def load_daily_selection(path: str | Path, *, now: datetime | None = None) -> list[str]:
    target = Path(path).expanduser()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if payload.get("trade_date") != beijing_now(now).strftime("%Y-%m-%d"):
        return []
    symbols = payload.get("symbols") or []
    return [str(symbol) for symbol in symbols if re.fullmatch(r"\d{6}", str(symbol))]


def generate_saved_tracking_report(
    *,
    state_path: str | Path,
    now: datetime | None = None,
    quote_fetcher: Callable | None = None,
) -> str:
    current = beijing_now(now)
    symbols = load_daily_selection(state_path, now=current)
    if not symbols:
        return "⚠️ 今日尚无可跟踪的推荐股票，跳过本次盘中报告。"

    entries = [{"symbol": symbol} for symbol in symbols]
    fetcher = quote_fetcher or fetch_watchlist_quotes
    try:
        rows, error = fetcher(entries)
    except Exception as exc:
        rows, error = [], str(exc)
    rows = constrain_to_watchlist(rows, entries)
    by_symbol = {str(row.get("symbol")): row for row in rows}
    ordered = [by_symbol[symbol] for symbol in symbols if symbol in by_symbol]
    if not ordered:
        return "\n".join(
            [
                "⚠️ **推荐股盘中行情不可用**",
                f"数据源错误：{error or '未返回有效行情'}",
                "本次不发布成交量和涨跌幅，避免使用失效数据。",
            ]
        )

    snapshot = format_recommendation_snapshot(
        ordered,
        limit=len(symbols),
        generated_at=current.strftime("%m月%d日 %H:%M"),
    )
    return "\n".join(
        [
            f"📊 **推荐股盘中情况报告** ({current.strftime('%Y 年%m月%d日')})",
            "",
            snapshot,
            "",
            "仅跟踪今日 09:30 生成的推荐名单，不重新选股。",
            "仅供参考，不构成投资建议。",
        ]
    )
