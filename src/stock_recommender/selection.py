from __future__ import annotations

import re
from typing import Callable, Iterable

from .config import MAX_FLOAT_MARKET_CAP, MIN_FLOAT_MARKET_CAP
from .data_sources import fetch_tick_rows
from .universe import row_matches_sector
from .utils import number


def filter_candidates(rows: Iterable[dict], sector_filters: str | Iterable[object] | None = None) -> list[dict]:
    candidates = []
    for row in rows:
        if not row_matches_sector(row, sector_filters):
            continue
        symbol = str(row.get("symbol") or "")
        name = str(row.get("name") or "")
        if not symbol.startswith(("0", "3", "6")):
            continue
        if "ST" in name.upper():
            continue
        if number(row.get("price")) <= 0 or number(row.get("turnover")) <= 0:
            continue
        float_market_cap = number(row.get("float_market_cap"), default=0.0)
        if float_market_cap > 0 and not (MIN_FLOAT_MARKET_CAP <= float_market_cap <= MAX_FLOAT_MARKET_CAP):
            continue
        candidates.append(row)
    return candidates


def tick_seconds(value) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    match = re.search(r"(\d{2}):(\d{2}):(\d{2})", text)
    if not match:
        return None
    hour, minute, second = (int(part) for part in match.groups())
    return hour * 3600 + minute * 60 + second


def evaluate_tick_ignition(ticks: Iterable[dict]) -> dict:
    normalized = []
    for tick in ticks:
        seconds = tick_seconds(tick.get("time") or tick.get("成交时间"))
        price = number(tick.get("price", tick.get("成交价格")))
        volume = number(tick.get("volume", tick.get("成交量")))
        if seconds is None or price <= 0:
            continue
        normalized.append({"seconds": seconds, "price": price, "volume": volume})
    normalized.sort(key=lambda item: item["seconds"])
    if len(normalized) < 2:
        return {"confirmed": False, "price_change_10s": 0.0, "volume_ratio": 0.0, "reason": "逐笔数据不足"}

    end = normalized[-1]["seconds"]
    recent = [item for item in normalized if end - 10 <= item["seconds"] <= end]
    prior = [item for item in normalized if end - 130 <= item["seconds"] < end - 10]
    if len(recent) < 2 or not prior:
        return {"confirmed": False, "price_change_10s": 0.0, "volume_ratio": 0.0, "reason": "缺少最近10秒或前2分钟对照数据"}

    start_price = recent[0]["price"]
    end_price = recent[-1]["price"]
    price_change = ((end_price - start_price) / start_price) * 100 if start_price > 0 else 0.0
    recent_volume = sum(item["volume"] for item in recent)
    prior_span = max(10, prior[-1]["seconds"] - prior[0]["seconds"] + 10)
    prior_avg_10s = sum(item["volume"] for item in prior) / max(1, prior_span / 10)
    volume_ratio = recent_volume / prior_avg_10s if prior_avg_10s > 0 else 0.0
    confirmed = price_change >= 2.0 and volume_ratio >= 8.0
    return {
        "confirmed": confirmed,
        "price_change_10s": round(price_change, 2),
        "volume_ratio": round(volume_ratio, 2),
        "recent_volume": round(recent_volume, 2),
        "prior_avg_10s_volume": round(prior_avg_10s, 2),
        "reason": "10秒价量点火确认" if confirmed else "10秒价量点火未确认",
    }


def attach_ignition_signals(candidates: list[dict], tick_fetcher: Callable | None = None, tick_limit: int = 2) -> None:
    fetcher = tick_fetcher or fetch_tick_rows
    for item in candidates[:max(0, tick_limit)]:
        try:
            ticks = fetcher(item["symbol"])
            item["ignition_signal"] = evaluate_tick_ignition(ticks)
        except Exception as exc:
            item["ignition_signal"] = {
                "confirmed": False,
                "price_change_10s": 0.0,
                "volume_ratio": 0.0,
                "reason": f"逐笔成交获取失败：{exc}",
            }


def price_position(row: dict) -> dict:
    price = number(row.get("price"))
    open_price = number(row.get("open"))
    prev_close = number(row.get("prev_close"))
    volume_hands = number(row.get("volume"))
    turnover = number(row.get("turnover"))
    vwap = turnover / (volume_hands * 100) if volume_hands > 0 else 0.0
    return {
        "open": open_price,
        "prev_close": prev_close,
        "vwap": round(vwap, 3) if vwap > 0 else 0.0,
        "above_open": bool(open_price > 0 and price >= open_price),
        "above_zero_line": bool(prev_close > 0 and price >= prev_close),
        "above_vwap": bool(vwap > 0 and price >= vwap),
    }


def analyze(row: dict) -> dict:
    percent = number(row.get("percent"))
    turnover_rate = number(row.get("turnover_rate"))
    turnover = number(row.get("turnover"))
    pe = number(row.get("pe"))
    score = 52
    reasons: list[str] = []
    risk_level = "中"

    if percent >= 7:
        score += 24
        risk_level = "高"
        reasons.append(f"强势拉升 {percent:.2f}%，短线热度很高")
    elif percent >= 3:
        score += 16
        reasons.append(f"上涨 {percent:.2f}%，题材动能偏强")
    elif percent >= 0.5:
        score += 8
        reasons.append(f"温和上涨 {percent:.2f}%，走势相对稳健")
    elif percent > -2:
        score += 2
        reasons.append(f"窄幅波动 {percent:.2f}%，适合继续观察")
    else:
        score -= 8
        reasons.append(f"回调 {abs(percent):.2f}%，需要控制风险")

    if turnover_rate >= 8:
        score += 10
        reasons.append(f"换手率 {turnover_rate:.2f}%，资金博弈活跃")
    elif turnover_rate >= 2:
        score += 5
        reasons.append(f"换手率 {turnover_rate:.2f}%，流动性较好")

    if turnover >= 1_000_000_000:
        score += 8
        reasons.append(f"成交额 {turnover / 100_000_000:.1f} 亿，关注度高")
    elif turnover >= 200_000_000:
        score += 4
        reasons.append(f"成交额 {turnover / 100_000_000:.1f} 亿")

    if 0 < pe <= 60:
        score += 4
        reasons.append(f"PE {pe:.2f}，估值未极端失真")
    elif pe > 120 or pe < 0:
        score -= 4
        reasons.append(f"PE {pe:.2f}，估值/盈利质量需谨慎")

    final_score = max(0, min(100, score))
    if final_score >= 85:
        rating, emoji = "强烈关注", "🔥"
    elif final_score >= 72:
        rating, emoji = "积极关注", "⭐"
    elif final_score >= 60:
        rating, emoji = "值得关注", "🌟"
    else:
        rating, emoji = "观察", "📊"

    return {
        **row,
        "score": final_score,
        "rating": rating,
        "rating_emoji": emoji,
        "reasons": reasons,
        "risk_level": risk_level,
    }


def select_agent_candidates(analyses: list[dict], limit: int) -> list[dict]:
    if limit <= 0:
        return []
    hot = [item for item in analyses if number(item.get("percent")) >= 7]
    moderate = [item for item in analyses if 0 <= number(item.get("percent")) < 7]
    weak = [item for item in analyses if number(item.get("percent")) < 0]

    selected: list[dict] = []

    def add(items: list[dict], count: int) -> None:
        for item in items:
            if len(selected) >= limit or count <= 0:
                return
            if all(existing["symbol"] != item["symbol"] for existing in selected):
                selected.append(item)
                count -= 1

    add(hot, min(2, limit))
    add(moderate, max(1, limit - len(selected)))
    add(weak, limit - len(selected))
    add(analyses, limit - len(selected))
    return selected[:limit]
