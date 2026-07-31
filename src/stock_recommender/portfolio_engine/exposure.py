"""Portfolio exposure calculations and constraints."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

from ..pipeline import PipelineContractError, StageInput, StageOutput
from .config import (
    SYSTEM_MAX_GROSS_EXPOSURE_PCT,
    SYSTEM_MAX_LONG_EXPOSURE_PCT,
    SYSTEM_MAX_LONG_POSITION_PCT,
    SYSTEM_MAX_NET_EXPOSURE_PCT,
    SYSTEM_MAX_POSITIONS,
    SYSTEM_MAX_SHORT_EXPOSURE_PCT,
    SYSTEM_MAX_SHORT_POSITION_PCT,
    VALID_EXPOSURE_MODES,
    ExposurePolicy,
)
from .contracts import PositionSide, TargetPosition


NET_TARGETS_FACT = "net_targets"


@dataclass(frozen=True)
class ExposureDiagnostic:
    gross_exposure_pct: float = 0.0
    net_exposure_pct: float = 0.0
    long_exposure_pct: float = 0.0
    short_exposure_pct: float = 0.0
    rejections: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "rejections",
            tuple(MappingProxyType(dict(item)) for item in self.rejections),
        )

    def __deepcopy__(self, memo: dict[int, object]) -> ExposureDiagnostic:
        memo[id(self)] = self
        return self


@dataclass(frozen=True)
class _Limits:
    mode: str
    max_positions: int
    gross: float
    net: float
    long: float
    short: float
    long_position: float
    short_position: float


def _finite_nonnegative(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite nonnegative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field_name} must be a finite nonnegative number")
    return number


def _effective_limits(policy: ExposurePolicy) -> _Limits:
    if not isinstance(policy, ExposurePolicy):
        raise ValueError("policy must be an ExposurePolicy")
    mode = policy.mode
    if mode not in VALID_EXPOSURE_MODES:
        raise ValueError(f"unsupported exposure mode: {mode}")
    if (
        isinstance(policy.max_positions, bool)
        or not isinstance(policy.max_positions, int)
        or policy.max_positions < 0
    ):
        raise ValueError("max_positions must be a nonnegative integer")

    gross = min(
        SYSTEM_MAX_GROSS_EXPOSURE_PCT,
        _finite_nonnegative(policy.max_gross_exposure_pct, "max_gross_exposure_pct"),
    )
    net = min(
        SYSTEM_MAX_NET_EXPOSURE_PCT,
        _finite_nonnegative(policy.max_net_exposure_pct, "max_net_exposure_pct"),
    )
    long = min(
        SYSTEM_MAX_LONG_EXPOSURE_PCT,
        _finite_nonnegative(policy.max_long_exposure_pct, "max_long_exposure_pct"),
    )
    short = min(
        SYSTEM_MAX_SHORT_EXPOSURE_PCT,
        _finite_nonnegative(policy.max_short_exposure_pct, "max_short_exposure_pct"),
    )
    long_position = min(
        SYSTEM_MAX_LONG_POSITION_PCT,
        _finite_nonnegative(policy.max_long_position_pct, "max_long_position_pct"),
    )
    short_position = min(
        SYSTEM_MAX_SHORT_POSITION_PCT,
        _finite_nonnegative(policy.max_short_position_pct, "max_short_position_pct"),
    )
    if mode == "LONG_ONLY":
        gross = min(gross, 100.0)
        net = min(net, 100.0)
        long = min(long, 100.0)
        short = 0.0
        short_position = 0.0
    elif mode == "LONG_LEVERAGED":
        gross = min(gross, 120.0)
        net = min(net, 120.0)
        long = min(long, 120.0)
        short = 0.0
        short_position = 0.0
    return _Limits(
        mode=mode,
        max_positions=min(policy.max_positions, SYSTEM_MAX_POSITIONS),
        gross=gross,
        net=net,
        long=long,
        short=short,
        long_position=long_position,
        short_position=short_position,
    )


def _target_sort_key(item: TargetPosition) -> tuple[float, str, str]:
    return (-item.signal_score, item.symbol, item.side.value)


def _safe_identity(item: object) -> tuple[str, str]:
    symbol = getattr(item, "symbol", "")
    side = getattr(item, "side", "")
    symbol_text = symbol if isinstance(symbol, str) else type(symbol).__name__
    side_text = side.value if isinstance(side, PositionSide) else str(side)
    return symbol_text, side_text


def _valid_target(item: object) -> bool:
    if not isinstance(item, TargetPosition):
        return False
    if not isinstance(item.symbol, str) or not item.symbol.strip():
        return False
    if item.side not in (PositionSide.LONG, PositionSide.SHORT):
        return False
    if not isinstance(item.model_id, str) or not isinstance(item.thesis_id, str):
        return False
    for value in (item.target_weight_pct, item.signal_score):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            number = float(value)
        except (OverflowError, TypeError, ValueError):
            return False
        if not math.isfinite(number):
            return False
    return item.target_weight_pct > 0


def _adjustment(
    item: TargetPosition,
    reason: str,
    original_weight: float,
    adjusted_weight: float,
) -> dict[str, object]:
    return {
        "symbol": item.symbol,
        "side": item.side.value,
        "reason": reason,
        "original_weight_pct": float(original_weight),
        "adjusted_weight_pct": float(adjusted_weight),
    }


def _rejection(item: object, reason: str) -> dict[str, object]:
    symbol, side = _safe_identity(item)
    return {"symbol": symbol, "side": side, "reason": reason}


def _scale(
    targets: list[TargetPosition],
    factor: float,
    reason: str,
    rejections: list[dict[str, object]],
    *,
    side: PositionSide | None = None,
    maximum_total: float,
) -> list[TargetPosition]:
    selected = [
        item for item in targets if side is None or item.side == side
    ]
    while math.fsum(
        item.target_weight_pct * factor for item in selected
    ) > maximum_total:
        factor = math.nextafter(factor, 0.0)
    adjusted: list[TargetPosition] = []
    for item in targets:
        if side is not None and item.side != side:
            adjusted.append(item)
            continue
        new_weight = item.target_weight_pct * factor
        rejections.append(
            _adjustment(
                item,
                reason,
                item.target_weight_pct,
                new_weight,
            )
        )
        if new_weight > 0:
            adjusted.append(replace(item, target_weight_pct=new_weight))
    return adjusted


def _side_total(targets: list[TargetPosition], side: PositionSide) -> float:
    return math.fsum(
        item.target_weight_pct for item in targets if item.side == side
    )


def _diagnostic(
    targets: list[TargetPosition],
    rejections: list[dict[str, object]],
) -> ExposureDiagnostic:
    long_exposure = _side_total(targets, PositionSide.LONG)
    short_exposure = _side_total(targets, PositionSide.SHORT)
    return ExposureDiagnostic(
        gross_exposure_pct=long_exposure + short_exposure,
        net_exposure_pct=long_exposure - short_exposure,
        long_exposure_pct=long_exposure,
        short_exposure_pct=short_exposure,
        rejections=tuple(rejections),
    )


def allocate_exposure(
    targets: tuple[TargetPosition, ...] | list[TargetPosition],
    policy: ExposurePolicy,
) -> tuple[tuple[TargetPosition, ...], ExposureDiagnostic]:
    """Apply mode and exposure limits without increasing any target."""

    limits = _effective_limits(policy)
    materialized = tuple(targets)
    invalid = sorted(
        (item for item in materialized if not _valid_target(item)),
        key=_safe_identity,
    )
    rejections = [_rejection(item, "INVALID_TARGET") for item in invalid]
    admitted = sorted(
        (item for item in materialized if _valid_target(item)),
        key=_target_sort_key,
    )
    symbols = [item.symbol for item in admitted]
    if len(symbols) != len(set(symbols)):
        raise ValueError("exposure targets must contain at most one side per symbol")

    if limits.mode in ("LONG_ONLY", "LONG_LEVERAGED"):
        allowed: list[TargetPosition] = []
        for item in admitted:
            if item.side == PositionSide.SHORT:
                rejections.append(_rejection(item, "MODE_DISALLOWS_SHORT"))
            else:
                allowed.append(item)
        admitted = allowed

    capped: list[TargetPosition] = []
    for item in admitted:
        cap = (
            limits.long_position
            if item.side == PositionSide.LONG
            else limits.short_position
        )
        if item.target_weight_pct > cap:
            rejections.append(
                _adjustment(
                    item,
                    "POSITION_CAP",
                    item.target_weight_pct,
                    cap,
                )
            )
            if cap > 0:
                capped.append(replace(item, target_weight_pct=cap))
        else:
            capped.append(item)
    admitted = capped

    selected = admitted[: limits.max_positions]
    for item in admitted[limits.max_positions :]:
        rejections.append(_rejection(item, "MAX_POSITIONS"))
    admitted = selected

    for side, cap in (
        (PositionSide.LONG, limits.long),
        (PositionSide.SHORT, limits.short),
    ):
        total = _side_total(admitted, side)
        if total > cap:
            admitted = _scale(
                admitted,
                cap / total,
                "DIRECTION_CAP",
                rejections,
                side=side,
                maximum_total=cap,
            )

    gross = _side_total(admitted, PositionSide.LONG) + _side_total(
        admitted, PositionSide.SHORT
    )
    if gross > limits.gross:
        admitted = _scale(
            admitted,
            limits.gross / gross,
            "GROSS_CAP",
            rejections,
            maximum_total=limits.gross,
        )

    long_exposure = _side_total(admitted, PositionSide.LONG)
    short_exposure = _side_total(admitted, PositionSide.SHORT)
    net_exposure = long_exposure - short_exposure
    if net_exposure > limits.net:
        target_long = short_exposure + limits.net
        admitted = _scale(
            admitted,
            target_long / long_exposure,
            "NET_CAP",
            rejections,
            side=PositionSide.LONG,
            maximum_total=target_long,
        )
    elif net_exposure < -limits.net:
        target_short = long_exposure + limits.net
        admitted = _scale(
            admitted,
            target_short / short_exposure,
            "NET_CAP",
            rejections,
            side=PositionSide.SHORT,
            maximum_total=target_short,
        )

    admitted = sorted(admitted, key=_target_sort_key)
    diagnostic = _diagnostic(admitted, rejections)
    for value in (
        diagnostic.gross_exposure_pct,
        diagnostic.net_exposure_pct,
        diagnostic.long_exposure_pct,
        diagnostic.short_exposure_pct,
    ):
        if not math.isfinite(value):
            raise ValueError("exposure allocation produced a nonfinite diagnostic")
    return tuple(admitted), diagnostic


def _single_net_target_fact(stage_input: StageInput) -> Mapping[str, object] | None:
    matching = [
        fact
        for fact in stage_input.upstream_facts
        if isinstance(fact, Mapping) and fact.get("kind") == NET_TARGETS_FACT
    ]
    if len(matching) > 1:
        raise PipelineContractError(f"duplicate upstream fact: {NET_TARGETS_FACT}")
    return matching[0] if matching else None


class ExposureBudgetStage:
    """Pure pipeline stage for target exposure allocation."""

    name = "exposure_budget"
    component_version = "1.0.0"

    def __init__(self, policy: ExposurePolicy):
        self._policy = policy

    def evaluate(self, stage_input: StageInput) -> StageOutput:
        fact = _single_net_target_fact(stage_input)
        items: object = () if fact is None else fact.get("items", ())
        if not isinstance(items, (list, tuple)):
            raise PipelineContractError("net_targets items must be a sequence")
        targets, diagnostic = allocate_exposure(items, self._policy)
        return StageOutput(
            stage=self.name,
            component_version=self.component_version,
            facts=(
                {"kind": "exposure_targets", "items": targets},
                {
                    "kind": "exposure_diagnostic",
                    "gross_exposure_pct": diagnostic.gross_exposure_pct,
                    "net_exposure_pct": diagnostic.net_exposure_pct,
                    "long_exposure_pct": diagnostic.long_exposure_pct,
                    "short_exposure_pct": diagnostic.short_exposure_pct,
                    "rejections": tuple(
                        dict(item) for item in diagnostic.rejections
                    ),
                },
            ),
        )
