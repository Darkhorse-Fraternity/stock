from __future__ import annotations

import statistics
from copy import deepcopy
from typing import Iterable, Mapping

from .parameters import default_allocation_config, normalize_allocation_config


MARKET_REGIME_MODEL_ID = str(default_allocation_config()["model"])
MARKET_REGIME_STATES = {"RISK_ON", "NEUTRAL", "RISK_OFF", "UNKNOWN"}
_STATE_LABELS = {
    "RISK_ON": "强势",
    "NEUTRAL": "震荡",
    "RISK_OFF": "弱势",
    "UNKNOWN": "数据不足",
}


def _finite(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result and abs(result) != float("inf") else default


def allocation_config(strategy: Mapping | None = None) -> dict:
    return normalize_allocation_config((strategy or {}).get("allocation"))


def _features(row: Mapping) -> Mapping | None:
    nested = row.get("signal_features")
    if isinstance(nested, Mapping):
        return nested
    if all(field in row for field in ("momentum20", "momentum60", "trend")):
        return row
    return None


def _exposure(config: Mapping, state: str) -> float:
    field = {
        "RISK_ON": "risk_on_exposure_pct",
        "NEUTRAL": "neutral_exposure_pct",
        "RISK_OFF": "risk_off_exposure_pct",
        "UNKNOWN": "unknown_exposure_pct",
    }[state]
    return min(100.0, max(0.0, _finite(config.get(field))))


def evaluate_market_regime(rows: Iterable[Mapping], strategy: Mapping | None = None) -> dict:
    """Evaluate sector breadth using only point-in-time signal features.

    The result is deliberately independent from ranking and execution.  It is a
    fact consumed by both the live portfolio Pipeline and the replay engine.
    """

    config = allocation_config(strategy)
    samples = [feature for row in rows if (feature := _features(row)) is not None]
    minimum = max(1, int(_finite(config.get("minimum_universe_size"), 1)))
    if not bool(config.get("enabled", True)):
        state = "RISK_ON"
        reason = "板块状态控制已关闭"
        samples = samples or []
    elif len(samples) < minimum:
        state = "UNKNOWN"
        reason = f"有效样本 {len(samples)} 只，少于最低要求 {minimum} 只"
    else:
        breadth20 = sum(_finite(item.get("momentum20")) > 0 for item in samples) / len(samples) * 100
        breadth60 = sum(_finite(item.get("momentum60")) > 0 for item in samples) / len(samples) * 100
        trend_breadth = sum(_finite(item.get("trend")) >= 1 for item in samples) / len(samples) * 100
        median20 = statistics.median(_finite(item.get("momentum20")) for item in samples) * 100
        threshold = _finite(config.get("breadth_threshold_pct"), 50.0)
        signals = sum(value >= threshold for value in (breadth20, breadth60, trend_breadth))
        if signals >= int(_finite(config.get("risk_on_min_signals"), 3)):
            state = "RISK_ON"
        elif signals >= int(_finite(config.get("neutral_min_signals"), 2)):
            state = "NEUTRAL"
        else:
            state = "RISK_OFF"
        reason = f"三项广度信号通过 {signals}/3，20 日动量中位数 {median20:+.2f}%"
        return {
            "model": MARKET_REGIME_MODEL_ID,
            "state": state,
            "label": _STATE_LABELS[state],
            "target_exposure_pct": _exposure(config, state),
            "sample_size": len(samples),
            "breadth20_pct": round(breadth20, 4),
            "breadth60_pct": round(breadth60, 4),
            "trend_breadth_pct": round(trend_breadth, 4),
            "median_momentum20_pct": round(median20, 4),
            "reason": reason,
        }

    return {
        "model": MARKET_REGIME_MODEL_ID,
        "state": state,
        "label": _STATE_LABELS[state],
        "target_exposure_pct": _exposure(config, state),
        "sample_size": len(samples),
        "breadth20_pct": None,
        "breadth60_pct": None,
        "trend_breadth_pct": None,
        "median_momentum20_pct": None,
        "reason": reason,
    }


def normalize_market_regime_decision(value: Mapping | None, strategy: Mapping | None = None) -> dict:
    config = allocation_config(strategy)
    raw = value if isinstance(value, Mapping) else {}
    state = str(raw.get("state") or "UNKNOWN").upper()
    if state not in MARKET_REGIME_STATES:
        state = "UNKNOWN"
    normalized = {
        "model": str(raw.get("model") or MARKET_REGIME_MODEL_ID),
        "state": state,
        "label": _STATE_LABELS[state],
        "target_exposure_pct": min(
            100.0,
            max(0.0, _finite(raw.get("target_exposure_pct"), _exposure(config, state))),
        ),
        "sample_size": max(0, int(_finite(raw.get("sample_size")))),
        "reason": str(raw.get("reason") or "未提供板块状态明细"),
    }
    for field in ("breadth20_pct", "breadth60_pct", "trend_breadth_pct", "median_momentum20_pct"):
        normalized[field] = None if raw.get(field) is None else round(_finite(raw.get(field)), 4)
    return normalized


def filter_absolute_momentum(
    rows: Iterable[Mapping],
    strategy: Mapping | None,
    decision: Mapping,
) -> list[dict]:
    normalized = normalize_market_regime_decision(decision, strategy)
    if normalized["target_exposure_pct"] <= 0:
        return []
    config = allocation_config(strategy)
    if not bool(config.get("enabled", True)):
        return [deepcopy(dict(row)) for row in rows]
    minimum_momentum = _finite(config.get("minimum_candidate_momentum20_pct")) / 100
    minimum_trend = _finite(config.get("minimum_candidate_trend"), 1.0)
    admitted = []
    for row in rows:
        feature = _features(row)
        if feature is None:
            continue
        if _finite(feature.get("momentum20")) < minimum_momentum:
            continue
        if _finite(feature.get("trend")) < minimum_trend:
            continue
        admitted.append(deepcopy(dict(row)))
    return admitted
