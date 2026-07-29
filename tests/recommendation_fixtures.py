from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Iterable

from stock_recommender.recommendation import RecommendationPlan
from stock_recommender.utils import beijing_now


FULL_EXPOSURE_MARKET_REGIME = {
    "model": "test_full_exposure",
    "state": "RISK_ON",
    "label": "强势",
    "target_exposure_pct": 100.0,
    "sample_size": 1,
    "breadth20_pct": 100.0,
    "breadth60_pct": 100.0,
    "trend_breadth_pct": 100.0,
    "median_momentum20_pct": 5.0,
    "reason": "测试显式指定满仓环境",
}


def candidates_with_positive_momentum(rows: Iterable[dict]) -> list[dict]:
    candidates = []
    for row in rows:
        item = deepcopy(dict(row))
        item.setdefault("signal_features", {"momentum20": 0.05, "momentum60": 0.08, "trend": 2})
        candidates.append(item)
    return candidates


def make_recommendation_plan(
    rows: Iterable[dict],
    *,
    now: datetime,
    board_code: str = "BK0800",
    board_name: str = "人工智能",
    market_regime: dict | None = None,
) -> RecommendationPlan:
    selected = tuple(candidates_with_positive_momentum(rows))
    return RecommendationPlan(
        generated_at=beijing_now(now),
        universe_type="board",
        watchlist_size=0,
        sector_filters=(),
        board_code=board_code,
        board_name=board_name,
        sources=tuple(sorted({str(item.get("source")) for item in selected if item.get("source")})),
        fetch_error=None,
        analyzed_count=len(selected),
        data_quality={
            "status": "READY",
            "reason": "测试数据完整",
            "raw_count": len(selected),
            "basic_count": len(selected),
            "history_ready_count": len(selected),
            "strategy_filtered_count": len(selected),
            "analyzed_count": len(selected),
            "selected_count": len(selected),
        },
        market_regime=deepcopy(market_regime or FULL_EXPOSURE_MARKET_REGIME),
        signal_contract={"model": "factor_rank_v1", "data_cutoff": "previous_close"},
        candidates=tuple(deepcopy(item) for item in selected),
        selected_candidates=selected,
    )
