from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .market_regime import filter_absolute_momentum, normalize_market_regime_decision
from .pipeline import PipelineRunner, StageInput, StageOutput
from .utils import number


ENTRY_PIPELINE_VERSION = "1.0.0"


def _fact(stage_input: StageInput, kind: str) -> dict:
    for item in reversed(stage_input.upstream_facts):
        if item.get("kind") == kind:
            return item
    return {}


@dataclass(frozen=True)
class CandidateNormalizationStage:
    raw_candidates: tuple[dict, ...]
    name: str = "candidate_normalization"
    component_version: str = "1.0.0"

    def evaluate(self, stage_input: StageInput) -> StageOutput:
        normalized: list[dict] = []
        used: set[str] = set()
        rejected = 0
        for rank, item in enumerate(self.raw_candidates, 1):
            symbol = str(item.get("symbol") or "").strip()
            price = number(item.get("price"), default=number(item.get("entry_price")))
            if len(symbol) != 6 or not symbol.isdigit() or symbol in used or price <= 0:
                rejected += 1
                continue
            used.add(symbol)
            raw_score = item.get("score", item.get("signal_score"))
            score = number(raw_score, default=max(0.0, 1.0 - (rank - 1) / 10))
            if score > 1:
                score /= 100
            normalized.append(
                {
                    "symbol": symbol,
                    "name": item.get("name") or symbol,
                    "price": price,
                    "score": min(1.0, max(0.0, score)),
                    "rank": rank,
                    "signal_features": dict(item.get("signal_features") or {}),
                }
            )
        normalized.sort(key=lambda item: (-item["score"], item["symbol"]))
        return StageOutput(
            stage=self.name,
            component_version=self.component_version,
            facts=({"kind": "normalized_candidates", "items": normalized},),
            diagnostics=({"accepted": len(normalized), "rejected": rejected},),
        )


@dataclass(frozen=True)
class PortfolioCapacityStage:
    max_positions: int
    target_weight_pct: float
    name: str = "portfolio_capacity"
    component_version: str = "1.0.0"

    def evaluate(self, stage_input: StageInput) -> StageOutput:
        candidates = list(_fact(stage_input, "regime_candidates").get("items") or [])
        allocation = _fact(stage_input, "market_regime")
        limit = max(1, min(10, int(self.max_positions)))
        exposure = number(allocation.get("target_exposure_pct"), default=100.0)
        target_weight = max(0.01, number(self.target_weight_pct, default=10.0))
        exposure_limit = max(0, int(exposure // target_weight))
        limit = min(limit, exposure_limit)
        return StageOutput(
            stage=self.name,
            component_version=self.component_version,
            facts=({"kind": "capacity_candidates", "items": candidates[:limit], "limit": limit},),
            diagnostics=({"input": len(candidates), "within_capacity": min(len(candidates), limit)},),
        )


@dataclass(frozen=True)
class MarketRegimeStage:
    strategy: dict
    decision: dict
    name: str = "market_regime"
    component_version: str = "1.0.0"

    def evaluate(self, stage_input: StageInput) -> StageOutput:
        candidates = list(_fact(stage_input, "normalized_candidates").get("items") or [])
        decision = normalize_market_regime_decision(self.decision, self.strategy)
        admitted = filter_absolute_momentum(candidates, self.strategy, decision)
        return StageOutput(
            stage=self.name,
            component_version=self.component_version,
            facts=(
                {"kind": "market_regime", **decision},
                {"kind": "regime_candidates", "items": admitted},
            ),
            diagnostics=(
                {
                    "state": decision["state"],
                    "target_exposure_pct": decision["target_exposure_pct"],
                    "input": len(candidates),
                    "absolute_momentum_admitted": len(admitted),
                },
            ),
        )


@dataclass(frozen=True)
class RiskAdmissionStage:
    enabled: bool
    trading_mode: str
    name: str = "risk_admission"
    component_version: str = "1.0.0"

    def evaluate(self, stage_input: StageInput) -> StageOutput:
        candidates = list(_fact(stage_input, "capacity_candidates").get("items") or [])
        admitted = candidates if self.enabled and self.trading_mode == "RUNNING" else []
        return StageOutput(
            stage=self.name,
            component_version=self.component_version,
            facts=({"kind": "admitted_candidates", "items": admitted},),
            diagnostics=(
                {
                    "enabled": self.enabled,
                    "trading_mode": self.trading_mode,
                    "admitted": len(admitted),
                },
            ),
        )


def run_entry_pipeline(
    strategy: dict,
    account: dict,
    candidates: Iterable[dict],
    *,
    run_id: str,
    as_of: str,
    market_regime: dict,
) -> tuple[list[dict], list[dict], list[dict]]:
    config = account["portfolio_config"]
    decision = normalize_market_regime_decision(market_regime, strategy)
    runner = PipelineRunner(
        [
            CandidateNormalizationStage(tuple(dict(item) for item in candidates)),
            MarketRegimeStage(strategy, decision),
            PortfolioCapacityStage(int(config["max_positions"]), number(config["target_weight_pct"])),
            RiskAdmissionStage(bool(config.get("enabled", True)), str(account.get("trading_mode", "RUNNING"))),
        ]
    )
    outputs = runner.run(
        StageInput(
            run_id=run_id,
            strategy_id=str(strategy.get("id") or account.get("strategy_id") or ""),
            strategy_version=int(strategy.get("revision") or account.get("strategy_revision") or 1),
            as_of=as_of,
            market_snapshot_id=f"daily:{as_of[:10]}",
            portfolio_snapshot_id=f"{account.get('id')}:{account.get('control_epoch', 1)}",
        )
    )
    normalized = list(outputs[0].facts[0]["items"])
    admitted = list(outputs[-1].facts[0]["items"])
    trace = [
        {
            "stage": output.stage,
            "component_version": output.component_version,
            "schema_version": output.schema_version,
            "diagnostics": list(output.diagnostics),
        }
        for output in outputs
    ]
    return normalized, admitted, trace
