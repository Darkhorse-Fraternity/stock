from __future__ import annotations

import hashlib
import math
import statistics
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

from .market_regime import evaluate_market_regime, filter_absolute_momentum
from .markets import market_date, market_profile, strategy_market
from .portfolio_engine.borrow import AVAILABLE, BorrowSecurity, BorrowSnapshot
from .portfolio_engine.contracts import (
    AccountSnapshot,
    MarketSnapshot,
    PlanRequest,
    PortfolioEvent,
    ProcessRequest,
)
from .portfolio_engine.ledger import InMemoryLedgerStore, LEDGER_SCHEMA_VERSION
from .portfolio_engine.service import PortfolioEngine
from .signal_engine import extract_signal_features, rank_signal_rows, select_ranked_signals
from .utils import number


SHANGHAI = ZoneInfo("Asia/Shanghai")
FEE_FIELDS = (
    "commission_rate_pct",
    "minimum_commission_cny",
    "stamp_duty_rate_pct",
    "transfer_fee_rate_pct",
    "slippage_bps",
)
PORTFOLIO_ENGINE_BACKTEST_VERSION = "portfolio-engine-v2"


def _finite_cost_multiplier(value: object) -> float:
    if type(value) not in (int, float):
        raise TypeError("cost_multiplier must be an int or float")
    try:
        multiplier = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("cost_multiplier must be finite and nonnegative") from exc
    if not math.isfinite(multiplier) or multiplier < 0:
        raise ValueError("cost_multiplier must be finite and nonnegative")
    return 0.0 if multiplier == 0 else multiplier


def _strict_date(value: object, field_name: str) -> date:
    if type(value) is date:
        return value
    if type(value) is not str:
        raise TypeError(f"{field_name} must be an ISO date string or date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO date") from exc


@dataclass(frozen=True)
class HistoricalBorrowRecord:
    day: date
    symbol: str
    shortable: bool
    easy_to_borrow: bool
    available_quantity: int | None
    borrow_apr_pct: float

    def __post_init__(self) -> None:
        if type(self.day) is not date:
            raise TypeError("day must be a date")
        if type(self.symbol) is not str or not self.symbol:
            raise ValueError("symbol must be a non-empty string")
        if type(self.shortable) is not bool or type(self.easy_to_borrow) is not bool:
            raise TypeError("borrow flags must be bool values")
        if self.available_quantity is not None and (
            type(self.available_quantity) is not int or self.available_quantity < 0
        ):
            raise ValueError("available_quantity must be a nonnegative integer or None")
        if type(self.borrow_apr_pct) not in (int, float):
            raise TypeError("borrow_apr_pct must be an int or float")
        if not math.isfinite(float(self.borrow_apr_pct)) or self.borrow_apr_pct < 0:
            raise ValueError("borrow_apr_pct must be finite and nonnegative")


@dataclass(frozen=True)
class BorrowResolution:
    snapshot: BorrowSnapshot
    estimated: bool
    history_complete: bool
    borrow_apr_by_symbol: Mapping[str, float]

    def __post_init__(self) -> None:
        if type(self.snapshot) is not BorrowSnapshot:
            raise TypeError("snapshot must be BorrowSnapshot")
        if type(self.estimated) is not bool or type(self.history_complete) is not bool:
            raise TypeError("borrow resolution flags must be bool values")
        object.__setattr__(
            self,
            "borrow_apr_by_symbol",
            MappingProxyType(dict(self.borrow_apr_by_symbol)),
        )


@dataclass(frozen=True)
class HistoricalBorrowBook:
    records: Mapping[date, Mapping[str, HistoricalBorrowRecord]]
    declared_complete: bool

    def __post_init__(self) -> None:
        if type(self.declared_complete) is not bool:
            raise TypeError("declared_complete must be a bool")
        copied: dict[date, Mapping[str, HistoricalBorrowRecord]] = {}
        for day, values in self.records.items():
            if type(day) is not date:
                raise TypeError("borrow history keys must be dates")
            if not isinstance(values, Mapping):
                raise TypeError("borrow history day values must be mappings")
            day_records: dict[str, HistoricalBorrowRecord] = {}
            for symbol, record in values.items():
                if type(symbol) is not str or not symbol:
                    raise ValueError("borrow history symbols must be non-empty strings")
                if type(record) is not HistoricalBorrowRecord:
                    raise TypeError("borrow history values must be HistoricalBorrowRecord")
                if record.day != day or record.symbol != symbol:
                    raise ValueError("borrow record identity must match its history keys")
                day_records[symbol] = record
            copied[day] = MappingProxyType(day_records)
        object.__setattr__(self, "records", MappingProxyType(copied))

    @classmethod
    def from_raw(
        cls,
        value: object,
        *,
        history_complete: bool,
    ) -> HistoricalBorrowBook:
        if type(history_complete) is not bool:
            raise TypeError("history_complete must be a bool")
        if not isinstance(value, Mapping):
            raise TypeError("borrow history must be a date mapping")
        records: dict[date, dict[str, HistoricalBorrowRecord]] = {}
        for raw_day, raw_symbols in value.items():
            day = _strict_date(raw_day, "borrow history date")
            if day in records:
                raise ValueError(f"duplicate borrow history date: {day.isoformat()}")
            if not isinstance(raw_symbols, Mapping):
                raise TypeError("borrow history symbols must be a mapping")
            day_records: dict[str, HistoricalBorrowRecord] = {}
            for raw_symbol, raw_record in raw_symbols.items():
                symbol = str(raw_symbol).strip()
                if not symbol or not isinstance(raw_record, Mapping):
                    raise TypeError("borrow history records require symbol mappings")
                if symbol in day_records:
                    raise ValueError(f"duplicate borrow history symbol: {symbol}")
                shortable = raw_record.get("shortable")
                easy = raw_record.get("easy_to_borrow")
                if type(shortable) is not bool or type(easy) is not bool:
                    raise TypeError("historical borrow flags must be bool values")
                raw_quantity = raw_record.get("available_quantity")
                if raw_quantity is not None and type(raw_quantity) is not int:
                    raise TypeError("historical borrow quantity must be an integer or None")
                raw_apr = raw_record.get("borrow_apr_pct")
                if type(raw_apr) not in (int, float):
                    raise TypeError("historical borrow APR must be an int or float")
                day_records[symbol] = HistoricalBorrowRecord(
                    day=day,
                    symbol=symbol,
                    shortable=shortable,
                    easy_to_borrow=easy,
                    available_quantity=raw_quantity,
                    borrow_apr_pct=float(raw_apr),
                )
            records[day] = day_records
        return cls(records=records, declared_complete=history_complete)

    def resolve(
        self,
        occurred_at: datetime,
        symbols: Iterable[str],
        *,
        estimated_borrow_apr_pct: object,
        cost_multiplier: object,
        market: str = "us",
    ) -> BorrowResolution:
        if type(occurred_at) is not datetime or occurred_at.tzinfo is None:
            raise ValueError("borrow resolution time must be timezone-aware")
        multiplier = _finite_cost_multiplier(cost_multiplier)
        if type(estimated_borrow_apr_pct) not in (int, float):
            raise TypeError("estimated_borrow_apr_pct must be an int or float")
        estimated_apr = float(estimated_borrow_apr_pct)
        if not math.isfinite(estimated_apr) or estimated_apr < 0:
            raise ValueError("estimated_borrow_apr_pct must be finite and nonnegative")
        day = market_date(occurred_at, market)
        exact = self.records.get(day, {})
        requested = tuple(sorted(set(symbols)))
        if any(type(symbol) is not str or not symbol for symbol in requested):
            raise ValueError("borrow symbols must be non-empty strings")
        securities: dict[str, BorrowSecurity] = {}
        aprs: dict[str, float] = {}
        estimated = False
        for symbol in requested:
            record = exact.get(symbol)
            if record is None:
                estimated = True
                apr = estimated_apr * multiplier
                securities[symbol] = BorrowSecurity(
                    symbol=symbol,
                    shortable=True,
                    easy_to_borrow=True,
                    borrow_apr_pct=apr,
                    available_quantity=None,
                )
            else:
                apr = record.borrow_apr_pct * multiplier
                securities[symbol] = BorrowSecurity(
                    symbol=symbol,
                    shortable=record.shortable,
                    easy_to_borrow=record.easy_to_borrow,
                    borrow_apr_pct=apr,
                    available_quantity=record.available_quantity,
                )
            aprs[symbol] = apr
        source = "estimated" if estimated else "historical"
        identity = hashlib.sha256(
            repr(
                (
                    day.isoformat(),
                    source,
                    tuple(
                        (
                            symbol,
                            security.shortable,
                            security.easy_to_borrow,
                            security.available_quantity,
                            security.borrow_apr_pct,
                        )
                        for symbol, security in sorted(securities.items())
                    ),
                )
            ).encode("utf-8")
        ).hexdigest()[:16]
        return BorrowResolution(
            snapshot=BorrowSnapshot(
                id=f"borrow:{day.isoformat()}:{identity}",
                status=AVAILABLE,
                securities=securities,
            ),
            estimated=estimated,
            history_complete=self.declared_complete and not estimated,
            borrow_apr_by_symbol=aprs,
        )


@dataclass(frozen=True)
class EngineReplayFrame:
    kind: str
    market: MarketSnapshot
    analyzed_rows: tuple[Mapping[str, Any], ...] = ()
    event_calendar: Mapping[str, int | None] = MappingProxyType({})
    record_nav: bool = False

    def __post_init__(self) -> None:
        if self.kind not in {"PLAN", "PROCESS"}:
            raise ValueError("frame kind must be PLAN or PROCESS")
        if type(self.market) is not MarketSnapshot:
            raise TypeError("frame market must be MarketSnapshot")
        rows = tuple(self.analyzed_rows)
        if any(not isinstance(item, Mapping) for item in rows):
            raise TypeError("analyzed_rows items must be mappings")
        if self.kind == "PROCESS" and (rows or self.event_calendar):
            raise ValueError("PROCESS frame must not carry signal inputs")
        if type(self.record_nav) is not bool:
            raise TypeError("record_nav must be a bool")
        object.__setattr__(self, "analyzed_rows", rows)
        object.__setattr__(self, "event_calendar", MappingProxyType(dict(self.event_calendar)))

    @classmethod
    def plan(
        cls,
        market: MarketSnapshot,
        *,
        analyzed_rows: Iterable[Mapping[str, Any]],
        event_calendar: Mapping[str, int | None],
        record_nav: bool = False,
    ) -> EngineReplayFrame:
        return cls(
            kind="PLAN",
            market=market,
            analyzed_rows=tuple(analyzed_rows),
            event_calendar=event_calendar,
            record_nav=record_nav,
        )

    @classmethod
    def process(
        cls,
        market: MarketSnapshot,
        *,
        record_nav: bool = False,
    ) -> EngineReplayFrame:
        return cls(kind="PROCESS", market=market, record_nav=record_nav)


@dataclass(frozen=True)
class EngineReplayResult:
    replay_mode: str
    event_fingerprints: tuple[tuple[tuple[Any, ...], ...], ...]
    fill_fingerprints: tuple[tuple[tuple[Any, ...], ...], ...]
    position_snapshots: tuple[tuple[tuple[Any, ...], ...], ...]
    nav_series: tuple[float, ...]
    final_nav: float
    final_positions: tuple[tuple[Any, ...], ...]
    event_types: tuple[str, ...]
    metrics: Mapping[str, float]
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


def _event_semantic_fingerprint(event: PortfolioEvent) -> tuple[Any, ...]:
    data = event.data
    return (
        event.type,
        data.get("account_id"),
        data.get("cost_type"),
        data.get("lifecycle_id"),
        data.get("symbol"),
        data.get("accrual_date"),
        data.get("intent_id"),
        data.get("quantity"),
        data.get("status"),
    )


def _positions_fingerprint(account: AccountSnapshot) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            item.symbol,
            item.side.value,
            item.quantity,
            item.average_cost,
            item.current_price,
            item.position_mode,
        )
        for item in sorted(account.positions, key=lambda value: value.symbol)
    )


def _cost_stressed_strategy(strategy: Mapping[str, Any], multiplier: float) -> dict:
    replay = deepcopy(dict(strategy))
    replay["id"] = str(replay.get("id") or "isolated-backtest")
    replay["name"] = str(replay.get("name") or "隔离回测")
    replay.setdefault("lifecycle", {})["stage"] = "paper"
    portfolio = replay.setdefault("portfolio", {})
    for field in FEE_FIELDS:
        portfolio[field] = number(portfolio.get(field)) * multiplier
    margin = replay.setdefault("margin_policy", {})
    margin["financing_apr_pct"] = number(margin.get("financing_apr_pct")) * multiplier
    short = replay.setdefault("short_policy", {})
    short["estimated_borrow_apr_pct"] = number(short.get("estimated_borrow_apr_pct"))
    return replay


def replay_engine_frames(
    *,
    strategy: Mapping[str, Any],
    frames: Iterable[EngineReplayFrame],
    borrow_book: HistoricalBorrowBook,
    signal_registry: Mapping[str, Any] | None = None,
    replay_mode: str = "backtest",
    cost_multiplier: object = 1.0,
) -> EngineReplayResult:
    """Replay immutable frames through the exact Paper PortfolioEngine boundary."""

    if replay_mode not in {"paper", "backtest"}:
        raise ValueError("replay_mode must be paper or backtest")
    multiplier = _finite_cost_multiplier(cost_multiplier)
    if type(borrow_book) is not HistoricalBorrowBook:
        raise TypeError("borrow_book must be HistoricalBorrowBook")
    timeline = tuple(frames)
    if not timeline or any(type(item) is not EngineReplayFrame for item in timeline):
        raise ValueError("frames must contain at least one EngineReplayFrame")
    for previous, current in zip(timeline, timeline[1:]):
        if current.market.occurred_at < previous.market.occurred_at:
            raise ValueError("replay frame times must not move backwards")
    replay_strategy = _cost_stressed_strategy(strategy, multiplier)
    strategy_id = str(replay_strategy["id"])
    strategy_revision = int(replay_strategy["revision"])
    initial_cash = number(replay_strategy.get("portfolio", {}).get("initial_cash"))
    if initial_cash <= 0:
        raise ValueError("portfolio initial_cash must be positive")
    account = AccountSnapshot(
        id=f"account:{strategy_id}",
        strategy_id=strategy_id,
        strategy_revision=strategy_revision,
        occurred_at=timeline[0].market.occurred_at,
        available_cash=initial_cash,
        snapshot_id=f"snapshot:{strategy_id}:initial",
    )

    event_frames: list[tuple[tuple[Any, ...], ...]] = []
    fill_frames: list[tuple[tuple[Any, ...], ...]] = []
    position_frames: list[tuple[tuple[Any, ...], ...]] = []
    nav_series: list[float] = []
    event_types: list[str] = []
    seen_event_ids: set[str] = set()
    estimated_any = False
    history_complete = True
    all_aprs: list[float] = []
    total_fees = 0.0

    with TemporaryDirectory(prefix="stock-backtest-ledger-") as directory:
        ledger = InMemoryLedgerStore(Path(directory) / "portfolio-v2.lock")
        ledger.create_account(account)
        engine = PortfolioEngine(signal_registry=signal_registry, ledger_store=ledger)
        for index, frame in enumerate(timeline):
            short_policy = replay_strategy.get("short_policy", {})
            resolution = borrow_book.resolve(
                frame.market.occurred_at,
                frame.market.quotes,
                estimated_borrow_apr_pct=number(
                    short_policy.get("estimated_borrow_apr_pct")
                ),
                cost_multiplier=multiplier,
                market=strategy_market(replay_strategy),
            )
            estimated_any = estimated_any or resolution.estimated
            history_complete = history_complete and resolution.history_complete
            all_aprs.extend(resolution.borrow_apr_by_symbol.values())
            current_account = ledger.load(strategy_id)
            run_key = f"engine-replay:{index:04d}:{frame.kind.lower()}:{frame.market.id}"
            if frame.kind == "PLAN":
                batch = engine.plan_and_commit(
                    PlanRequest(
                        run_key=run_key,
                        strategy=replay_strategy,
                        account=current_account,
                        analyzed_rows=frame.analyzed_rows,
                        market=frame.market,
                        borrow=resolution.snapshot,
                        event_calendar=frame.event_calendar,
                    )
                )
            else:
                batch = engine.process_and_commit(
                    ProcessRequest(
                        run_key=run_key,
                        strategy=replay_strategy,
                        account=current_account,
                        market=frame.market,
                        borrow=resolution.snapshot,
                    )
                )
            total_fees += sum(fill.fees for fill in batch.fills)
            fill_frames.append(
                tuple(
                    (
                        fill.intent_id,
                        fill.symbol,
                        fill.quantity,
                        fill.price,
                        fill.status,
                    )
                    for fill in batch.fills
                )
            )
            new_events = tuple(
                event
                for event in batch.events
                if event.id not in seen_event_ids
            )
            seen_event_ids.update(event.id for event in new_events)
            event_types.extend(event.type for event in new_events)
            event_frames.append(
                tuple(_event_semantic_fingerprint(event) for event in new_events)
            )
            if frame.record_nav:
                snapshot = engine.performance(strategy_id, frame.market)
                position_frames.append(_positions_fingerprint(snapshot.account))
                nav_series.append(snapshot.metrics.equity)

        final_frame = timeline[-1]
        final_snapshot = engine.performance(strategy_id, final_frame.market)
        final_positions = _positions_fingerprint(final_snapshot.account)
        if not nav_series:
            position_frames.append(final_positions)
            nav_series.append(final_snapshot.metrics.equity)
        ledger.validate_integrity()

    apr_summary = {
        "minimum": min(all_aprs) if all_aprs else 0.0,
        "maximum": max(all_aprs) if all_aprs else 0.0,
        "average": statistics.fmean(all_aprs) if all_aprs else 0.0,
    }
    return EngineReplayResult(
        replay_mode=replay_mode,
        event_fingerprints=tuple(event_frames),
        fill_fingerprints=tuple(fill_frames),
        position_snapshots=tuple(position_frames),
        nav_series=tuple(nav_series),
        final_nav=final_snapshot.metrics.equity,
        final_positions=final_positions,
        event_types=tuple(event_types),
        metrics={
            "transaction_fees": total_fees,
            "financing_cost": final_snapshot.metrics.accrued_financing_cost,
            "borrow_cost": final_snapshot.metrics.accrued_borrow_cost,
        },
        metadata={
            "financing_apr_pct": number(
                replay_strategy.get("margin_policy", {}).get("financing_apr_pct")
            ),
            "borrow_apr_pct": apr_summary,
            "cost_multiplier": multiplier,
            "borrow_cost_estimated": estimated_any,
            "borrow_history_complete": history_complete,
            "portfolio_engine_version": PORTFOLIO_ENGINE_BACKTEST_VERSION,
            "ledger_schema_version": LEDGER_SCHEMA_VERSION,
            "strategy_schema_version": int(replay_strategy.get("version", 0)),
            "strategy_revision": strategy_revision,
            "policy_revision": strategy_revision,
        },
    )


def normalize_universe_snapshots(value: object) -> dict[date, set[str]]:
    snapshots: dict[date, set[str]] = {}
    if isinstance(value, dict):
        entries = value.items()
    elif isinstance(value, list):
        entries = ((item.get("date"), item.get("symbols")) for item in value if isinstance(item, dict))
    else:
        entries = ()
    for raw_day, raw_symbols in entries:
        try:
            day = raw_day if isinstance(raw_day, date) else datetime.strptime(str(raw_day)[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if not isinstance(raw_symbols, (list, tuple, set)):
            continue
        symbols = {str(symbol).strip() for symbol in raw_symbols if str(symbol).strip()}
        if symbols:
            snapshots[day] = symbols
    return dict(sorted(snapshots.items()))


def normalize_event_calendar_history(
    value: object,
) -> dict[date, dict[str, int | None]]:
    """Parse the JSON dataset calendar into exact, date-keyed snapshots."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("event calendar history must be a date mapping")
    history: dict[date, dict[str, int | None]] = {}
    for raw_day, raw_symbols in value.items():
        day = _strict_date(raw_day, "event calendar history date")
        if day in history:
            raise ValueError(
                f"duplicate event calendar history date: {day.isoformat()}"
            )
        if not isinstance(raw_symbols, Mapping):
            raise TypeError("event calendar history symbols must be a mapping")
        calendar: dict[str, int | None] = {}
        for raw_symbol, raw_sessions in raw_symbols.items():
            if type(raw_symbol) is not str or not raw_symbol.strip():
                raise ValueError("event calendar symbols must be non-empty strings")
            symbol = raw_symbol.strip()
            if symbol in calendar:
                raise ValueError(f"duplicate event calendar symbol: {symbol}")
            if raw_sessions is not None and (
                type(raw_sessions) is not int or raw_sessions < 0
            ):
                raise ValueError(
                    "event calendar sessions must be a nonnegative integer or null"
                )
            calendar[symbol] = raw_sessions
        history[day] = calendar
    return dict(sorted(history.items()))


def universe_for_day(
    snapshots: dict[date, set[str]],
    cutoff: date,
    fallback: set[str],
    *,
    ordered_days: tuple[date, ...] | None = None,
) -> tuple[set[str], bool]:
    snapshot_days = ordered_days if ordered_days is not None else tuple(snapshots)
    index = bisect_right(snapshot_days, cutoff) - 1
    if index < 0:
        return set(fallback), False
    return set(snapshots[snapshot_days[index]]), True


def _replay_strategy(strategy: dict, cost_multiplier: float) -> dict:
    multiplier = _finite_cost_multiplier(cost_multiplier)
    replay = _cost_stressed_strategy(strategy, multiplier)
    replay.setdefault("portfolio", {})["enabled"] = True
    return replay


def _quote(symbol: str, row: dict, *, phase: str, previous: dict | None = None) -> dict:
    price = (
        number(row.get("entry_price")) or number(row.get("open"))
        if phase == "open"
        else number(row.get("exit_price")) or number(row.get("close"))
    )
    previous_close = number((previous or {}).get("close"))
    percent = (price / previous_close - 1) * 100 if previous_close > 0 and price > 0 else 0.0
    quote = {
        "symbol": symbol,
        "name": str(row.get("name") or symbol),
        "price": price,
        "percent": percent,
        "volume": number(row.get("volume")),
        "turnover": number(row.get("turnover")),
        "bar_open": price,
        "bar_high": price if phase == "open" else number(row.get("high"), default=price),
        "bar_low": price if phase == "open" else number(row.get("low"), default=price),
    }
    for field in ("upper_limit", "lower_limit"):
        if number(row.get(field)) > 0:
            quote[field] = number(row.get(field))
    if row.get(f"{phase}_volume") is not None:
        quote["bar_volume"] = number(row.get(f"{phase}_volume"))
    return quote


def _benchmark_return(
    benchmark: dict[date, dict] | None,
    panel: dict[str, dict[date, dict]],
    previous_day: date,
    current_day: date,
    eligible: set[str],
) -> float:
    if benchmark and previous_day in benchmark and current_day in benchmark:
        previous = number(benchmark[previous_day].get("close"))
        current = number(benchmark[current_day].get("close"))
        return current / previous - 1 if previous > 0 and current > 0 else 0.0
    returns = []
    for symbol in eligible:
        history = panel.get(symbol) or {}
        if previous_day not in history or current_day not in history:
            continue
        previous = number(history[previous_day].get("close"))
        current = number(history[current_day].get("close"))
        if previous > 0 and current > 0:
            returns.append(current / previous - 1)
    return statistics.fmean(returns) if returns else 0.0


def _short_signal_fields(rows: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    history = tuple(rows)
    closes = [number(item.get("close")) for item in history]
    if len(closes) < 61 or any(price <= 0 for price in closes):
        return {}
    daily_returns = [
        current / previous - 1
        for previous, current in zip(closes[-21:-1], closes[-20:])
        if previous > 0
    ]
    volatility = (
        statistics.pstdev(daily_returns) * math.sqrt(252)
        if len(daily_returns) >= 2
        else 0.0
    )
    turnovers = [
        number(item.get("turnover"))
        or number(item.get("close")) * number(item.get("volume"))
        for item in history[-20:]
    ]
    return {
        "price": closes[-1],
        "ma20": statistics.fmean(closes[-20:]),
        "ma60": statistics.fmean(closes[-60:]),
        "volatility20": volatility,
        "turnover": statistics.fmean(turnovers) if turnovers else 0.0,
        "one_day_return": closes[-1] / closes[-2] - 1,
    }


def _calendar_for_day(
    history: Mapping[date, Mapping[str, int | None]] | None,
    cutoff: date,
    symbols: Iterable[str],
) -> dict[str, int | None]:
    if not history:
        return {}
    exact = history.get(cutoff)
    if not isinstance(exact, Mapping):
        return {}
    allowed = set(symbols)
    result: dict[str, int | None] = {}
    for symbol, sessions in exact.items():
        if symbol not in allowed:
            continue
        if sessions is not None and (type(sessions) is not int or sessions < 0):
            continue
        result[symbol] = sessions
    return result


def replay_portfolio_fold(
    panel: dict[str, dict[date, dict]],
    benchmark: dict[date, dict] | None,
    test_dates: list[date],
    all_dates: list[date],
    strategy: dict,
    *,
    universe_snapshots: dict[date, set[str]] | None = None,
    minimum_history: int = 61,
    top_n: int = 3,
    cost_multiplier: float = 1.0,
    execution_price_mode: str = "daily_open_close_proxy",
    borrow_book: HistoricalBorrowBook | None = None,
    event_calendar_history: Mapping[date, Mapping[str, int | None]] | None = None,
    replay_mode: str = "backtest",
) -> dict:
    if not test_dates:
        return {
            "days": [],
            "fold_return": 0.0,
            "benchmark_return": 0.0,
            "maximum_drawdown": 0.0,
            "coverage_complete": False,
            "execution_data_coverage_complete": False,
            "event_calendar_coverage_complete": False,
        }
    multiplier = _finite_cost_multiplier(cost_multiplier)
    signal_strategy = _replay_strategy(strategy, 1.0)
    profile = market_profile(strategy_market(signal_strategy))
    date_index = {day: index for index, day in enumerate(all_dates)}
    snapshots = universe_snapshots or {}
    snapshot_days = tuple(snapshots)
    fallback = set(panel)
    history_dates = {symbol: tuple(history) for symbol, history in panel.items()}
    initial_cash = number(signal_strategy.get("portfolio", {}).get("initial_cash"))
    if initial_cash <= 0:
        raise ValueError("portfolio initial_cash must be positive")
    benchmark_equity = 1.0
    coverage_complete = bool(snapshots)
    exact_execution_prices = execution_price_mode == "intraday_0935_1500"
    execution_data_coverage_complete = exact_execution_prices
    event_calendar_coverage_complete = bool(event_calendar_history)
    frames: list[EngineReplayFrame] = []
    day_facts: list[dict[str, Any]] = []

    for signal_day in test_dates:
        signal_index = date_index.get(signal_day, -1)
        if signal_index < minimum_history or signal_index <= 0:
            continue
        cutoff_day = all_dates[signal_index - 1]
        eligible, covered = universe_for_day(snapshots, cutoff_day, fallback, ordered_days=snapshot_days)
        coverage_complete = coverage_complete and covered
        signal_rows = []
        for symbol in sorted(eligible):
            history = panel.get(symbol) or {}
            symbol_dates = history_dates.get(symbol) or ()
            available_end = bisect_left(symbol_dates, signal_day)
            if available_end < minimum_history or cutoff_day not in history or signal_day not in history:
                continue
            available_dates = symbol_dates[available_end - minimum_history : available_end]
            features = extract_signal_features(
                [history[day] for day in available_dates],
                minimum_rows=minimum_history,
            )
            if not features:
                continue
            short_fields = _short_signal_fields(
                [history[day] for day in available_dates]
            )
            signal_rows.append(
                {
                    "symbol": symbol,
                    "name": str(history[cutoff_day].get("name") or symbol),
                    "price": number(history[cutoff_day].get("close")),
                    "percent": features.get("latest_return", 0.0) * 100,
                    "signal_features": features,
                    "cutoff_date": cutoff_day.isoformat(),
                    "momentum20": features.get("momentum20"),
                    "momentum60": features.get("momentum60"),
                    **short_fields,
                }
            )
        ranked = rank_signal_rows(signal_rows, strategy=signal_strategy)
        market_regime = evaluate_market_regime(ranked, signal_strategy)
        eligible_signals = filter_absolute_momentum(
            ranked,
            signal_strategy,
            market_regime,
        )
        selected = select_ranked_signals(
            eligible_signals,
            top_n,
            strategy=signal_strategy,
        )
        selected_symbols = {str(item["symbol"]) for item in selected}
        analyzed_rows = tuple(
            {
                **dict(item),
                "selected_for_long": str(item.get("symbol")) in selected_symbols,
            }
            for item in ranked
        )
        cutoff_quotes: dict[str, dict[str, Any]] = {}
        open_quotes: dict[str, dict[str, Any]] = {}
        close_quotes: dict[str, dict[str, Any]] = {}
        for symbol in sorted(panel):
            history = panel.get(symbol) or {}
            row = history.get(signal_day)
            previous = history.get(cutoff_day)
            if not row or not previous:
                execution_data_coverage_complete = False
                continue
            required_execution_fields = [
                "entry_price",
                "exit_price",
                "open_volume",
                "close_volume",
            ]
            if profile.has_daily_price_limits:
                required_execution_fields.extend(["upper_limit", "lower_limit"])
            if any(row.get(field) is None for field in required_execution_fields):
                execution_data_coverage_complete = False
            cutoff_quotes[symbol] = _quote(
                symbol,
                previous,
                phase="close",
                previous=history.get(all_dates[signal_index - 2])
                if signal_index >= 2
                else None,
            )
            open_quotes[symbol] = _quote(
                symbol,
                row,
                phase="open",
                previous=previous,
            )
            close_quotes[symbol] = _quote(
                symbol,
                row,
                phase="close",
                previous=previous,
            )
        cutoff_time = datetime.combine(
            cutoff_day,
            profile.session_end,
            tzinfo=profile.timezone,
        )
        open_time = datetime.combine(
            signal_day,
            time(9, 35),
            tzinfo=profile.timezone,
        )
        close_time = datetime.combine(
            signal_day,
            profile.session_end,
            tzinfo=profile.timezone,
        )
        calendar_symbols = tuple(
            str(item["symbol"])
            for item in analyzed_rows
            if type(item.get("symbol")) is str and item.get("symbol")
        )
        calendar = _calendar_for_day(
            event_calendar_history,
            cutoff_day,
            calendar_symbols,
        )
        event_calendar_coverage_complete = (
            event_calendar_coverage_complete
            and set(calendar_symbols).issubset(calendar)
        )
        frames.extend(
            (
                EngineReplayFrame.plan(
                    MarketSnapshot(
                        id=f"backtest:{cutoff_day.isoformat()}:cutoff",
                        occurred_at=cutoff_time,
                        quotes=cutoff_quotes,
                    ),
                    analyzed_rows=analyzed_rows,
                    event_calendar=calendar,
                ),
                EngineReplayFrame.process(
                    MarketSnapshot(
                        id=f"backtest:{signal_day.isoformat()}:open",
                        occurred_at=open_time,
                        quotes=open_quotes,
                    )
                ),
                EngineReplayFrame.process(
                    MarketSnapshot(
                        id=f"backtest:{signal_day.isoformat()}:close",
                        occurred_at=close_time,
                        quotes=close_quotes,
                    ),
                    record_nav=True,
                ),
            )
        )
        benchmark_daily = _benchmark_return(benchmark, panel, cutoff_day, signal_day, eligible)
        benchmark_equity *= 1 + benchmark_daily
        day_facts.append(
            {
                "signal_date": signal_day.isoformat(),
                "cutoff_date": cutoff_day.isoformat(),
                "symbols": [item["symbol"] for item in selected],
                "market_regime": market_regime["state"],
                "market_regime_detail": deepcopy(market_regime),
                "target_exposure_pct": market_regime["target_exposure_pct"],
                "benchmark_return": benchmark_daily,
            }
        )

    if not frames:
        return {
            "days": [],
            "fold_return": 0.0,
            "benchmark_return": benchmark_equity - 1,
            "maximum_drawdown": 0.0,
            "coverage_complete": coverage_complete,
            "execution_data_coverage_complete": execution_data_coverage_complete,
            "event_calendar_coverage_complete": event_calendar_coverage_complete,
        }
    replay = replay_engine_frames(
        strategy=signal_strategy,
        frames=frames,
        borrow_book=borrow_book or HistoricalBorrowBook.from_raw(
            {},
            history_complete=False,
        ),
        replay_mode=replay_mode,
        cost_multiplier=multiplier,
    )
    day_results: list[dict[str, Any]] = []
    previous_nav = initial_cash
    peak_nav = initial_cash
    maximum_drawdown = 0.0
    for index, (facts, nav, positions) in enumerate(
        zip(day_facts, replay.nav_series, replay.position_snapshots, strict=True)
    ):
        daily_return = nav / previous_nav - 1 if previous_nav > 0 else 0.0
        previous_nav = nav
        peak_nav = max(peak_nav, nav)
        drawdown = nav / peak_nav - 1 if peak_nav > 0 else 0.0
        maximum_drawdown = min(maximum_drawdown, drawdown)
        frame_events = replay.event_fingerprints[index * 3 : index * 3 + 3]
        day_results.append(
            {
                **facts,
                "nav": round(nav, 4),
                "daily_return": daily_return,
                "excess_return": daily_return - facts["benchmark_return"],
                "drawdown": drawdown,
                "positions": len(positions),
                "actions": [
                    fingerprint[0]
                    for event_frame in frame_events
                    for fingerprint in event_frame
                ],
            }
        )

    final_nav = replay.final_nav
    return {
        "days": day_results,
        "fold_return": final_nav / initial_cash - 1 if initial_cash > 0 else 0.0,
        "benchmark_return": benchmark_equity - 1,
        "maximum_drawdown": maximum_drawdown,
        "coverage_complete": coverage_complete,
        "execution_data_coverage_complete": execution_data_coverage_complete,
        "event_calendar_coverage_complete": event_calendar_coverage_complete,
        "closed_trades": sum(
            1
            for event_type in replay.event_types
            if event_type == "POSITION_CLOSED"
        ),
        "final_positions": len(replay.final_positions),
        "event_fingerprints": replay.event_fingerprints,
        "fill_fingerprints": replay.fill_fingerprints,
        "position_snapshots": replay.position_snapshots,
        "nav_series": replay.nav_series,
        "cost_metrics": dict(replay.metrics),
        "metadata": dict(replay.metadata),
    }
