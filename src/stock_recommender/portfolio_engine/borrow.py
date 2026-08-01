"""Borrow eligibility and availability rules for short positions."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from ..pipeline import PipelineContractError, StageInput, StageOutput
from .config import ShortPolicy
from .contracts import PositionSide, PositionSnapshot, TargetPosition


AVAILABLE = "AVAILABLE"
UNAVAILABLE = "UNAVAILABLE"
DEGRADED = "DEGRADED"
_BORROW_STATUSES = frozenset({AVAILABLE, UNAVAILABLE, DEGRADED})


class _ImmutableBorrowValue:
    __slots__ = ()

    def __deepcopy__(self, memo: dict[int, object]):
        memo[id(self)] = self
        return self


def _require_string(value: object, field_name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    if not value:
        raise ValueError(f"{field_name} must not be empty")


def _require_exact_bool(value: object, field_name: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be a bool")


def _optional_nonnegative_number(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float):
        raise TypeError(f"{field_name} must be an int, float, or None")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field_name} must be finite and nonnegative")
    return 0.0 if number == 0.0 else number


@dataclass(frozen=True)
class BorrowSecurity(_ImmutableBorrowValue):
    symbol: str
    shortable: bool
    easy_to_borrow: bool
    borrow_apr_pct: float | None = None

    def __post_init__(self) -> None:
        _require_string(self.symbol, "symbol")
        _require_exact_bool(self.shortable, "shortable")
        _require_exact_bool(self.easy_to_borrow, "easy_to_borrow")
        object.__setattr__(
            self,
            "borrow_apr_pct",
            _optional_nonnegative_number(self.borrow_apr_pct, "borrow_apr_pct"),
        )


@dataclass(frozen=True)
class BorrowSnapshot(_ImmutableBorrowValue):
    id: str
    status: str
    securities: Mapping[str, BorrowSecurity] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_string(self.id, "id")
        _require_string(self.status, "status")
        if self.status not in _BORROW_STATUSES:
            raise ValueError(f"unsupported borrow snapshot status: {self.status}")
        if not isinstance(self.securities, Mapping):
            raise TypeError("securities must be a mapping")
        copied: dict[str, BorrowSecurity] = {}
        for key, security in self.securities.items():
            _require_string(key, "securities key")
            if type(security) is not BorrowSecurity:
                raise TypeError("securities values must be BorrowSecurity")
            if key != security.symbol:
                raise ValueError(
                    "securities mapping key must match BorrowSecurity.symbol"
                )
            copied[key] = security
        object.__setattr__(self, "securities", MappingProxyType(copied))

    @classmethod
    def unavailable(cls, snapshot_id: str = "borrow-unavailable") -> BorrowSnapshot:
        return cls(id=snapshot_id, status=UNAVAILABLE, securities={})


@dataclass(frozen=True)
class BorrowRejection(_ImmutableBorrowValue):
    symbol: str
    reason: str

    def __post_init__(self) -> None:
        _require_string(self.symbol, "symbol")
        _require_string(self.reason, "reason")


@dataclass(frozen=True)
class BorrowAdmissionResult(_ImmutableBorrowValue):
    admitted_targets: tuple[TargetPosition, ...] = ()
    rejections: tuple[BorrowRejection, ...] = ()
    position_modes: Mapping[str, str] = field(default_factory=dict)
    borrow_apr_by_symbol: Mapping[str, float | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        admitted = tuple(self.admitted_targets)
        if any(type(item) is not TargetPosition for item in admitted):
            raise TypeError("admitted_targets items must be TargetPosition")
        rejections = tuple(self.rejections)
        if any(type(item) is not BorrowRejection for item in rejections):
            raise TypeError("rejections items must be BorrowRejection")
        if not isinstance(self.position_modes, Mapping):
            raise TypeError("position_modes must be a mapping")
        modes: dict[str, str] = {}
        for symbol, mode in self.position_modes.items():
            _require_string(symbol, "position_modes key")
            if mode not in {"NORMAL", "COVER_ONLY"}:
                raise ValueError(f"unsupported position mode: {mode}")
            modes[symbol] = mode
        if not isinstance(self.borrow_apr_by_symbol, Mapping):
            raise TypeError("borrow_apr_by_symbol must be a mapping")
        aprs = {
            symbol: _optional_nonnegative_number(apr, "borrow APR")
            for symbol, apr in self.borrow_apr_by_symbol.items()
        }
        for symbol in aprs:
            _require_string(symbol, "borrow_apr_by_symbol key")
        object.__setattr__(self, "admitted_targets", admitted)
        object.__setattr__(self, "rejections", rejections)
        object.__setattr__(self, "position_modes", MappingProxyType(modes))
        object.__setattr__(self, "borrow_apr_by_symbol", MappingProxyType(aprs))


def _validate_policy(policy: object) -> ShortPolicy:
    if type(policy) is not ShortPolicy:
        raise TypeError("policy must be ShortPolicy")
    for field_name in (
        "require_shortable",
        "require_easy_to_borrow",
        "block_on_borrow_data_missing",
    ):
        _require_exact_bool(getattr(policy, field_name), f"policy.{field_name}")
    return policy


def _materialize_typed(
    values: Iterable[object], item_type: type, field_name: str
) -> tuple:
    if isinstance(values, (str, bytes, bytearray, Mapping)):
        raise TypeError(f"{field_name} must be an iterable")
    try:
        items = tuple(values)
    except TypeError as exc:
        raise TypeError(f"{field_name} must be an iterable") from exc
    if any(type(item) is not item_type for item in items):
        raise TypeError(f"{field_name} items must be {item_type.__name__}")
    symbols = [item.symbol for item in items]
    if len(symbols) != len(set(symbols)):
        raise ValueError(f"{field_name} must not contain duplicate symbols")
    return items


def _security_failure(
    snapshot: BorrowSnapshot,
    symbol: str,
    policy: ShortPolicy,
) -> tuple[str | None, float | None]:
    if snapshot.status == UNAVAILABLE:
        return "BORROW_DATA_MISSING", None
    security = snapshot.securities.get(symbol)
    if security is None:
        return "BORROW_DATA_MISSING", None
    if policy.require_shortable and not security.shortable:
        return "SHORT_NOT_SHORTABLE", security.borrow_apr_pct
    if policy.require_easy_to_borrow and not security.easy_to_borrow:
        return "NOT_EASY_TO_BORROW", security.borrow_apr_pct
    return None, security.borrow_apr_pct


def admit_borrow(
    targets: Iterable[TargetPosition],
    snapshot: BorrowSnapshot,
    policy: ShortPolicy,
    existing_positions: Iterable[PositionSnapshot],
) -> BorrowAdmissionResult:
    """Admit target short risk and restrict unsafe existing shorts to cover-only."""

    if type(snapshot) is not BorrowSnapshot:
        raise TypeError("snapshot must be BorrowSnapshot")
    resolved_policy = _validate_policy(policy)
    resolved_targets = _materialize_typed(targets, TargetPosition, "targets")
    positions = _materialize_typed(
        existing_positions, PositionSnapshot, "existing_positions"
    )

    admitted: list[TargetPosition] = []
    rejections: list[BorrowRejection] = []
    aprs: dict[str, float | None] = {}
    for item in resolved_targets:
        if item.side is PositionSide.LONG:
            admitted.append(item)
            continue
        reason, apr = _security_failure(snapshot, item.symbol, resolved_policy)
        if reason == "BORROW_DATA_MISSING" and not (
            resolved_policy.block_on_borrow_data_missing
        ):
            admitted.append(item)
            aprs[item.symbol] = None
        elif reason is not None:
            rejections.append(BorrowRejection(symbol=item.symbol, reason=reason))
        else:
            admitted.append(item)
            aprs[item.symbol] = apr

    modes: dict[str, str] = {}
    for position in positions:
        if position.side is not PositionSide.SHORT:
            continue
        reason, _ = _security_failure(snapshot, position.symbol, resolved_policy)
        modes[position.symbol] = "COVER_ONLY" if reason is not None else "NORMAL"

    return BorrowAdmissionResult(
        admitted_targets=tuple(admitted),
        rejections=tuple(rejections),
        position_modes=modes,
        borrow_apr_by_symbol=aprs,
    )


class BorrowAdmissionStage:
    """Pure pipeline adapter for borrow admission after exposure allocation."""

    name = "borrow_admission"
    component_version = "1.0.0"

    def __init__(
        self,
        snapshot: BorrowSnapshot,
        policy: ShortPolicy,
        existing_positions: Iterable[PositionSnapshot],
    ) -> None:
        self._snapshot = snapshot
        self._policy = policy
        self._existing_positions = tuple(existing_positions)

    def evaluate(self, stage_input: StageInput) -> StageOutput:
        matching = [
            fact
            for fact in stage_input.upstream_facts
            if isinstance(fact, Mapping) and fact.get("kind") == "exposure_targets"
        ]
        if len(matching) > 1:
            raise PipelineContractError("duplicate upstream fact: exposure_targets")
        items: object = () if not matching else matching[0].get("items", ())
        if not isinstance(items, (tuple, list)):
            raise PipelineContractError("exposure_targets items must be a sequence")
        try:
            result = admit_borrow(
                items,
                self._snapshot,
                self._policy,
                self._existing_positions,
            )
        except (TypeError, ValueError) as exc:
            raise PipelineContractError(str(exc)) from exc
        rejection_facts = tuple(
            {"symbol": item.symbol, "reason": item.reason}
            for item in result.rejections
        )
        return StageOutput(
            stage=self.name,
            component_version=self.component_version,
            facts=(
                {"kind": "borrow_targets", "items": result.admitted_targets},
                {
                    "kind": "borrow_diagnostic",
                    "rejections": rejection_facts,
                    "borrow_apr_by_symbol": dict(result.borrow_apr_by_symbol),
                },
                {"kind": "position_modes", "items": dict(result.position_modes)},
            ),
        )


__all__ = (
    "AVAILABLE",
    "DEGRADED",
    "UNAVAILABLE",
    "BorrowAdmissionResult",
    "BorrowAdmissionStage",
    "BorrowRejection",
    "BorrowSecurity",
    "BorrowSnapshot",
    "admit_borrow",
)
