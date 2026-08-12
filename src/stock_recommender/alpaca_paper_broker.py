"""Alpaca Paper adapter for the portfolio execution port."""

from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Callable, Iterable, Mapping
from decimal import Decimal, InvalidOperation

from .markets import strategy_market
from .portfolio_engine.contracts import AccountSnapshot, OrderIntent, OrderSide
from .portfolio_engine.ports import BrokerOrderSnapshot


PAPER_TRADING_URL = "https://paper-api.alpaca.markets"


def alpaca_client_order_id(strategy_id: str, intent_id: str) -> str:
    """Return a stable ID that stays below Alpaca's client ID limit."""

    if type(strategy_id) is not str or not strategy_id:
        raise ValueError("strategy_id must be a non-empty string")
    if type(intent_id) is not str or not intent_id:
        raise ValueError("intent_id must be a non-empty string")
    strategy_hash = hashlib.sha256(strategy_id.encode("utf-8")).hexdigest()[:8]
    material = strategy_id + "\x1f" + intent_id
    intent_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"sa-{strategy_hash}-{intent_hash}"


def _text(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip()


def _whole_quantity(value: object, field_name: str) -> int:
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError(f"Alpaca {field_name} is invalid") from exc
    if not quantity.is_finite() or quantity < 0 or quantity != quantity.to_integral_value():
        raise RuntimeError(f"Alpaca {field_name} must be a nonnegative whole number")
    return int(quantity)


def _positive_price(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("Alpaca filled average price is invalid") from exc
    if not math.isfinite(number) or number <= 0:
        raise RuntimeError("Alpaca filled average price must be positive")
    return number


class AlpacaPaperBroker:
    """Submit market orders idempotently and expose cumulative broker state."""

    name = "alpaca_paper_execution"

    def __init__(
        self,
        strategy_id: str,
        client: object,
        *,
        order_request_factory: Callable[..., object],
        all_orders_request_factory: Callable[[], object],
    ) -> None:
        if type(strategy_id) is not str or not strategy_id:
            raise ValueError("strategy_id must be a non-empty string")
        self._strategy_id = strategy_id
        self._client_order_prefix = alpaca_client_order_id(strategy_id, "prefix").rsplit(
            "-", 1
        )[0] + "-"
        self._client = client
        self._order_request_factory = order_request_factory
        self._all_orders_request_factory = all_orders_request_factory
        self._orders: dict[str, object] | None = None
        self._ready_checked = False

    @classmethod
    def from_env(cls, strategy_id: str) -> AlpacaPaperBroker:
        key_id = (
            os.getenv("STOCK_AGENT_ALPACA_API_KEY_ID")
            or os.getenv("APCA_API_KEY_ID")
            or ""
        ).strip()
        secret = (
            os.getenv("STOCK_AGENT_ALPACA_API_SECRET_KEY")
            or os.getenv("APCA_API_SECRET_KEY")
            or ""
        ).strip()
        if not key_id or not secret:
            raise RuntimeError("Alpaca Paper execution credentials are not configured")

        configured_url = os.getenv("STOCK_AGENT_ALPACA_TRADING_URL", PAPER_TRADING_URL)
        normalized_url = configured_url.strip().rstrip("/").removesuffix("/v2")
        if normalized_url != PAPER_TRADING_URL:
            raise RuntimeError("Alpaca execution URL must be the Paper Trading endpoint")

        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import OrderSide as AlpacaOrderSide
        from alpaca.trading.enums import QueryOrderStatus, TimeInForce
        from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

        client = TradingClient(key_id, secret, paper=True)

        def order_request_factory(**values: object) -> object:
            side = values.pop("side")
            return MarketOrderRequest(
                **values,
                side=(
                    AlpacaOrderSide.BUY
                    if side == "buy"
                    else AlpacaOrderSide.SELL
                ),
                time_in_force=TimeInForce.DAY,
            )

        def all_orders_request_factory() -> object:
            return GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500)

        return cls(
            strategy_id,
            client,
            order_request_factory=order_request_factory,
            all_orders_request_factory=all_orders_request_factory,
        )

    def _load_orders(self, *, refresh: bool = False) -> dict[str, object]:
        if self._orders is None or refresh:
            values = self._client.get_orders(filter=self._all_orders_request_factory())
            if not isinstance(values, Iterable):
                raise RuntimeError("Alpaca get_orders returned an invalid response")
            orders: dict[str, object] = {}
            for item in values:
                client_id = _text(getattr(item, "client_order_id", ""))
                if client_id:
                    orders[client_id] = item
            self._orders = orders
        return self._orders

    def _submit(self, intent: OrderIntent, client_order_id: str) -> object:
        request = self._order_request_factory(
            symbol=intent.symbol,
            qty=intent.quantity,
            side=("buy" if intent.order_side is OrderSide.BUY else "sell"),
            client_order_id=client_order_id,
        )
        try:
            return self._client.submit_order(order_data=request)
        except Exception:
            # A timeout or concurrent retry may have accepted the order. Resolve the
            # client ID once before surfacing the original failure.
            existing = self._load_orders(refresh=True).get(client_order_id)
            if existing is not None:
                return existing
            raise

    def assert_ready(self, account: AccountSnapshot) -> None:
        """Fail closed when a fresh Paper account does not match local state."""

        if type(account) is not AccountSnapshot:
            raise TypeError("account must be AccountSnapshot")
        if self._ready_checked:
            return
        broker_account = self._client.get_account()
        status = _text(getattr(broker_account, "status", "")).lower()
        if status != "active":
            raise RuntimeError(f"Alpaca Paper account is not active: {status or 'unknown'}")
        orders = self._load_orders()
        all_managed_orders = {
            client_id: order
            for client_id, order in orders.items()
            if client_id.startswith("sa-")
        }
        managed_orders = {
            client_id: order
            for client_id, order in all_managed_orders.items()
            if client_id.startswith(self._client_order_prefix)
        }
        if all_managed_orders and not managed_orders:
            raise RuntimeError(
                "Alpaca Paper account is already managed by a different strategy"
            )
        if not managed_orders:
            positions = self._client.get_all_positions()
            actual: dict[str, tuple[str, int]] = {}
            for item in positions:
                symbol = _text(getattr(item, "symbol", ""))
                side = _text(getattr(item, "side", "")).upper()
                quantity = _whole_quantity(getattr(item, "qty", None), "position quantity")
                if symbol:
                    actual[symbol] = (side, quantity)
            expected = {
                item.symbol: (item.side.value, item.quantity)
                for item in account.positions
            }
            if actual != expected:
                raise RuntimeError(
                    "Alpaca Paper positions do not match the local portfolio; "
                    "a controlled cutover is required"
                )
            if not expected:
                broker_cash = float(getattr(broker_account, "cash", "nan"))
                if (
                    not math.isfinite(broker_cash)
                    or abs(broker_cash - account.available_cash) > 0.01
                ):
                    raise RuntimeError(
                        "Alpaca Paper cash does not match the local portfolio; "
                        "a controlled cutover is required"
                    )
        self._ready_checked = True

    def place_or_get(self, intent: OrderIntent) -> BrokerOrderSnapshot:
        if type(intent) is not OrderIntent:
            raise TypeError("intent must be OrderIntent")
        client_order_id = alpaca_client_order_id(self._strategy_id, intent.id)
        order = self._load_orders().get(client_order_id)
        if order is None:
            order = self._submit(intent, client_order_id)
            self._load_orders()[client_order_id] = order

        status = _text(getattr(order, "status", "")).lower()
        quantity = _whole_quantity(getattr(order, "qty", None), "order quantity")
        filled_quantity = _whole_quantity(
            getattr(order, "filled_qty", None), "filled quantity"
        )
        average_price = _positive_price(getattr(order, "filled_avg_price", None))
        if filled_quantity and average_price is None:
            raise RuntimeError("Alpaca filled order is missing its average price")
        if not status:
            raise RuntimeError("Alpaca order status is missing")
        rejection_reason = None
        if status in {"rejected", "stopped", "suspended"}:
            rejection_reason = _text(getattr(order, "reject_reason", "")) or status

        snapshot = BrokerOrderSnapshot(
            order_id=_text(getattr(order, "id", "")),
            client_order_id=_text(getattr(order, "client_order_id", "")),
            symbol=_text(getattr(order, "symbol", "")),
            side=_text(getattr(order, "side", "")).lower(),
            quantity=quantity,
            filled_quantity=filled_quantity,
            filled_average_price=average_price,
            status=status,
            rejection_reason=rejection_reason,
        )
        if not snapshot.order_id:
            raise RuntimeError("Alpaca order ID is missing")
        if snapshot.client_order_id != client_order_id:
            raise RuntimeError("Alpaca client order ID does not match intent")
        if snapshot.symbol != intent.symbol:
            raise RuntimeError("Alpaca order symbol does not match intent")
        if snapshot.quantity != intent.quantity:
            raise RuntimeError("Alpaca order quantity does not match intent")
        expected_side = "buy" if intent.order_side is OrderSide.BUY else "sell"
        if snapshot.side != expected_side:
            raise RuntimeError("Alpaca order side does not match intent")
        return snapshot


def execution_backend_for_strategy(
    strategy: Mapping[str, object],
) -> AlpacaPaperBroker | None:
    """Select an execution adapter explicitly; simulation remains an offline mode."""

    backend = os.getenv("STOCK_AGENT_EXECUTION_BACKEND", "simulation").strip().lower()
    if backend in {"", "simulation"}:
        return None
    if backend != "alpaca_paper":
        raise RuntimeError(f"unsupported execution backend: {backend}")
    market = strategy_market(strategy)
    lifecycle_value = strategy.get("lifecycle")
    lifecycle = (
        str(lifecycle_value.get("stage", ""))
        if isinstance(lifecycle_value, Mapping)
        else ""
    )
    if market != "us" or lifecycle != "paper":
        raise RuntimeError("Alpaca Paper execution requires a US paper strategy")
    strategy_id = str(strategy.get("id") or "")
    if not strategy_id:
        raise RuntimeError("Alpaca Paper execution requires strategy.id")
    return AlpacaPaperBroker.from_env(strategy_id)


__all__ = [
    "AlpacaPaperBroker",
    "PAPER_TRADING_URL",
    "alpaca_client_order_id",
    "execution_backend_for_strategy",
]
