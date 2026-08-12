"""One-month long/short replay against an isolated Docker PostgreSQL schema."""

from __future__ import annotations

import math
import os
import threading
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from postgres_integration import (
    isolated_postgres_schema,
    non_test_database_fingerprint,
    non_test_schemas,
    require_isolated_schema_name,
    schema_exists,
    test_schema_count as count_test_schemas,
)
from stock_recommender.portfolio_engine.borrow import BorrowSecurity, BorrowSnapshot
from stock_recommender.portfolio_engine.canonical import canonical_graph
from stock_recommender.portfolio_engine.config import (
    default_exposure_policy,
    default_margin_policy,
    default_short_policy,
)
from stock_recommender.portfolio_engine.contracts import (
    AccrualLifecycle,
    AccountSnapshot,
    CarryAccrualRecord,
    CarryCostType,
    DecisionBatch,
    ExecutionFill,
    ExecutionProgressFill,
    MarketSnapshot,
    OrderExecutionProgress,
    OrderIntent,
    OrderSide,
    PlanRequest,
    PortfolioEvent,
    PortfolioLedgerView,
    PositionEffect,
    PositionRiskUpdate,
    PositionSettlementUpdate,
    PositionSide,
    PositionSnapshot,
    ProcessRequest,
    RevisionTransition,
    SignalCandidate,
    stable_execution_intent_id,
    stable_execution_progress_fill_id,
)
from stock_recommender.portfolio_engine.ledger import (
    InMemoryLedgerStore,
    JsonLedgerStore,
    LedgerError,
    _batch_fingerprint,
    _canonical_batch_facts,
    _carry_to_json,
    _event_to_json,
    _execution_account_fact,
    _intent_to_json,
    _progress_fill_to_json,
    _progress_to_json,
    _risk_fact_for_batch,
    _risk_update_to_json,
    _stable_snapshot_id,
)
from stock_recommender.portfolio_engine.request_identity import request_fingerprint
from stock_recommender.portfolio_engine.service import PortfolioEngine


DATABASE_URL = os.getenv("STOCK_AGENT_TEST_DATABASE_URL")
START = datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc)
SYMBOLS = tuple(f"L{index}" for index in range(8)) + ("S0", "S1")


class FixedSignals:
    def __init__(self, model_id: str, side: PositionSide, symbols: tuple[str, ...]):
        self.model_id = model_id
        self.side = side
        self.symbols = symbols

    def evaluate(self, rows, event_calendar):
        del rows, event_calendar
        weight = 14.9 if self.side is PositionSide.LONG else 5.0
        return tuple(
            SignalCandidate(
                symbol=symbol,
                side=self.side,
                score=1.0 - index / 100.0,
                requested_weight_pct=weight,
                model_id=self.model_id,
                thesis_id=f"{self.model_id}:{symbol}:month",
            )
            for index, symbol in enumerate(self.symbols)
        )


def long_short_strategy(strategy_id: str = "month-long-short") -> dict:
    exposure = default_exposure_policy()
    exposure.update(
        {
            "mode": "LONG_SHORT",
            "max_positions": 10,
            "max_gross_exposure_pct": 150.0,
            "max_net_exposure_pct": 120.0,
            "max_long_exposure_pct": 120.0,
            "max_short_exposure_pct": 30.0,
        }
    )
    margin = default_margin_policy()
    margin.update(
        {
            "maintenance_margin_pct": 30.0,
            "liquidation_buffer_pct": 10.0,
            "financing_apr_pct": 8.0,
        }
    )
    short = default_short_policy()
    short.update(
        {
            "signal_model": "month-short-v1",
            "estimated_borrow_apr_pct": 10.0,
        }
    )
    return {
        "version": 6,
        "id": strategy_id,
        "revision": 1,
        "market": "us",
        "signal": {"model": "month-long-v1"},
        "lifecycle": {"stage": "paper"},
        "exposure_policy": exposure,
        "margin_policy": margin,
        "short_policy": short,
        "portfolio": {
            "initial_cash": 100_000.0,
            "commission_rate_pct": 0.01,
            "minimum_commission_cny": 0.0,
            "stamp_duty_rate_pct": 0.0,
            "transfer_fee_rate_pct": 0.0,
            "slippage_bps": 0.0,
            # Force partial fills on the first execution, then finish later.
            "max_bar_participation_pct": 5.0,
            "peak_equity": 100_000.0,
        },
    }


def _market(session: int) -> MarketSnapshot:
    occurred_at = START + timedelta(days=session)
    quotes = {}
    for index, symbol in enumerate(SYMBOLS):
        if symbol.startswith("L"):
            price = (
                100.0
                if session <= 2
                else 100.0 + session * (1.0 + index / 10.0)
            )
        else:
            # Session 12 produces a squeeze; later sessions recover.
            price = 200.0 if session == 12 else 100.0 - session * 0.6 + index
        quotes[symbol] = {
            "price": max(5.0, price),
            "bar_open": max(5.0, price),
            "bar_high": max(5.0, price * 1.01),
            "bar_low": max(5.0, price * 0.99),
            "bar_volume": (
                500 if session == 1 and symbol == "L7" else 1_000_000
            ),
            # S0 session 7 exercises the real SHORT_SQUEEZE path without also
            # crossing the 6% short stop-loss threshold. Session 12 remains a
            # distinct price-driven SHORT_STOP_LOSS path.
            "percent": 12.0 if session == 7 and symbol == "S0" else 0.0,
            "volume_ratio": 4.0 if session == 7 and symbol == "S0" else 1.0,
            "one_day_return": 12.0 if session == 12 else 0.0,
            "volatility_20d_pct": 20.0,
        }
    return MarketSnapshot(
        id=f"month-market-{session:02d}",
        occurred_at=occurred_at,
        quotes=quotes,
    )


def _borrow(session: int) -> BorrowSnapshot:
    if session == 0:
        return BorrowSnapshot.unavailable("month-borrow-missing")
    securities = {
        symbol: BorrowSecurity(
            symbol=symbol,
            shortable=not (session >= 8 and symbol == "S1"),
            easy_to_borrow=not (session >= 8 and symbol == "S1"),
            borrow_apr_pct=12.0,
            available_quantity=1_000_000,
        )
        for symbol in SYMBOLS
    }
    return BorrowSnapshot(
        id=f"month-borrow-{session:02d}",
        status="AVAILABLE",
        securities=securities,
    )


def _settlement_payload(update: PositionSettlementUpdate) -> dict:
    return {
        "symbol": update.symbol,
        "side": update.side.value,
        "quantity": update.quantity,
        "sellable_quantity": update.sellable_quantity,
        "sellable_on": (
            None if update.sellable_on is None else update.sellable_on.isoformat()
        ),
    }


def _nav_payload(metrics) -> dict:
    result = {}
    for field_name in metrics.__dataclass_fields__:
        value = getattr(metrics, field_name)
        if type(value) is float and not math.isfinite(value):
            value = "Infinity" if value > 0 else "-Infinity"
        result[field_name] = value
    return result


def _risk_stage_oracle(batch: DecisionBatch) -> tuple[frozenset[str], dict | None]:
    for output in batch.stage_outputs:
        if output.stage != "portfolio_risk":
            continue
        for fact in output.facts:
            if fact.get("kind") != "risk_diagnostic":
                continue
            items = tuple(
                dict(item)
                for item in fact.get("items", ())
                if isinstance(item, dict) or hasattr(item, "get")
            )
            reasons = frozenset(
                str(item["reason"])
                for item in items
                if item.get("code") == "POSITION_RISK" and item.get("reason")
            )
            margin = next(
                (item for item in items if item.get("code") == "MARGIN_RISK"),
                None,
            )
            return reasons, margin
    return frozenset(), None


def _backtest_daily_fingerprint(
    store,
    engine,
    strategy_id: str,
    market: MarketSnapshot,
    risk_rows: list[dict],
    event_origins: dict[str, tuple[int, str | None, int]],
) -> tuple:
    """Extract the complete backtest fact graph through the memory adapter."""

    live = store.load_view(strategy_id)
    history = store.load_performance_view(strategy_id)
    snapshot = engine.performance(strategy_id, market)
    batches = history.batches
    runs = tuple(
        (
            batch.run_key,
            batch.strategy_revision,
            batch.portfolio_snapshot_id,
            _stable_snapshot_id(batch),
            batch.market_snapshot_id,
            batch.request_fingerprint,
            _batch_fingerprint(batch, _canonical_batch_facts(batch)),
            canonical_graph(batch),
        )
        for batch in batches
    )
    intent_rows = []
    seen_intents = set()
    for batch in batches:
        canonical_facts = _canonical_batch_facts(batch)
        for ordinal, intent in enumerate(canonical_facts[0]):
            if intent.id in seen_intents:
                continue
            seen_intents.add(intent.id)
            intent_rows.append(
                (batch.run_key, ordinal, _intent_to_json(intent))
            )
    progress_rows = tuple(
        (batch.run_key, ordinal, _progress_to_json(progress))
        for batch in batches
        for ordinal, progress in enumerate(_canonical_batch_facts(batch)[2])
    )
    fill_rows = []
    seen_fills = set()
    for batch in batches:
        fill_ordinal = 0
        for progress in _canonical_batch_facts(batch)[2]:
            for fill in progress.fills:
                if fill.id in seen_fills:
                    continue
                seen_fills.add(fill.id)
                fill_rows.append(
                    (batch.run_key, fill_ordinal, _progress_fill_to_json(fill))
                )
                fill_ordinal += 1
    settlement_rows = tuple(
        (batch.run_key, ordinal, _settlement_payload(update))
        for batch in batches
        for ordinal, update in enumerate(_canonical_batch_facts(batch)[4])
    )
    carry_rows = tuple(
        (batch.run_key, ordinal, _carry_to_json(record))
        for batch in batches
        for ordinal, record in enumerate(_canonical_batch_facts(batch)[5])
    )
    event_rows = tuple(
        (*event_origins[event.id], _event_to_json(event))
        for event in history.events
    )
    return (
        ("committed_runs", runs),
        (
            "all_intents",
            tuple(_intent_to_json(item) for item in history.intents),
        ),
        ("intent_facts", tuple(intent_rows)),
        (
            "open_intents",
            tuple(_intent_to_json(item) for item in live.open_intents),
        ),
        (
            "execution_progress",
            tuple(_progress_to_json(item) for item in history.execution_progress),
        ),
        ("execution_progress_facts", progress_rows),
        ("fills", tuple(fill_rows)),
        ("risk_facts_and_updates", tuple(risk_rows)),
        ("settlement_updates", settlement_rows),
        ("carry_accruals", carry_rows),
        (
            "financing_lifecycle",
            canonical_graph(history.account.financing_lifecycle),
        ),
        (
            "borrow_lifecycles",
            tuple(
                (item.symbol, canonical_graph(item.borrow_lifecycle))
                for item in history.account.positions
                if item.borrow_lifecycle is not None
            ),
        ),
        ("events", event_rows),
        (
            "positions",
            tuple(canonical_graph(item) for item in history.account.positions),
        ),
        ("nav", _nav_payload(snapshot.metrics)),
    )


def _postgres_daily_fingerprint(
    store,
    engine,
    strategy_id: str,
    market: MarketSnapshot,
) -> tuple:
    """Extract typed state plus every normalized SQL fact from PostgreSQL."""

    import psycopg
    from psycopg import sql

    live = store.load_view(strategy_id)
    history = store.load_performance_view(strategy_id)
    snapshot = engine.performance(strategy_id, market)
    schema = sql.Identifier(store.schema)
    with psycopg.connect(DATABASE_URL) as connection:
        runs = tuple(
            connection.execute(
                sql.SQL(
                    "SELECT run_key,strategy_revision,source_snapshot_id,"
                    "result_snapshot_id,market_snapshot_id,request_fingerprint,"
                    "batch_fingerprint,batch_payload FROM {}.committed_runs "
                    "WHERE strategy_id=%s ORDER BY commit_ordinal"
                ).format(schema),
                (strategy_id,),
            ).fetchall()
        )
        intent_rows = tuple(
            (row[0], row[1], row[2])
            for row in connection.execute(
                sql.SQL(
                    "SELECT i.run_key,i.ordinal,i.payload FROM "
                    "{}.order_intents AS i JOIN {}.committed_runs AS r ON "
                    "r.strategy_id=i.strategy_id AND r.run_key=i.run_key "
                    "WHERE i.strategy_id=%s ORDER BY r.commit_ordinal,"
                    "i.ordinal,i.intent_id"
                ).format(schema, schema),
                (strategy_id,),
            ).fetchall()
        )
        progress_rows = tuple(
            (row[0], row[1], row[2])
            for row in connection.execute(
                sql.SQL(
                    "SELECT p.run_key,p.ordinal,p.payload FROM "
                    "{}.execution_progress AS p JOIN {}.committed_runs AS r ON "
                    "r.strategy_id=p.strategy_id AND r.run_key=p.run_key "
                    "WHERE p.strategy_id=%s ORDER BY r.commit_ordinal,p.ordinal,p.intent_id"
                ).format(schema, schema),
                (strategy_id,),
            ).fetchall()
        )
        fill_rows = tuple(
            (row[0], row[1], row[2])
            for row in connection.execute(
                sql.SQL(
                    "SELECT f.run_key,f.ordinal,f.payload FROM {}.fills AS f "
                    "JOIN {}.committed_runs AS r ON r.strategy_id=f.strategy_id "
                    "AND r.run_key=f.run_key WHERE f.strategy_id=%s "
                    "ORDER BY r.commit_ordinal,f.ordinal,f.progress_fill_id"
                ).format(schema, schema),
                (strategy_id,),
            ).fetchall()
        )
        risk_rows = tuple(
            {
                "fact_id": row[0],
                "run_key": row[1],
                "strategy_revision": row[2],
                "portfolio_snapshot_id": row[3],
                "market_snapshot_id": row[4],
                "occurred_at": row[5].isoformat(),
                "ordinal": row[6],
                "update": row[7],
            }
            for row in connection.execute(
                sql.SQL(
                    "SELECT f.fact_id,f.run_key,f.strategy_revision,"
                    "f.portfolio_snapshot_id,f.market_snapshot_id,f.occurred_at,"
                    "f.ordinal,u.payload FROM {}.position_risk_facts AS f JOIN "
                    "{}.position_risk_updates AS u ON u.strategy_id=f.strategy_id "
                    "AND u.fact_id=f.fact_id JOIN {}.committed_runs AS r ON "
                    "r.strategy_id=f.strategy_id AND r.run_key=f.run_key "
                    "WHERE f.strategy_id=%s "
                    "ORDER BY r.commit_ordinal,f.ordinal,f.fact_id"
                ).format(schema, schema, schema),
                (strategy_id,),
            ).fetchall()
        )
        settlement_rows = tuple(
            (row[0], row[1], row[2])
            for row in connection.execute(
                sql.SQL(
                    "SELECT s.run_key,s.ordinal,s.payload FROM "
                    "{}.settlement_updates AS s JOIN {}.committed_runs AS r ON "
                    "r.strategy_id=s.strategy_id AND r.run_key=s.run_key "
                    "WHERE s.strategy_id=%s ORDER BY r.commit_ordinal,s.ordinal,s.symbol"
                ).format(schema, schema),
                (strategy_id,),
            ).fetchall()
        )
        carry_rows = tuple(
            (row[0], row[1], row[2])
            for row in connection.execute(
                sql.SQL(
                    "SELECT c.run_key,c.ordinal,c.payload FROM {}.carry_accruals AS c "
                    "JOIN {}.committed_runs AS r ON r.strategy_id=c.strategy_id "
                    "AND r.run_key=c.run_key WHERE c.strategy_id=%s "
                    "ORDER BY r.commit_ordinal,c.ordinal,c.accrual_id"
                ).format(schema, schema),
                (strategy_id,),
            ).fetchall()
        )
        event_rows = tuple(
            (row[0], row[1], row[2], row[3])
            for row in connection.execute(
                sql.SQL(
                    "SELECT e.strategy_revision,e.run_key,e.ordinal,e.payload "
                    "FROM {}.events AS e WHERE e.strategy_id=%s "
                    "ORDER BY e.event_ordinal"
                ).format(schema),
                (strategy_id,),
            ).fetchall()
        )
    return (
        ("committed_runs", runs),
        (
            "all_intents",
            tuple(_intent_to_json(item) for item in history.intents),
        ),
        ("intent_facts", intent_rows),
        (
            "open_intents",
            tuple(_intent_to_json(item) for item in live.open_intents),
        ),
        (
            "execution_progress",
            tuple(_progress_to_json(item) for item in history.execution_progress),
        ),
        ("execution_progress_facts", progress_rows),
        ("fills", fill_rows),
        ("risk_facts_and_updates", risk_rows),
        ("settlement_updates", settlement_rows),
        ("carry_accruals", carry_rows),
        (
            "financing_lifecycle",
            canonical_graph(history.account.financing_lifecycle),
        ),
        (
            "borrow_lifecycles",
            tuple(
                (item.symbol, canonical_graph(item.borrow_lifecycle))
                for item in history.account.positions
                if item.borrow_lifecycle is not None
            ),
        ),
        ("events", event_rows),
        (
            "positions",
            tuple(canonical_graph(item) for item in history.account.positions),
        ),
        ("nav", _nav_payload(snapshot.metrics)),
    )


def run_backtest_22_sessions(*, strategy: dict) -> dict:
    """Independent in-memory backtest orchestration for the shared scenario."""

    strategy = deepcopy(strategy)
    with TemporaryDirectory(prefix="stock-month-backtest-") as temporary:
        store = InMemoryLedgerStore(Path(temporary) / "ledger.lock")
        store.create_account(
            AccountSnapshot(
                id=f"account-{strategy['id']}",
                strategy_id=strategy["id"],
                strategy_revision=strategy["revision"],
                occurred_at=START,
                available_cash=strategy["portfolio"]["initial_cash"],
                snapshot_id=f"bootstrap-{strategy['id']}",
            )
        )
        engine = PortfolioEngine(
            signal_registry={
                "month-long-v1": FixedSignals(
                    "month-long-v1",
                    PositionSide.LONG,
                    tuple(f"L{index}" for index in range(8)),
                ),
                "month-short-v1": FixedSignals(
                    "month-short-v1", PositionSide.SHORT, ("S0", "S1")
                ),
            },
            ledger_store=store,
        )
        fingerprints = []
        daily = []
        event_types = set()
        fill_statuses = set()
        position_modes = set()
        diagnostic_codes = set()
        intent_reasons = set()
        seen_event_ids = set()
        risk_rows = []
        opened = store.load_performance_view(strategy["id"]).events
        event_origins = {
            item.id: (1, None, ordinal) for ordinal, item in enumerate(opened)
        }
        maximum_positions = maximum_gross = maximum_net = maximum_short = 0.0
        for session in range(22):
            market = _market(session)
            borrow = _borrow(session)
            current = store.load(strategy["id"])
            if session == 10:
                before_ids = {
                    item.id
                    for item in store.load_performance_view(strategy["id"]).events
                }
                store.transition_revision(
                    RevisionTransition(
                        id=f"month-margin-revision-{strategy['id']}",
                        strategy_id=strategy["id"],
                        expected_snapshot_id=current.snapshot_id,
                        from_revision=1,
                        to_revision=2,
                        occurred_at=market.occurred_at,
                    )
                )
                for event in store.load_performance_view(strategy["id"]).events:
                    if event.id not in before_ids:
                        event_origins[event.id] = (2, None, 0)
                strategy["revision"] = 2
                strategy["margin_policy"]["maintenance_margin_pct"] = 80.0
                strategy["margin_policy"]["liquidation_buffer_pct"] = 10.0
                current = store.load(strategy["id"])
            if session in {0, 6, 13, 19}:
                request = PlanRequest(
                    run_key=f"month-{session:02d}-plan",
                    strategy=strategy,
                    account=current,
                    analyzed_rows=tuple({"symbol": symbol} for symbol in SYMBOLS),
                    market=market,
                    borrow=borrow,
                    event_calendar={symbol: None for symbol in SYMBOLS},
                )
                batch = replace(
                    engine.evaluate(request),
                    request_fingerprint=request_fingerprint(request),
                )
                store.commit(batch)
            else:
                batch = engine.process_and_commit(
                    ProcessRequest(
                        run_key=f"month-{session:02d}-process",
                        strategy=strategy,
                        account=current,
                        market=market,
                        borrow=borrow,
                    )
                )
            committed = store.load(strategy["id"])
            execution_account = _execution_account_fact(batch)
            risk_occurred_at = (
                current.occurred_at
                if execution_account is None
                else execution_account.occurred_at
            )
            canonical_updates = _canonical_batch_facts(batch)[3]
            for ordinal, update in enumerate(canonical_updates):
                fact = _risk_fact_for_batch(
                    batch,
                    update,
                    risk_occurred_at,
                )
                risk_rows.append(
                    {
                        "fact_id": fact.fact_id,
                        "run_key": batch.run_key,
                        "strategy_revision": batch.strategy_revision,
                        "portfolio_snapshot_id": batch.portfolio_snapshot_id,
                        "market_snapshot_id": batch.market_snapshot_id,
                        "occurred_at": risk_occurred_at.isoformat(),
                        "ordinal": ordinal,
                        "update": _risk_update_to_json(update),
                    }
                )
            history = store.load_performance_view(strategy["id"])
            new_events = tuple(
                item for item in history.events if item.id not in seen_event_ids
            )
            unmapped_events = tuple(
                event for event in new_events if event.id not in event_origins
            )
            for ordinal, event in enumerate(unmapped_events):
                event_origins[event.id] = (
                    batch.strategy_revision,
                    batch.run_key,
                    ordinal,
                )
            seen_event_ids.update(item.id for item in new_events)
            fingerprint = _backtest_daily_fingerprint(
                store,
                engine,
                strategy["id"],
                market,
                risk_rows,
                event_origins,
            )
            fingerprints.append(fingerprint)
            snapshot = engine.performance(strategy["id"], market)
            risk_reasons, margin = _risk_stage_oracle(batch)
            daily.append(
                {
                    "run_key": batch.run_key,
                    "event_types": frozenset(item.type for item in new_events),
                    "fill_statuses": frozenset(item.status for item in batch.fills),
                    "intent_reasons": frozenset(item.reason for item in batch.intents),
                    "diagnostic_codes": frozenset(
                        str(item.get("code")) for item in batch.diagnostics
                    ),
                    "diagnostics": tuple(dict(item) for item in batch.diagnostics),
                    "risk_reasons": risk_reasons,
                    "position_modes": {
                        item.symbol: item.position_mode for item in committed.positions
                    },
                    "margin": margin,
                    "open_progress": {
                        item.symbol: item.status
                        for item in store.load_view(strategy["id"]).execution_progress
                    },
                }
            )
            event_types.update(item.type for item in new_events)
            fill_statuses.update(item.status for item in batch.fills)
            position_modes.update(item.position_mode for item in committed.positions)
            diagnostic_codes.update(str(item.get("code")) for item in batch.diagnostics)
            intent_reasons.update(item.reason for item in batch.intents)
            metrics = snapshot.metrics
            maximum_positions = max(maximum_positions, len(committed.positions))
            maximum_gross = max(maximum_gross, metrics.gross_exposure_pct)
            maximum_net = max(maximum_net, abs(metrics.net_exposure_pct))
            maximum_short = max(maximum_short, metrics.short_exposure_pct)
        store.validate_integrity()
        final = store.load(strategy["id"])
        return {
            "sessions": len(fingerprints),
            "fingerprints": tuple(fingerprints),
            "daily": tuple(daily),
            "maximum_positions": maximum_positions,
            "maximum_gross_exposure_pct": maximum_gross,
            "maximum_net_exposure_pct": maximum_net,
            "maximum_short_exposure_pct": maximum_short,
            "event_types": frozenset(event_types),
            "fill_statuses": frozenset(fill_statuses),
            "position_modes": frozenset(position_modes),
            "diagnostic_codes": frozenset(diagnostic_codes),
            "intent_reasons": frozenset(intent_reasons),
            "financing_cost": final.accrued_financing_cost,
            "borrow_cost": final.accrued_borrow_cost,
        }


def run_paper_postgres_22_sessions(store, *, strategy: dict) -> dict:
    """Independent paper orchestration using real PostgreSQL typed views."""

    strategy = deepcopy(strategy)
    store.create_account(
        AccountSnapshot(
            id=f"account-{strategy['id']}",
            strategy_id=strategy["id"],
            strategy_revision=strategy["revision"],
            occurred_at=START,
            available_cash=strategy["portfolio"]["initial_cash"],
            snapshot_id=f"bootstrap-{strategy['id']}",
        )
    )
    engine = PortfolioEngine(
        signal_registry={
            "month-long-v1": FixedSignals(
                "month-long-v1",
                PositionSide.LONG,
                tuple(f"L{index}" for index in range(8)),
            ),
            "month-short-v1": FixedSignals(
                "month-short-v1", PositionSide.SHORT, ("S0", "S1")
            ),
        },
        ledger_store=store,
    )
    fingerprints = []
    daily = []
    event_types = set()
    fill_statuses = set()
    position_modes = set()
    diagnostic_codes = set()
    intent_reasons = set()
    seen_event_ids = set()
    maximum_positions = maximum_gross = maximum_net = maximum_short = 0.0
    for session in range(22):
        market = _market(session)
        borrow = _borrow(session)
        current = store.load(strategy["id"])
        if session == 10:
            store.transition_revision(
                RevisionTransition(
                    id=f"month-margin-revision-{strategy['id']}",
                    strategy_id=strategy["id"],
                    expected_snapshot_id=current.snapshot_id,
                    from_revision=1,
                    to_revision=2,
                    occurred_at=market.occurred_at,
                )
            )
            strategy["revision"] = 2
            strategy["margin_policy"]["maintenance_margin_pct"] = 80.0
            strategy["margin_policy"]["liquidation_buffer_pct"] = 10.0
            current = store.load_view(strategy["id"]).account
        if session in {0, 6, 13, 19}:
            batch = engine.plan_and_commit(
                PlanRequest(
                    run_key=f"month-{session:02d}-plan",
                    strategy=strategy,
                    account=current,
                    analyzed_rows=tuple({"symbol": symbol} for symbol in SYMBOLS),
                    market=market,
                    borrow=borrow,
                    event_calendar={symbol: None for symbol in SYMBOLS},
                )
            )
        else:
            batch = engine.process_and_commit(
                ProcessRequest(
                    run_key=f"month-{session:02d}-process",
                    strategy=strategy,
                    account=current,
                    market=market,
                    borrow=borrow,
                )
            )
        committed = store.load_view(strategy["id"]).account
        history = store.load_performance_view(strategy["id"])
        new_events = tuple(
            item for item in history.events if item.id not in seen_event_ids
        )
        seen_event_ids.update(item.id for item in new_events)
        fingerprints.append(
            _postgres_daily_fingerprint(
                store,
                engine,
                strategy["id"],
                market,
            )
        )
        snapshot = engine.performance(strategy["id"], market)
        risk_reasons, margin = _risk_stage_oracle(batch)
        daily.append(
            {
                "run_key": batch.run_key,
                "event_types": frozenset(item.type for item in new_events),
                "fill_statuses": frozenset(item.status for item in batch.fills),
                "intent_reasons": frozenset(item.reason for item in batch.intents),
                "diagnostic_codes": frozenset(
                    str(item.get("code")) for item in batch.diagnostics
                ),
                "diagnostics": tuple(dict(item) for item in batch.diagnostics),
                "risk_reasons": risk_reasons,
                "position_modes": {
                    item.symbol: item.position_mode for item in committed.positions
                },
                "margin": margin,
                "open_progress": {
                    item.symbol: item.status
                    for item in store.load_view(strategy["id"]).execution_progress
                },
            }
        )
        event_types.update(item.type for item in new_events)
        fill_statuses.update(item.status for item in batch.fills)
        position_modes.update(item.position_mode for item in committed.positions)
        diagnostic_codes.update(str(item.get("code")) for item in batch.diagnostics)
        intent_reasons.update(item.reason for item in batch.intents)
        metrics = snapshot.metrics
        maximum_positions = max(maximum_positions, len(committed.positions))
        maximum_gross = max(maximum_gross, metrics.gross_exposure_pct)
        maximum_net = max(maximum_net, abs(metrics.net_exposure_pct))
        maximum_short = max(maximum_short, metrics.short_exposure_pct)
    final = store.load(strategy["id"])
    return {
        "sessions": len(fingerprints),
        "fingerprints": tuple(fingerprints),
        "daily": tuple(daily),
        "maximum_positions": maximum_positions,
        "maximum_gross_exposure_pct": maximum_gross,
        "maximum_net_exposure_pct": maximum_net,
        "maximum_short_exposure_pct": maximum_short,
        "event_types": frozenset(event_types),
        "fill_statuses": frozenset(fill_statuses),
        "position_modes": frozenset(position_modes),
        "diagnostic_codes": frozenset(diagnostic_codes),
        "intent_reasons": frozenset(intent_reasons),
        "financing_cost": final.accrued_financing_cost,
        "borrow_cost": final.accrued_borrow_cost,
    }


def _simple_batch(
    account: AccountSnapshot,
    *,
    run_key: str,
    intent_id: str,
    market_snapshot_id: str,
) -> DecisionBatch:
    intent = OrderIntent(
        id=intent_id,
        symbol="L0",
        position_side=PositionSide.LONG,
        order_side=OrderSide.BUY,
        position_effect=PositionEffect.OPEN,
        quantity=1,
        reason="REBALANCE",
        created_snapshot_id=market_snapshot_id,
        created_market_at=account.occurred_at,
    )
    return DecisionBatch(
        run_key=run_key,
        strategy_id=account.strategy_id,
        strategy_revision=account.strategy_revision,
        portfolio_snapshot_id=account.snapshot_id,
        market_snapshot_id=market_snapshot_id,
        request_fingerprint=f"request-{run_key}",
        intents=(intent,),
        events=(
            PortfolioEvent(
                id=f"event-{run_key}",
                type="PIPELINE_COMPLETED",
                occurred_at=account.occurred_at,
                data={
                    "run_key": run_key,
                    "market_snapshot_id": market_snapshot_id,
                },
            ),
        ),
        diagnostics=({"code": "TEST_BATCH", "run_key": run_key},),
    )


def _simple_filled_batch(account: AccountSnapshot) -> DecisionBatch:
    batch = _simple_batch(
        account,
        run_key="normalized-authority-run",
        intent_id="normalized-authority-intent",
        market_snapshot_id="normalized-authority-market",
    )
    intent = batch.intents[0]
    fill_values = {
        "intent_id": intent.id,
        "symbol": intent.symbol,
        "position_side": intent.position_side,
        "order_side": intent.order_side,
        "snapshot_id": batch.market_snapshot_id,
        "occurred_at": account.occurred_at + timedelta(minutes=1),
        "quantity": 1,
        "price": 10.0,
        "fees": 0.0,
        "commission": 0.0,
        "status": "FILLED",
    }
    detail = ExecutionProgressFill(
        id=stable_execution_progress_fill_id(**fill_values),
        **fill_values,
    )
    return replace(
        batch,
        fills=(
            ExecutionFill(
                intent_id=intent.id,
                symbol=intent.symbol,
                quantity=1,
                price=10.0,
                fees=0.0,
                status="FILLED",
            ),
        ),
        execution_progress=(
            OrderExecutionProgress(
                intent_id=intent.id,
                symbol=intent.symbol,
                position_side=intent.position_side,
                order_side=intent.order_side,
                intent_quantity=1,
                execution_policy_fingerprint="normalized-authority-policy",
                fills=(detail,),
            ),
        ),
    )


@unittest.skipUnless(DATABASE_URL, "requires Docker PostgreSQL")
class MonthLongShortIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from stock_recommender.portfolio_engine.postgres_store import PostgresLedgerStore

        cls.PostgresLedgerStore = PostgresLedgerStore

    def test_schema_name_guard_refuses_existing_names(self):
        for unsafe in ("public", "pg_catalog", "stock_agent_test_nope", "x; DROP SCHEMA public"):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                require_isolated_schema_name(unsafe)

    def test_postgres_store_bootstraps_a_missing_schema(self):
        before_schemas = non_test_schemas(DATABASE_URL)
        before_fingerprint = non_test_database_fingerprint(DATABASE_URL)
        with isolated_postgres_schema(DATABASE_URL, create=False) as schema:
            self.assertFalse(schema_exists(DATABASE_URL, schema))
            self.PostgresLedgerStore(DATABASE_URL, schema=schema)
            self.assertTrue(schema_exists(DATABASE_URL, schema))
        self.assertFalse(schema_exists(DATABASE_URL, schema))
        self.assertEqual(non_test_schemas(DATABASE_URL), before_schemas)
        self.assertEqual(
            non_test_database_fingerprint(DATABASE_URL),
            before_fingerprint,
        )

    def test_postgres_store_is_independent_and_schema_is_normalized(self):
        self.assertFalse(issubclass(self.PostgresLedgerStore, JsonLedgerStore))
        required_tables = {
            "accounts",
            "positions",
            "borrow_lifecycles",
            "financing_lifecycles",
            "committed_runs",
            "order_intent_definitions",
            "open_intents_current",
            "execution_progress_current",
            "order_intents",
            "execution_progress",
            "fills",
            "position_risk_facts",
            "position_risk_updates",
            "settlement_updates",
            "carry_accruals",
            "events",
            "revision_transitions",
        }
        with isolated_postgres_schema(DATABASE_URL) as schema:
            self.PostgresLedgerStore(DATABASE_URL, schema=schema)
            import psycopg

            with psycopg.connect(DATABASE_URL) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = %s AND table_type = 'BASE TABLE'
                        """,
                        (schema,),
                    ).fetchall()
                }
                constraints = connection.execute(
                    """
                    SELECT tc.table_name, tc.constraint_type
                    FROM information_schema.table_constraints AS tc
                    WHERE tc.table_schema = %s
                    """,
                    (schema,),
                ).fetchall()
                hot_path_indexes = {
                    (row[0], row[1])
                    for row in connection.execute(
                        """
                        SELECT tablename, indexname
                        FROM pg_indexes
                        WHERE schemaname = %s
                          AND tablename IN ('execution_progress', 'events')
                        """,
                        (schema,),
                    ).fetchall()
                }
            self.assertNotIn("ledger_state", tables)
            self.assertTrue(required_tables.issubset(tables))
            by_table = {}
            for table, kind in constraints:
                by_table.setdefault(table, set()).add(kind)
            for table in required_tables:
                self.assertIn("PRIMARY KEY", by_table.get(table, set()), table)
            for table in required_tables - {"accounts"}:
                self.assertIn("FOREIGN KEY", by_table.get(table, set()), table)
            self.assertIn("UNIQUE", by_table.get("committed_runs", set()))
            self.assertIn(
                ("execution_progress", "execution_progress_intent_lookup"),
                hot_path_indexes,
            )
            self.assertIn(
                ("events", "events_strategy_recent"),
                hot_path_indexes,
            )
            self.assertIn(
                ("events", "events_strategy_event_id"),
                hot_path_indexes,
            )

    def test_migration_open_intent_accepts_future_cumulative_progress(self):
        with isolated_postgres_schema(DATABASE_URL) as schema:
            store = self.PostgresLedgerStore(DATABASE_URL, schema=schema)
            account = AccountSnapshot(
                id="account-migration-open",
                strategy_id="migration-open",
                strategy_revision=1,
                occurred_at=START,
                available_cash=1_000.0,
                snapshot_id="migration-open-bootstrap",
            )
            identity = {
                "symbol": "NVDA",
                "position_side": PositionSide.LONG,
                "order_side": OrderSide.BUY,
                "position_effect": PositionEffect.OPEN,
                "quantity": 10,
                "reason": "migration-open",
                "created_snapshot_id": account.snapshot_id,
                "created_market_at": START + timedelta(minutes=1),
            }
            intent = OrderIntent(
                id=stable_execution_intent_id(**identity),
                **identity,
            )
            store.create_account(account)

            seeded = store.bootstrap_open_state(
                PortfolioLedgerView(account=account, open_intents=(intent,))
            )

            self.assertEqual(seeded.open_intents, (intent,))
            fill_facts = {
                "intent_id": intent.id,
                "symbol": intent.symbol,
                "position_side": intent.position_side,
                "order_side": intent.order_side,
                "snapshot_id": "migration-open-market-1",
                "occurred_at": START + timedelta(minutes=2),
                "quantity": 5,
                "price": 100.0,
                "fees": 0.5,
                "commission": 0.5,
                "status": "PARTIAL",
            }
            progress_fill = ExecutionProgressFill(
                id=stable_execution_progress_fill_id(**fill_facts),
                **fill_facts,
            )
            progress = OrderExecutionProgress(
                intent_id=intent.id,
                symbol=intent.symbol,
                position_side=intent.position_side,
                order_side=intent.order_side,
                intent_quantity=intent.quantity,
                execution_policy_fingerprint="migration-policy",
                fills=(progress_fill,),
            )
            batch = DecisionBatch(
                run_key="migration-open-run-1",
                strategy_id=account.strategy_id,
                strategy_revision=account.strategy_revision,
                portfolio_snapshot_id=account.snapshot_id,
                market_snapshot_id=progress_fill.snapshot_id,
                request_fingerprint="migration-open-request-1",
                fills=(
                    ExecutionFill(
                        intent_id=intent.id,
                        symbol=intent.symbol,
                        quantity=5,
                        price=100.0,
                        fees=0.5,
                        status="PARTIAL",
                    ),
                ),
                execution_progress=(progress,),
            )

            current = store.commit(batch)
            view = store.load_view(account.strategy_id)

            import psycopg
            from psycopg import sql

            with psycopg.connect(DATABASE_URL) as connection:
                current_counts = connection.execute(
                    sql.SQL(
                        "SELECT "
                        "(SELECT COUNT(*) FROM {}.open_intents_current), "
                        "(SELECT COUNT(*) FROM {}.execution_progress_current)"
                    ).format(sql.Identifier(schema), sql.Identifier(schema))
                ).fetchone()

            self.assertEqual(current.positions[0].quantity, 5)
            self.assertEqual(view.execution_progress, (progress,))
            self.assertEqual(view.open_intents, (intent,))
            self.assertEqual(current_counts, (1, 1))

            final_fill_facts = {
                **fill_facts,
                "snapshot_id": "migration-open-market-2",
                "occurred_at": START + timedelta(minutes=3),
                "price": 101.0,
                "status": "FILLED",
            }
            final_progress_fill = ExecutionProgressFill(
                id=stable_execution_progress_fill_id(**final_fill_facts),
                **final_fill_facts,
            )
            completed_progress = OrderExecutionProgress(
                intent_id=intent.id,
                symbol=intent.symbol,
                position_side=intent.position_side,
                order_side=intent.order_side,
                intent_quantity=intent.quantity,
                execution_policy_fingerprint="migration-policy",
                fills=(progress_fill, final_progress_fill),
            )
            completed = store.commit(
                DecisionBatch(
                    run_key="migration-open-run-2",
                    strategy_id=account.strategy_id,
                    strategy_revision=account.strategy_revision,
                    portfolio_snapshot_id=current.snapshot_id,
                    market_snapshot_id=final_progress_fill.snapshot_id,
                    request_fingerprint="migration-open-request-2",
                    fills=(
                        ExecutionFill(
                            intent_id=intent.id,
                            symbol=intent.symbol,
                            quantity=5,
                            price=101.0,
                            fees=0.5,
                            status="FILLED",
                        ),
                    ),
                    execution_progress=(completed_progress,),
                )
            )
            completed_view = store.load_view(account.strategy_id)
            with psycopg.connect(DATABASE_URL) as connection:
                completed_counts = connection.execute(
                    sql.SQL(
                        "SELECT "
                        "(SELECT COUNT(*) FROM {}.open_intents_current), "
                        "(SELECT COUNT(*) FROM {}.execution_progress_current)"
                    ).format(sql.Identifier(schema), sql.Identifier(schema))
                ).fetchone()

            self.assertEqual(completed.positions[0].quantity, 10)
            self.assertEqual(completed_view.open_intents, ())
            self.assertEqual(completed_view.execution_progress, ())
            self.assertEqual(completed_counts, (0, 0))

    def test_intent_definition_registry_enforces_relationships_and_conflicts(self):
        with isolated_postgres_schema(DATABASE_URL) as schema:
            store = self.PostgresLedgerStore(DATABASE_URL, schema=schema)
            account = AccountSnapshot(
                id="account-normalized-authority",
                strategy_id="normalized-authority",
                strategy_revision=1,
                occurred_at=START,
                available_cash=1_000.0,
                snapshot_id="normalized-authority-bootstrap",
            )
            store.create_account(account)
            batch = _simple_filled_batch(account)
            store.commit(batch)

            import psycopg
            from psycopg import sql

            with psycopg.connect(DATABASE_URL) as connection:
                relationships = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT tc.table_name
                        FROM information_schema.table_constraints AS tc
                        JOIN information_schema.constraint_column_usage AS ccu
                          ON ccu.constraint_schema=tc.constraint_schema
                         AND ccu.constraint_name=tc.constraint_name
                        WHERE tc.constraint_schema=%s
                          AND tc.constraint_type='FOREIGN KEY'
                          AND ccu.table_name='order_intent_definitions'
                        """,
                        (schema,),
                    ).fetchall()
                }
                self.assertEqual(
                    relationships,
                    {
                        "order_intents",
                        "open_intents_current",
                        "execution_progress",
                        "fills",
                    },
                )
                with self.assertRaises(psycopg.errors.ForeignKeyViolation):
                    connection.execute(
                        sql.SQL(
                            "DELETE FROM {}.order_intent_definitions "
                            "WHERE strategy_id=%s AND intent_id=%s"
                        ).format(sql.Identifier(schema)),
                        (batch.strategy_id, batch.intents[0].id),
                    )
                connection.rollback()

            current = store.load(batch.strategy_id)
            conflict = _simple_batch(
                current,
                run_key="intent-definition-conflict",
                intent_id=batch.intents[0].id,
                market_snapshot_id="intent-definition-conflict-market",
            )
            conflict = replace(
                conflict,
                intents=(replace(conflict.intents[0], quantity=2),),
            )
            with self.assertRaisesRegex(LedgerError, "conflicting intent ID"):
                store.commit(conflict)

            with psycopg.connect(DATABASE_URL) as connection:
                definition_count = connection.execute(
                    sql.SQL(
                        "SELECT COUNT(*) FROM {}.order_intent_definitions "
                        "WHERE strategy_id=%s AND intent_id=%s"
                    ).format(sql.Identifier(schema)),
                    (batch.strategy_id, batch.intents[0].id),
                ).fetchone()[0]
                occurrence_count = connection.execute(
                    sql.SQL(
                        "SELECT COUNT(*) FROM {}.order_intents "
                        "WHERE strategy_id=%s AND intent_id=%s"
                    ).format(sql.Identifier(schema)),
                    (batch.strategy_id, batch.intents[0].id),
                ).fetchone()[0]
            self.assertEqual(definition_count, 1)
            self.assertEqual(occurrence_count, 1)

    def test_create_load_and_typed_views_round_trip_current_account(self):
        with isolated_postgres_schema(DATABASE_URL) as schema:
            store = self.PostgresLedgerStore(DATABASE_URL, schema=schema)
            empty = AccountSnapshot(
                id="account-normalized-empty",
                strategy_id="normalized-empty",
                strategy_revision=1,
                occurred_at=START,
                available_cash=100_000.0,
                snapshot_id="normalized-empty-bootstrap",
            )
            self.assertEqual(store.create_account(empty), empty)
            self.assertEqual(store.create_account(empty), empty)
            self.assertEqual(store.load(empty.strategy_id), empty)
            empty_view = store.load_view(empty.strategy_id)
            self.assertEqual(empty_view.account, empty)
            self.assertEqual(empty_view.open_intents, ())
            self.assertEqual(empty_view.execution_progress, ())
            self.assertEqual(
                [item.type for item in empty_view.recent_events],
                ["ACCOUNT_OPENED"],
            )
            performance = store.load_performance_view(empty.strategy_id)
            self.assertEqual(performance.account, empty)
            self.assertEqual(performance.intents, ())
            self.assertEqual(performance.execution_progress, ())
            self.assertEqual(performance.batches, ())

            financed = AccountSnapshot(
                id="account-normalized-current",
                strategy_id="normalized-current",
                strategy_revision=3,
                occurred_at=START,
                available_cash=25_000.0,
                restricted_short_proceeds=5_000.0,
                margin_loan=10_000.0,
                financing_lifecycle=AccrualLifecycle(
                    id="financing-normalized-current",
                    started_on=START.date(),
                ),
                positions=(
                    PositionSnapshot(
                        symbol="L0",
                        side=PositionSide.LONG,
                        quantity=100,
                        average_cost=100.0,
                        current_price=102.0,
                    ),
                    PositionSnapshot(
                        symbol="S0",
                        side=PositionSide.SHORT,
                        quantity=50,
                        average_cost=100.0,
                        current_price=98.0,
                        borrow_lifecycle=AccrualLifecycle(
                            id="borrow-normalized-current-S0",
                            started_on=START.date(),
                        ),
                    ),
                ),
                snapshot_id="normalized-current-snapshot",
            )
            store.create_account(financed)
            self.assertEqual(store.load(financed.strategy_id), financed)
            self.assertEqual(
                store.load_performance_view(financed.strategy_id).account,
                financed,
            )
            self.assertEqual(
                store.list_accounts(),
                (financed, empty),
            )

    def test_same_market_snapshot_repeated_canonical_intents_are_per_run_facts(self):
        strategy_id = "repeated-canonical-intents"
        strategy = long_short_strategy(strategy_id)
        market = _market(0)
        analyzed_rows = tuple({"symbol": symbol} for symbol in SYMBOLS)
        event_calendar = {symbol: None for symbol in SYMBOLS}

        def exercise(store):
            store.create_account(
                AccountSnapshot(
                    id=f"account-{strategy_id}",
                    strategy_id=strategy_id,
                    strategy_revision=1,
                    occurred_at=START,
                    available_cash=100_000.0,
                    snapshot_id=f"bootstrap-{strategy_id}",
                )
            )
            engine = PortfolioEngine(
                signal_registry={
                    "month-long-v1": FixedSignals(
                        "month-long-v1",
                        PositionSide.LONG,
                        tuple(f"L{index}" for index in range(8)),
                    ),
                    "month-short-v1": FixedSignals(
                        "month-short-v1", PositionSide.SHORT, ("S0", "S1")
                    ),
                },
                ledger_store=store,
            )
            batches = []
            for index in range(2):
                batches.append(
                    engine.plan_and_commit(
                        PlanRequest(
                            run_key=f"same-market-run-{index}",
                            strategy=strategy,
                            account=store.load(strategy_id),
                            analyzed_rows=analyzed_rows,
                            market=market,
                            borrow=_borrow(0),
                            event_calendar=event_calendar,
                        )
                    )
                )
            self.assertTrue(batches[0].intents)
            self.assertEqual(
                tuple(item.id for item in batches[0].intents),
                tuple(item.id for item in batches[1].intents),
            )
            loaded = tuple(
                store.load_committed_batch(
                    strategy_id, batch.run_key, batch.request_fingerprint
                )
                for batch in batches
            )
            self.assertTrue(all(item is not None for item in loaded))
            performance = store.load_performance_view(strategy_id)
            self.assertEqual(len(performance.batches), 2)
            return batches, loaded, performance

        with TemporaryDirectory() as temp_dir:
            memory_result = exercise(
                InMemoryLedgerStore(Path(temp_dir) / "repeated-intents.lock")
            )
        with isolated_postgres_schema(DATABASE_URL) as schema:
            postgres_result = exercise(
                self.PostgresLedgerStore(DATABASE_URL, schema=schema)
            )

        memory_batches, memory_loaded, memory_performance = memory_result
        postgres_batches, postgres_loaded, postgres_performance = postgres_result
        self.assertEqual(
            tuple(canonical_graph(item) for item in postgres_batches),
            tuple(canonical_graph(item) for item in memory_batches),
        )
        self.assertEqual(
            tuple(canonical_graph(item) for item in postgres_loaded),
            tuple(canonical_graph(item) for item in memory_loaded),
        )
        self.assertEqual(
            tuple(canonical_graph(item) for item in postgres_performance.batches),
            tuple(canonical_graph(item) for item in memory_performance.batches),
        )

    def test_repeated_event_and_carry_ids_are_preserved_in_each_run(self):
        with isolated_postgres_schema(DATABASE_URL) as schema:
            store = self.PostgresLedgerStore(DATABASE_URL, schema=schema)
            account = AccountSnapshot(
                id="account-repeated-carry",
                strategy_id="repeated-carry",
                strategy_revision=1,
                occurred_at=START,
                available_cash=1_000.0,
                margin_loan=100.0,
                financing_lifecycle=AccrualLifecycle(
                    id="financing-repeated-carry",
                    started_on=START.date(),
                ),
                snapshot_id="repeated-carry-bootstrap",
            )
            store.create_account(account)
            accrual = CarryAccrualRecord(
                account_id=account.id,
                cost_type=CarryCostType.FINANCING,
                accrual_date=(START + timedelta(days=1)).date(),
                elapsed_days=1,
                amount=2.0,
                lifecycle_id=account.financing_lifecycle.id,
            )
            event = PortfolioEvent(
                id="repeated-financing-event",
                type="FINANCING_COST_ACCRUED",
                occurred_at=START + timedelta(days=1),
                data={
                    "account_id": account.id,
                    "cost_type": "FINANCING",
                    "lifecycle_id": accrual.lifecycle_id,
                    "symbol": None,
                    "accrual_date": accrual.accrual_date.isoformat(),
                    "elapsed_days": accrual.elapsed_days,
                    "amount": accrual.amount,
                },
            )
            batches = []
            for index in range(2):
                current = store.load(account.strategy_id)
                batch = DecisionBatch(
                    run_key=f"repeated-carry-run-{index}",
                    strategy_id=account.strategy_id,
                    strategy_revision=1,
                    portfolio_snapshot_id=current.snapshot_id,
                    market_snapshot_id="repeated-carry-market",
                    request_fingerprint=f"repeated-carry-request-{index}",
                    events=(event,),
                    carry_accruals=(accrual,),
                )
                store.commit(batch)
                batches.append(batch)

            loaded = tuple(
                store.load_committed_batch(
                    account.strategy_id, batch.run_key, batch.request_fingerprint
                )
                for batch in batches
            )
            self.assertEqual(
                tuple(item.events for item in loaded), ((event,), (event,))
            )
            self.assertEqual(
                tuple(item.carry_accruals for item in loaded),
                ((accrual,), (accrual,)),
            )
            self.assertEqual(
                len(store.load_performance_view(account.strategy_id).batches),
                2,
            )

    def test_idempotent_commit_retry_revalidates_normalized_facts(self):
        with isolated_postgres_schema(DATABASE_URL) as schema:
            store = self.PostgresLedgerStore(DATABASE_URL, schema=schema)
            account = AccountSnapshot(
                id="account-retry-validation",
                strategy_id="retry-validation",
                strategy_revision=1,
                occurred_at=START,
                available_cash=1_000.0,
                snapshot_id="retry-validation-bootstrap",
            )
            store.create_account(account)
            batch = _simple_batch(
                account,
                run_key="retry-validation-run",
                intent_id="retry-validation-intent",
                market_snapshot_id="retry-validation-market",
            )
            store.commit(batch)

            import psycopg
            from psycopg import sql

            with psycopg.connect(DATABASE_URL) as connection:
                connection.execute(
                    sql.SQL(
                        "UPDATE {}.order_intents SET payload=jsonb_set("
                        "payload,'{{quantity}}','2'::jsonb) "
                        "WHERE strategy_id=%s AND run_key=%s"
                    ).format(sql.Identifier(schema)),
                    (account.strategy_id, batch.run_key),
                )

            with self.assertRaises(LedgerError):
                store.commit(batch)

    def test_concurrent_create_account_is_idempotent_and_fail_closed(self):
        import psycopg

        class BarrierCursor:
            def __init__(self, cursor, barrier):
                self._cursor = cursor
                self._barrier = barrier
                self._await_missing_account = False

            def __enter__(self):
                self._cursor.__enter__()
                return self

            def __exit__(self, *args):
                return self._cursor.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self._cursor, name)

            def execute(self, query, params=None):
                self._await_missing_account = (
                    "SELECT strategy_id FROM" in str(query)
                    and "accounts" in str(query)
                    and "FOR UPDATE" in str(query)
                )
                self._cursor.execute(query, params)
                return self

            def fetchone(self):
                row = self._cursor.fetchone()
                if self._await_missing_account and row is None:
                    self._barrier.wait(timeout=5)
                return row

        class BarrierConnection:
            def __init__(self, connection, barrier):
                self._connection = connection
                self._barrier = barrier

            def __enter__(self):
                self._connection.__enter__()
                return self

            def __exit__(self, *args):
                return self._connection.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self._connection, name)

            def cursor(self, *args, **kwargs):
                return BarrierCursor(
                    self._connection.cursor(*args, **kwargs), self._barrier
                )

        def run_pair(stores, accounts):
            barrier = threading.Barrier(2)
            results = []
            errors = []
            real_connect = psycopg.connect

            def synchronized_connect(*args, **kwargs):
                connection = real_connect(*args, **kwargs)
                connection.execute("SET lock_timeout = '5s'")
                connection.execute("SET statement_timeout = '8s'")
                return BarrierConnection(connection, barrier)

            def create(store, account):
                try:
                    results.append(store.create_account(account))
                except Exception as error:  # captured for cross-thread assertion
                    errors.append(error)

            with patch("psycopg.connect", side_effect=synchronized_connect):
                threads = tuple(
                    threading.Thread(target=create, args=(store, account))
                    for store, account in zip(stores, accounts)
                )
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
            return results, errors

        with isolated_postgres_schema(DATABASE_URL) as schema:
            stores = tuple(
                self.PostgresLedgerStore(DATABASE_URL, schema=schema) for _ in range(2)
            )
            same = AccountSnapshot(
                id="account-concurrent-same",
                strategy_id="concurrent-same",
                strategy_revision=1,
                occurred_at=START,
                available_cash=1_000.0,
                snapshot_id="concurrent-same-bootstrap",
            )
            results, errors = run_pair(stores, (same, same))
            self.assertEqual(errors, [])
            self.assertEqual(results, [same, same])
            self.assertEqual(stores[0].load(same.strategy_id), same)

            first = replace(
                same,
                id="account-concurrent-conflict",
                strategy_id="concurrent-conflict",
                snapshot_id="concurrent-conflict-bootstrap-a",
            )
            second = replace(
                first,
                available_cash=2_000.0,
                snapshot_id="concurrent-conflict-bootstrap-b",
            )
            results, errors = run_pair(stores, (first, second))
            self.assertEqual(len(results), 1)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], LedgerError)
            self.assertIn(
                stores[0].load(first.strategy_id),
                (first, second),
            )

    def test_performance_view_uses_one_database_snapshot(self):
        with isolated_postgres_schema(DATABASE_URL) as schema:
            reader = self.PostgresLedgerStore(DATABASE_URL, schema=schema)
            writer = self.PostgresLedgerStore(DATABASE_URL, schema=schema)
            account = AccountSnapshot(
                id="account-consistent-read",
                strategy_id="consistent-read",
                strategy_revision=1,
                occurred_at=START,
                available_cash=1_000.0,
                snapshot_id="consistent-read-bootstrap",
            )
            reader.create_account(account)
            batch = _simple_batch(
                account,
                run_key="consistent-read-run",
                intent_id="consistent-read-intent",
                market_snapshot_id="consistent-read-market",
            )

            account_read = threading.Event()
            write_finished = threading.Event()
            original_load_account = reader._load_account_cursor
            views = []
            errors = []

            def load_account_then_pause(cursor, strategy_id):
                loaded = original_load_account(cursor, strategy_id)
                account_read.set()
                if not write_finished.wait(timeout=8):
                    raise AssertionError("writer did not finish before read timeout")
                return loaded

            reader._load_account_cursor = load_account_then_pause

            def read_view():
                try:
                    views.append(reader.load_performance_view(account.strategy_id))
                except Exception as error:  # captured for cross-thread assertion
                    errors.append(error)

            def commit_after_account_read():
                try:
                    if not account_read.wait(timeout=8):
                        raise AssertionError("reader did not reach account barrier")
                    writer.commit(batch)
                except Exception as error:  # captured for cross-thread assertion
                    errors.append(error)
                finally:
                    write_finished.set()

            threads = (
                threading.Thread(target=read_view),
                threading.Thread(target=commit_after_account_read),
            )
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(views), 1)

            committed = writer.load(account.strategy_id)
            observed = (views[0].account.snapshot_id, len(views[0].batches))
            self.assertIn(
                observed,
                {
                    (account.snapshot_id, 0),
                    (committed.snapshot_id, 1),
                },
            )

    def test_normalized_fact_rows_are_required_to_rebuild_batches(self):
        mutations = {
            "missing_fact": (
                "DELETE FROM {}.order_intents WHERE strategy_id=%s AND run_key=%s",
                (),
            ),
            "modified_payload": (
                "UPDATE {}.order_intents SET payload=jsonb_set("
                "payload,'{{quantity}}','2'::jsonb) "
                "WHERE strategy_id=%s AND run_key=%s",
                (),
            ),
            "ordinal_gap": (
                "UPDATE {}.order_intents SET ordinal=2 "
                "WHERE strategy_id=%s AND run_key=%s",
                (),
            ),
            "extra_fact": (
                """
                INSERT INTO {}.order_intents
                (strategy_id,intent_id,run_key,strategy_revision,ordinal,
                 batch_ordinal,symbol,position_side,order_side,position_effect,
                 quantity,reason,created_snapshot_id,created_market_at,
                 cancelled_revision,payload)
                SELECT strategy_id,'normalized-authority-extra',run_key,
                       strategy_revision,1,NULL,symbol,position_side,order_side,
                       position_effect,quantity,reason,created_snapshot_id,
                       created_market_at,cancelled_revision,
                       jsonb_set(payload,'{{id}}',to_jsonb(
                           'normalized-authority-extra'::text))
                FROM {}.order_intents
                WHERE strategy_id=%s AND run_key=%s
                """,
                ("double_schema", "extra_definition"),
            ),
            "audit_payload": (
                "UPDATE {}.committed_runs SET batch_payload='{{}}'::jsonb "
                "WHERE strategy_id=%s AND run_key=%s",
                (),
            ),
            "invalid_event_source": (
                "UPDATE {}.events SET source_kind='SYSTEM' "
                "WHERE strategy_id=%s AND run_key=%s AND source_kind='BATCH'",
                (),
            ),
            "derived_as_batch_with_null_ordinal": (
                "UPDATE {}.events SET source_kind='BATCH' "
                "WHERE strategy_id=%s AND run_key=%s AND source_kind='DERIVED'",
                ("derived",),
            ),
            "batch_with_null_ordinal": (
                "UPDATE {}.events SET batch_ordinal=NULL "
                "WHERE strategy_id=%s AND run_key=%s AND source_kind='BATCH'",
                (),
            ),
            "derived_with_nonnull_ordinal": (
                "UPDATE {}.events SET batch_ordinal=0 "
                "WHERE strategy_id=%s AND run_key=%s AND source_kind='DERIVED'",
                ("derived",),
            ),
            "batch_as_derived_with_nonnull_ordinal": (
                "UPDATE {}.events SET source_kind='DERIVED' "
                "WHERE strategy_id=%s AND run_key=%s AND source_kind='BATCH'",
                (),
            ),
            "result_snapshot_id": (
                "UPDATE {}.committed_runs SET result_snapshot_id="
                "'tampered-result-snapshot' "
                "WHERE strategy_id=%s AND run_key=%s",
                (),
            ),
        }
        import psycopg
        from psycopg import sql

        for mutation, (statement, options) in mutations.items():
            with self.subTest(mutation=mutation):
                with isolated_postgres_schema(DATABASE_URL) as schema:
                    store = self.PostgresLedgerStore(DATABASE_URL, schema=schema)
                    account = AccountSnapshot(
                        id="account-normalized-authority",
                        strategy_id="normalized-authority",
                        strategy_revision=1,
                        occurred_at=START,
                        available_cash=100_000.0,
                        snapshot_id="normalized-authority-bootstrap",
                    )
                    store.create_account(account)
                    batch = (
                        _simple_filled_batch(account)
                        if "derived" in options
                        else _simple_batch(
                            account,
                            run_key="normalized-authority-run",
                            intent_id="normalized-authority-intent",
                            market_snapshot_id="normalized-authority-market",
                        )
                    )
                    store.commit(batch)
                    self.assertEqual(
                        store.load_committed_batch(
                            account.strategy_id,
                            batch.run_key,
                            batch.request_fingerprint,
                        ),
                        batch,
                    )
                    identifiers = [sql.Identifier(schema)]
                    if "double_schema" in options:
                        identifiers.append(sql.Identifier(schema))
                    with psycopg.connect(DATABASE_URL) as connection:
                        if "extra_definition" in options:
                            connection.execute(
                                sql.SQL(
                                    """
                                    INSERT INTO {}.order_intent_definitions
                                    (strategy_id,intent_id,intent_fingerprint,payload)
                                    SELECT strategy_id,'normalized-authority-extra',
                                           'tampered-extra-definition',
                                           jsonb_set(payload,'{{id}}',to_jsonb(
                                               'normalized-authority-extra'::text))
                                    FROM {}.order_intents
                                    WHERE strategy_id=%s AND run_key=%s
                                    """
                                ).format(
                                    sql.Identifier(schema),
                                    sql.Identifier(schema),
                                ),
                                (account.strategy_id, batch.run_key),
                            )
                        connection.execute(
                            sql.SQL(statement).format(*identifiers),
                            (account.strategy_id, batch.run_key),
                        )

                    readers = (
                        lambda: store.load_committed_batch(
                            account.strategy_id,
                            batch.run_key,
                            batch.request_fingerprint,
                        ),
                        lambda: store.load_performance_view(account.strategy_id),
                    )
                    for reader in readers:
                        with self.subTest(reader=reader), self.assertRaises(ValueError):
                            reader()

    def test_create_account_round_trips_financing_and_borrow_carry_history(self):
        with isolated_postgres_schema(DATABASE_URL) as schema:
            store = self.PostgresLedgerStore(DATABASE_URL, schema=schema)
            financing = AccrualLifecycle(
                id="financing-create-carry",
                started_on=START.date(),
            )
            borrow = AccrualLifecycle(
                id="borrow-create-carry-S0",
                started_on=START.date(),
            )
            account = AccountSnapshot(
                id="account-create-carry",
                strategy_id="create-carry",
                strategy_revision=1,
                occurred_at=START,
                available_cash=100_000.0,
                margin_loan=1_000.0,
                accrued_financing_cost=1.25,
                accrued_borrow_cost=0.75,
                financing_lifecycle=financing,
                positions=(
                    PositionSnapshot(
                        symbol="S0",
                        side=PositionSide.SHORT,
                        quantity=10,
                        average_cost=100.0,
                        current_price=99.0,
                        borrow_lifecycle=borrow,
                    ),
                ),
                carry_accruals=(
                    CarryAccrualRecord(
                        account_id="account-create-carry",
                        cost_type=CarryCostType.FINANCING,
                        accrual_date=START.date(),
                        elapsed_days=1,
                        amount=1.25,
                        lifecycle_id=financing.id,
                    ),
                    CarryAccrualRecord(
                        account_id="account-create-carry",
                        cost_type=CarryCostType.BORROW,
                        accrual_date=START.date(),
                        elapsed_days=1,
                        amount=0.75,
                        lifecycle_id=borrow.id,
                        symbol="S0",
                    ),
                ),
                snapshot_id="create-carry-bootstrap",
            )
            store.create_account(account)
            self.assertEqual(store.load(account.strategy_id), account)
            self.assertEqual(store.load_view(account.strategy_id).account, account)
            self.assertEqual(
                store.load_performance_view(account.strategy_id).account,
                account,
            )
            self.assertEqual(store.create_account(account), account)

    def test_committed_batches_and_facts_keep_database_commit_order(self):
        with isolated_postgres_schema(DATABASE_URL) as schema:
            store = self.PostgresLedgerStore(DATABASE_URL, schema=schema)
            account = AccountSnapshot(
                id="account-commit-order",
                strategy_id="commit-order",
                strategy_revision=1,
                occurred_at=START,
                available_cash=100_000.0,
                snapshot_id="commit-order-bootstrap",
            )
            store.create_account(account)
            z_batch = _simple_batch(
                account,
                run_key="z-run",
                intent_id="z-intent",
                market_snapshot_id="z-market",
            )
            z_result = store.commit(z_batch)
            a_batch = _simple_batch(
                z_result,
                run_key="a-run",
                intent_id="a-intent",
                market_snapshot_id="a-market",
            )
            a_result = store.commit(a_batch)

            import psycopg
            from psycopg import sql

            def commit_ordinals():
                with psycopg.connect(DATABASE_URL) as connection:
                    return tuple(
                        row[0]
                        for row in connection.execute(
                            sql.SQL(
                                "SELECT commit_ordinal FROM {}.committed_runs "
                                "WHERE strategy_id=%s ORDER BY commit_ordinal"
                            ).format(sql.Identifier(schema)),
                            (account.strategy_id,),
                        ).fetchall()
                    )

            before_retry = commit_ordinals()
            self.assertEqual(store.commit(a_batch), a_result)
            self.assertEqual(commit_ordinals(), before_retry)
            self.assertEqual(len(before_retry), 2)
            self.assertLess(before_retry[0], before_retry[1])

            performance = store.load_performance_view(account.strategy_id)
            self.assertEqual(
                tuple(item.run_key for item in performance.batches),
                ("z-run", "a-run"),
            )
            self.assertEqual(
                tuple(item.id for item in performance.intents),
                ("z-intent", "a-intent"),
            )
            self.assertEqual(
                tuple(
                    item.id
                    for item in performance.events
                    if item.type == "PIPELINE_COMPLETED"
                ),
                ("event-z-run", "event-a-run"),
            )

    def test_normalized_single_run_revision_idempotency_and_rollback(self):
        with isolated_postgres_schema(DATABASE_URL) as schema:
            store = self.PostgresLedgerStore(DATABASE_URL, schema=schema)
            account = AccountSnapshot(
                id="account-normalized-commit",
                strategy_id="normalized-commit",
                strategy_revision=1,
                occurred_at=START,
                available_cash=1_000.0,
                margin_loan=100.0,
                financing_lifecycle=AccrualLifecycle(
                    id="financing-normalized-commit",
                    started_on=START.date(),
                ),
                snapshot_id="normalized-commit-bootstrap",
            )
            store.create_account(account)
            intent = OrderIntent(
                id="normalized-partial-intent",
                symbol="L0",
                position_side=PositionSide.LONG,
                order_side=OrderSide.BUY,
                position_effect=PositionEffect.OPEN,
                quantity=10,
                reason="REBALANCE",
                created_snapshot_id="normalized-market-0",
                created_market_at=START,
            )
            fill_values = {
                "intent_id": intent.id,
                "symbol": intent.symbol,
                "position_side": intent.position_side,
                "order_side": intent.order_side,
                "snapshot_id": "normalized-market-1",
                "occurred_at": START + timedelta(days=1),
                "quantity": 5,
                "price": 10.0,
                "fees": 1.0,
                "commission": 1.0,
                "status": "PARTIAL",
            }
            detail = ExecutionProgressFill(
                id=stable_execution_progress_fill_id(**fill_values),
                **fill_values,
            )
            progress = OrderExecutionProgress(
                intent_id=intent.id,
                symbol=intent.symbol,
                position_side=intent.position_side,
                order_side=intent.order_side,
                intent_quantity=intent.quantity,
                execution_policy_fingerprint="normalized-policy-v1",
                fills=(detail,),
            )
            accrual = CarryAccrualRecord(
                account_id=account.id,
                cost_type=CarryCostType.FINANCING,
                accrual_date=(START + timedelta(days=1)).date(),
                elapsed_days=1,
                amount=2.0,
                lifecycle_id=account.financing_lifecycle.id,
            )
            carry_event = PortfolioEvent(
                id="normalized-financing-event",
                type="FINANCING_COST_ACCRUED",
                occurred_at=START + timedelta(days=1),
                data={
                    "account_id": account.id,
                    "cost_type": "FINANCING",
                    "lifecycle_id": accrual.lifecycle_id,
                    "symbol": None,
                    "accrual_date": accrual.accrual_date.isoformat(),
                    "elapsed_days": 1,
                    "amount": 2.0,
                },
            )
            batch = DecisionBatch(
                run_key="normalized-run-1",
                strategy_id=account.strategy_id,
                strategy_revision=1,
                portfolio_snapshot_id=account.snapshot_id,
                market_snapshot_id="normalized-market-1",
                request_fingerprint="normalized-request-1",
                intents=(intent,),
                fills=(
                    ExecutionFill(
                        intent_id=intent.id,
                        symbol=intent.symbol,
                        quantity=5,
                        price=10.0,
                        fees=1.0,
                        status="PARTIAL",
                    ),
                ),
                events=(carry_event,),
                execution_progress=(progress,),
                position_risk_updates=(
                    PositionRiskUpdate(
                        symbol="L0",
                        side=PositionSide.LONG,
                        peak_price=10.0,
                        trough_price=None,
                        trailing_active=False,
                        position_mode="NORMAL",
                    ),
                ),
                position_settlement_updates=(
                    PositionSettlementUpdate(
                        symbol="L0",
                        side=PositionSide.LONG,
                        quantity=5,
                        sellable_quantity=0,
                        sellable_on=(START + timedelta(days=2)).date(),
                    ),
                ),
                carry_accruals=(accrual,),
            )

            committed = store.commit(batch)
            self.assertEqual(committed.available_cash, 947.0)
            self.assertEqual(committed.accrued_financing_cost, 2.0)
            self.assertEqual(committed.positions[0].quantity, 5)
            self.assertEqual(committed.positions[0].sellable_quantity, 0)
            self.assertEqual(committed.positions[0].peak_price, 10.0)
            view = store.load_view(account.strategy_id)
            self.assertEqual(view.account, committed)
            self.assertEqual(view.open_intents, (intent,))
            self.assertEqual(view.execution_progress, (progress,))
            performance = store.load_performance_view(account.strategy_id)
            self.assertEqual(performance.batches, (batch,))
            self.assertEqual(performance.intents, (intent,))
            self.assertEqual(performance.execution_progress, (progress,))
            self.assertEqual(
                {item.type for item in performance.events},
                {
                    "ACCOUNT_OPENED",
                    "ORDER_PARTIAL",
                    "RISK_CHANGED",
                    "FINANCING_COST_ACCRUED",
                },
            )

            self.assertEqual(store.commit(batch), committed)
            with self.assertRaisesRegex(ValueError, "run_key.*different"):
                store.commit(replace(batch, diagnostics=({"code": "CONFLICT"},)))

            transition = RevisionTransition(
                id="normalized-transition-r2",
                strategy_id=account.strategy_id,
                expected_snapshot_id=committed.snapshot_id,
                from_revision=1,
                to_revision=2,
                occurred_at=START + timedelta(days=2),
            )
            transitioned = store.transition_revision(transition)
            self.assertEqual(transitioned.strategy_revision, 2)
            self.assertEqual(store.transition_revision(transition), transitioned)
            self.assertEqual(store.load_view(account.strategy_id).open_intents, ())
            self.assertIn(
                "REVISION_TRANSITIONED",
                {
                    item.type
                    for item in store.load_performance_view(account.strategy_id).events
                },
            )

            rollback_batch = DecisionBatch(
                run_key="normalized-rollback-run",
                strategy_id=account.strategy_id,
                strategy_revision=2,
                portfolio_snapshot_id=transitioned.snapshot_id,
                market_snapshot_id="normalized-market-rollback",
                request_fingerprint="normalized-rollback-request",
            )
            with patch.object(
                store,
                "_write_current_account",
                side_effect=RuntimeError("deliberate write failure"),
            ), self.assertRaisesRegex(RuntimeError, "deliberate write failure"):
                store.commit(rollback_batch)
            self.assertIsNone(
                store.load_committed_batch(
                    account.strategy_id,
                    rollback_batch.run_key,
                    rollback_batch.request_fingerprint,
                )
            )
            self.assertEqual(store.load(account.strategy_id), transitioned)

    def test_cleanup_after_success_assertion_exception_and_client_disconnect(self):
        before = non_test_schemas(DATABASE_URL)
        created = []

        with isolated_postgres_schema(DATABASE_URL) as schema:
            created.append(schema)
            self.assertTrue(schema_exists(DATABASE_URL, schema))

        failed_schema = None
        with self.assertRaises(AssertionError):
            with isolated_postgres_schema(DATABASE_URL) as schema:
                failed_schema = schema
                self.fail("deliberate assertion failure")
        created.append(failed_schema)

        raised_schema = None
        with self.assertRaisesRegex(RuntimeError, "deliberate body exception"):
            with isolated_postgres_schema(DATABASE_URL) as schema:
                raised_schema = schema
                raise RuntimeError("deliberate body exception")
        created.append(raised_schema)

        disconnected_schema = None
        with isolated_postgres_schema(DATABASE_URL) as schema:
            disconnected_schema = schema
            import psycopg

            connection = psycopg.connect(DATABASE_URL)
            backend_pid = connection.info.backend_pid
            with psycopg.connect(DATABASE_URL, autocommit=True) as controller:
                controller.execute("SELECT pg_terminate_backend(%s)", (backend_pid,))
            with self.assertRaises(psycopg.OperationalError):
                connection.execute("SELECT 1")
            connection.close()
        created.append(disconnected_schema)

        for schema in created:
            self.assertIsNotNone(schema)
            self.assertFalse(schema_exists(DATABASE_URL, schema))
        self.assertTrue(set(before).issubset(non_test_schemas(DATABASE_URL)))

    def test_one_month_replay_is_isolated_and_respects_all_caps(self):
        before = non_test_schemas(DATABASE_URL)
        before_database = non_test_database_fingerprint(DATABASE_URL)
        baseline_relations = tuple(
            (schema_name, table_name)
            for schema_name, table_name, *_rest in before_database
        )
        self.assertEqual(count_test_schemas(DATABASE_URL), 0)
        with isolated_postgres_schema(DATABASE_URL) as schema:
            pg_store = self.PostgresLedgerStore(DATABASE_URL, schema=schema)
            pg_result = run_paper_postgres_22_sessions(
                pg_store,
                strategy=long_short_strategy(),
            )
            memory_result = run_backtest_22_sessions(
                strategy=long_short_strategy(),
            )
            self.assertEqual(pg_result["sessions"], 22)
            self.assertLessEqual(pg_result["maximum_positions"], 10)
            self.assertLessEqual(pg_result["maximum_gross_exposure_pct"], 150.0)
            self.assertLessEqual(pg_result["maximum_net_exposure_pct"], 120.0)
            self.assertLessEqual(pg_result["maximum_short_exposure_pct"], 30.0)
            for session, (pg_facts, memory_facts) in enumerate(
                zip(pg_result["fingerprints"], memory_result["fingerprints"])
            ):
                for pg_bucket, memory_bucket in zip(pg_facts, memory_facts):
                    self.assertEqual(pg_bucket[0], memory_bucket[0])
                    if (
                        isinstance(pg_bucket[1], tuple)
                        and isinstance(memory_bucket[1], tuple)
                    ):
                        self.assertEqual(
                            len(pg_bucket[1]),
                            len(memory_bucket[1]),
                            f"session={session} bucket={pg_bucket[0]}",
                        )
                        for ordinal, (pg_row, memory_row) in enumerate(
                            zip(pg_bucket[1], memory_bucket[1])
                        ):
                            self.assertEqual(
                                pg_row,
                                memory_row,
                                f"session={session} bucket={pg_bucket[0]} "
                                f"row={ordinal}",
                            )
                    else:
                        self.assertEqual(
                            pg_bucket[1],
                            memory_bucket[1],
                            f"session={session} bucket={pg_bucket[0]}",
                        )
            self.assertEqual(pg_result["daily"], memory_result["daily"])
            self.assertIn("PARTIAL", pg_result["fill_statuses"])
            self.assertGreater(pg_result["financing_cost"], 0.0)
            self.assertGreater(pg_result["borrow_cost"], 0.0)
            self.assertIn("COVER_ONLY", pg_result["position_modes"])
            self.assertIn("BORROW_DATA_MISSING", pg_result["diagnostic_codes"])
            self.assertNotIn(
                "DELEVERAGING_PROJECTION_FAILED",
                pg_result["diagnostic_codes"],
            )
            self.assertIn("SHORT_STOP_LOSS", pg_result["intent_reasons"])
            self.assertIn("FINANCING_COST_ACCRUED", pg_result["event_types"])
            self.assertIn("BORROW_COST_ACCRUED", pg_result["event_types"])

            daily = pg_result["daily"]
            self.assertIn("BORROW_DATA_MISSING", daily[0]["diagnostic_codes"])
            self.assertIn("PARTIAL", daily[1]["fill_statuses"])
            self.assertEqual(daily[1]["open_progress"].get("L7"), "PARTIAL")
            self.assertIn("FILLED", daily[2]["fill_statuses"])
            self.assertNotIn("L7", daily[2]["open_progress"])
            self.assertIn("FINANCING_COST_ACCRUED", daily[2]["event_types"])
            self.assertIn("SHORT_SQUEEZE", daily[7]["risk_reasons"])
            self.assertEqual(daily[7]["position_modes"].get("S0"), "COVER_ONLY")
            self.assertEqual(daily[8]["position_modes"].get("S1"), "COVER_ONLY")
            self.assertEqual(daily[10]["margin"]["state"], "MARGIN_CALL")
            self.assertGreaterEqual(
                daily[10]["margin"]["final_margin_rate_pct"],
                90.0,
            )
            self.assertIn("SHORT_STOP_LOSS", daily[12]["intent_reasons"])
        self.assertFalse(schema_exists(DATABASE_URL, schema))
        self.assertTrue(set(before).issubset(non_test_schemas(DATABASE_URL)))
        # The PostgreSQL instance is shared: unrelated test schemas may appear
        # concurrently. Every table that existed before this replay must still
        # exist with identical definition, row count, and row multiset hash.
        self.assertEqual(
            non_test_database_fingerprint(
                DATABASE_URL,
                relations=baseline_relations,
            ),
            before_database,
        )
        self.assertEqual(count_test_schemas(DATABASE_URL), 0)

    def test_concurrent_duplicate_commit_is_idempotent(self):
        with isolated_postgres_schema(DATABASE_URL) as schema:
            store_a = self.PostgresLedgerStore(DATABASE_URL, schema=schema)
            store_b = self.PostgresLedgerStore(DATABASE_URL, schema=schema)
            strategy = long_short_strategy("month-concurrent")
            store_a.create_account(
                AccountSnapshot(
                    id="account-month-concurrent",
                    strategy_id=strategy["id"],
                    strategy_revision=1,
                    occurred_at=START,
                    available_cash=100_000.0,
                    snapshot_id="bootstrap-month-concurrent",
                )
            )
            engine = PortfolioEngine(
                signal_registry={
                    "month-long-v1": FixedSignals(
                        "month-long-v1", PositionSide.LONG, ("L0",)
                    ),
                    "month-short-v1": FixedSignals(
                        "month-short-v1", PositionSide.SHORT, ("S0",)
                    ),
                }
            )
            source = store_a.load(strategy["id"])
            request = PlanRequest(
                    run_key="same-run",
                    strategy=strategy,
                    account=source,
                    analyzed_rows=({"symbol": "L0"}, {"symbol": "S0"}),
                    market=_market(1),
                    borrow=_borrow(1),
                    event_calendar={"L0": None, "S0": None},
            )
            batch = replace(
                engine.evaluate(request),
                request_fingerprint=request_fingerprint(request),
            )
            results = []
            errors = []
            barrier = threading.Barrier(2)

            def commit_once(target_store):
                try:
                    barrier.wait()
                    results.append(target_store.commit(batch))
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [
                threading.Thread(target=commit_once, args=(store_a,)),
                threading.Thread(target=commit_once, args=(store_b,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertFalse(errors)
            self.assertEqual(len(results), 2)
            self.assertEqual(len({item.snapshot_id for item in results}), 1)
            self.assertEqual(
                store_a.load_committed_batch(
                    strategy["id"], "same-run", batch.request_fingerprint
                ),
                batch,
            )
            self.assertEqual(
                store_a.load_performance_view(strategy["id"]),
                store_b.load_performance_view(strategy["id"]),
            )
            import psycopg
            from psycopg import sql

            with psycopg.connect(DATABASE_URL) as connection:
                count = connection.execute(
                    sql.SQL(
                        "SELECT COUNT(*) FROM {}.committed_runs "
                        "WHERE strategy_id = %s AND run_key = %s"
                    ).format(sql.Identifier(schema)),
                    (strategy["id"], "same-run"),
                ).fetchone()[0]
            self.assertEqual(count, 1)
            expected_facts = _canonical_batch_facts(batch)
            table_expectations = {
                "order_intents": len(expected_facts[0]),
                "execution_progress": len(expected_facts[2]),
                "fills": sum(len(item.fills) for item in expected_facts[2]),
                "position_risk_facts": len(expected_facts[3]),
                "position_risk_updates": len(expected_facts[3]),
                "settlement_updates": len(expected_facts[4]),
                "carry_accruals": len(expected_facts[5]),
            }
            with psycopg.connect(DATABASE_URL) as connection:
                for table_name, expected in table_expectations.items():
                    actual = connection.execute(
                        sql.SQL("SELECT COUNT(*) FROM {}.{} WHERE strategy_id=%s").format(
                            sql.Identifier(schema),
                            sql.Identifier(table_name),
                        ),
                        (strategy["id"],),
                    ).fetchone()[0]
                    self.assertEqual(actual, expected, table_name)

            conflict_strategy = long_short_strategy("month-concurrent-conflict")
            conflict_account = AccountSnapshot(
                id="account-month-concurrent-conflict",
                strategy_id=conflict_strategy["id"],
                strategy_revision=1,
                occurred_at=START,
                available_cash=100_000.0,
                snapshot_id="bootstrap-month-concurrent-conflict",
            )
            store_a.create_account(conflict_account)
            conflict_request = PlanRequest(
                run_key="conflicting-run",
                strategy=conflict_strategy,
                account=store_a.load(conflict_strategy["id"]),
                analyzed_rows=({"symbol": "L0"}, {"symbol": "S0"}),
                market=_market(1),
                borrow=_borrow(1),
                event_calendar={"L0": None, "S0": None},
            )
            conflict_base = replace(
                engine.evaluate(conflict_request),
                request_fingerprint=request_fingerprint(conflict_request),
            )
            conflict_other = replace(
                conflict_base,
                diagnostics=(*conflict_base.diagnostics, {"code": "CONFLICT"}),
            )
            conflict_results = []
            conflict_errors = []
            conflict_barrier = threading.Barrier(2)

            def commit_conflict(target_store, candidate):
                try:
                    conflict_barrier.wait()
                    target_store.commit(candidate)
                    conflict_results.append(candidate)
                except Exception as exc:  # pragma: no cover - asserted below
                    conflict_errors.append(exc)

            conflict_threads = [
                threading.Thread(
                    target=commit_conflict,
                    args=(store_a, conflict_base),
                ),
                threading.Thread(
                    target=commit_conflict,
                    args=(store_b, conflict_other),
                ),
            ]
            for thread in conflict_threads:
                thread.start()
            for thread in conflict_threads:
                thread.join()
            self.assertEqual(len(conflict_results), 1)
            self.assertEqual(len(conflict_errors), 1)
            self.assertIsInstance(conflict_errors[0], ValueError)
            self.assertIn("different facts", str(conflict_errors[0]))
            winner = conflict_results[0]
            self.assertEqual(
                store_a.load_committed_batch(
                    conflict_strategy["id"],
                    conflict_base.run_key,
                    conflict_base.request_fingerprint,
                ),
                winner,
            )
            with psycopg.connect(DATABASE_URL) as connection:
                conflict_run_count = connection.execute(
                    sql.SQL(
                        "SELECT COUNT(*) FROM {}.committed_runs "
                        "WHERE strategy_id=%s AND run_key=%s"
                    ).format(sql.Identifier(schema)),
                    (conflict_strategy["id"], conflict_base.run_key),
                ).fetchone()[0]
            self.assertEqual(conflict_run_count, 1)


if __name__ == "__main__":
    unittest.main()
