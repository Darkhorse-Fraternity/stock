from __future__ import annotations

import math
import statistics
from copy import deepcopy
from datetime import date, datetime
from typing import Iterable, Mapping


SIGNAL_MODEL_ID = "factor_rank_v1"
SIGNAL_DATA_CUTOFF = "previous_trading_day_close"
SIGNAL_FEATURE_FIELDS = (
    "momentum20",
    "momentum60",
    "trend",
    "volume_ratio",
    "inverse_volatility",
    "drawdown",
)
DEFAULT_FACTOR_WEIGHTS = {field: 1.0 for field in SIGNAL_FEATURE_FIELDS}
MINIMUM_HISTORY_ROWS = 61

_FEATURE_LABELS = {
    "momentum20": "20 日动量",
    "momentum60": "60 日动量",
    "trend": "均线趋势",
    "volume_ratio": "成交量相对强度",
    "inverse_volatility": "波动率控制",
    "drawdown": "距 60 日高点",
}


class SignalDataError(ValueError):
    pass


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


def _finite_number(value, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _history_row(row: Mapping) -> dict | None:
    close = _finite_number(row.get("close", row.get("收盘")))
    if close <= 0:
        return None
    row_date = _date_value(row.get("date", row.get("日期")))
    open_price = _finite_number(row.get("open", row.get("开盘")), close) or close
    return {
        "date": row_date,
        "open": open_price,
        "close": close,
        "high": _finite_number(row.get("high", row.get("最高")), close) or close,
        "low": _finite_number(row.get("low", row.get("最低")), close) or close,
        "volume": _finite_number(row.get("volume", row.get("成交量"))),
    }


def normalize_signal_history(
    history_rows: Iterable[Mapping],
    *,
    cutoff: date | datetime | str | None = None,
) -> list[dict]:
    cutoff_date = _date_value(cutoff) if cutoff is not None else None
    by_date: dict[date, dict] = {}
    undated: list[dict] = []
    for raw in history_rows:
        row = _history_row(raw)
        if row is None:
            continue
        row_date = row["date"]
        if cutoff_date is not None:
            if row_date is None or row_date >= cutoff_date:
                continue
        if row_date is None:
            undated.append(row)
        else:
            by_date[row_date] = row
    if cutoff_date is not None:
        return [by_date[item] for item in sorted(by_date)]
    return [*undated, *[by_date[item] for item in sorted(by_date)]]


def extract_signal_features(
    history_rows: Iterable[Mapping],
    *,
    cutoff: date | datetime | str | None = None,
    minimum_rows: int = MINIMUM_HISTORY_ROWS,
) -> dict | None:
    required = max(MINIMUM_HISTORY_ROWS, int(minimum_rows))
    history = normalize_signal_history(history_rows, cutoff=cutoff)
    if len(history) < required:
        return None
    history = history[-max(required, MINIMUM_HISTORY_ROWS) :]
    closes = [row["close"] for row in history]
    volumes = [row["volume"] for row in history]
    latest = closes[-1]
    momentum20 = latest / closes[-21] - 1 if closes[-21] > 0 else 0.0
    momentum60 = latest / closes[-61] - 1 if closes[-61] > 0 else 0.0
    ma5 = statistics.fmean(closes[-5:])
    ma20 = statistics.fmean(closes[-20:])
    ma60 = statistics.fmean(closes[-60:])
    returns = [
        current / previous - 1
        for previous, current in zip(closes[-21:-1], closes[-20:])
        if previous > 0
    ]
    volatility = statistics.pstdev(returns) * math.sqrt(252) if len(returns) >= 2 else 0.0
    peak = max(closes[-60:])
    drawdown = latest / peak - 1 if peak > 0 else 0.0
    previous_volume = [value for value in volumes[-21:-1] if value > 0]
    volume_ratio = volumes[-1] / statistics.fmean(previous_volume) if previous_volume and volumes[-1] > 0 else 1.0
    latest_date = history[-1]["date"]
    return {
        "momentum20": momentum20,
        "momentum60": momentum60,
        "trend": (1.0 if ma5 >= ma20 else 0.0) + (1.0 if ma20 >= ma60 else 0.0),
        "volume_ratio": min(volume_ratio, 5.0),
        "inverse_volatility": -volatility,
        "drawdown": drawdown,
        "latest_return": latest / closes[-2] - 1 if closes[-2] > 0 else 0.0,
        "history_rows": len(history),
        "history_latest_date": latest_date.isoformat() if latest_date else None,
    }


def _percentile_ranks(values: list[tuple[str, float]]) -> dict[str, float]:
    if not values:
        return {}
    if len(values) == 1:
        return {values[0][0]: 0.5}
    ordered = sorted(values, key=lambda item: (item[1], item[0]))
    ranks: dict[str, float] = {}
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        average_index = (index + end - 1) / 2
        for symbol, _ in ordered[index:end]:
            ranks[symbol] = average_index / (len(ordered) - 1)
        index = end
    return ranks


def factor_weights(strategy: dict | None = None) -> dict[str, float]:
    configured = (strategy or {}).get("signal", {}).get("factor_weights")
    raw = configured if isinstance(configured, dict) else DEFAULT_FACTOR_WEIGHTS
    weights = {
        field: max(0.0, _finite_number(raw.get(field), DEFAULT_FACTOR_WEIGHTS[field]))
        for field in SIGNAL_FEATURE_FIELDS
    }
    total = sum(weights.values())
    if total <= 0:
        weights = deepcopy(DEFAULT_FACTOR_WEIGHTS)
        total = sum(weights.values())
    return {field: value / total for field, value in weights.items()}


def _score_details(features: Mapping[str, Mapping], strategy: dict | None = None) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    complete = {
        str(symbol): item
        for symbol, item in features.items()
        if all(field in item for field in SIGNAL_FEATURE_FIELDS)
    }
    ranks = {
        field: _percentile_ranks([(symbol, _finite_number(item[field])) for symbol, item in complete.items()])
        for field in SIGNAL_FEATURE_FIELDS
    }
    weights = factor_weights(strategy)
    scores = {
        symbol: sum(ranks[field][symbol] * weights[field] for field in SIGNAL_FEATURE_FIELDS) * 100
        for symbol in complete
    }
    return scores, ranks


def score_feature_map(features: Mapping[str, Mapping], strategy: dict | None = None) -> list[tuple[str, float]]:
    scores, _ = _score_details(features, strategy)
    return sorted(
        ((symbol, round(score, 4)) for symbol, score in scores.items()),
        key=lambda item: (item[1], item[0]),
        reverse=True,
    )


def _factor_reason(field: str, value: float) -> str:
    label = _FEATURE_LABELS[field]
    if field in {"momentum20", "momentum60", "drawdown"}:
        return f"{label} {value * 100:+.2f}%"
    if field == "volume_ratio":
        return f"{label} {value:.2f} 倍"
    if field == "inverse_volatility":
        return f"20 日年化波动率 {-value * 100:.2f}%"
    return f"{label} {value:.0f}/2"


def rank_signal_rows(rows: Iterable[Mapping], strategy: dict | None = None) -> list[dict]:
    materialized = [dict(row) for row in rows]
    feature_map = {
        str(row.get("symbol") or ""): row.get("signal_features")
        for row in materialized
        if str(row.get("symbol") or "") and isinstance(row.get("signal_features"), dict)
    }
    scores, ranks = _score_details(feature_map, strategy)
    chase_state = (strategy or {}).get("parameters", {}).get("chase_risk_pct", {})
    chase_threshold = _finite_number(chase_state.get("value"), 7.0) if chase_state.get("enabled") else 7.0
    ranked = []
    for row in materialized:
        symbol = str(row.get("symbol") or "")
        if symbol not in scores:
            continue
        feature = feature_map[symbol]
        score = round(scores[symbol], 4)
        percent = _finite_number(row.get("percent"), _finite_number(feature.get("latest_return")) * 100)
        reasons = []
        risk_level = "中"
        if percent >= chase_threshold:
            risk_level = "高"
            reasons.append(f"当日涨幅 {percent:.2f}% 超过追高阈值 {chase_threshold:g}%")
        strongest = sorted(
            SIGNAL_FEATURE_FIELDS,
            key=lambda field: (ranks[field][symbol], field),
            reverse=True,
        )
        reasons.extend(_factor_reason(field, _finite_number(feature[field])) for field in strongest[:3])
        if score >= 75:
            rating, emoji = "强烈关注", "🔥"
        elif score >= 60:
            rating, emoji = "积极关注", "⭐"
        elif score >= 45:
            rating, emoji = "值得关注", "🌟"
        else:
            rating, emoji = "观察", "📊"
        ranked.append(
            {
                **row,
                "score": score,
                "signal_score": score,
                "signal_model": SIGNAL_MODEL_ID,
                "signal_feature_ranks": {field: round(ranks[field][symbol], 6) for field in SIGNAL_FEATURE_FIELDS},
                "rating": rating,
                "rating_emoji": emoji,
                "reasons": reasons,
                "risk_level": risk_level,
            }
        )
    ranked.sort(key=lambda item: (_finite_number(item.get("score")), str(item.get("symbol"))), reverse=True)
    for index, row in enumerate(ranked, 1):
        row["signal_rank"] = index
    return ranked


def select_ranked_signals(
    rows: Iterable[Mapping],
    limit: int,
    *,
    strategy: dict | None = None,
) -> list[dict]:
    if limit <= 0:
        return []
    ranked = sorted(
        (dict(row) for row in rows),
        key=lambda item: (_finite_number(item.get("score", item.get("signal_score"))), str(item.get("symbol"))),
        reverse=True,
    )
    signal_config = (strategy or {}).get("signal", {})
    hot_cap = max(0, int(signal_config.get("max_hot_candidates", 2)))
    chase_state = (strategy or {}).get("parameters", {}).get("chase_risk_pct", {})
    chase_threshold = _finite_number(chase_state.get("value"), 7.0) if chase_state.get("enabled") else 7.0
    hot = [item for item in ranked if _finite_number(item.get("percent")) >= chase_threshold]
    moderate = [item for item in ranked if 0 <= _finite_number(item.get("percent")) < chase_threshold]
    weak = [item for item in ranked if _finite_number(item.get("percent")) < 0]
    selected: list[dict] = []
    used: set[str] = set()

    def add(items: Iterable[dict], count: int) -> None:
        for item in items:
            if len(selected) >= limit or count <= 0:
                return
            symbol = str(item.get("symbol") or "")
            if not symbol or symbol in used:
                continue
            selected.append(item)
            used.add(symbol)
            count -= 1

    add(hot, min(hot_cap, limit))
    add(moderate, limit - len(selected))
    add(weak, limit - len(selected))
    add(ranked, limit - len(selected))
    return selected[:limit]


def signal_contract(strategy: dict | None = None, *, cutoff: date | datetime | str | None = None) -> dict:
    config = (strategy or {}).get("signal", {})
    cutoff_date = _date_value(cutoff) if cutoff is not None else None
    return {
        "model": SIGNAL_MODEL_ID,
        "data_cutoff": SIGNAL_DATA_CUTOFF,
        "cutoff_date_exclusive": cutoff_date.isoformat() if cutoff_date else None,
        "run_time": str(config.get("run_time") or "08:00"),
        "minimum_history_rows": max(MINIMUM_HISTORY_ROWS, int(config.get("minimum_history_rows", MINIMUM_HISTORY_ROWS))),
        "factor_weights": factor_weights(strategy),
        "max_hot_candidates": max(0, int(config.get("max_hot_candidates", 2))),
    }
