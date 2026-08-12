"""Ledger decorator that keeps a frozen JSON lifecycle archive read-only."""

from __future__ import annotations

from pathlib import Path

from .contracts import (
    AccountSnapshot,
    DecisionBatch,
    PortfolioPerformanceLedgerView,
    RevisionTransition,
)
from .ledger import JsonLedgerStore
from .ports import LedgerStore
from .progress_state import merge_latest_execution_progress


class ArchivedLedgerStore:
    """Delegate live state to one store and merge a frozen lifecycle archive.

    No write ever reaches ``archive``.  This is intentionally a decorator over
    the ``LedgerStore`` port so runtime code remains unaware of migrations.
    """

    def __init__(self, live: LedgerStore, archive_path: str | Path) -> None:
        self.live = live
        self.archive = JsonLedgerStore(archive_path)

    def create_account(self, account: AccountSnapshot) -> AccountSnapshot:
        return self.live.create_account(account)

    def load(self, strategy_id: str) -> AccountSnapshot:
        return self.live.load(strategy_id)

    def load_view(self, strategy_id: str):
        return self.live.load_view(strategy_id)

    def list_accounts(self) -> tuple[AccountSnapshot, ...]:
        return self.live.list_accounts()

    def transition_revision(self, transition: RevisionTransition) -> AccountSnapshot:
        return self.live.transition_revision(transition)

    def load_committed_batch(
        self,
        strategy_id: str,
        run_key: str,
        request_fingerprint: str,
    ) -> DecisionBatch | None:
        # This port method is part of every plan/process hot path.  A miss for
        # a new run key must not parse and validate the entire legacy archive.
        return self.live.load_committed_batch(
            strategy_id,
            run_key,
            request_fingerprint,
        )

    def load_archived_committed_batch(
        self,
        strategy_id: str,
        run_key: str,
        request_fingerprint: str,
    ) -> DecisionBatch | None:
        """Perform an explicit, cold-path lookup in the frozen archive."""

        try:
            return self.archive.load_committed_batch(
                strategy_id,
                run_key,
                request_fingerprint,
            )
        except KeyError:
            return None

    def commit(self, batch: DecisionBatch) -> AccountSnapshot:
        # The archive is deliberately absent from the write path.  Looking up
        # an old request would parse and validate the entire legacy JSON file
        # in every short-lived scheduler process, recreating the latency this
        # migration removes.  Historical inspection remains available through
        # the explicit archive API above; new commits are authoritative in live.
        return self.live.commit(batch)

    def load_performance_view(
        self,
        strategy_id: str,
    ) -> PortfolioPerformanceLedgerView:
        live = self.live.load_performance_view(strategy_id)
        try:
            archived = self.archive.load_performance_view(strategy_id)
        except KeyError:
            return live

        intents_by_id = {item.id: item for item in archived.intents}
        for item in live.intents:
            previous = intents_by_id.get(item.id)
            if previous is not None and previous != item:
                raise ValueError(f"conflicting archived intent ID: {item.id}")
            intents_by_id[item.id] = item

        events_by_id = {item.id: item for item in archived.events}
        for item in live.events:
            # PostgreSQL is bootstrapped from the final archived snapshot.  Its
            # synthetic account-open event marks migration, not strategy birth.
            if item.type == "ACCOUNT_OPENED":
                continue
            previous = events_by_id.get(item.id)
            if previous is not None and previous != item:
                raise ValueError(f"conflicting archived event ID: {item.id}")
            events_by_id[item.id] = item

        batches_by_key = {item.run_key: item for item in archived.batches}
        for item in live.batches:
            previous = batches_by_key.get(item.run_key)
            if previous is not None and previous != item:
                raise ValueError(f"conflicting archived run key: {item.run_key}")
            batches_by_key[item.run_key] = item

        progress = merge_latest_execution_progress(
            archived.execution_progress,
            live.execution_progress,
        )
        missing_intents = sorted(
            item.intent_id for item in progress if item.intent_id not in intents_by_id
        )
        completeness_reasons = tuple(
            reason
            for complete, reason in (
                (archived.lifecycle_complete, archived.lifecycle_reason),
                (live.lifecycle_complete, live.lifecycle_reason),
            )
            if not complete and reason
        )
        if missing_intents:
            completeness_reasons = (
                *completeness_reasons,
                "execution progress exists without an archived or live order intent",
            )
        complete = not completeness_reasons
        reason = None if complete else "; ".join(dict.fromkeys(completeness_reasons))
        return PortfolioPerformanceLedgerView(
            account=live.account,
            intents=tuple(
                sorted(intents_by_id.values(), key=lambda item: item.created_market_at)
            ),
            execution_progress=progress,
            events=tuple(events_by_id.values()),
            # Both stores already expose commit order.  The archive is the
            # immutable prefix and the live database is the suffix; run keys
            # are identifiers, not a chronology.
            batches=tuple(batches_by_key.values()),
            lifecycle_complete=complete,
            lifecycle_reason=reason,
        )


__all__ = ["ArchivedLedgerStore"]
