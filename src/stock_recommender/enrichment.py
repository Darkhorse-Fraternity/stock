from __future__ import annotations

import math
import os
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Callable, Iterable

from .parameters import load_strategy_config


TECHNICAL_PARAMETER_IDS = {
    "listed_days_min",
    "ma5_above_ma20",
    "ma20_above_ma60",
    "price_above_ma20",
    "rsi_min",
    "rsi_max",
    "macd_bullish",
    "breakout_20d",
    "distance_52w_high_max",
    "volatility_20d_max",
}

FINANCIAL_PARAMETER_IDS = {
    "fcf_yield_min",
    "revenue_growth_min",
    "profit_growth_min",
    "eps_growth_min",
    "roe_min",
    "roa_min",
    "roic_min",
    "gross_margin_min",
    "net_margin_min",
    "operating_cashflow_positive",
    "free_cashflow_positive",
    "debt_ratio_max",
    "current_ratio_min",
}


def _finite_number(value, default: float | None = None) -> float | None:
    if value in (None, "", "-"):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


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


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    multiplier = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append((value - result[-1]) * multiplier + result[-1])
    return result


def _rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    deltas = [current - previous for previous, current in zip(values, values[1:])]
    gains = [max(delta, 0.0) for delta in deltas]
    losses = [max(-delta, 0.0) for delta in deltas]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        average_gain = ((average_gain * (period - 1)) + gain) / period
        average_loss = ((average_loss * (period - 1)) + loss) / period
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def calculate_technical_indicators(history_rows: Iterable[dict]) -> dict:
    normalized = []
    for row in history_rows:
        close = _finite_number(row.get("close", row.get("收盘")))
        if close is None or close <= 0:
            continue
        high = _finite_number(row.get("high", row.get("最高")), close) or close
        normalized.append({"date": _date_value(row.get("date", row.get("日期"))), "close": close, "high": high})
    normalized.sort(key=lambda item: item["date"] or date.min)
    closes = [item["close"] for item in normalized]
    if len(closes) < 2:
        return {}

    latest = closes[-1]
    ma5 = _sma(closes, 5)
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    dif_series = [short - long for short, long in zip(ema12, ema26)]
    dea_series = _ema_series(dif_series, 9)
    rsi = _rsi(closes)

    previous_20 = closes[-21:-1]
    highs_52w = [item["high"] for item in normalized[-252:]]
    high_52w = max(highs_52w) if highs_52w else latest
    recent = closes[-21:]
    log_returns = [math.log(current / previous) for previous, current in zip(recent, recent[1:]) if previous > 0 and current > 0]
    volatility = statistics.pstdev(log_returns) * math.sqrt(252) * 100 if len(log_returns) >= 2 else 0.0
    dated = [item["date"] for item in normalized if item["date"]]

    result = {
        "technical_source": "AkShare 前复权日线",
        "latest_history_date": dated[-1].isoformat() if dated else None,
        "latest_history_close": round(latest, 4),
        "listed_days": (dated[-1] - dated[0]).days if len(dated) >= 2 else len(closes) - 1,
        "ma5": round(ma5, 4) if ma5 is not None else None,
        "ma20": round(ma20, 4) if ma20 is not None else None,
        "ma60": round(ma60, 4) if ma60 is not None else None,
        "rsi": round(rsi, 2) if rsi is not None else None,
        "macd": round(dif_series[-1], 4),
        "macd_signal": round(dea_series[-1], 4),
        "macd_bullish": dif_series[-1] >= dea_series[-1],
        "breakout_20d": bool(previous_20 and latest >= max(previous_20)),
        "distance_52w_high": round(max(0.0, (high_52w - latest) / high_52w * 100), 2) if high_52w > 0 else 0.0,
        "volatility_20d": round(volatility, 2),
    }
    if ma5 is not None and ma20 is not None:
        result["ma5_above_ma20"] = ma5 >= ma20
    if ma20 is not None and ma60 is not None:
        result["ma20_above_ma60"] = ma20 >= ma60
    if ma20 is not None:
        result["price_above_ma20"] = latest >= ma20
    return result


def normalize_financial_snapshot(record: dict, *, total_market_cap: float = 0.0) -> dict:
    mapping = {
        "revenue_growth": "TOTALOPERATEREVETZ",
        "profit_growth": "PARENTNETPROFITTZ",
        "eps_growth": "EPSJBTZ",
        "roe": "ROEJQ",
        "roa": "ZZCJLL",
        "roic": "ROIC",
        "gross_margin": "XSMLL",
        "net_margin": "XSJLL",
        "debt_ratio": "ZCFZL",
        "current_ratio": "LD",
        "free_cashflow": "FCFF_BACK",
    }
    result = {"financial_source": "AkShare 东方财富财务分析"}
    for target, source in mapping.items():
        value = _finite_number(record.get(source))
        if value is not None:
            result[target] = round(value, 4)
    operating_cashflow = _finite_number(record.get("MGJYXJJE"))
    if operating_cashflow is not None:
        result["operating_cashflow_per_share"] = round(operating_cashflow, 6)
        result["operating_cashflow_positive"] = operating_cashflow > 0
    if "free_cashflow" in result:
        result["free_cashflow_positive"] = result["free_cashflow"] > 0
        if total_market_cap > 0:
            result["fcf_yield"] = round(result["free_cashflow"] / total_market_cap * 100, 4)
    report_date = _date_value(record.get("REPORT_DATE"))
    if report_date:
        result["financial_report_date"] = report_date.isoformat()
    return result


def fetch_daily_history(symbol: str) -> list[dict]:
    import akshare as ak

    frame = ak.stock_zh_a_hist(
        symbol=str(symbol),
        period="daily",
        start_date="19700101",
        end_date="20500101",
        adjust="qfq",
        timeout=float(os.getenv("STOCK_AGENT_ENRICH_TIMEOUT", "8")),
    )
    return [
        {
            "date": row.get("日期"),
            "open": row.get("开盘"),
            "close": row.get("收盘"),
            "high": row.get("最高"),
            "low": row.get("最低"),
            "volume": row.get("成交量"),
            "turnover": row.get("成交额"),
        }
        for _, row in frame.iterrows()
    ]


def _financial_symbol(symbol: str) -> str:
    code = str(symbol)
    if code.startswith("6"):
        return f"{code}.SH"
    if code.startswith("8"):
        return f"{code}.BJ"
    return f"{code}.SZ"


def fetch_financial_record(symbol: str) -> dict:
    import akshare as ak

    frame = ak.stock_financial_analysis_indicator_em(symbol=_financial_symbol(symbol), indicator="按报告期")
    if frame.empty:
        return {}
    return frame.iloc[0].to_dict()


def _requires(strategy: dict, parameter_ids: set[str]) -> bool:
    states = strategy.get("parameters", {})
    return any(states.get(parameter_id, {}).get("enabled") for parameter_id in parameter_ids)


def enrich_candidates(
    rows: Iterable[dict],
    *,
    strategy: dict | None = None,
    history_fetcher: Callable[[str], list[dict]] | None = None,
    financial_fetcher: Callable[[str], dict] | None = None,
    limit: int | None = None,
) -> list[dict]:
    current = strategy or load_strategy_config()
    needs_technical = _requires(current, TECHNICAL_PARAMETER_IDS)
    needs_financial = _requires(current, FINANCIAL_PARAMETER_IDS)
    candidates = [dict(row) for row in rows]
    if not needs_technical and not needs_financial:
        return candidates

    maximum = limit if limit is not None else int(os.getenv("STOCK_AGENT_ENRICH_LIMIT", "12"))
    target_count = min(len(candidates), max(0, maximum))
    history_client = history_fetcher or fetch_daily_history
    financial_client = financial_fetcher or fetch_financial_record

    def enrich(index: int) -> tuple[int, dict]:
        row = dict(candidates[index])
        errors = []
        if needs_technical:
            try:
                row.update(calculate_technical_indicators(history_client(row["symbol"])))
            except Exception as exc:
                errors.append(f"technical: {exc}")
        if needs_financial:
            try:
                record = financial_client(row["symbol"])
                row.update(normalize_financial_snapshot(record, total_market_cap=_finite_number(row.get("total_market_cap"), 0.0) or 0.0))
            except Exception as exc:
                errors.append(f"financial: {exc}")
        if errors:
            row["enrichment_errors"] = errors
        return index, row

    workers = min(4, target_count)
    if workers <= 0:
        return candidates
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="stock-enrich") as executor:
        futures = [executor.submit(enrich, index) for index in range(target_count)]
        for future in as_completed(futures):
            index, row = future.result()
            candidates[index] = row
    return candidates
