"""Ports for portfolio engine access to external capabilities."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .borrow import BorrowSnapshot
from .contracts import (
    AccountSnapshot,
    DecisionBatch,
    EventCalendar,
    ExecutionFill,
    MarketSnapshot,
    OrderIntent,
    PortfolioLedgerView,
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

    def transition_revision(
        self,
        transition: RevisionTransition,
    ) -> AccountSnapshot: ...

    def commit(self, batch: DecisionBatch) -> AccountSnapshot: ...

    def list_accounts(self) -> tuple[AccountSnapshot, ...]: ...


class BrokerExecutionPort(Protocol):
    """Future broker boundary; simulation does not depend on this port."""

    def submit(
        self,
        intents: tuple[OrderIntent, ...],
    ) -> tuple[ExecutionFill, ...]: ...
