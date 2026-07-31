"""Configuration contracts for portfolio engine behavior."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from ..markets import CN_MARKET, normalize_market


SYSTEM_MAX_POSITIONS = 10
SYSTEM_MAX_GROSS_EXPOSURE_PCT = 150.0
SYSTEM_MAX_NET_EXPOSURE_PCT = 120.0
SYSTEM_MAX_LONG_EXPOSURE_PCT = 120.0
SYSTEM_MAX_SHORT_EXPOSURE_PCT = 30.0
SYSTEM_MAX_LONG_POSITION_PCT = 15.0
SYSTEM_MAX_SHORT_POSITION_PCT = 5.0
VALID_EXPOSURE_MODES = {"LONG_ONLY", "LONG_LEVERAGED", "LONG_SHORT"}


class StrategyPolicyError(ValueError):
    """Raised when a strategy requests an unsupported or unsafe policy."""


@dataclass(frozen=True)
class ExposurePolicy:
    mode: str = "LONG_ONLY"
    max_positions: int = SYSTEM_MAX_POSITIONS
    max_gross_exposure_pct: float = SYSTEM_MAX_GROSS_EXPOSURE_PCT
    max_net_exposure_pct: float = SYSTEM_MAX_NET_EXPOSURE_PCT
    max_long_exposure_pct: float = SYSTEM_MAX_LONG_EXPOSURE_PCT
    max_short_exposure_pct: float = SYSTEM_MAX_SHORT_EXPOSURE_PCT
    max_long_position_pct: float = SYSTEM_MAX_LONG_POSITION_PCT
    max_short_position_pct: float = SYSTEM_MAX_SHORT_POSITION_PCT


@dataclass(frozen=True)
class MarginPolicy:
    maintenance_margin_pct: float = 30.0
    liquidation_buffer_pct: float = 10.0
    financing_apr_pct: float = 8.0
    accrual_mode: str = "DAILY"


@dataclass(frozen=True)
class ShortPolicy:
    signal_model: str = "short_trend_breakdown_v1"
    require_shortable: bool = True
    require_easy_to_borrow: bool = True
    estimated_borrow_apr_pct: float = 8.0
    cost_stress_multiplier: float = 2.0
    block_on_borrow_data_missing: bool = True
    stop_loss_pct: float = 6.0
    trailing_activation_pct: float = 8.0
    trailing_rebound_pct: float = 4.0
    event_blackout_sessions: int = 2
    squeeze_rise_pct: float = 10.0
    squeeze_volume_ratio: float = 3.0
    maximum_volatility_20d_pct: float = 80.0


def default_exposure_policy() -> dict[str, Any]:
    return asdict(ExposurePolicy())


def default_margin_policy() -> dict[str, Any]:
    return asdict(MarginPolicy())


def default_short_policy() -> dict[str, Any]:
    return asdict(ShortPolicy())


def _finite_number(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _bounded_exposure_number(
    value: object,
    default: float,
    maximum: float,
) -> float:
    return min(maximum, max(0.0, _finite_number(value, default)))


def _bounded_exposure_integer(
    value: object,
    default: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(maximum, max(0, number))


def _safe_nonnegative_number(
    value: object,
    default: float,
    *,
    maximum: float,
    minimum: float = 0.0,
) -> float:
    number = _finite_number(value, default)
    if number < minimum:
        return default
    return min(maximum, number)


def _safe_nonnegative_integer(
    value: object,
    default: int,
    *,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(maximum, number) if number >= 0 else default


def _safe_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return default


def normalize_exposure_policy(value: object) -> dict[str, Any]:
    defaults = default_exposure_policy()
    provided = value if isinstance(value, Mapping) else {}
    raw_mode = provided.get("mode", defaults["mode"])
    mode = str(raw_mode or defaults["mode"]).strip().upper()
    if mode not in VALID_EXPOSURE_MODES:
        raise StrategyPolicyError(f"不支持的敞口模式: {raw_mode}")
    return {
        "mode": mode,
        "max_positions": _bounded_exposure_integer(
            provided.get("max_positions", defaults["max_positions"]),
            defaults["max_positions"],
            SYSTEM_MAX_POSITIONS,
        ),
        "max_gross_exposure_pct": _bounded_exposure_number(
            provided.get(
                "max_gross_exposure_pct",
                defaults["max_gross_exposure_pct"],
            ),
            defaults["max_gross_exposure_pct"],
            SYSTEM_MAX_GROSS_EXPOSURE_PCT,
        ),
        "max_net_exposure_pct": _bounded_exposure_number(
            provided.get("max_net_exposure_pct", defaults["max_net_exposure_pct"]),
            defaults["max_net_exposure_pct"],
            SYSTEM_MAX_NET_EXPOSURE_PCT,
        ),
        "max_long_exposure_pct": _bounded_exposure_number(
            provided.get(
                "max_long_exposure_pct",
                defaults["max_long_exposure_pct"],
            ),
            defaults["max_long_exposure_pct"],
            SYSTEM_MAX_LONG_EXPOSURE_PCT,
        ),
        "max_short_exposure_pct": _bounded_exposure_number(
            provided.get(
                "max_short_exposure_pct",
                defaults["max_short_exposure_pct"],
            ),
            defaults["max_short_exposure_pct"],
            SYSTEM_MAX_SHORT_EXPOSURE_PCT,
        ),
        "max_long_position_pct": _bounded_exposure_number(
            provided.get(
                "max_long_position_pct",
                defaults["max_long_position_pct"],
            ),
            defaults["max_long_position_pct"],
            SYSTEM_MAX_LONG_POSITION_PCT,
        ),
        "max_short_position_pct": _bounded_exposure_number(
            provided.get(
                "max_short_position_pct",
                defaults["max_short_position_pct"],
            ),
            defaults["max_short_position_pct"],
            SYSTEM_MAX_SHORT_POSITION_PCT,
        ),
    }


def effective_exposure_policy(value: object) -> ExposurePolicy:
    policy = normalize_exposure_policy(value)
    mode = policy["mode"]
    if mode == "LONG_ONLY":
        policy.update(
            {
                "max_gross_exposure_pct": min(
                    100.0, policy["max_gross_exposure_pct"]
                ),
                "max_net_exposure_pct": min(100.0, policy["max_net_exposure_pct"]),
                "max_long_exposure_pct": min(
                    100.0, policy["max_long_exposure_pct"]
                ),
                "max_short_exposure_pct": 0.0,
                "max_short_position_pct": 0.0,
            }
        )
    elif mode == "LONG_LEVERAGED":
        policy.update(
            {
                "max_gross_exposure_pct": min(
                    120.0, policy["max_gross_exposure_pct"]
                ),
                "max_net_exposure_pct": min(120.0, policy["max_net_exposure_pct"]),
                "max_long_exposure_pct": min(
                    120.0, policy["max_long_exposure_pct"]
                ),
                "max_short_exposure_pct": 0.0,
                "max_short_position_pct": 0.0,
            }
        )
    return ExposurePolicy(**policy)


def normalize_margin_policy(value: object) -> dict[str, Any]:
    defaults = default_margin_policy()
    provided = value if isinstance(value, Mapping) else {}
    accrual_mode = str(
        provided.get("accrual_mode", defaults["accrual_mode"])
        or defaults["accrual_mode"]
    ).strip().upper()
    return {
        "maintenance_margin_pct": _safe_nonnegative_number(
            provided.get(
                "maintenance_margin_pct",
                defaults["maintenance_margin_pct"],
            ),
            defaults["maintenance_margin_pct"],
            maximum=100.0,
            minimum=0.01,
        ),
        "liquidation_buffer_pct": _safe_nonnegative_number(
            provided.get(
                "liquidation_buffer_pct",
                defaults["liquidation_buffer_pct"],
            ),
            defaults["liquidation_buffer_pct"],
            maximum=100.0,
        ),
        "financing_apr_pct": _safe_nonnegative_number(
            provided.get("financing_apr_pct", defaults["financing_apr_pct"]),
            defaults["financing_apr_pct"],
            maximum=100.0,
        ),
        "accrual_mode": accrual_mode if accrual_mode == "DAILY" else "DAILY",
    }


def normalize_short_policy(value: object) -> dict[str, Any]:
    defaults = default_short_policy()
    provided = value if isinstance(value, Mapping) else {}
    signal_model = str(
        provided.get("signal_model", defaults["signal_model"])
        or defaults["signal_model"]
    ).strip()[:120]
    return {
        "signal_model": signal_model or defaults["signal_model"],
        "require_shortable": _safe_bool(
            provided.get("require_shortable"), defaults["require_shortable"]
        ),
        "require_easy_to_borrow": _safe_bool(
            provided.get("require_easy_to_borrow"),
            defaults["require_easy_to_borrow"],
        ),
        "estimated_borrow_apr_pct": _safe_nonnegative_number(
            provided.get(
                "estimated_borrow_apr_pct",
                defaults["estimated_borrow_apr_pct"],
            ),
            defaults["estimated_borrow_apr_pct"],
            maximum=100.0,
        ),
        "cost_stress_multiplier": _safe_nonnegative_number(
            provided.get(
                "cost_stress_multiplier",
                defaults["cost_stress_multiplier"],
            ),
            defaults["cost_stress_multiplier"],
            maximum=100.0,
            minimum=1.0,
        ),
        "block_on_borrow_data_missing": _safe_bool(
            provided.get("block_on_borrow_data_missing"),
            defaults["block_on_borrow_data_missing"],
        ),
        "stop_loss_pct": _safe_nonnegative_number(
            provided.get("stop_loss_pct", defaults["stop_loss_pct"]),
            defaults["stop_loss_pct"],
            maximum=100.0,
            minimum=0.01,
        ),
        "trailing_activation_pct": _safe_nonnegative_number(
            provided.get(
                "trailing_activation_pct",
                defaults["trailing_activation_pct"],
            ),
            defaults["trailing_activation_pct"],
            maximum=100.0,
            minimum=0.01,
        ),
        "trailing_rebound_pct": _safe_nonnegative_number(
            provided.get(
                "trailing_rebound_pct",
                defaults["trailing_rebound_pct"],
            ),
            defaults["trailing_rebound_pct"],
            maximum=100.0,
            minimum=0.01,
        ),
        "event_blackout_sessions": _safe_nonnegative_integer(
            provided.get(
                "event_blackout_sessions",
                defaults["event_blackout_sessions"],
            ),
            defaults["event_blackout_sessions"],
            maximum=252,
        ),
        "squeeze_rise_pct": _safe_nonnegative_number(
            provided.get("squeeze_rise_pct", defaults["squeeze_rise_pct"]),
            defaults["squeeze_rise_pct"],
            maximum=100.0,
            minimum=0.01,
        ),
        "squeeze_volume_ratio": _safe_nonnegative_number(
            provided.get(
                "squeeze_volume_ratio",
                defaults["squeeze_volume_ratio"],
            ),
            defaults["squeeze_volume_ratio"],
            maximum=100.0,
            minimum=0.01,
        ),
        "maximum_volatility_20d_pct": _safe_nonnegative_number(
            provided.get(
                "maximum_volatility_20d_pct",
                defaults["maximum_volatility_20d_pct"],
            ),
            defaults["maximum_volatility_20d_pct"],
            maximum=1000.0,
            minimum=0.01,
        ),
    }


def _strategy_market(strategy: Mapping[str, Any]) -> str:
    direct = strategy.get("market")
    if direct is not None:
        return normalize_market(direct)
    parameters = strategy.get("parameters")
    if not isinstance(parameters, Mapping):
        return CN_MARKET
    state = parameters.get("market")
    if not isinstance(state, Mapping) or not state.get("enabled", True):
        return CN_MARKET
    return normalize_market(state.get("value"))


def validate_strategy_policies(strategy: Mapping[str, Any]) -> None:
    exposure = normalize_exposure_policy(strategy.get("exposure_policy"))
    normalize_margin_policy(strategy.get("margin_policy"))
    normalize_short_policy(strategy.get("short_policy"))
    market = _strategy_market(strategy)
    if exposure["mode"] != "LONG_ONLY" and market != "us":
        raise StrategyPolicyError(
            f"市场 {market or 'unknown'} 仅支持 LONG_ONLY，不能启用 {exposure['mode']}"
        )
