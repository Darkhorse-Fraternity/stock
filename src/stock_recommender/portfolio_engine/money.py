"""Deterministic monetary transitions shared by simulation and ledger replay.

The public account contract stores JSON-compatible floats.  Every balance
transition is nevertheless evaluated with ``Decimal(str(value))`` and converted
to float only once at the contract boundary.  We deliberately do not quantize:
fees and carrying costs may have sub-cent precision in a simulation, and both
the producer and ledger replay must preserve exactly the same value.
"""

from __future__ import annotations

import math
from dataclasses import replace
from decimal import Decimal

from .contracts import AccountSnapshot


def decimal_amount(value: object, field_name: str) -> Decimal:
    """Return a finite Decimal using the account contract's textual value."""

    if type(value) not in (int, float, Decimal):
        raise TypeError(f"{field_name} must be numeric")
    amount = value if type(value) is Decimal else Decimal(str(value))
    if not amount.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return amount


def canonical_amount(value: Decimal, field_name: str) -> float:
    """Cross the Decimal-to-float account boundary exactly once."""

    if type(value) is not Decimal:
        raise TypeError(f"{field_name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} is outside the account numeric range")
    return 0.0 if result == 0.0 else result


def charge_cash_or_margin(
    account: AccountSnapshot,
    amount: object,
    *,
    field_name: str = "amount",
) -> AccountSnapshot:
    """Charge cash first, then margin, with deterministic Decimal arithmetic."""

    if type(account) is not AccountSnapshot:
        raise TypeError("account must be AccountSnapshot")
    charge = decimal_amount(amount, field_name)
    if charge < 0:
        raise ValueError(f"{field_name} must be nonnegative")
    cash = decimal_amount(account.available_cash, "available_cash")
    loan = decimal_amount(account.margin_loan, "margin_loan")
    cash_used = min(max(Decimal(0), cash), charge)
    return replace(
        account,
        available_cash=canonical_amount(cash - cash_used, "available_cash"),
        margin_loan=canonical_amount(loan + charge - cash_used, "margin_loan"),
    )
