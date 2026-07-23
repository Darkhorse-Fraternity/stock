from __future__ import annotations

import json
import math
import os
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:  # pragma: no cover - fcntl is present on the Linux deployment and macOS dev host.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from .data_sources import fetch_watchlist_quotes
from .market_regime import allocation_config, normalize_market_regime_decision
from .parameters import load_strategy_config, normalize_portfolio_config
from .portfolio_pipeline import ENTRY_PIPELINE_VERSION, run_entry_pipeline
from .runtime import assert_strategy_runnable
from .universe import constrain_to_watchlist
from .utils import beijing_now, number


PORTFOLIO_STORE_VERSION = 1
OPEN_ORDER_STATES = {"INTENDED", "ACCEPTED", "PARTIAL"}
ACTION_EVENT_TYPES = {
    "ORDER_INTENDED",
    "ORDER_FILLED",
    "ORDER_PARTIAL",
    "ORDER_CANCELLED",
    "ORDER_EXPIRED",
    "EXIT_TRIGGERED",
    "RISK_CHANGED",
    "STRATEGY_VERSION_ACTIVATED",
}
STORE_LOCK = threading.RLock()


def portfolio_store_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    configured = os.getenv("STOCK_AGENT_PORTFOLIO_PATH", "data/strategy_portfolios.json").strip()
    return Path(configured or "data/strategy_portfolios.json").expanduser()


@contextmanager
def _file_lock(target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_suffix(f"{target.suffix}.lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _empty_store() -> dict:
    return {"version": PORTFOLIO_STORE_VERSION, "accounts": {}}


def _read_store_unlocked(target: Path) -> dict:
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_store()
    accounts = payload.get("accounts") if isinstance(payload, dict) else None
    return {
        "version": PORTFOLIO_STORE_VERSION,
        "accounts": accounts if isinstance(accounts, dict) else {},
    }


def _write_store_unlocked(target: Path, store: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def load_portfolio_store(path: str | Path | None = None) -> dict:
    target = portfolio_store_path(path)
    with STORE_LOCK, _file_lock(target):
        return deepcopy(_read_store_unlocked(target))


def _timestamp(now: datetime) -> str:
    return beijing_now(now).isoformat(timespec="seconds")


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return beijing_now(parsed)


def _next_business_date(value: date) -> date:
    current = value + timedelta(days=1)
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current


def _round(value: float, digits: int = 6) -> float:
    return round(float(value), digits)


def _new_account(strategy: dict, now: datetime) -> dict:
    config = normalize_portfolio_config(strategy.get("portfolio"))
    initial_cash = number(config["initial_cash"])
    max_positions = int(config["max_positions"])
    account = {
        "id": strategy.get("id"),
        "strategy_id": strategy.get("id"),
        "strategy_name": strategy.get("name") or "股票策略",
        "strategy_revision": int(strategy.get("revision") or 1),
        "strategy_stage": strategy.get("lifecycle", {}).get("stage", "draft"),
        "signal_model": strategy.get("signal", {}).get("model", "factor_rank_v1"),
        "signal_time": strategy.get("signal", {}).get("run_time", "08:00"),
        "last_market_regime": None,
        "target_exposure_pct": 100.0,
        "created_at": _timestamp(now),
        "updated_at": _timestamp(now),
        "initial_cash": initial_cash,
        "cash": initial_cash,
        "reserved_cash": 0.0,
        "high_water_nav": initial_cash,
        "latest_nav": initial_cash,
        "latest_complete_nav": initial_cash,
        "risk_level": "NORMAL",
        "trading_mode": "RUNNING",
        "control_epoch": 1,
        "portfolio_config": config,
        "slots": [
            {"id": index + 1, "state": "AVAILABLE", "symbol": None}
            for index in range(max_positions)
        ],
        "positions": {},
        "orders": [],
        "closed_trades": [],
        "events": [],
        "nav_history": [],
        "committed_run_keys": [],
        "event_keys": [],
        "last_candidates": [],
        "last_candidate_date": None,
    }
    _append_event(
        account,
        now,
        "ACCOUNT_OPENED",
        "模拟策略账户已创建",
        key=f"account-opened:{strategy.get('id')}",
        data={"initial_cash": initial_cash, "max_positions": max_positions},
    )
    _append_nav(account, now)
    return account


def create_portfolio_account(strategy: dict, *, now: datetime | None = None) -> dict:
    """Create an isolated in-memory account for replay and simulation."""
    current = beijing_now(now)
    if not strategy.get("id"):
        raise ValueError("策略必须先保存后才能创建持仓账户")
    return deepcopy(_new_account(strategy, current))


def _append_event(
    account: dict,
    now: datetime,
    event_type: str,
    message: str,
    *,
    key: str,
    data: dict | None = None,
) -> dict | None:
    event_keys = account.setdefault("event_keys", [])
    if key in event_keys:
        return None
    event = {
        "id": uuid.uuid4().hex,
        "key": key,
        "type": event_type,
        "occurred_at": _timestamp(now),
        "strategy_revision": account.get("strategy_revision", 1),
        "message": str(message),
        "data": deepcopy(data or {}),
    }
    event_keys.append(key)
    account.setdefault("events", []).append(event)
    account["event_keys"] = event_keys[-5000:]
    account["events"] = account["events"][-5000:]
    return event


def _mutate_account(
    strategy: dict,
    now: datetime,
    path: str | Path | None,
    mutation: Callable[[dict], list[dict]],
) -> tuple[dict, list[dict]]:
    if not strategy.get("id"):
        raise ValueError("策略必须先保存后才能创建持仓账户")
    target = portfolio_store_path(path)
    with STORE_LOCK, _file_lock(target):
        store = _read_store_unlocked(target)
        account = store["accounts"].get(strategy["id"])
        if not isinstance(account, dict):
            account = _new_account(strategy, now)
        events = mutation(account)
        account["updated_at"] = _timestamp(now)
        store["accounts"][strategy["id"]] = account
        _write_store_unlocked(target, store)
        return deepcopy(account), deepcopy(events)


def _mutate_in_memory(
    account: dict,
    strategy: dict,
    now: datetime,
    mutation: Callable[[dict], list[dict]],
) -> tuple[dict, list[dict]]:
    if str(account.get("strategy_id") or "") != str(strategy.get("id") or ""):
        raise ValueError("回放账户与策略不匹配")
    events = mutation(account)
    account["updated_at"] = _timestamp(now)
    return account, events


def load_portfolio_account(
    strategy_id: str | None = None,
    *,
    path: str | Path | None = None,
) -> dict | None:
    store = load_portfolio_store(path)
    target_id = strategy_id
    if not target_id:
        target_id = load_strategy_config().get("id")
    account = store["accounts"].get(str(target_id or ""))
    return deepcopy(account) if isinstance(account, dict) else None


def _open_orders(account: dict, side: str | None = None) -> list[dict]:
    return [
        order
        for order in account.get("orders", [])
        if order.get("status") in OPEN_ORDER_STATES and (side is None or order.get("side") == side)
    ]


def _position_value(position: dict) -> float:
    price = number(position.get("current_price"), default=number(position.get("average_cost")))
    return max(0.0, price * number(position.get("quantity")))


def _account_nav(account: dict) -> tuple[float, float]:
    market_value = sum(_position_value(position) for position in account.get("positions", {}).values())
    return number(account.get("cash")) + market_value, market_value


def _append_nav(account: dict, now: datetime) -> None:
    nav, market_value = _account_nav(account)
    high_water = max(number(account.get("high_water_nav"), default=nav), nav)
    account["high_water_nav"] = _round(high_water)
    account["latest_nav"] = _round(nav)
    account["latest_complete_nav"] = _round(nav)
    drawdown = (high_water - nav) / high_water * 100 if high_water > 0 else 0.0
    point = {
        "at": _timestamp(now),
        "nav": _round(nav),
        "cash": _round(number(account.get("cash"))),
        "market_value": _round(market_value),
        "cumulative_return_pct": _round((nav / number(account.get("initial_cash"), default=nav) - 1) * 100),
        "drawdown_pct": _round(drawdown),
        "risk_level": account.get("risk_level", "NORMAL"),
        "trading_mode": account.get("trading_mode", "RUNNING"),
    }
    history = account.setdefault("nav_history", [])
    if history and history[-1].get("at") == point["at"]:
        history[-1] = point
    else:
        history.append(point)
    account["nav_history"] = history[-5000:]


def _refresh_reservations(account: dict) -> None:
    account["reserved_cash"] = _round(
        sum(number(order.get("reserved_cash")) for order in _open_orders(account, "BUY"))
    )


def _cancel_order(account: dict, order: dict, now: datetime, reason: str) -> dict | None:
    if order.get("status") not in OPEN_ORDER_STATES:
        return None
    order["status"] = "CANCELLED"
    order["cancel_reason"] = reason
    order["updated_at"] = _timestamp(now)
    if order.get("side") == "BUY":
        order["reserved_cash"] = 0.0
        symbol = order.get("symbol")
        if symbol not in account.get("positions", {}) and not any(
            candidate is not order
            and candidate.get("symbol") == symbol
            and candidate.get("side") == "BUY"
            and candidate.get("status") in OPEN_ORDER_STATES
            for candidate in account.get("orders", [])
        ):
            for slot in account.get("slots", []):
                if slot.get("symbol") == symbol and slot.get("state") == "RESERVED":
                    slot.update({"state": "AVAILABLE", "symbol": None})
    _refresh_reservations(account)
    return _append_event(
        account,
        now,
        "ORDER_CANCELLED",
        f"{order.get('name') or order.get('symbol')} 的{order.get('side')}订单已取消：{reason}",
        key=f"order-cancelled:{order['id']}:{reason}",
        data={"order_id": order["id"], "symbol": order.get("symbol"), "reason": reason},
    )


def _activate_strategy(account: dict, strategy: dict, now: datetime) -> list[dict]:
    revision = int(strategy.get("revision") or 1)
    config = normalize_portfolio_config(strategy.get("portfolio"))
    changed = revision != int(account.get("strategy_revision") or 1) or config != account.get("portfolio_config")
    account["strategy_name"] = strategy.get("name") or account.get("strategy_name")
    account["strategy_stage"] = strategy.get("lifecycle", {}).get("stage", "draft")
    account["signal_model"] = strategy.get("signal", {}).get("model", "factor_rank_v1")
    account["signal_time"] = strategy.get("signal", {}).get("run_time", "08:00")
    if not changed:
        account["portfolio_config"] = config
        return []
    events: list[dict] = []
    account["control_epoch"] = int(account.get("control_epoch") or 0) + 1
    for order in list(_open_orders(account)):
        event = _cancel_order(account, order, now, "策略版本切换")
        if event:
            events.append(event)
    for position in account.get("positions", {}).values():
        position["signal_invalid_days"] = 0
        position["comparison_baseline_score"] = number(position.get("current_score"))
    previous_revision = account.get("strategy_revision")
    account["strategy_revision"] = revision
    account["portfolio_config"] = config
    max_positions = int(config["max_positions"])
    slots = account.setdefault("slots", [])
    while len(slots) < max_positions:
        slots.append({"id": len(slots) + 1, "state": "AVAILABLE", "symbol": None})
    event = _append_event(
        account,
        now,
        "STRATEGY_VERSION_ACTIVATED",
        f"策略版本从 v{previous_revision} 切换到 v{revision}",
        key=f"strategy-version:{revision}:{account['control_epoch']}",
        data={"from_revision": previous_revision, "to_revision": revision, "control_epoch": account["control_epoch"]},
    )
    if event:
        events.append(event)
    return events


def _available_slot(account: dict) -> dict | None:
    max_positions = int(account.get("portfolio_config", {}).get("max_positions", 10))
    return next((slot for slot in account.get("slots", [])[:max_positions] if slot.get("state") == "AVAILABLE"), None)


def _estimated_buy_reservation(account: dict, price: float) -> tuple[int, float]:
    config = account["portfolio_config"]
    nav = number(account.get("latest_nav"), default=number(account.get("initial_cash")))
    available = max(0.0, number(account.get("cash")) - number(account.get("reserved_cash")))
    current_exposure = sum(
        int(number(position.get("quantity"))) * number(position.get("current_price"))
        for position in account.get("positions", {}).values()
    ) + number(account.get("reserved_cash"))
    exposure_budget = nav * number(account.get("target_exposure_pct"), default=100.0) / 100
    remaining_exposure = max(0.0, exposure_budget - current_exposure)
    budget = min(nav * number(config["target_weight_pct"]) / 100, available, remaining_exposure)
    worst_price = price * 1.10
    commission_rate = number(config["commission_rate_pct"]) / 100
    transfer_rate = number(config["transfer_fee_rate_pct"]) / 100
    minimum = number(config["minimum_commission_cny"])
    if budget <= minimum or worst_price <= 0:
        return 0, 0.0
    approximate_unit_cost = worst_price * (1 + commission_rate + transfer_rate)
    quantity = math.floor((budget - minimum) / approximate_unit_cost / 100) * 100
    if quantity <= 0:
        return 0, 0.0
    notional = worst_price * quantity
    fees = max(minimum, notional * commission_rate) + notional * transfer_rate
    return quantity, _round(notional + fees)


def _create_order(
    account: dict,
    now: datetime,
    *,
    side: str,
    symbol: str,
    name: str,
    quantity: int,
    reason: str,
    slot_id: int,
    signal_price: float = 0.0,
    score: float | None = None,
    reserved_cash: float = 0.0,
    replacement_candidate: dict | None = None,
) -> tuple[dict, dict | None]:
    purpose = "ENTRY" if side == "BUY" else "EXIT"
    key = f"{account['strategy_id']}:{_timestamp(now)}:{slot_id}:{symbol}:{side}:{reason}"
    existing = next((item for item in account.get("orders", []) if item.get("key") == key), None)
    if existing:
        return existing, None
    order = {
        "id": uuid.uuid4().hex,
        "key": key,
        "strategy_revision": account.get("strategy_revision", 1),
        "control_epoch": account.get("control_epoch", 1),
        "side": side,
        "purpose": purpose,
        "symbol": symbol,
        "name": name or symbol,
        "slot_id": slot_id,
        "quantity": int(quantity),
        "filled_quantity": 0,
        "filled_notional": 0.0,
        "commission_charged": 0.0,
        "status": "INTENDED",
        "reason": reason,
        "signal_price": _round(signal_price),
        "score": _round(score) if score is not None else None,
        "reserved_cash": _round(reserved_cash),
        "valid_date": beijing_now(now).date().isoformat() if side == "BUY" else None,
        "created_at": _timestamp(now),
        "updated_at": _timestamp(now),
        "replacement_candidate": deepcopy(replacement_candidate),
    }
    account.setdefault("orders", []).append(order)
    event = _append_event(
        account,
        now,
        "ORDER_INTENDED",
        f"计划{('买入' if side == 'BUY' else '退出')} {name or symbol}：{reason}",
        key=f"order-intended:{order['id']}",
        data={
            "order_id": order["id"], "side": side, "symbol": symbol, "name": name or symbol,
            "quantity": quantity, "reason": reason, "signal_price": signal_price, "slot_id": slot_id,
        },
    )
    _refresh_reservations(account)
    return order, event


def _has_exit_order(account: dict, symbol: str) -> bool:
    return any(order.get("symbol") == symbol and order.get("side") == "SELL" for order in _open_orders(account))


def _trigger_exit(account: dict, position: dict, now: datetime, reason: str) -> list[dict]:
    if _has_exit_order(account, position["symbol"]):
        return []
    quantity = int(number(position.get("quantity")))
    if quantity <= 0:
        return []
    event = _append_event(
        account,
        now,
        "EXIT_TRIGGERED",
        f"{position.get('name') or position['symbol']} 触发退出：{reason}",
        key=f"exit:{position['id']}:{reason}:{account.get('control_epoch')}:{_timestamp(now)[:16]}",
        data={"position_id": position["id"], "symbol": position["symbol"], "reason": reason},
    )
    _, order_event = _create_order(
        account,
        now,
        side="SELL",
        symbol=position["symbol"],
        name=position.get("name") or position["symbol"],
        quantity=quantity,
        reason=reason,
        slot_id=int(position["slot_id"]),
        signal_price=number(position.get("current_price")),
        score=number(position.get("current_score")),
    )
    return [item for item in (event, order_event) if item]


def _expire_old_entries(account: dict, now: datetime) -> list[dict]:
    events: list[dict] = []
    current_date = beijing_now(now).date().isoformat()
    for order in _open_orders(account, "BUY"):
        if order.get("valid_date") == current_date:
            continue
        order["status"] = "EXPIRED"
        order["reserved_cash"] = 0.0
        order["updated_at"] = _timestamp(now)
        symbol = order.get("symbol")
        if symbol not in account.get("positions", {}):
            for slot in account.get("slots", []):
                if slot.get("symbol") == symbol and slot.get("state") == "RESERVED":
                    slot.update({"state": "AVAILABLE", "symbol": None})
        event = _append_event(
            account,
            now,
            "ORDER_EXPIRED",
            f"{order.get('name') or symbol} 入场订单已过期",
            key=f"order-expired:{order['id']}",
            data={"order_id": order["id"], "symbol": symbol},
        )
        if event:
            events.append(event)
    _refresh_reservations(account)
    return events


def plan_daily_candidates(
    strategy: dict,
    candidates: Iterable[dict],
    *,
    now: datetime | None = None,
    path: str | Path | None = None,
    account: dict | None = None,
    market_regime: dict,
) -> tuple[dict, list[dict]]:
    assert_strategy_runnable(strategy, execution_kind="scheduled", mode="report")
    current = beijing_now(now)
    raw_candidates = tuple(deepcopy(dict(item)) for item in candidates)
    decision = normalize_market_regime_decision(market_regime, strategy)

    def mutation(account: dict) -> list[dict]:
        events = _activate_strategy(account, strategy, current)
        _roll_settlements(account, current)
        run_key = (
            f"daily:{current.date().isoformat()}:"
            f"strategy-r{int(strategy.get('revision') or 1)}:"
            f"entry-pipeline-v{ENTRY_PIPELINE_VERSION}"
        )
        if run_key in account.setdefault("committed_run_keys", []):
            return events
        normalized, admitted_candidates, pipeline_trace = run_entry_pipeline(
            strategy,
            account,
            raw_candidates,
            run_id=run_key,
            as_of=_timestamp(current),
            market_regime=decision,
        )
        account["last_market_regime"] = deepcopy(decision)
        account["target_exposure_pct"] = decision["target_exposure_pct"]
        account["last_pipeline_trace"] = deepcopy(pipeline_trace)
        pipeline_event = _append_event(
            account,
            current,
            "PIPELINE_COMPLETED",
            f"入场 Pipeline 已完成：{len(admitted_candidates)} 个候选通过",
            key=f"pipeline:{run_key}:{account.get('control_epoch', 1)}",
            data={
                "run_id": run_key,
                "stages": pipeline_trace,
                "admitted": len(admitted_candidates),
                "market_regime": deepcopy(decision),
            },
        )
        if pipeline_event:
            events.append(pipeline_event)
        events.extend(_expire_old_entries(account, current))
        regime_config = allocation_config(strategy)
        if decision["target_exposure_pct"] <= 0 and regime_config.get("exit_on_risk_off", True):
            for order in list(_open_orders(account, "BUY")):
                event = _cancel_order(account, order, current, f"MARKET_REGIME_{decision['state']}")
                if event:
                    events.append(event)
            for position in sorted(
                account.get("positions", {}).values(),
                key=lambda item: (number(item.get("current_score")), item.get("symbol") or ""),
            ):
                events.extend(_trigger_exit(account, position, current, f"MARKET_REGIME_{decision['state']}"))
        elif regime_config.get("rebalance_to_target_exposure", True):
            nav = number(account.get("latest_nav"), default=number(account.get("initial_cash")))
            target_value = nav * decision["target_exposure_pct"] / 100
            projected_value = sum(
                int(number(position.get("quantity")))
                * number(position.get("current_price"), default=number(position.get("average_cost")))
                for position in account.get("positions", {}).values()
            ) + number(account.get("reserved_cash"))
            if projected_value > target_value:
                for order in list(_open_orders(account, "BUY")):
                    released_cash = number(order.get("reserved_cash"))
                    event = _cancel_order(account, order, current, f"MARKET_REGIME_{decision['state']}_REBALANCE")
                    if event:
                        events.append(event)
                    projected_value -= released_cash
                for position in sorted(
                    account.get("positions", {}).values(),
                    key=lambda item: (number(item.get("current_score")), item.get("symbol") or ""),
                ):
                    if projected_value <= target_value:
                        break
                    events.extend(_trigger_exit(account, position, current, f"MARKET_REGIME_{decision['state']}_REBALANCE"))
                    projected_value -= int(number(position.get("quantity"))) * number(
                        position.get("current_price"), default=number(position.get("average_cost"))
                    )
        by_symbol = {item["symbol"]: item for item in normalized}
        for position in account.get("positions", {}).values():
            candidate = by_symbol.get(position["symbol"])
            if candidate:
                position["signal_invalid_days"] = 0
                position["current_score"] = candidate["score"]
            else:
                position["signal_invalid_days"] = int(position.get("signal_invalid_days") or 0) + 1
                if position["signal_invalid_days"] >= int(account["portfolio_config"]["signal_invalid_days"]):
                    events.extend(_trigger_exit(account, position, current, "SIGNAL_INVALIDATED"))
        account["last_candidates"] = deepcopy(normalized)
        account["last_candidate_date"] = current.date().isoformat()

        occupied = len(account.get("positions", {})) + len(
            [order for order in _open_orders(account, "BUY") if order.get("symbol") not in account.get("positions", {})]
        )
        max_positions = int(account["portfolio_config"]["max_positions"])
        target_weight = max(0.01, number(account["portfolio_config"]["target_weight_pct"], default=10.0))
        exposure_slots = max(0, int(decision["target_exposure_pct"] // target_weight))
        effective_max_positions = min(max_positions, exposure_slots)
        current_symbols = set(account.get("positions", {})) | {order.get("symbol") for order in _open_orders(account, "BUY")}
        if account.get("trading_mode") == "RUNNING" and account["portfolio_config"].get("enabled", True):
            for candidate in admitted_candidates:
                if occupied >= effective_max_positions or candidate["symbol"] in current_symbols:
                    continue
                slot = _available_slot(account)
                if slot is None:
                    break
                quantity, reservation = _estimated_buy_reservation(account, candidate["price"])
                if quantity <= 0:
                    break
                slot.update({"state": "RESERVED", "symbol": candidate["symbol"]})
                _, event = _create_order(
                    account,
                    current,
                    side="BUY",
                    symbol=candidate["symbol"],
                    name=candidate["name"],
                    quantity=quantity,
                    reason="SIGNAL_ENTRY",
                    slot_id=int(slot["id"]),
                    signal_price=candidate["price"],
                    score=candidate["score"],
                    reserved_cash=reservation,
                )
                if event:
                    events.append(event)
                current_symbols.add(candidate["symbol"])
                occupied += 1

        if account.get("trading_mode") == "RUNNING" and effective_max_positions > 0 and occupied >= effective_max_positions and admitted_candidates:
            new_candidate = next((item for item in admitted_candidates if item["symbol"] not in current_symbols), None)
            positions = [position for position in account.get("positions", {}).values() if position.get("sellable_quantity", 0) > 0]
            weakest = min(positions, key=lambda item: (number(item.get("current_score")), item["symbol"]), default=None)
            if new_candidate and weakest:
                deteriorated = weakest["symbol"] not in by_symbol and number(weakest.get("current_score")) <= number(weakest.get("comparison_baseline_score"))
                delta = new_candidate["score"] - number(weakest.get("current_score"))
                config = account["portfolio_config"]
                round_trip_cost_pct = (
                    number(config["commission_rate_pct"]) * 2
                    + number(config["stamp_duty_rate_pct"])
                    + number(config["transfer_fee_rate_pct"]) * 2
                    + number(config["slippage_bps"]) * 2 / 100
                )
                required_delta = max(
                    number(config["replacement_score_delta"]),
                    round_trip_cost_pct / 100 * number(config["replacement_cost_multiple"]),
                )
                if deteriorated and delta >= required_delta:
                    weakest["replacement_candidate"] = {
                        **deepcopy(new_candidate),
                        "score_delta": _round(delta, 6),
                        "required_delta": _round(required_delta, 6),
                    }
                    events.extend(_trigger_exit(account, weakest, current, "REPLACED_BY_STRONGER_CANDIDATE"))

        account["committed_run_keys"].append(run_key)
        account["committed_run_keys"] = account["committed_run_keys"][-2000:]
        _refresh_reservations(account)
        _append_nav(account, current)
        return events

    if account is not None:
        return _mutate_in_memory(account, strategy, current, mutation)
    return _mutate_account(strategy, current, path, mutation)


def _commission_increment(order: dict, notional: float, config: dict) -> float:
    cumulative = number(order.get("filled_notional")) + notional
    required = max(number(config["minimum_commission_cny"]), cumulative * number(config["commission_rate_pct"]) / 100)
    return max(0.0, required - number(order.get("commission_charged")))


def _fill_capacity(order: dict, quote: dict, config: dict) -> int:
    remaining = int(order["quantity"] - order.get("filled_quantity", 0))
    bar_volume = quote.get("bar_volume")
    if bar_volume is None:
        return remaining
    capacity = int(number(bar_volume) * number(config["max_bar_participation_pct"]) / 100)
    if order["side"] == "BUY":
        return min(remaining, capacity // 100 * 100)
    if remaining < 100 and capacity >= remaining:
        return remaining
    return min(remaining, capacity // 100 * 100)


def _limit_locked(order: dict, quote: dict) -> bool:
    base = number(quote.get("bar_open"), default=number(quote.get("price")))
    high = number(quote.get("bar_high"), default=number(quote.get("high"), default=base))
    low = number(quote.get("bar_low"), default=number(quote.get("low"), default=base))
    upper = number(quote.get("upper_limit"))
    lower = number(quote.get("lower_limit"))
    if order["side"] == "BUY" and upper > 0 and base >= upper and low >= upper:
        return True
    if order["side"] == "SELL" and lower > 0 and base <= lower and high <= lower:
        return True
    return False


def _execute_order(account: dict, order: dict, quote: dict, now: datetime) -> list[dict]:
    events: list[dict] = []
    if order.get("status") not in OPEN_ORDER_STATES:
        return events
    created_at = _parse_time(order.get("created_at"))
    if created_at is None or created_at >= now:
        return events
    if order["side"] == "BUY":
        if order.get("valid_date") != now.date().isoformat():
            return events
        if order.get("control_epoch") != account.get("control_epoch") or account.get("trading_mode") != "RUNNING":
            event = _cancel_order(account, order, now, "当前风险状态禁止入场")
            return [event] if event else []
    if _limit_locked(order, quote):
        order["last_block_reason"] = "LIMIT_LOCKED"
        order["updated_at"] = _timestamp(now)
        return events
    base_price = number(quote.get("bar_open"), default=number(quote.get("price")))
    if base_price <= 0:
        order["last_block_reason"] = "NO_VALID_PRICE"
        return events
    config = account["portfolio_config"]
    quantity = _fill_capacity(order, quote, config)
    if order["side"] == "SELL":
        position = account.get("positions", {}).get(order["symbol"])
        quantity = min(quantity, int(number((position or {}).get("sellable_quantity"))))
    if quantity <= 0:
        order["last_block_reason"] = "NO_EXECUTABLE_QUANTITY"
        return events
    slippage = number(config["slippage_bps"]) / 10_000
    fill_price = base_price * (1 + slippage if order["side"] == "BUY" else 1 - slippage)
    bar_high = number(quote.get("bar_high"), default=number(quote.get("high")))
    bar_low = number(quote.get("bar_low"), default=number(quote.get("low")))
    if bar_high > 0:
        fill_price = min(fill_price, bar_high)
    if bar_low > 0:
        fill_price = max(fill_price, bar_low)
    fill_price = _round(fill_price, 4)
    notional = fill_price * quantity
    commission = _commission_increment(order, notional, config)
    transfer_fee = notional * number(config["transfer_fee_rate_pct"]) / 100
    stamp_duty = notional * number(config["stamp_duty_rate_pct"]) / 100 if order["side"] == "SELL" else 0.0
    fees = commission + transfer_fee + stamp_duty

    if order["side"] == "BUY":
        while quantity >= 100 and notional + fees > number(account.get("cash")):
            quantity -= 100
            notional = fill_price * quantity
            commission = _commission_increment(order, notional, config) if quantity else 0.0
            transfer_fee = notional * number(config["transfer_fee_rate_pct"]) / 100
            fees = commission + transfer_fee
        if quantity <= 0:
            order["last_block_reason"] = "INSUFFICIENT_CASH"
            return events
        total_cost = notional + fees
        account["cash"] = _round(number(account.get("cash")) - total_cost)
        position = account.get("positions", {}).get(order["symbol"])
        if position is None:
            position = {
                "id": uuid.uuid4().hex,
                "symbol": order["symbol"],
                "name": order.get("name") or order["symbol"],
                "slot_id": order["slot_id"],
                "quantity": 0,
                "sellable_quantity": 0,
                "average_cost": 0.0,
                "first_entry_price": fill_price,
                "first_entry_at": _timestamp(now),
                "sellable_on": _next_business_date(now.date()).isoformat(),
                "current_price": fill_price,
                "peak_price": fill_price,
                "trailing_active": False,
                "signal_invalid_days": 0,
                "entry_score": order.get("score"),
                "comparison_baseline_score": order.get("score"),
                "current_score": order.get("score"),
                "realized_pnl": 0.0,
                "buy_fees": 0.0,
                "sell_fees": 0.0,
            }
            account.setdefault("positions", {})[order["symbol"]] = position
        previous_quantity = int(position["quantity"])
        previous_cost = number(position["average_cost"]) * previous_quantity
        position["quantity"] = previous_quantity + quantity
        position["average_cost"] = _round((previous_cost + total_cost) / position["quantity"], 4)
        position["buy_fees"] = _round(number(position.get("buy_fees")) + fees)
        position["current_price"] = fill_price
        position["peak_price"] = max(number(position.get("peak_price")), fill_price)
        for slot in account.get("slots", []):
            if int(slot["id"]) == int(order["slot_id"]):
                slot.update({"state": "OCCUPIED", "symbol": order["symbol"]})
                break
        order["reserved_cash"] = max(0.0, number(order.get("reserved_cash")) - total_cost)
    else:
        position = account.get("positions", {}).get(order["symbol"])
        if position is None:
            event = _cancel_order(account, order, now, "持仓已不存在")
            return [event] if event else []
        proceeds = notional - fees
        allocated_cost = number(position["average_cost"]) * quantity
        realized = proceeds - allocated_cost
        account["cash"] = _round(number(account.get("cash")) + proceeds)
        position["quantity"] = int(position["quantity"]) - quantity
        position["sellable_quantity"] = max(0, int(position.get("sellable_quantity", 0)) - quantity)
        position["realized_pnl"] = _round(number(position.get("realized_pnl")) + realized)
        position["sell_fees"] = _round(number(position.get("sell_fees")) + fees)

    order["filled_quantity"] = int(order.get("filled_quantity", 0)) + quantity
    order["filled_notional"] = _round(number(order.get("filled_notional")) + notional)
    order["commission_charged"] = _round(number(order.get("commission_charged")) + commission)
    order["last_fill_price"] = fill_price
    order["last_fill_at"] = _timestamp(now)
    order["updated_at"] = _timestamp(now)
    completed = order["filled_quantity"] >= int(order["quantity"])
    order["status"] = "FILLED" if completed else "PARTIAL"
    event_type = "ORDER_FILLED" if completed else "ORDER_PARTIAL"
    event = _append_event(
        account,
        now,
        event_type,
        f"{order.get('name') or order['symbol']} {('买入' if order['side'] == 'BUY' else '卖出')}成交 {quantity} 股",
        key=f"fill:{order['id']}:{order['filled_quantity']}:{_timestamp(now)}",
        data={
            "order_id": order["id"], "side": order["side"], "symbol": order["symbol"],
            "name": order.get("name"), "quantity": quantity, "fill_price": fill_price,
            "fees": _round(fees), "slippage_bps": config["slippage_bps"], "reason": order.get("reason"),
        },
    )
    if event:
        events.append(event)

    if order["side"] == "SELL":
        position = account.get("positions", {}).get(order["symbol"])
        if position and int(position["quantity"]) <= 0:
            closed = {
                "id": uuid.uuid4().hex,
                "position_id": position["id"],
                "symbol": position["symbol"],
                "name": position.get("name"),
                "slot_id": position["slot_id"],
                "first_entry_at": position.get("first_entry_at"),
                "first_entry_price": position.get("first_entry_price"),
                "average_cost": position.get("average_cost"),
                "closed_at": _timestamp(now),
                "exit_price": fill_price,
                "realized_pnl": position.get("realized_pnl", 0.0),
                "return_pct": _round(number(position.get("realized_pnl")) / max(1.0, number(position.get("average_cost")) * int(order["quantity"])) * 100),
                "exit_reason": order.get("reason"),
                "buy_fees": position.get("buy_fees", 0.0),
                "sell_fees": position.get("sell_fees", 0.0),
            }
            account.setdefault("closed_trades", []).append(closed)
            del account["positions"][order["symbol"]]
            for slot in account.get("slots", []):
                if int(slot["id"]) == int(position["slot_id"]):
                    slot.update({"state": "AVAILABLE", "symbol": None})
                    break
            candidate = position.get("replacement_candidate")
            if candidate and account.get("last_candidate_date") == now.date().isoformat() and account.get("trading_mode") == "RUNNING":
                normalized = next((item for item in account.get("last_candidates", []) if item.get("symbol") == candidate.get("symbol")), None)
                if normalized:
                    slot = _available_slot(account)
                    quantity_to_buy, reservation = _estimated_buy_reservation(account, number(normalized.get("price")))
                    if slot and quantity_to_buy > 0:
                        slot.update({"state": "RESERVED", "symbol": normalized["symbol"]})
                        _, continuation_event = _create_order(
                            account,
                            now,
                            side="BUY",
                            symbol=normalized["symbol"],
                            name=normalized.get("name") or normalized["symbol"],
                            quantity=quantity_to_buy,
                            reason="REPLACEMENT_CONTINUATION",
                            slot_id=int(slot["id"]),
                            signal_price=number(normalized.get("price")),
                            score=number(normalized.get("score")),
                            reserved_cash=reservation,
                        )
                        if continuation_event:
                            events.append(continuation_event)
    _refresh_reservations(account)
    return events


def _roll_settlements(account: dict, now: datetime) -> None:
    settlement_date = now.date().isoformat()
    for position in account.get("positions", {}).values():
        if settlement_date >= str(position.get("sellable_on") or "9999-12-31"):
            position["sellable_quantity"] = int(position["quantity"])


def _mark_positions(account: dict, quotes: dict[str, dict], now: datetime) -> None:
    config = account["portfolio_config"]
    for symbol, position in account.get("positions", {}).items():
        quote = quotes.get(symbol)
        if not quote:
            continue
        price = number(quote.get("price"), default=number(quote.get("bar_close")))
        if price <= 0:
            continue
        position["current_price"] = price
        position["day_change_pct"] = number(quote.get("percent"))
        position["volume_hands"] = number(quote.get("volume"))
        position["turnover_cny"] = number(quote.get("turnover"))
        position["last_mark_at"] = _timestamp(now)
        peak = max(number(position.get("peak_price")), price)
        position["peak_price"] = peak
        gain_pct = (price / number(position.get("average_cost")) - 1) * 100 if number(position.get("average_cost")) > 0 else 0.0
        if gain_pct >= number(config["trailing_activation_pct"]):
            position["trailing_active"] = True
        position["unrealized_pnl"] = _round((price - number(position.get("average_cost"))) * int(position["quantity"]))
        position["return_pct"] = _round(gain_pct)
        position["peak_drawdown_pct"] = _round((price / peak - 1) * 100) if peak > 0 else 0.0


def _set_risk_state(account: dict, level: str, mode: str, now: datetime, drawdown: float) -> list[dict]:
    previous = (account.get("risk_level"), account.get("trading_mode"))
    if previous == (level, mode):
        return []
    restrictive = {"RUNNING": 0, "ENTRY_BLOCKED": 1, "EXIT_ONLY": 2, "MANUAL_HALT": 3}
    events: list[dict] = []
    if restrictive.get(mode, 0) > restrictive.get(str(account.get("trading_mode")), 0):
        account["control_epoch"] = int(account.get("control_epoch") or 0) + 1
        for order in list(_open_orders(account, "BUY")):
            event = _cancel_order(account, order, now, f"风险状态切换为 {mode}")
            if event:
                events.append(event)
    account["risk_level"] = level
    account["trading_mode"] = mode
    event = _append_event(
        account,
        now,
        "RISK_CHANGED",
        f"组合风险切换为 {level}/{mode}，当前回撤 {drawdown:.2f}%",
        key=f"risk:{account.get('control_epoch')}:{level}:{mode}:{_timestamp(now)[:16]}",
        data={"from": previous, "risk_level": level, "trading_mode": mode, "drawdown_pct": _round(drawdown)},
    )
    if event:
        events.append(event)
    return events


def _evaluate_risk(account: dict, now: datetime) -> list[dict]:
    nav, market_value = _account_nav(account)
    high_water = max(number(account.get("high_water_nav"), default=nav), nav)
    drawdown = (high_water - nav) / high_water * 100 if high_water > 0 else 0.0
    config = account["portfolio_config"]
    if drawdown >= number(config["halt_drawdown_pct"]):
        target = ("BREACHED", "MANUAL_HALT")
    elif drawdown >= number(config["derisk_drawdown_pct"]):
        target = ("DE_RISKING", "EXIT_ONLY")
    elif drawdown >= number(config["warning_drawdown_pct"]):
        target = ("WARNING", "ENTRY_BLOCKED")
    else:
        target = (account.get("risk_level", "NORMAL"), account.get("trading_mode", "RUNNING"))
        if target == ("WARNING", "ENTRY_BLOCKED") and drawdown < 10:
            target = ("NORMAL", "RUNNING")
    events = _set_risk_state(account, target[0], target[1], now, drawdown)
    if account.get("trading_mode") in {"EXIT_ONLY", "MANUAL_HALT"}:
        for position in list(account.get("positions", {}).values()):
            events.extend(_trigger_exit(account, position, now, "PORTFOLIO_RISK_BREACH"))
    elif account.get("trading_mode") == "ENTRY_BLOCKED" and nav > 0:
        exposure_pct = market_value / nav * 100
        target_exposure = number(config["warning_max_exposure_pct"])
        positions = sorted(
            account.get("positions", {}).values(),
            key=lambda item: (number(item.get("current_score")), item["symbol"]),
        )
        remaining_value = market_value
        for position in positions:
            if remaining_value / nav * 100 <= target_exposure:
                break
            events.extend(_trigger_exit(account, position, now, "PORTFOLIO_WARNING_DERISK"))
            remaining_value -= _position_value(position)
    return events


def _evaluate_position_exits(account: dict, now: datetime) -> list[dict]:
    config = account["portfolio_config"]
    events: list[dict] = []
    for position in list(account.get("positions", {}).values()):
        if _has_exit_order(account, position["symbol"]):
            continue
        price = number(position.get("current_price"))
        cost = number(position.get("average_cost"))
        peak = number(position.get("peak_price"))
        if price <= 0 or cost <= 0:
            continue
        if (price / cost - 1) * 100 <= -number(config["stop_loss_pct"]):
            events.extend(_trigger_exit(account, position, now, "STOP_LOSS"))
        elif position.get("trailing_active") and peak > 0 and (price / peak - 1) * 100 <= -number(config["trailing_drawdown_pct"]):
            events.extend(_trigger_exit(account, position, now, "TRAILING_STOP"))
    return events


def process_market_snapshot(
    strategy: dict,
    quotes: Iterable[dict],
    *,
    now: datetime | None = None,
    path: str | Path | None = None,
    account: dict | None = None,
) -> tuple[dict, list[dict]]:
    current = beijing_now(now)
    quote_map = {str(item.get("symbol")): deepcopy(item) for item in quotes if str(item.get("symbol") or "")}

    def mutation(account: dict) -> list[dict]:
        events = _activate_strategy(account, strategy, current)
        run_key = f"market:{current.isoformat(timespec='minutes')}"
        if run_key in account.setdefault("committed_run_keys", []):
            return events
        events.extend(_expire_old_entries(account, current))
        _roll_settlements(account, current)
        order_ids = [order["id"] for order in _open_orders(account)]
        for order_id in order_ids:
            order = next((item for item in account.get("orders", []) if item.get("id") == order_id), None)
            if order is None:
                continue
            quote = quote_map.get(order["symbol"])
            if quote:
                events.extend(_execute_order(account, order, quote, current))
        _mark_positions(account, quote_map, current)
        events.extend(_evaluate_position_exits(account, current))
        events.extend(_evaluate_risk(account, current))
        account["committed_run_keys"].append(run_key)
        account["committed_run_keys"] = account["committed_run_keys"][-2000:]
        _refresh_reservations(account)
        _append_nav(account, current)
        return events

    if account is not None:
        return _mutate_in_memory(account, strategy, current, mutation)
    return _mutate_account(strategy, current, path, mutation)


def monitor_portfolio(
    strategy: dict,
    *,
    now: datetime | None = None,
    path: str | Path | None = None,
    quote_fetcher: Callable | None = None,
) -> tuple[dict, list[dict], str | None]:
    assert_strategy_runnable(strategy, execution_kind="scheduled", mode="risk")
    current = beijing_now(now)
    account = load_portfolio_account(strategy.get("id"), path=path)
    if account is None:
        account, _ = _mutate_account(strategy, current, path, lambda value: _activate_strategy(value, strategy, current))
    symbols = list(account.get("positions", {}))
    symbols.extend(order["symbol"] for order in _open_orders(account) if order.get("symbol") not in symbols)
    if not symbols:
        return account, [], None
    entries = [{"symbol": symbol, "name": account.get("positions", {}).get(symbol, {}).get("name", symbol)} for symbol in symbols]
    fetcher = quote_fetcher or fetch_watchlist_quotes
    try:
        rows, error = fetcher(entries)
    except Exception as exc:
        rows, error = [], str(exc)
    rows = constrain_to_watchlist(rows, entries)
    if not rows:
        return account, [], error or "未返回有效行情"
    account, events = process_market_snapshot(strategy, rows, now=current, path=path)
    return account, [event for event in events if event.get("type") in ACTION_EVENT_TYPES], error


def _with_event_link(base_url: str, strategy_id: str, event_id: str | None = None) -> str:
    target = str(base_url or "").strip()
    if not target:
        return ""
    parts = urlsplit(target)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["strategy_id"] = strategy_id
    if event_id:
        query["event"] = event_id
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def format_action_notifications(account: dict, events: Iterable[dict], *, performance_url: str = "") -> str:
    selected = [event for event in events if event.get("type") in ACTION_EVENT_TYPES]
    if not selected:
        return ""
    stage = "模拟盘" if account.get("strategy_stage") != "live" else "已批准实盘"
    lines = [
        "🚨 **策略持仓动作通知**",
        f"策略：{account.get('strategy_name')} · v{account.get('strategy_revision')} · {stage}",
        f"风险：{account.get('risk_level')} / {account.get('trading_mode')}",
        "",
    ]
    for event in selected:
        data = event.get("data") or {}
        line = f"- {event.get('message')}"
        if data.get("fill_price") is not None:
            line += f"；成交价 ¥{number(data.get('fill_price')):.2f}，费用 ¥{number(data.get('fees')):.2f}"
        link = _with_event_link(performance_url, str(account.get("strategy_id") or ""), event.get("id"))
        if link:
            line += f"；[查看事件]({link})"
        lines.append(line)
    lines.extend(["", "模拟盘数据，仅供策略验证，不构成投资建议。"])
    return "\n".join(lines)


def format_portfolio_summary(account: dict, *, performance_url: str = "", quote_error: str | None = None) -> str:
    nav = number(account.get("latest_nav"), default=number(account.get("initial_cash")))
    initial = number(account.get("initial_cash"), default=nav)
    drawdown = number((account.get("nav_history") or [{}])[-1].get("drawdown_pct"))
    return_pct = (nav / initial - 1) * 100 if initial > 0 else 0.0
    positions = sorted(account.get("positions", {}).values(), key=lambda item: int(item.get("slot_id") or 0))
    regime = normalize_market_regime_decision(account.get("last_market_regime"))
    lines = [
        "📊 **策略持仓每小时报告**",
        f"策略：{account.get('strategy_name')} · v{account.get('strategy_revision')} · {account.get('strategy_stage')}",
        f"信号：{account.get('signal_model', 'factor_rank_v1')} @ {account.get('signal_time', '08:00')} · 前一交易日收盘数据",
        f"板块：{regime['label']}（{regime['state']}） · 目标仓位 {regime['target_exposure_pct']:.0f}% · {regime['model']}",
        f"净值：¥{nav:,.2f}（累计 {return_pct:+.2f}%） · 现金 ¥{number(account.get('cash')):,.2f} · 回撤 {drawdown:.2f}%",
        f"风险：{account.get('risk_level')} / {account.get('trading_mode')} · 持仓 {len(positions)}/{account.get('portfolio_config', {}).get('max_positions', 10)}",
        "",
        "**当前持仓**",
    ]
    if not positions:
        lines.append("- 当前空仓，等待符合条件的入场信号。")
    for position in positions:
        sellable = int(number(position.get("sellable_quantity")))
        lines.append(
            f"- {position.get('name')} ({position.get('symbol')})：现价 ¥{number(position.get('current_price')):.2f}，"
            f"当日 {number(position.get('day_change_pct')):+.2f}%，持仓 {number(position.get('return_pct')):+.2f}%，"
            f"仓位 {number(position.get('quantity')) * number(position.get('current_price')) / max(nav, 1) * 100:.1f}%，"
            f"可卖 {sellable} 股，成交量 {number(position.get('volume_hands')):.0f} 手"
        )
    pending = _open_orders(account)
    if pending:
        lines.extend(["", "**待处理订单**"])
        for order in pending:
            lines.append(
                f"- {order.get('side')} {order.get('name')}：{order.get('status')} "
                f"{order.get('filled_quantity', 0)}/{order.get('quantity')} 股 · {order.get('reason')}"
            )
    if quote_error:
        lines.extend(["", f"⚠️ 行情提示：{quote_error}"])
    link = _with_event_link(performance_url, str(account.get("strategy_id") or ""))
    if link:
        lines.extend(["", f"📈 [查看策略表现]({link})"])
    lines.extend(["", "模拟盘数据，仅供策略验证，不构成投资建议。"])
    return "\n".join(lines)


def build_strategy_performance(
    *,
    strategy_id: str | None = None,
    path: str | Path | None = None,
    now: datetime | None = None,
    quote_fetcher: Callable | None = None,
) -> dict:
    current = beijing_now(now)
    account = load_portfolio_account(strategy_id, path=path) if strategy_id else None
    strategy = load_strategy_config(strategy_id=strategy_id) if strategy_id else load_strategy_config()
    if account is None:
        account = load_portfolio_account(strategy.get("id"), path=path)
    if account is None:
        account = _new_account(strategy, current)
    projected = deepcopy(account)
    _roll_settlements(projected, current)
    symbols = list(projected.get("positions", {}))
    quote_error = None
    if symbols and quote_fetcher is not False:
        fetcher = quote_fetcher or fetch_watchlist_quotes
        try:
            rows, quote_error = fetcher([{"symbol": symbol} for symbol in symbols])
        except Exception as exc:
            rows, quote_error = [], str(exc)
        quotes = {str(row.get("symbol")): row for row in rows}
        _mark_positions(projected, quotes, current)
        _append_nav(projected, current)
    nav = number(projected.get("latest_nav"), default=number(projected.get("initial_cash")))
    initial = number(projected.get("initial_cash"), default=nav)
    positions = []
    for position in projected.get("positions", {}).values():
        market_value = _position_value(position)
        positions.append(
            {
                **deepcopy(position),
                "market_value": _round(market_value),
                "weight_pct": _round(market_value / nav * 100 if nav > 0 else 0.0),
                "exit_distance_pct": _round(
                    min(
                        (number(position.get("current_price")) / (number(position.get("average_cost")) * 0.92) - 1) * 100
                        if number(position.get("average_cost")) > 0 else 0.0,
                        (number(position.get("current_price")) / (number(position.get("peak_price")) * 0.95) - 1) * 100
                        if position.get("trailing_active") and number(position.get("peak_price")) > 0 else 999.0,
                    )
                ),
            }
        )
    positions.sort(key=lambda item: int(item.get("slot_id") or 0))
    closed = list(projected.get("closed_trades", []))
    wins = [trade for trade in closed if number(trade.get("realized_pnl")) > 0]
    unrealized = sum(number(position.get("unrealized_pnl")) for position in positions)
    realized = sum(number(trade.get("realized_pnl")) for trade in closed)
    history = list(projected.get("nav_history", []))
    maximum_drawdown = max((number(point.get("drawdown_pct")) for point in history), default=0.0)
    return {
        "generated_at": current.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "quote_error": quote_error,
        "strategy": {
            "id": projected.get("strategy_id"),
            "name": projected.get("strategy_name"),
            "revision": projected.get("strategy_revision", 1),
            "stage": projected.get("strategy_stage", "draft"),
            "signal_model": strategy.get("signal", {}).get("model", "factor_rank_v1"),
            "signal_time": strategy.get("signal", {}).get("run_time", "08:00"),
            "signal_data_cutoff": strategy.get("signal", {}).get("data_cutoff", "previous_trading_day_close"),
            "allocation_model": strategy.get("allocation", {}).get("model", "trend_breadth_v1"),
            "market_regime": deepcopy(projected.get("last_market_regime")),
            "risk_level": projected.get("risk_level", "NORMAL"),
            "trading_mode": projected.get("trading_mode", "RUNNING"),
            "benchmark_symbol": projected.get("portfolio_config", {}).get("benchmark_symbol", "000300"),
            "benchmark_name": projected.get("portfolio_config", {}).get("benchmark_name", "沪深 300 全收益"),
        },
        "summary": {
            "initial_cash": _round(initial),
            "nav": _round(nav),
            "cash": _round(number(projected.get("cash"))),
            "reserved_cash": _round(number(projected.get("reserved_cash"))),
            "market_value": _round(sum(_position_value(item) for item in positions)),
            "cumulative_return_pct": _round((nav / initial - 1) * 100 if initial > 0 else 0.0),
            "maximum_drawdown_pct": _round(maximum_drawdown),
            "realized_pnl": _round(realized),
            "unrealized_pnl": _round(unrealized),
            "position_count": len(positions),
            "max_positions": int(projected.get("portfolio_config", {}).get("max_positions", 10)),
            "target_exposure_pct": number(projected.get("target_exposure_pct"), default=100.0),
            "closed_trade_count": len(closed),
            "win_rate_pct": _round(len(wins) / len(closed) * 100) if closed else None,
        },
        "nav_history": history,
        "positions": positions,
        "orders": list(reversed(projected.get("orders", [])[-100:])),
        "closed_trades": list(reversed(closed[-200:])),
        "events": list(reversed(projected.get("events", [])[-200:])),
        "config": deepcopy(projected.get("portfolio_config", {})),
        "allocation": deepcopy(strategy.get("allocation", {})),
    }
