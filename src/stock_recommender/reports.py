from __future__ import annotations

import ipaddress
import math
import re
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote, urlsplit, urlunsplit

from .config import DEFAULT_BOARD_CODE, DEFAULT_BOARD_NAME, DEFAULT_LLM_TIMEOUT_SECONDS
from .context import (
    collect_recommendation_plan,
    enrich_recommendation_plan_with_ticks,
    extract_market_payload,
    generate_agent_context_from_plan,
    portfolio_signal_payload,
    recommendation_context_payload,
)
from .llm import call_llm_analysis
from .market_regime import normalize_market_regime_decision
from .markets import CN_MARKET, market_profile, strategy_market
from .parameters import chase_risk_threshold, normalize_portfolio_config
from .portfolio_engine.contracts import DecisionBatch, PortfolioSnapshot
from .recommendation import RecommendationOutput, RecommendationPlan
from .universe import normalize_sector_filters, normalize_watchlist
from .utils import number


def _normalized_url_component(value: str, *, safe: str) -> str:
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        raise ValueError("invalid percent escape")

    def normalize_escape(match: re.Match[str]) -> str:
        byte = int(match.group(1), 16)
        character = chr(byte)
        if character.isascii() and (
            character.isalnum() or character in "-._~"
        ):
            return character
        return f"%{byte:02X}"

    normalized = re.sub(r"%([0-9A-Fa-f]{2})", normalize_escape, value)
    return quote(normalized, safe=f"{safe}%")


def _normalized_url_host(hostname: str) -> str:
    if ":" in hostname:
        return f"[{ipaddress.IPv6Address(hostname).compressed}]"
    try:
        return str(ipaddress.IPv4Address(hostname))
    except ipaddress.AddressValueError:
        pass
    ascii_host = hostname.encode("idna").decode("ascii").lower()
    if len(ascii_host) > 253:
        raise ValueError("hostname is too long")
    labels = ascii_host.rstrip(".").split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or re.fullmatch(r"[a-z0-9-]+", label) is None
        for label in labels
    ):
        raise ValueError("invalid hostname")
    return ascii_host


def _safe_performance_url(url: object) -> str:
    target = str(url or "").strip()
    if (
        not target
        or len(target) > 2048
        or any(
            character.isspace()
            or ord(character) < 32
            or 127 <= ord(character) <= 159
            for character in target
        )
    ):
        return ""
    try:
        parsed = urlsplit(target)
        port = parsed.port
        hostname = _normalized_url_host(parsed.hostname or "")
        path = _normalized_url_component(
            parsed.path,
            safe="/:@!$&'*+,;=-._~",
        )
        query = _normalized_url_component(
            parsed.query,
            safe="=&/?@!$'*+,;:-._~",
        )
        fragment = _normalized_url_component(
            parsed.fragment,
            safe="=&/?@!$'*+,;:-._~",
        )
    except (UnicodeError, ValueError):
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    authority = f"{hostname}:{port}" if port is not None else hostname
    return urlunsplit((parsed.scheme.lower(), authority, path, query, fragment))


def append_performance_link(report: str, url: str) -> str:
    target = _safe_performance_url(url)
    if not target or target in report:
        return report
    return f"{report.rstrip()}\n\n📊 [查看策略表现](<{target}>)"


def _ratio_text(value: float) -> str:
    return f"{value:.2f}%" if math.isfinite(value) else "--"


def _position_direction(side: object) -> str:
    return "多头" if getattr(side, "value", side) == "LONG" else "空头"


def _intent_action(item: object) -> str:
    effect = getattr(item.position_effect, "value", item.position_effect)
    if getattr(item.position_side, "value", item.position_side) == "SHORT":
        return "空头回补" if effect in {"REDUCE", "CLOSE"} else "空头卖出"
    return "多头减仓" if effect in {"REDUCE", "CLOSE"} else "多头买入"


def _risk_reason_text(reason: object) -> str:
    return {
        "MARGIN_CALL": "保证金追缴，强制去杠杆（保证金率低于维持线）",
        "LONG_STOP_LOSS": "多头止损",
        "SHORT_STOP_LOSS": "空头止损回补",
        "LONG_TRAILING_STOP": "多头追踪止盈",
        "SHORT_TRAILING_STOP": "空头追踪止盈回补",
    }.get(str(reason or ""), "")


def _intent_line(item: object) -> str:
    reason = _risk_reason_text(item.reason)
    suffix = f" · {reason}" if reason else ""
    return (
        f"订单意图：{item.symbol} {_intent_action(item)} {item.quantity} 股"
        f"{suffix}"
    )


def _fill_action(
    item: object,
    intents_by_id: Mapping[str, object],
    progress_by_intent: Mapping[str, object],
) -> str:
    intent = intents_by_id.get(item.intent_id)
    if intent is not None:
        direction = _intent_action(intent)
    else:
        progress = progress_by_intent.get(item.intent_id)
        side = getattr(getattr(progress, "position_side", None), "value", None)
        order_side = getattr(getattr(progress, "order_side", None), "value", None)
        if side == "SHORT":
            direction = "空头回补" if order_side == "BUY" else "空头卖出"
        elif side == "LONG":
            direction = "多头买入" if order_side == "BUY" else "多头卖出"
        else:
            direction = "方向未知"
    return (
        f"模拟成交：{item.symbol} {direction} "
        f"{item.quantity} 股 @ {item.price:.2f}"
    )


def _risk_update_action(item: object, intents: tuple[object, ...]) -> str:
    matched_reason = next(
        (
            _risk_reason_text(intent.reason)
            for intent in intents
            if intent.symbol == item.symbol and intent.position_side == item.side
        ),
        "",
    )
    direction = _position_direction(item.side)
    if matched_reason:
        return f"风险动作：{item.symbol} {direction} · {matched_reason}"
    if item.position_mode == "COVER_ONLY":
        return f"风险状态：{item.symbol} 空头 · 仅允许空头回补"
    if item.trailing_active:
        return f"风险状态：{item.symbol} {direction} · 追踪退出已激活"
    return f"风险状态：{item.symbol} {direction} · 风险参数已更新"


def _event_action(item: object) -> str:
    labels = {
        "RISK_CHANGED": "风险状态已更新",
        "FINANCING_COST_ACCRUED": "融资成本计提",
        "BORROW_COST_ACCRUED": "借券成本计提",
    }
    event_type = str(getattr(item, "type", ""))
    label = labels.get(event_type, event_type)
    data = getattr(item, "data", {})
    if event_type == "RISK_CHANGED" and isinstance(data, Mapping):
        reason = _risk_reason_text(data.get("reason"))
        if reason:
            return reason
        if data.get("position_mode") == "COVER_ONLY":
            return "仅允许空头回补"
    return label


def _account_metric_lines(snapshot: PortfolioSnapshot) -> tuple[str, str]:
    metrics = snapshot.metrics
    return (
        (
            f"总敞口 {_ratio_text(metrics.gross_exposure_pct)} · "
            f"净敞口 {_ratio_text(metrics.net_exposure_pct)} · "
            f"保证金率 {_ratio_text(metrics.margin_rate_pct)}"
        ),
        (
            f"融资负债 {metrics.margin_loan:,.2f} · "
            f"融资成本 {metrics.accrued_financing_cost:,.2f} · "
            f"借券成本 {metrics.accrued_borrow_cost:,.2f}"
        ),
    )


def format_portfolio_snapshot(
    strategy: Mapping[str, Any],
    snapshot: PortfolioSnapshot,
    *,
    performance_url: str = "",
) -> str:
    exposure_policy = strategy.get("exposure_policy")
    max_positions = (
        exposure_policy.get("max_positions", 10)
        if isinstance(exposure_policy, Mapping)
        else 10
    )
    lines = [
        "📊 **策略持仓每小时报告**",
        f"策略：{strategy.get('name') or strategy.get('id')} · v{strategy.get('revision')}",
        (
            f"净值：{snapshot.metrics.equity:,.2f} · "
            f"可用现金：{snapshot.account.available_cash:,.2f} · "
            f"持仓：{len(snapshot.positions)}/{max_positions}"
        ),
        *_account_metric_lines(snapshot),
    ]
    for held in snapshot.positions:
        lines.append(
            f"- {held.symbol} {_position_direction(held.side)}：{held.quantity} 股 · "
            f"现价 {held.current_price:.2f}"
        )
    if not snapshot.positions:
        lines.append("- 当前空仓")
    return append_performance_link("\n".join(lines), performance_url)


def format_portfolio_actions(
    strategy: Mapping[str, Any],
    batch: DecisionBatch,
    *,
    snapshot: PortfolioSnapshot,
    performance_url: str = "",
) -> str:
    intents_by_id = {item.id: item for item in batch.intents}
    progress_by_intent = {item.intent_id: item for item in batch.execution_progress}
    actions = [
        *(_intent_line(item) for item in batch.intents),
        *(
            _fill_action(item, intents_by_id, progress_by_intent)
            for item in batch.fills
        ),
        *(
            _risk_update_action(item, batch.intents)
            for item in batch.position_risk_updates
        ),
        *(f"事件：{_event_action(item)}" for item in batch.events),
    ]
    if not actions:
        return ""
    lines = [
        "⚠️ **策略组合动作通知**",
        f"策略：{strategy.get('name') or strategy.get('id')} · v{strategy.get('revision')}",
        *_account_metric_lines(snapshot),
        *actions,
    ]
    return append_performance_link("\n".join(lines), performance_url)


def decorate_strategy_output(report: str, strategy: dict | None) -> str:
    if not strategy or not strategy.get("id"):
        return report
    stage = strategy.get("lifecycle", {}).get("stage", "draft")
    labels = {
        "draft": "🧪 **草稿策略输出（不构成正式推荐）**",
        "backtesting": "🧪 **回测中策略输出（不构成正式推荐）**",
        "paper": "🧪 **模拟盘观察（非实盘推荐）**",
        "live": "✅ **已通过门禁的实盘策略**",
        "paused": "⏸️ **已暂停策略输出**",
        "archived": "📦 **已归档策略输出**",
    }
    version = (
        f"使用策略：{strategy.get('name') or strategy.get('id')} ({strategy.get('id')}) · "
        f"市场：{market_profile(strategy_market(strategy)).label} · "
        f"策略版本：v{strategy.get('revision', 1)} · {stage} · "
        f"信号：{strategy.get('signal', {}).get('model', 'factor_rank_v1')} @ "
        f"{strategy.get('signal', {}).get('run_time', '08:00')}"
    )
    return f"{labels.get(stage, labels['draft'])}\n{version}\n\n{report}"


def format_volume(value: float, *, market: object = CN_MARKET) -> str:
    volume = number(value)
    unit = market_profile(market).volume_unit
    if volume >= 100_000_000:
        return f"{volume / 100_000_000:.2f} 亿{unit}"
    if volume >= 10_000:
        return f"{volume / 10_000:.2f} 万{unit}"
    return f"{volume:.0f} {unit}"


def format_recommendation_snapshot(
    rows: Iterable[dict],
    *,
    limit: int = 3,
    generated_at: str = "",
    market: object | None = None,
) -> str:
    selected = list(rows)[: max(0, limit)]
    if not selected:
        return ""
    lines = ["📈 **推荐股每小时成交与涨跌跟踪**"]
    if generated_at:
        lines.append(f"更新时间：{generated_at}")
    normalized_market = market or (selected[0].get("market") if selected else CN_MARKET)
    profile = market_profile(normalized_market)
    for item in selected:
        percent = number(item.get("change_percent", item.get("percent")))
        price = number(item.get("price"))
        volume = number(item.get("volume_hands", item.get("volume")))
        turnover = number(item.get("turnover_cny", item.get("turnover")))
        signal_score = number(item.get("signal_score", item.get("score")), default=float("nan"))
        score_text = f"，信号分 {signal_score:.2f}/100" if signal_score == signal_score else ""
        features = item.get("signal_features") or {}
        momentum = number(features.get("momentum20"), default=float("nan"))
        trend = number(features.get("trend"), default=float("nan"))
        factor_text = (
            f"，20日动量 {momentum * 100:+.2f}%，趋势 {trend:.0f}/2"
            if momentum == momentum and trend == trend
            else ""
        )
        lines.append(
            f"- {item.get('name') or item.get('symbol')} ({item.get('symbol')})："
            f"最新价 {profile.currency_symbol}{price:.2f}，涨跌幅 {percent:+.2f}%；"
            f"成交量 {format_volume(volume, market=profile.code)}，"
            f"成交额 {format_amount(turnover, market=profile.code)}{score_text}{factor_text}"
        )
    return "\n".join(lines)


def format_market_regime_summary(decision: dict) -> str:
    normalized = normalize_market_regime_decision(decision)
    breadth = (
        normalized.get("breadth20_pct"),
        normalized.get("breadth60_pct"),
        normalized.get("trend_breadth_pct"),
    )
    if all(value is not None for value in breadth):
        detail = f"20日/60日/趋势广度 {breadth[0]:.1f}%/{breadth[1]:.1f}%/{breadth[2]:.1f}%"
    else:
        detail = f"有效样本 {normalized['sample_size']} 只"
    return (
        f"🧭 **板块状态 / 市场状态**：{normalized['label']}（{normalized['state']}） · "
        f"目标仓位 {normalized['target_exposure_pct']:.0f}% · {detail} · "
        f"模型 {normalized['model']}"
    )


def format_data_quality_funnel(data_quality: dict) -> str:
    quality = data_quality or {}
    source_labels = {
        "primary": "主数据源",
        "snapshot_realtime": "完整板块快照 + 实时行情",
        "watchlist": "自选股池实时行情",
        "injected_primary": "指定数据源",
        "unavailable": "不可用",
    }
    source = source_labels.get(str(quality.get("source_mode") or ""), str(quality.get("source_mode") or "未知"))
    status = "可运行" if quality.get("status") == "READY" else "已阻断"
    return (
        "🔎 **数据漏斗**："
        f"股票池 {int(number(quality.get('raw_count')))} → "
        f"实时行情 {int(number(quality.get('quote_count', quality.get('raw_count'))))} → "
        f"基础过滤 {int(number(quality.get('basic_count')))} → "
        f"历史特征 {int(number(quality.get('history_ready_count')))} → "
        f"策略过滤 {int(number(quality.get('strategy_filtered_count')))} → "
        f"动量准入 {int(number(quality.get('absolute_momentum_count')))} → "
        f"最终 {int(number(quality.get('selected_count')))}；"
        f"{source} · {status}"
    )


def format_portfolio_signal_facts(plan: RecommendationPlan) -> str:
    signals = portfolio_signal_payload(plan)
    if not signals:
        return ""
    lines = ["🧭 **组合模型信号**"]
    for item in signals:
        direction = "做多" if item["side"] == "LONG" else "做空"
        lines.append(
            f"- {direction} {item['symbol']} · 权重 {item['requested_weight_pct']:g}% "
            f"· {item['model_id']}"
        )
    return "\n".join(lines)


def render_report(plan: RecommendationPlan, *, strategy: dict | None = None) -> str:
    current_strategy = strategy
    report_time = plan.generated_at
    eligible = list(plan.candidates)
    error = plan.fetch_error
    market_regime = plan.market_regime
    profile = market_profile(plan.market)
    signal_facts = format_portfolio_signal_facts(plan)

    if not plan.selected_candidates:
        return decorate_strategy_output("\n".join(
            [
                f"📊 **策略运行报告** ({report_time.strftime('%Y 年%m月%d日')})",
                "",
                format_market_regime_summary(market_regime),
                "",
                format_data_quality_funnel(plan.data_quality),
                "",
                *([signal_facts, ""] if signal_facts else []),
                "本次没有匹配股票，不新增持仓；既有持仓继续由退出 Pipeline 管理。",
                f"数据状态：{error or market_regime['reason']}",
                "",
                "仅供参考，不构成投资建议。",
            ]
        ), current_strategy)

    top = list(plan.selected_candidates)
    avg_score = sum(item["score"] for item in top) / len(top)
    sources = list(plan.sources)
    universe_label = "自选股池" if plan.universe_type == "watchlist" else f"{plan.board_name}板块"
    filter_label = f"（板块：{'、'.join(plan.sector_filters)}）" if plan.sector_filters else ""

    report = [
        f"📊 **策略运行报告** ({report_time.strftime('%Y 年%m月%d日')})",
        "=" * 30,
        "",
        f"🎯 **{universe_label}{filter_label}精选 {len(top)} 只值得关注的股票**",
        "",
        format_market_regime_summary(market_regime),
        "",
        format_data_quality_funnel(plan.data_quality),
        "",
        *([signal_facts, ""] if signal_facts else []),
    ]

    for index, stock in enumerate(top, 1):
        sector_line = f"  🏷️ **板块**: {stock.get('sector')}" if stock.get("sector") else None
        report.extend(
            [
                f"{stock['rating_emoji']} **【推荐 #{index}】{stock['name']} ({stock['symbol']})**",
                f"  🎯 **评级**: {stock['rating']} (factor_rank_v1：{number(stock['score']):.1f}/100)",
                *([sector_line] if sector_line else []),
                f"  💰 **最新价**: {profile.currency_symbol}{number(stock.get('price')):.2f}",
                f"  📊 **涨跌幅**: {number(stock.get('percent')):.2f}% ({number(stock.get('change')):+.2f})",
                f"  🔄 **换手率**: {number(stock.get('turnover_rate')):.2f}%",
                f"  💵 **成交额**: {format_amount(number(stock.get('turnover')), market=plan.market)}",
                f"  ⚠️ **风险等级**: {stock['risk_level']}",
                "",
                "  **📝 推荐理由**:",
            ]
        )
        for reason in stock["reasons"][:5]:
            report.append(f"    • {reason}")
        report.extend(["", "  " + "─" * 28, ""])

    snapshot = format_recommendation_snapshot(
        top,
        limit=len(top),
        generated_at=report_time.strftime("%m月%d日 %H:%M"),
        market=plan.market,
    )
    if snapshot:
        report.extend([snapshot, ""])

    if avg_score >= 80:
        sentiment, position = "🟢 题材热度较高，注意追高风险", "中等仓位"
    elif avg_score >= 65:
        sentiment, position = "🟡 题材活跃，精选个股", "中低仓位"
    else:
        sentiment, position = "🔴 信号一般，控制仓位", "轻仓观察"

    report.extend(
        [
            "=" * 30,
            "💡 **今日总结**",
            f"  • {sentiment}",
            f"  • 建议仓位：{position}",
            f"  • 平均评分：{avg_score:.0f}/100",
            "",
            "🎲 **风险提示**",
            "  • 市场有风险，投资需谨慎",
            "  • 以上仅为技术面量化筛选，不构成投资建议",
            "",
            "📌 **数据说明**",
            f"  • 更新时间：{report_time.strftime('%m月%d日 %H:%M')}",
            f"  • 数据来源：{'；'.join(sources)}",
            "  • 信号口径：北京时间 08:00 运行，仅使用对应市场前一交易日收盘数据",
            f"  • 任务频率：北京时间工作日 08:00 推荐；{profile.label}开市期间整点跟踪持仓股",
            "",
            "=" * 30,
        ]
    )
    return decorate_strategy_output("\n".join(report), current_strategy)


def render_strategy_plan_report(plan: RecommendationPlan, *, strategy: dict | None = None) -> str:
    """Render deterministic Pipeline output without delegating decisions to the LLM."""
    portfolio = normalize_portfolio_config((strategy or {}).get("portfolio"))
    report_time = plan.generated_at
    selected = list(plan.selected_candidates)
    profile = market_profile(plan.market)
    universe_label = "自选股池" if plan.universe_type == "watchlist" else f"{plan.board_name}板块"
    filter_label = f"（板块：{'、'.join(plan.sector_filters)}）" if plan.sector_filters else ""
    signal_facts = format_portfolio_signal_facts(plan)

    report = [
        f"📋 **确定性策略入场计划** ({report_time.strftime('%Y 年%m月%d日')})",
        "",
        format_market_regime_summary(plan.market_regime),
        "",
        format_data_quality_funnel(plan.data_quality),
        "",
        f"候选范围：{universe_label}{filter_label}",
        *(["", signal_facts] if signal_facts else []),
    ]
    if not selected:
        report.extend(
            [
                "策略动作：本次不新增持仓；既有持仓继续由退出 Pipeline 管理。",
                f"数据状态：{plan.fetch_error or plan.market_regime['reason']}",
                "",
            ]
        )
    else:
        report.extend(["策略动作：候选进入组合 Pipeline，实际成交以容量、风控与撮合结果为准。", ""])
        for index, stock in enumerate(selected, 1):
            reason_text = "；".join(str(reason) for reason in stock.get("reasons") or []) or "满足当前策略筛选条件"
            report.extend(
                [
                    f"{index}. **{stock['name']} ({stock['symbol']})**",
                    (
                        f"   参考价 {profile.currency_symbol}{number(stock.get('price')):.2f} · "
                        f"当日涨跌 {number(stock.get('percent', stock.get('change_percent'))):+.2f}% · "
                        f"信号分 {number(stock.get('score', stock.get('signal_score'))):.1f}/100"
                    ),
                    f"   入选依据：{reason_text}",
                ]
            )
        report.append("")

    report.extend(
        [
            "🧩 **组合 Pipeline 与退出规则**",
            (
                f"  • 容量：每策略最多持有 {portfolio['max_positions']} 只；"
                f"目标单股仓位 {portfolio['target_weight_pct']:g}%"
            ),
            (
                f"  • 退出：止损 {portfolio['stop_loss_pct']:g}%；"
                f"盈利达到 {portfolio['trailing_activation_pct']:g}% 后启用追踪止盈，"
                f"回撤 {portfolio['trailing_drawdown_pct']:g}% 退出"
            ),
            f"  • 信号失效：连续 {portfolio['signal_invalid_days']} 个交易日后退出或替换",
            (
                "  • 执行："
                + ("美股整股模拟、允许日内卖出" if profile.same_day_sell else "A 股 100 股一手、T+1 可卖")
                + "；订单还会经过组合容量、风险准入、费用、滑点与成交量限制"
            ),
            "",
            "📌 **数据与任务说明**",
            f"  • 更新时间：{report_time.strftime('%m月%d日 %H:%M')}",
            f"  • 数据来源：{'；'.join(plan.sources) or '暂无有效行情源'}",
            "  • 信号口径：北京时间工作日 08:00 运行，仅使用对应市场前一交易日收盘数据",
            f"  • 持仓跟踪：{profile.label}开市期间整点汇报成交量、涨跌幅与退出动作",
            "",
            "仅供策略验证，不构成投资建议。",
        ]
    )
    return decorate_strategy_output("\n".join(report), strategy)


def generate_report_result(
    *,
    now: datetime | None = None,
    board_code: str = DEFAULT_BOARD_CODE,
    board_name: str = DEFAULT_BOARD_NAME,
    top_n: int = 3,
    board_fetcher: Callable | None = None,
    fallback_fetcher: Callable | None = None,
    history_fetcher: Callable | None = None,
    financial_fetcher: Callable | None = None,
    enrich_limit: int | None = None,
    strategy: dict | None = None,
    watchlist: str | Iterable[object] | None = None,
    sector_filters: str | Iterable[object] | None = None,
    watchlist_fetcher: Callable | None = None,
) -> RecommendationOutput:
    watchlist_entries = normalize_watchlist(
        watchlist,
        market=strategy_market(strategy),
    )
    sectors = normalize_sector_filters(sector_filters)
    plan = collect_recommendation_plan(
        now=now,
        board_code=board_code,
        board_name=board_name,
        candidate_limit=top_n,
        selection_limit=top_n,
        board_fetcher=board_fetcher,
        fallback_fetcher=fallback_fetcher,
        history_fetcher=history_fetcher,
        financial_fetcher=financial_fetcher,
        enrich_limit=enrich_limit,
        strategy=strategy,
        watchlist=watchlist_entries if watchlist is not None else None,
        sector_filters=sectors if sector_filters is not None else None,
        watchlist_fetcher=watchlist_fetcher,
    )
    return render_report_result(plan, strategy=strategy)


def render_report_result(
    plan: RecommendationPlan,
    *,
    strategy: dict | None = None,
) -> RecommendationOutput:
    """Render a deterministic report from an already computed strategy plan."""

    return RecommendationOutput(report=render_report(plan, strategy=strategy), plan=plan)


def generate_report(**kwargs) -> str:
    return generate_report_result(**kwargs).report


def generate_ai_report_result(
    *,
    now: datetime | None = None,
    board_code: str = DEFAULT_BOARD_CODE,
    board_name: str = DEFAULT_BOARD_NAME,
    candidate_limit: int = 5,
    tracking_limit: int = 3,
    board_fetcher: Callable | None = None,
    fallback_fetcher: Callable | None = None,
    watchlist: str | Iterable[object] | None = None,
    sector_filters: str | Iterable[object] | None = None,
    watchlist_fetcher: Callable | None = None,
    enable_tick: bool = False,
    tick_fetcher: Callable | None = None,
    tick_limit: int = 2,
    history_fetcher: Callable | None = None,
    financial_fetcher: Callable | None = None,
    enrich_limit: int | None = None,
    llm_client: Callable | None = None,
    llm_base_url: str = "",
    llm_model: str = "Flux_AI/Flux_AI:latest",
    llm_api_key: str = "ollama",
    llm_timeout: int = DEFAULT_LLM_TIMEOUT_SECONDS,
    strategy: dict | None = None,
) -> RecommendationOutput:
    current_strategy = strategy
    selection_limit = int((current_strategy or {}).get("validation", {}).get("top_n", min(3, candidate_limit)))
    plan = collect_recommendation_plan(
        now=now,
        board_code=board_code,
        board_name=board_name,
        candidate_limit=candidate_limit,
        selection_limit=selection_limit,
        board_fetcher=board_fetcher,
        fallback_fetcher=fallback_fetcher,
        history_fetcher=history_fetcher,
        financial_fetcher=financial_fetcher,
        enrich_limit=enrich_limit,
        strategy=current_strategy,
        watchlist=watchlist,
        sector_filters=sector_filters,
        watchlist_fetcher=watchlist_fetcher,
    )
    return render_ai_report_result(
        plan,
        tracking_limit=tracking_limit,
        llm_client=llm_client,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        llm_timeout=llm_timeout,
        strategy=current_strategy,
        enable_tick=enable_tick,
        tick_fetcher=tick_fetcher,
        tick_limit=tick_limit,
    )


def render_ai_report_result(
    plan: RecommendationPlan,
    *,
    tracking_limit: int = 3,
    llm_client: Callable | None = None,
    llm_base_url: str = "",
    llm_model: str = "Flux_AI/Flux_AI:latest",
    llm_api_key: str = "ollama",
    llm_timeout: int = DEFAULT_LLM_TIMEOUT_SECONDS,
    strategy: dict | None = None,
    enable_tick: bool = False,
    tick_fetcher: Callable | None = None,
    tick_limit: int = 2,
) -> RecommendationOutput:
    """Render optional AI commentary after the deterministic plan is available.

    Callers that execute a scheduled strategy can persist ``plan`` before
    entering this adapter. A slow model or failed delivery therefore cannot
    erase the portfolio decision for the day.
    """

    current_strategy = strategy
    if enable_tick:
        plan = enrich_recommendation_plan_with_ticks(
            plan,
            tick_fetcher=tick_fetcher,
            tick_limit=tick_limit,
        )
    context = generate_agent_context_from_plan(plan, strategy=current_strategy)
    payload = recommendation_context_payload(plan)
    market_regime = plan.market_regime
    regime_summary = format_market_regime_summary(market_regime)
    if payload.get("data_quality", {}).get("status") == "BLOCKED":
        quality = payload.get("data_quality") or {}
        unavailable = quality.get("source_mode") == "unavailable" and not quality.get("raw_count")
        report = decorate_strategy_output("\n".join(
            [
                (
                    "⚠️ **实时行情不可用，本次不生成策略动作**"
                    if unavailable
                    else "⚠️ **数据覆盖不足，本次不生成策略动作**"
                ),
                "",
                regime_summary,
                "",
                format_data_quality_funnel(quality),
                f"阻断原因：{quality.get('reason') or payload.get('fetch_error') or 'unknown'}",
                "今日不生成股票推荐。",
                "已有持仓不会因为数据不足被清空，仍由价格风控与退出 Pipeline 管理。",
                "",
                "仅供参考，不构成投资建议。",
            ]
        ), current_strategy)
        return RecommendationOutput(report=report, plan=plan)
    if payload.get("fetch_error") or any("估算兜底" in source for source in payload.get("source", [])):
        report = decorate_strategy_output("\n".join(
            [
                "⚠️ **实时行情不可用**",
                "",
                regime_summary,
                "",
                f"数据源错误：{payload.get('fetch_error') or 'unknown'}",
                "今日不生成股票推荐，避免基于兜底数据误导交易判断。",
                "",
                "仅供参考，不构成投资建议。",
            ]
        ), current_strategy)
        return RecommendationOutput(report=report, plan=plan)
    if not payload.get("candidates"):
        report = decorate_strategy_output(
            (
                f"⚪ **本次不新增持仓**\n\n{regime_summary}\n\n"
                "板块状态或个股绝对动量未通过；既有持仓继续由退出 Pipeline 管理。"
                "\n\n仅供参考，不构成投资建议。"
            ),
            current_strategy,
        )
        return RecommendationOutput(report=report, plan=plan)
    client = llm_client or call_llm_analysis
    try:
        analysis = client(
            context,
            base_url=llm_base_url,
            model=llm_model,
            api_key=llm_api_key,
            timeout=llm_timeout,
        )
        guarded = apply_risk_guard(context, analysis, strategy=current_strategy)
        by_symbol = {str(item.get("symbol")): item for item in payload.get("candidates") or []}
        snapshot_rows = [
            by_symbol[symbol]
            for symbol in payload.get("portfolio_candidates") or []
            if symbol in by_symbol
        ][:tracking_limit]
        snapshot = format_recommendation_snapshot(
            snapshot_rows,
            limit=tracking_limit,
            generated_at=payload.get("generated_at") or "",
            market=plan.market,
        )
        explained = f"{regime_summary}\n\n{guarded}"
        result = f"{explained}\n\n{snapshot}" if snapshot else explained
        report = decorate_strategy_output(result, current_strategy)
        return RecommendationOutput(report=report, plan=plan)
    except Exception:
        fallback = render_strategy_plan_report(plan, strategy=current_strategy)
        report = (
            "ℹ️ **AI 解说暂不可用**（策略计算、组合风控与执行 Pipeline 不受影响）\n\n"
            f"{fallback}"
        )
        return RecommendationOutput(report=report, plan=plan)


def generate_ai_report(**kwargs) -> str:
    return generate_ai_report_result(**kwargs).report


def apply_risk_guard(context: str, analysis: str, *, strategy: dict | None = None) -> str:
    payload = extract_market_payload(context)
    if not payload:
        return analysis
    candidates = payload.get("candidates") or []
    known_symbols = {str(item.get("symbol")) for item in candidates}
    mentioned_symbols = {
        symbol
        for symbol in known_symbols
        if re.search(rf"(?<![A-Z0-9]){re.escape(symbol)}(?![A-Z0-9])", analysis, flags=re.I)
    }
    unknown_symbols = set(re.findall(r"\b\d{6}\b", analysis)) - known_symbols
    if payload.get("market", "cn") == "cn" and unknown_symbols:
        return build_conservative_report(payload, "数据一致性校验失败：AI 输出包含非候选股票代码", strategy=strategy)
    required_symbols = set(payload.get("portfolio_candidates") or [])
    if not required_symbols.issubset(mentioned_symbols):
        return build_conservative_report(payload, "AI 未完整解释策略已确定的入池股票", strategy=strategy)

    has_high_risk = any(number(item.get("change_percent")) >= chase_risk_threshold(strategy) for item in candidates)
    unsafe_position = any(term in analysis for term in ["重仓", "中仓", "满仓", "30-40", "30%", "40%"])
    if not has_high_risk or not unsafe_position:
        return analysis

    return build_conservative_report(payload, "AI 原始输出包含对高涨幅股票的过高仓位建议", strategy=strategy)


def format_amount(value: float, *, market: object = CN_MARKET) -> str:
    amount = number(value)
    profile = market_profile(market)
    if profile.currency == "USD":
        if amount >= 1_000_000_000:
            return f"{profile.currency_symbol}{amount / 1_000_000_000:.2f}B"
        if amount >= 1_000_000:
            return f"{profile.currency_symbol}{amount / 1_000_000:.2f}M"
        if amount >= 1_000:
            return f"{profile.currency_symbol}{amount / 1_000:.1f}K"
        return f"{profile.currency_symbol}{amount:.0f}"
    if amount >= 100_000_000:
        return f"{amount / 100_000_000:.1f} 亿"
    if amount >= 10_000:
        return f"{amount / 10_000:.0f} 万"
    return f"{amount:.0f}"


def format_cny(value: float) -> str:
    return format_amount(value, market=CN_MARKET)


def market_sentiment_profile(candidates: list[dict], *, strategy: dict | None = None) -> dict:
    if not candidates:
        return {"label": "弱", "position": "2成", "entries": "3次", "summary": "候选不足，按弱情绪处理"}
    avg_score = sum(number(item.get("signal_score")) for item in candidates) / len(candidates)
    strong_count = sum(1 for item in candidates if number(item.get("change_percent")) >= chase_risk_threshold(strategy))
    if avg_score >= 75 or strong_count >= 2:
        return {"label": "强", "position": "8成", "entries": "10次", "summary": f"平均信号分 {avg_score:.0f}，强势候选 {strong_count} 只"}
    if avg_score >= 65:
        return {"label": "中", "position": "5成", "entries": "5次", "summary": f"平均信号分 {avg_score:.0f}，题材有热度但未全面爆发"}
    return {"label": "弱", "position": "2成", "entries": "3次", "summary": f"平均信号分 {avg_score:.0f}，信号偏弱"}


def candidate_action(item: dict, sentiment: dict, *, strategy: dict | None = None) -> dict:
    pct = number(item.get("change_percent"))
    turnover_rate = number(item.get("turnover_rate"))
    threshold = chase_risk_threshold(strategy)
    if pct >= threshold:
        return {
            "label": "观望或极轻仓试错",
            "position": "不超过1成",
            "entries": "最多1次",
            "reason": f"涨幅已经超过 {threshold:g}% 追高阈值，按系统风控规则降级",
        }
    if pct >= 3.5 and turnover_rate >= 2:
        return {
            "label": "低仓观察",
            "position": "2成以内",
            "entries": "2-3次",
            "reason": "涨幅和换手有启动特征，但未满足严格盘口确认",
        }
    return {
        "label": "观察，等待盘口确认",
        "position": "暂不买入",
        "entries": "确认后再拆分",
        "reason": "信号未过热，可放入观察池等待盘口确认",
    }


def ignition_signal_summary(signal: dict | None) -> str:
    if not signal:
        return "点火信号：未接入逐笔成交"
    status = "确认" if signal.get("confirmed") else "未确认"
    return (
        f"点火信号：{status}；10秒涨幅 {number(signal.get('price_change_10s')):.2f}%，"
        f"量比 {number(signal.get('volume_ratio')):.2f}x；{signal.get('reason') or '无说明'}"
    )


def price_position_summary(position: dict | None) -> str:
    if not position:
        return "价格位置数据不足"
    parts = []
    parts.append("开盘价上方" if position.get("above_open") else "未站上开盘价")
    parts.append("0轴上方" if position.get("above_zero_line") else "未站上0轴")
    parts.append("均价线上方" if position.get("above_vwap") else "未站上均价线")
    return "、".join(parts)


def append_candidate_explanation(
    lines: list[str],
    item: dict,
    sentiment: dict,
    *,
    strategy: dict | None = None,
    market: object = CN_MARKET,
) -> None:
    profile = market_profile(market)
    action = candidate_action(item, sentiment, strategy=strategy)
    pct = number(item.get("change_percent"))
    turnover_rate = number(item.get("turnover_rate"))
    turnover = number(item.get("turnover_cny"))
    pe = number(item.get("pe"))
    score = number(item.get("signal_score"))
    float_market_cap = number(item.get("float_market_cap_cny"))
    position_summary = price_position_summary(item.get("price_position"))
    ignition_summary = ignition_signal_summary(item.get("ignition_signal"))
    source = item.get("source") or "结构化行情"
    reasons = item.get("machine_reasons") or []
    tick_source_note = (
        "- 当前版本已尝试接入 10 秒逐笔成交；集合竞价最后一分钟成交量、五档盘口吃单数据仍未稳定接入，不在本报告中伪造判断。"
        if profile.supports_tick_ignition
        else f"- {profile.label}逐笔点火与盘口数据尚未接入，本报告不据此生成判断。"
    )
    lines.extend(
        [
            f"### {item['name']} ({item['symbol']})",
            "**入选理由**",
            f"- 来自今日 {item.get('board_name', DEFAULT_BOARD_NAME)} 股票池，实时数据源为{source}。",
            f"- 涨幅 {pct:.2f}%，换手率 {turnover_rate:.2f}%，成交额 {format_amount(turnover, market=market)}，机器信号分 {score:.0f}/100。",
            f"- 流通市值 {format_amount(float_market_cap, market=market)}，用于执行当前策略的市值筛选。",
            f"- 价格位置：{position_summary}。",
            f"- {ignition_summary}。",
            f"- PE {pe:.2f}，用于辅助判断估值风险，不单独作为买入依据。",
        ]
    )
    for reason in reasons[:3]:
        lines.append(f"- 量化理由：{reason}")
    lines.extend(
        [
            "**风险/降级理由**",
            f"- {action['reason']}。",
            tick_source_note,
            "**操作建议**",
            f"- 建议：{action['label']}。",
            f"- 单股仓位：{action['position']}；买入拆分：{action['entries']}。",
            "",
        ]
    )


def build_conservative_report(payload: dict, reason: str, *, strategy: dict | None = None) -> str:
    candidates = payload.get("candidates") or []
    threshold = chase_risk_threshold(strategy)
    high_risk = [item for item in candidates if number(item.get("change_percent")) >= threshold]
    moderate = [item for item in candidates if 0 <= number(item.get("change_percent")) < threshold]
    by_symbol = {str(item.get("symbol")): item for item in candidates}
    selected = [by_symbol[symbol] for symbol in payload.get("portfolio_candidates") or [] if symbol in by_symbol]
    if not selected:
        selected = (high_risk[:3] + moderate[:2])[:5]
    sentiment = market_sentiment_profile(candidates, strategy=strategy)
    market = payload.get("market") or strategy_market(strategy)
    profile = market_profile(market)
    lines = [
        "⚠️ **系统风控修正**",
        "",
        f"{reason}，已按系统风控规则覆盖。",
        f"今日情绪：{sentiment['label']}（{sentiment['summary']}）。",
        f"总仓位框架：{sentiment['position']}；计划买入拆分：{sentiment['entries']}。",
        "",
        "## 候选与理由",
    ]
    if not selected:
        lines.extend(["暂无可解释候选。", "", "仅供参考，不构成投资建议。"])
        return "\n".join(lines)
    for item in selected:
        item.setdefault("board_name", payload.get("board_name") or DEFAULT_BOARD_NAME)
        append_candidate_explanation(
            lines,
            item,
            sentiment,
            strategy=strategy,
            market=market,
        )
    lines.extend(
        [
            "## 最终建议",
            f"- 今日不建议追高，涨幅超过 {threshold:g}% 的候选只允许观望或极轻仓试错。",
            (
                "- 真正买点需等待 10 秒量价点火、均价线/开盘价/0轴上方；盘口判断仍需稳定盘口源确认。"
                if profile.supports_tick_ignition
                else f"- {profile.label}逐笔点火与盘口能力尚未接入；仅使用已声明可用的行情和日线信号。"
            ),
            "- 仅供参考，不构成投资建议。",
        ]
    )
    return "\n".join(lines)
