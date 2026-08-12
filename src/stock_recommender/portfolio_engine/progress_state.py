"""Pure reconciliation for cumulative execution-progress snapshots."""

from __future__ import annotations

from collections.abc import Iterable

from .contracts import OrderExecutionProgress


class ProgressStateConflict(RuntimeError):
    """Raised when two snapshots for one intent are not append-only."""


def merge_latest_execution_progress(
    *groups: Iterable[OrderExecutionProgress],
) -> tuple[OrderExecutionProgress, ...]:
    """Merge stores without trusting timestamps as proof of causality."""

    merged: dict[str, OrderExecutionProgress] = {}
    for group in groups:
        for item in group:
            previous = merged.get(item.intent_id)
            if previous is None:
                merged[item.intent_id] = item
                continue
            identity_matches = (
                previous.symbol == item.symbol
                and previous.position_side is item.position_side
                and previous.order_side is item.order_side
                and previous.intent_quantity == item.intent_quantity
                and previous.execution_policy_fingerprint
                == item.execution_policy_fingerprint
                and previous.position_average_cost == item.position_average_cost
            )
            if not identity_matches:
                raise ProgressStateConflict(
                    f"execution progress identity conflict: {item.intent_id}"
                )
            if item == previous:
                continue
            if item.fills[: len(previous.fills)] == previous.fills:
                merged[item.intent_id] = item
                continue
            if previous.fills[: len(item.fills)] == item.fills:
                continue
            raise ProgressStateConflict(
                f"execution progress is not append-only: {item.intent_id}"
            )
    return tuple(merged[key] for key in sorted(merged))


__all__ = [
    "ProgressStateConflict",
    "merge_latest_execution_progress",
]
