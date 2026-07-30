from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping

from .market_regime import evaluate_market_regime, filter_absolute_momentum
from .selection import select_agent_candidates
from .signal_engine import signal_contract
from .utils import number


@dataclass(frozen=True, slots=True)
class RecommendationPlan:
    """Structured output of one recommendation run.

    This object is the boundary between data collection/selection and every
    downstream adapter. Reports render it, tracking persists it, and the
    portfolio consumes its selected candidates. No downstream component needs
    to recover execution data from human-readable text.
    """

    generated_at: datetime
    universe_type: str
    watchlist_size: int
    sector_filters: tuple[str, ...]
    board_code: str
    board_name: str
    sources: tuple[str, ...]
    fetch_error: str | None
    analyzed_count: int
    data_quality: dict
    market_regime: dict
    signal_contract: dict
    candidates: tuple[dict, ...]
    selected_candidates: tuple[dict, ...]
    market: str = "cn"

    def __post_init__(self) -> None:
        candidate_symbols = tuple(str(item.get("symbol") or "") for item in self.candidates)
        selected_symbols = tuple(str(item.get("symbol") or "") for item in self.selected_candidates)
        if len(candidate_symbols) != len(set(candidate_symbols)):
            raise ValueError("recommendation candidates must have unique symbols")
        if len(selected_symbols) != len(set(selected_symbols)):
            raise ValueError("selected recommendation candidates must have unique symbols")
        if not set(selected_symbols).issubset(candidate_symbols):
            raise ValueError("selected recommendation candidates must be present in candidates")
        if not isinstance(self.market_regime, dict) or not self.market_regime.get("state"):
            raise ValueError("recommendation plan requires an explicit market regime")
        if not isinstance(self.data_quality, dict) or self.data_quality.get("status") not in {"READY", "BLOCKED"}:
            raise ValueError("recommendation plan requires explicit data quality")


@dataclass(frozen=True, slots=True)
class RecommendationOutput:
    report: str
    plan: RecommendationPlan


def build_recommendation_plan(
    *,
    generated_at: datetime,
    analyses: Iterable[Mapping],
    strategy: Mapping | None,
    board_code: str,
    board_name: str,
    market_analyses: Iterable[Mapping] | None = None,
    watchlist_size: int = 0,
    sector_filters: Iterable[str] = (),
    fetch_error: str | None = None,
    data_quality: Mapping | None = None,
    candidate_limit: int = 8,
    selection_limit: int = 3,
    market: str = "cn",
) -> RecommendationPlan:
    analyzed = [deepcopy(dict(item)) for item in analyses]
    market_rows = (
        [deepcopy(dict(item)) for item in market_analyses]
        if market_analyses is not None
        else analyzed
    )
    quality = deepcopy(dict(data_quality or {}))
    quality.setdefault("status", "READY")
    quality.setdefault("reason", "数据覆盖满足策略运行要求")
    quality["analyzed_count"] = len(analyzed)
    quality["market_analyzed_count"] = len(market_rows)
    if quality["status"] == "BLOCKED":
        decision = evaluate_market_regime([], strategy)
        decision.update(
            {
                "state": "UNKNOWN",
                "label": "数据不足",
                "target_exposure_pct": 0.0,
                "sample_size": len(market_rows),
                "reason": str(quality.get("reason") or "数据覆盖不足"),
            }
        )
        eligible = []
    else:
        decision = evaluate_market_regime(market_rows, strategy)
        eligible = filter_absolute_momentum(analyzed, strategy, decision)
    normalized_selection_limit = max(0, int(selection_limit))
    normalized_candidate_limit = max(normalized_selection_limit, max(0, int(candidate_limit)))
    candidates = select_agent_candidates(eligible, normalized_candidate_limit, strategy=strategy)
    selected = select_agent_candidates(eligible, normalized_selection_limit, strategy=strategy)
    quality["absolute_momentum_count"] = len(eligible)
    quality["candidate_count"] = len(candidates)
    quality["selected_count"] = len(selected)
    sources = sorted({str(item.get("source")) for item in candidates if item.get("source")})
    normalized_sectors = tuple(str(item) for item in sector_filters if str(item))
    return RecommendationPlan(
        generated_at=generated_at,
        universe_type="watchlist" if watchlist_size else "board",
        watchlist_size=max(0, int(watchlist_size)),
        sector_filters=normalized_sectors,
        board_code=str(board_code),
        board_name=str(board_name),
        sources=tuple(sources),
        fetch_error=fetch_error,
        analyzed_count=len(analyzed),
        data_quality=quality,
        market_regime=deepcopy(decision),
        signal_contract=signal_contract(dict(strategy or {}), cutoff=generated_at.date()),
        candidates=tuple(deepcopy(item) for item in candidates),
        selected_candidates=tuple(deepcopy(item) for item in selected),
        market=str(market),
    )


def recommendation_tracking_entries(plan: RecommendationPlan) -> list[dict]:
    entries: list[dict] = []
    for item in plan.selected_candidates:
        price = number(item.get("price"), default=number(item.get("entry_price")))
        if price <= 0:
            continue
        entries.append(
            {
                "symbol": str(item.get("symbol") or ""),
                "name": str(item.get("name") or item.get("symbol") or ""),
                "entry_price": price,
                "initial_change_pct": number(item.get("percent", item.get("change_percent"))),
                "max_observed_price": price,
                "min_observed_price": price,
                "last_price": price,
                "last_volume": None,
                "score": number(item.get("score", item.get("signal_score")), default=0.0),
                "signal_features": deepcopy(dict(item.get("signal_features") or {})),
                "market": plan.market,
            }
        )
    return entries
