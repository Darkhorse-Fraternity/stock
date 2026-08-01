from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


CN_MARKET = "cn"
US_MARKET = "us"


@dataclass(frozen=True, slots=True)
class MarketProfile:
    code: str
    label: str
    timezone: ZoneInfo
    currency: str
    currency_symbol: str
    lot_size: int
    volume_unit: str
    same_day_sell: bool
    default_universe_code: str
    default_universe_name: str
    session_start: time
    session_end: time
    midday_start: time | None = None
    midday_end: time | None = None
    uses_code_prefixes: bool = False
    uses_special_treatment_labels: bool = False
    has_daily_price_limits: bool = False
    supports_tick_ignition: bool = False
    unsupported_parameter_ids: frozenset[str] = frozenset()


MARKET_PROFILES = {
    CN_MARKET: MarketProfile(
        code=CN_MARKET,
        label="A股",
        timezone=ZoneInfo("Asia/Shanghai"),
        currency="CNY",
        currency_symbol="¥",
        lot_size=100,
        volume_unit="手",
        same_day_sell=False,
        default_universe_code="BK0800",
        default_universe_name="人工智能",
        session_start=time(9, 30),
        session_end=time(15, 0),
        midday_start=time(11, 30),
        midday_end=time(13, 0),
        uses_code_prefixes=True,
        uses_special_treatment_labels=True,
        has_daily_price_limits=True,
        supports_tick_ignition=True,
    ),
    US_MARKET: MarketProfile(
        code=US_MARKET,
        label="美股",
        timezone=ZoneInfo("America/New_York"),
        currency="USD",
        currency_symbol="$",
        lot_size=1,
        volume_unit="股",
        same_day_sell=True,
        default_universe_code="NASDAQ100",
        default_universe_name="纳斯达克100",
        session_start=time(9, 30),
        session_end=time(16, 0),
        unsupported_parameter_ids=frozenset(
            {
                "stock_prefixes",
                "exclude_st",
                "exclude_limit_up",
                "turnover_rate_min",
                "turnover_rate_max",
                "volume_ratio_min",
                "float_market_cap_min",
                "float_market_cap_max",
                "pb_max",
                "ignition_price_10s_min",
                "ignition_volume_ratio_min",
                "fcf_yield_min",
                "revenue_growth_min",
                "profit_growth_min",
                "eps_growth_min",
                "roe_min",
                "roa_min",
                "roic_min",
                "gross_margin_min",
                "net_margin_min",
                "operating_cashflow_positive",
                "free_cashflow_positive",
                "debt_ratio_max",
                "current_ratio_min",
            }
        ),
    ),
}

_MARKET_ALIASES = {
    "a": CN_MARKET,
    "a股": CN_MARKET,
    "ashare": CN_MARKET,
    "china": CN_MARKET,
    "cn": CN_MARKET,
    "中国": CN_MARKET,
    "美股": US_MARKET,
    "usa": US_MARKET,
    "us": US_MARKET,
    "美国": US_MARKET,
}


def normalize_market(value: object, default: str = CN_MARKET) -> str:
    text = str(value or "").strip().casefold()
    normalized = _MARKET_ALIASES.get(text, text)
    if normalized in MARKET_PROFILES:
        return normalized
    fallback = str(default or CN_MARKET).strip().casefold()
    return fallback if fallback in MARKET_PROFILES else CN_MARKET


def market_profile(value: object = CN_MARKET) -> MarketProfile:
    return MARKET_PROFILES[normalize_market(value)]


def parameter_applicable(parameter_id: object, market: object = CN_MARKET) -> bool:
    return str(parameter_id or "") not in market_profile(market).unsupported_parameter_ids


def strategy_market(strategy: dict | None) -> str:
    state = (strategy or {}).get("parameters", {}).get("market", {})
    return normalize_market(state.get("value") if state.get("enabled", True) else CN_MARKET)


def strict_strategy_market(strategy: Mapping[str, object]) -> str:
    """Resolve one explicit strategy market without defaults or ambiguity."""

    if not isinstance(strategy, Mapping):
        raise TypeError("strategy must be a mapping")
    values: list[str] = []
    if "market" in strategy:
        direct = strategy.get("market")
        if type(direct) is not str or not direct.strip():
            raise ValueError("strategy market must be explicitly cn or us")
        values.append(direct.strip().casefold())

    if "parameters" in strategy:
        parameters = strategy.get("parameters")
        if not isinstance(parameters, Mapping):
            raise ValueError("strategy parameters must be a mapping")
        if "market" in parameters:
            market_parameter = parameters.get("market")
            if not isinstance(market_parameter, Mapping):
                raise ValueError("strategy parameters.market must be a mapping")
            if "value" not in market_parameter:
                raise ValueError("strategy parameters.market.value is required")
            nested = market_parameter.get("value")
            if type(nested) is not str or not nested.strip():
                raise ValueError("strategy market must be explicitly cn or us")
            values.append(nested.strip().casefold())

    if not values or any(item not in MARKET_PROFILES for item in values):
        raise ValueError("strategy market must be explicitly cn or us")
    if len(set(values)) != 1:
        raise ValueError("strategy market identities conflict")
    return values[0]


def strategy_universe(
    strategy: dict | None,
    *,
    board_code: object | None = None,
    board_name: object | None = None,
    market: object | None = None,
) -> tuple[str, str]:
    profile = market_profile(
        strategy_market(strategy) if market is None else market
    )
    parameters = (strategy or {}).get("parameters", {})
    configured_code = str(
        board_code
        or (parameters.get("board_code") or {}).get("value")
        or profile.default_universe_code
    ).strip()
    configured_name = str(
        board_name
        or (parameters.get("board_name") or {}).get("value")
        or profile.default_universe_name
    ).strip()
    if profile.code == US_MARKET:
        if configured_code in {"", "BK0800"}:
            configured_code = profile.default_universe_code
        if configured_name in {"", "人工智能"}:
            configured_name = profile.default_universe_name
    return configured_code, configured_name


def market_now(now: datetime | None = None, market: object = CN_MARKET) -> datetime:
    profile = market_profile(market)
    current = now or datetime.now(profile.timezone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return current.astimezone(profile.timezone)


def market_date(now: datetime | None = None, market: object = CN_MARKET) -> date:
    return market_now(now, market).date()


def next_business_date(value: date) -> date:
    current = value + timedelta(days=1)
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current


def order_session_date(now: datetime, market: object = CN_MARKET) -> date:
    """Return the exchange-local session for an order created at ``now``.

    A recommendation generated after a market closes belongs to the next
    weekday session. This is especially important for the 08:00 China-time US
    run, which occurs after the previous New York session and before the next.
    """

    profile = market_profile(market)
    current = market_now(now, profile.code)
    session_date = current.date()
    if current.weekday() >= 5:
        while session_date.weekday() >= 5:
            session_date += timedelta(days=1)
        return session_date
    if current.timetz().replace(tzinfo=None) > profile.session_end:
        return next_business_date(session_date)
    return session_date


def is_market_open(now: datetime | None = None, market: object = CN_MARKET) -> bool:
    profile = market_profile(market)
    current = market_now(now, profile.code)
    if current.weekday() >= 5:
        return False
    clock = current.timetz().replace(tzinfo=None)
    if not profile.session_start <= clock <= profile.session_end:
        return False
    if profile.midday_start and profile.midday_end:
        return not profile.midday_start < clock < profile.midday_end
    return True
