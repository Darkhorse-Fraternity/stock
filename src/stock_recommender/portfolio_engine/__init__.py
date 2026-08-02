"""Stable public boundary for portfolio decision and transaction workflows."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .contracts import (
        PerformanceProjectionRequest,
        PerformanceStrategySource,
        PlanRequest,
        PortfolioSnapshot,
        ProcessRequest,
        StrategyPerformanceProjection,
    )
    from .service import PortfolioEngine

__all__ = (
    "PlanRequest",
    "PerformanceProjectionRequest",
    "PerformanceStrategySource",
    "PortfolioEngine",
    "PortfolioSnapshot",
    "ProcessRequest",
    "StrategyPerformanceProjection",
)


def __getattr__(name: str) -> Any:
    if name == "PortfolioEngine":
        from .service import PortfolioEngine

        return PortfolioEngine
    if name in {
        "PerformanceProjectionRequest",
        "PerformanceStrategySource",
        "PlanRequest",
        "PortfolioSnapshot",
        "ProcessRequest",
        "StrategyPerformanceProjection",
    }:
        from . import contracts

        return getattr(contracts, name)
    raise AttributeError(name)
