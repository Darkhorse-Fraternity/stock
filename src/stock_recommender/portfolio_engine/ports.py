"""Ports for portfolio engine access to external capabilities."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Protocol

from .contracts import DecisionBatch, ExecutionFill, MarketSnapshot, OrderIntent


class QuoteProvider(Protocol):
    def snapshot(
        self,
        symbols: tuple[str, ...],
        occurred_at: datetime,
    ) -> MarketSnapshot: ...


class BorrowProvider(Protocol):
    def snapshot(
        self,
        symbols: tuple[str, ...],
        occurred_at: datetime,
    ) -> Mapping[str, Mapping[str, Any]]: ...


class EventCalendarProvider(Protocol):
    def sessions_until_events(
        self,
        symbols: tuple[str, ...],
        occurred_at: datetime,
    ) -> Mapping[str, int | None]: ...


class LedgerStore(Protocol):
    def load(self, strategy_id: str) -> Mapping[str, Any]: ...

    def commit(self, batch: DecisionBatch) -> Mapping[str, Any]: ...


class BrokerExecutionPort(Protocol):
    """Future broker boundary; simulation does not depend on this port."""

    def submit(
        self,
        intents: tuple[OrderIntent, ...],
    ) -> tuple[ExecutionFill, ...]: ...
