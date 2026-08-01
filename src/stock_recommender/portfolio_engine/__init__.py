"""Stable public boundary for portfolio decision and transaction workflows."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .contracts import PlanRequest, PortfolioSnapshot, ProcessRequest
    from .service import PortfolioEngine

__all__ = (
    "PlanRequest",
    "PortfolioEngine",
    "PortfolioSnapshot",
    "ProcessRequest",
)


def __getattr__(name: str) -> Any:
    if name == "PortfolioEngine":
        from .service import PortfolioEngine

        return PortfolioEngine
    if name in {"PlanRequest", "PortfolioSnapshot", "ProcessRequest"}:
        from . import contracts

        return getattr(contracts, name)
    raise AttributeError(name)
