"""Construction of target portfolio positions from domain inputs."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

from ..pipeline import PipelineContractError, StageInput, StageOutput
from .contracts import PositionSide, SignalCandidate, TargetPosition


SIGNAL_CANDIDATES_FACT = "signal_candidates"
NET_TARGETS_FACT = "net_targets"


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _representative(candidates: list[SignalCandidate]) -> SignalCandidate:
    if not candidates:
        raise RuntimeError("winning target direction has no signal candidate")
    return min(
        candidates,
        key=lambda item: (-item.score, item.model_id, item.thesis_id),
    )


def net_signal_candidates(
    signals: Iterable[SignalCandidate],
) -> tuple[TargetPosition, ...]:
    """Net immutable signed signal weights into one target per symbol."""

    grouped: dict[
        str,
        dict[PositionSide, list[SignalCandidate]],
    ] = {}
    for item in signals:
        if not isinstance(item, SignalCandidate):
            raise ValueError("signals must contain SignalCandidate values")
        weight = _finite_number(item.requested_weight_pct)
        score = _finite_number(item.score)
        if weight is None or weight <= 0 or score is None:
            raise ValueError("signal weight and score must be finite and weight positive")
        if not isinstance(item.symbol, str) or not item.symbol.strip():
            raise ValueError("signal symbol must be non-empty")
        if item.side not in (PositionSide.LONG, PositionSide.SHORT):
            raise ValueError("signal side must be LONG or SHORT")
        if not isinstance(item.model_id, str) or not isinstance(item.thesis_id, str):
            raise ValueError("signal model_id and thesis_id must be strings")
        directions = grouped.setdefault(
            item.symbol,
            {PositionSide.LONG: [], PositionSide.SHORT: []},
        )
        directions[item.side].append(item)

    targets: list[TargetPosition] = []
    for symbol in sorted(grouped):
        directions = grouped[symbol]
        try:
            long_weight = math.fsum(
                sorted(
                    item.requested_weight_pct
                    for item in directions[PositionSide.LONG]
                )
            )
            short_weight = math.fsum(
                sorted(
                    item.requested_weight_pct
                    for item in directions[PositionSide.SHORT]
                )
            )
        except OverflowError as exc:
            raise ValueError("aggregated signal weight must remain finite") from exc
        if not math.isfinite(long_weight) or not math.isfinite(short_weight):
            raise ValueError("aggregated signal weight must remain finite")
        net_weight = long_weight - short_weight
        if not math.isfinite(net_weight):
            raise ValueError("net signal weight must remain finite")
        if net_weight == 0:
            continue
        side = PositionSide.LONG if net_weight > 0 else PositionSide.SHORT
        representative = _representative(directions[side])
        targets.append(
            TargetPosition(
                symbol=symbol,
                side=side,
                target_weight_pct=abs(net_weight),
                signal_score=representative.score,
                model_id=representative.model_id,
                thesis_id=representative.thesis_id,
            )
        )

    return tuple(
        sorted(
            targets,
            key=lambda item: (-item.signal_score, item.symbol, item.side.value),
        )
    )


def _single_upstream_fact(
    stage_input: StageInput,
    kind: str,
) -> Mapping[str, object] | None:
    matching = [
        fact
        for fact in stage_input.upstream_facts
        if isinstance(fact, Mapping) and fact.get("kind") == kind
    ]
    if len(matching) > 1:
        raise PipelineContractError(f"duplicate upstream fact: {kind}")
    return matching[0] if matching else None


class TargetNettingStage:
    """Pure pipeline stage for deterministic same-symbol signal netting."""

    name = "target_netting"
    component_version = "1.0.0"

    def evaluate(self, stage_input: StageInput) -> StageOutput:
        fact = _single_upstream_fact(stage_input, SIGNAL_CANDIDATES_FACT)
        items: object = () if fact is None else fact.get("items", ())
        if not isinstance(items, (list, tuple)):
            raise PipelineContractError("signal_candidates items must be a sequence")
        targets = net_signal_candidates(items)
        return StageOutput(
            stage=self.name,
            component_version=self.component_version,
            facts=({"kind": NET_TARGETS_FACT, "items": targets},),
        )
