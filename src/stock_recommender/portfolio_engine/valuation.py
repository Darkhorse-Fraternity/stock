"""Valuation calculations for portfolio holdings and accounts."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace

from .contracts import (
    AccountSnapshot,
    PortfolioMetrics,
    PositionSide,
    PositionSnapshot,
    ValuationResult,
)


class ValuationError(ValueError):
    """Raised when a complete, finite valuation cannot be produced."""


def _require_position(position: object) -> PositionSnapshot:
    if type(position) is not PositionSnapshot:
        raise TypeError("position must be PositionSnapshot")
    return position


def _require_price(price: object, symbol: str) -> float:
    if type(price) not in (int, float):
        raise ValuationError(f"price for {symbol} must be a real number")
    try:
        resolved = float(price)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValuationError(f"price for {symbol} must be finite and positive") from exc
    if not math.isfinite(resolved) or resolved <= 0:
        raise ValuationError(f"price for {symbol} must be finite and positive")
    return resolved


def _require_nonnegative_amount(value: object, field_name: str) -> float:
    if type(value) not in (int, float):
        raise TypeError(f"{field_name} must be an int or float")
    try:
        resolved = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValuationError(f"{field_name} must be finite and nonnegative") from exc
    if not math.isfinite(resolved) or resolved < 0:
        raise ValuationError(f"{field_name} must be finite and nonnegative")
    return resolved


def _finite_result(value: float, calculation: str) -> float:
    if not math.isfinite(value):
        raise ValuationError(f"{calculation} must produce a finite result")
    if value == 0:
        return 0.0
    return value


def position_market_value(position: PositionSnapshot, price: object) -> float:
    """Return the positive notional value for a long or short position."""

    resolved_position = _require_position(position)
    resolved_price = _require_price(price, resolved_position.symbol)
    try:
        value = replace(resolved_position, current_price=resolved_price).market_value
    except (OverflowError, ValueError) as exc:
        raise ValuationError("position market value must be finite") from exc
    if value is None:
        raise ValuationError("position market value was not calculated")
    return value


def position_unrealized_pnl(position: PositionSnapshot, price: object) -> float:
    """Return direction-aware unrealized profit and loss."""

    resolved_position = _require_position(position)
    resolved_price = _require_price(price, resolved_position.symbol)
    try:
        pnl = replace(resolved_position, current_price=resolved_price).unrealized_pnl
    except (OverflowError, ValueError) as exc:
        raise ValuationError("position unrealized P&L must be finite") from exc
    if pnl is None:
        raise ValuationError("position unrealized P&L was not calculated")
    return pnl


def account_equity(
    account: AccountSnapshot,
    *,
    long_market_value: object,
    short_liability: object,
) -> float:
    """Return account equity without deducting already-paid costs again."""

    if type(account) is not AccountSnapshot:
        raise TypeError("account must be AccountSnapshot")
    long_value = _require_nonnegative_amount(
        long_market_value,
        "long_market_value",
    )
    short_value = _require_nonnegative_amount(short_liability, "short_liability")
    try:
        equity = (
            float(account.available_cash)
            + float(account.restricted_short_proceeds)
            + long_value
            - short_value
            - float(account.margin_loan)
        )
    except OverflowError as exc:
        raise ValuationError("account equity overflowed") from exc
    return _finite_result(equity, "account equity")


def _finite_ratio(numerator: float, denominator: float) -> float:
    try:
        value = numerator / denominator * 100.0
    except OverflowError as exc:
        raise ValuationError("portfolio ratio overflowed") from exc
    return _finite_result(value, "portfolio ratio")


def _portfolio_ratios(
    long_market_value: float,
    short_liability: float,
    equity: float,
) -> tuple[float, float, float, float, float]:
    gross_value = _finite_result(
        long_market_value + short_liability,
        "gross market value",
    )
    if gross_value == 0:
        return 0.0, 0.0, 0.0, 0.0, math.inf
    if equity == 0:
        net_value = long_market_value - short_liability
        if net_value > 0:
            net_exposure = math.inf
        elif net_value < 0:
            net_exposure = -math.inf
        else:
            net_exposure = 0.0
        return (
            math.inf if long_market_value > 0 else 0.0,
            math.inf if short_liability > 0 else 0.0,
            math.inf,
            net_exposure,
            0.0,
        )
    return (
        _finite_ratio(long_market_value, equity),
        _finite_ratio(short_liability, equity),
        _finite_ratio(gross_value, equity),
        _finite_ratio(long_market_value - short_liability, equity),
        _finite_ratio(equity, gross_value),
    )


def value_account(
    account: AccountSnapshot,
    prices: Mapping[str, object],
) -> ValuationResult:
    """Value an immutable account against one complete price snapshot."""

    if type(account) is not AccountSnapshot:
        raise TypeError("account must be AccountSnapshot")
    if not isinstance(prices, Mapping):
        raise TypeError("prices must be a mapping")

    missing_symbols = tuple(
        position.symbol
        for position in account.positions
        if position.symbol not in prices
    )
    if missing_symbols:
        raise ValuationError(
            "missing prices for held symbols: " + ", ".join(missing_symbols)
        )

    resolved_prices = {
        position.symbol: _require_price(prices[position.symbol], position.symbol)
        for position in account.positions
    }
    valued_positions: list[PositionSnapshot] = []
    long_values: list[float] = []
    short_values: list[float] = []
    for position in account.positions:
        price = resolved_prices[position.symbol]
        market_value = position_market_value(position, price)
        position_unrealized_pnl(position, price)
        valued_positions.append(
            replace(
                position,
                current_price=price,
            )
        )
        if position.side is PositionSide.LONG:
            long_values.append(market_value)
        else:
            short_values.append(market_value)

    long_market_value = _finite_result(
        sum(long_values, 0.0),
        "long market value",
    )
    short_liability = _finite_result(
        sum(short_values, 0.0),
        "short liability",
    )
    equity = account_equity(
        account,
        long_market_value=long_market_value,
        short_liability=short_liability,
    )
    (
        long_exposure_pct,
        short_exposure_pct,
        gross_exposure_pct,
        net_exposure_pct,
        margin_rate_pct,
    ) = _portfolio_ratios(long_market_value, short_liability, equity)

    metrics = PortfolioMetrics(
        available_cash=account.available_cash,
        restricted_short_proceeds=account.restricted_short_proceeds,
        margin_loan=account.margin_loan,
        accrued_financing_cost=account.accrued_financing_cost,
        accrued_borrow_cost=account.accrued_borrow_cost,
        long_market_value=long_market_value,
        short_liability=short_liability,
        equity=equity,
        long_exposure_pct=long_exposure_pct,
        short_exposure_pct=short_exposure_pct,
        gross_exposure_pct=gross_exposure_pct,
        net_exposure_pct=net_exposure_pct,
        margin_rate_pct=margin_rate_pct,
    )
    valued_account = replace(account, positions=tuple(valued_positions))
    return ValuationResult(account=valued_account, metrics=metrics)


__all__ = (
    "ValuationError",
    "account_equity",
    "position_market_value",
    "position_unrealized_pnl",
    "value_account",
)
