"""Ports for portfolio engine access to external capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .borrow import BorrowSnapshot
from .contracts import (
    AccountSnapshot,
    DecisionBatch,
    EventCalendar,
    MarketSnapshot,
    OrderIntent,
    PortfolioLedgerView,
    PortfolioPerformanceLedgerView,
    RevisionTransition,
)


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
    ) -> BorrowSnapshot: ...


class EventCalendarProvider(Protocol):
    def sessions_until_events(
        self,
        symbols: tuple[str, ...],
        occurred_at: datetime,
    ) -> EventCalendar: ...


class LedgerStore(Protocol):
    def create_account(self, account: AccountSnapshot) -> AccountSnapshot: ...

    def load(self, strategy_id: str) -> AccountSnapshot: ...

    def load_view(self, strategy_id: str) -> PortfolioLedgerView: ...

    def load_performance_view(
        self,
        strategy_id: str,
    ) -> PortfolioPerformanceLedgerView: ...

    def transition_revision(
        self,
        transition: RevisionTransition,
    ) -> AccountSnapshot: ...

    def load_committed_batch(
        self,
        strategy_id: str,
        run_key: str,
        request_fingerprint: str,
    ) -> DecisionBatch | None: ...

    def commit(self, batch: DecisionBatch) -> AccountSnapshot: ...

    def list_accounts(self) -> tuple[AccountSnapshot, ...]: ...


@dataclass(frozen=True)
class BrokerOrderSnapshot:
    """Broker-owned cumulative order state, independent of any vendor SDK."""

    order_id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: int
    filled_quantity: int
    filled_average_price: float | None
    status: str
    rejection_reason: str | None = None


class BrokerExecutionPort(Protocol):
    """Idempotent boundary for submitting or reconciling one broker order."""

    name: str

    def assert_ready(self, account: AccountSnapshot) -> None: ...

    def place_or_get(self, intent: OrderIntent) -> BrokerOrderSnapshot: ...
