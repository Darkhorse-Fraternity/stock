from __future__ import annotations

import re
from datetime import datetime
from typing import Callable, Iterable

from .config import DEFAULT_BOARD_CODE, DEFAULT_BOARD_NAME, DEFAULT_LLM_TIMEOUT_SECONDS
from .context import collect_analyzed_candidates, extract_market_payload, generate_agent_context
from .llm import call_llm_analysis
from .universe import normalize_sector_filters, normalize_watchlist
from .utils import number


def format_volume_hands(value: float) -> str:
    volume = number(value)
    if volume >= 100_000_000:
        return f"{volume / 100_000_000:.2f} 亿手"
    if volume >= 10_000:
        return f"{volume / 10_000:.2f} 万手"
    return f"{volume:.0f} 手"


def format_recommendation_snapshot(rows: Iterable[dict], *, limit: int = 3, generated_at: str = "") -> str:
    selected = list(rows)[: max(0, limit)]
    if not selected:
        return ""
    lines = ["📈 **推荐股每小时成交与涨跌跟踪**"]
    if generated_at:
        lines.append(f"更新时间：{generated_at}")
    for item in selected:
        percent = number(item.get("change_percent", item.get("percent")))
        price = number(item.get("price"))
        volume = number(item.get("volume_hands", item.get("volume")))
        turnover = number(item.get("turnover_cny", item.get("turnover")))
        lines.append(
            f"- {item.get('name') or item.get('symbol')} ({item.get('symbol')})："
            f"最新价 ¥{price:.2f}，涨跌幅 {percent:+.2f}%；"
            f"成交量 {format_volume_hands(volume)}，成交额 {format_cny(turnover)}"
        )
    return "\n".join(lines)


def select_snapshot_candidates(analysis: str, candidates: list[dict], limit: int = 3) -> list[dict]:
    by_symbol = {str(item.get("symbol")): item for item in candidates}
    selected: list[dict] = []
    for match in re.finditer(r"\b\d{6}\b", analysis):
        item = by_symbol.get(match.group(0))
        if item is not None and item not in selected:
            selected.append(item)
            if len(selected) >= limit:
                return selected
    return selected


def generate_report(
    *,
    now: datetime | None = None,
    board_code: str = DEFAULT_BOARD_CODE,
    board_name: str = DEFAULT_BOARD_NAME,
    top_n: int = 3,
    board_fetcher: Callable | None = None,
    fallback_fetcher: Callable | None = None,
    watchlist: str | Iterable[object] | None = None,
    sector_filters: str | Iterable[object] | None = None,
    watchlist_fetcher: Callable | None = None,
) -> str:
    watchlist_entries = normalize_watchlist(watchlist)
    sectors = normalize_sector_filters(sector_filters)
    report_time, analyses, error = collect_analyzed_candidates(
        now=now,
        board_code=board_code,
        board_name=board_name,
        board_fetcher=board_fetcher,
        fallback_fetcher=fallback_fetcher,
        watchlist=watchlist_entries,
        sector_filters=sectors,
        watchlist_fetcher=watchlist_fetcher,
    )
    top = analyses[:top_n]
    universe_label = "自选股池" if watchlist_entries else f"{board_name}板块"
    filter_label = f"（板块：{'、'.join(sectors)}）" if sectors else ""
    if not top:
        return "\n".join(
            [
                f"📊 **股票每日推荐** ({report_time.strftime('%Y 年%m月%d日')})",
                "=" * 30,
                "",
                f"🎯 **{universe_label}{filter_label}**",
                "",
                "暂无符合条件的候选股票。",
                f"原因：{error or '候选数量为 0'}",
                "",
                "仅供参考，不构成投资建议。",
            ]
        )

    avg_score = sum(item["score"] for item in top) / len(top)
    sources = sorted({item.get("source") for item in top if item.get("source")})

    report = [
        f"📊 **股票每日推荐** ({report_time.strftime('%Y 年%m月%d日')})",
        "=" * 30,
        "",
        f"🎯 **{universe_label}{filter_label}精选 {len(top)} 只值得关注的股票**",
        "",
    ]

    for index, stock in enumerate(top, 1):
        sector_line = f"  🏷️ **板块**: {stock.get('sector')}" if stock.get("sector") else None
        report.extend(
            [
                f"{stock['rating_emoji']} **【推荐 #{index}】{stock['name']} ({stock['symbol']})**",
                f"  🎯 **评级**: {stock['rating']} (评分：{stock['score']}/100)",
                *([sector_line] if sector_line else []),
                f"  💰 **最新价**: ¥{number(stock.get('price')):.2f}",
                f"  📊 **涨跌幅**: {number(stock.get('percent')):.2f}% ({number(stock.get('change')):+.2f})",
                f"  🔄 **换手率**: {number(stock.get('turnover_rate')):.2f}%",
                f"  💵 **成交额**: {number(stock.get('turnover')) / 100_000_000:.1f} 亿",
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
        limit=top_n,
        generated_at=report_time.strftime("%m月%d日 %H:%M"),
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
            f"  • 数据来源：{'；'.join(sources) or '无'}",
            "  • 任务频率：北京时间工作日交易时段每小时播报一次",
            "",
            "=" * 30,
        ]
    )
    return "\n".join(report)


def generate_ai_report(
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
    llm_client: Callable | None = None,
    llm_base_url: str = "",
    llm_model: str = "Flux_AI/Flux_AI:latest",
    llm_api_key: str = "ollama",
    llm_timeout: int = DEFAULT_LLM_TIMEOUT_SECONDS,
) -> str:
    context = generate_agent_context(
        now=now,
        board_code=board_code,
        board_name=board_name,
        candidate_limit=candidate_limit,
        board_fetcher=board_fetcher,
        fallback_fetcher=fallback_fetcher,
        watchlist=watchlist,
        sector_filters=sector_filters,
        watchlist_fetcher=watchlist_fetcher,
        enable_tick=enable_tick,
        tick_fetcher=tick_fetcher,
        tick_limit=tick_limit,
    )
    payload = extract_market_payload(context) or {}
    if (
        payload.get("fetch_error")
        or not payload.get("candidates")
        or any("估算兜底" in source for source in payload.get("source", []))
    ):
        return "\n".join(
            [
                "⚠️ **实时行情不可用**",
                "",
                f"数据源错误：{payload.get('fetch_error') or 'unknown'}",
                "今日不生成股票推荐，避免基于兜底数据误导交易判断。",
                "",
                "仅供参考，不构成投资建议。",
            ]
        )
    client = llm_client or call_llm_analysis
    try:
        analysis = client(
            context,
            base_url=llm_base_url,
            model=llm_model,
            api_key=llm_api_key,
            timeout=llm_timeout,
        )
        guarded = apply_risk_guard(context, analysis)
        snapshot_rows = select_snapshot_candidates(guarded, payload.get("candidates") or [], tracking_limit)
        snapshot = format_recommendation_snapshot(
            snapshot_rows,
            limit=tracking_limit,
            generated_at=payload.get("generated_at") or "",
        )
        return f"{guarded}\n\n{snapshot}" if snapshot else guarded
    except Exception as exc:
        fallback = generate_report(
            now=now,
            board_code=board_code,
            board_name=board_name,
            top_n=min(3, candidate_limit),
            board_fetcher=board_fetcher,
            fallback_fetcher=fallback_fetcher,
            watchlist=watchlist,
            sector_filters=sector_filters,
            watchlist_fetcher=watchlist_fetcher,
        )
        return f"⚠️ AI 分析失败：{exc}\n\n以下为脚本兜底报告：\n\n{fallback}"


def apply_risk_guard(context: str, analysis: str) -> str:
    payload = extract_market_payload(context)
    if not payload:
        return analysis
    candidates = payload.get("candidates") or []
    known_symbols = {str(item.get("symbol")) for item in candidates}
    mentioned_symbols = set(re.findall(r"\b\d{6}\b", analysis))
    unknown_symbols = mentioned_symbols - known_symbols
    if unknown_symbols:
        return build_conservative_report(payload, "数据一致性校验失败：AI 输出包含非候选股票代码")

    has_high_risk = any(number(item.get("change_percent")) >= 7 for item in candidates)
    unsafe_position = any(term in analysis for term in ["重仓", "中仓", "满仓", "30-40", "30%", "40%"])
    if not has_high_risk or not unsafe_position:
        return analysis

    return build_conservative_report(payload, "AI 原始输出包含对高涨幅股票的过高仓位建议")


def format_cny(value: float) -> str:
    amount = number(value)
    if amount >= 100_000_000:
        return f"{amount / 100_000_000:.1f} 亿"
    if amount >= 10_000:
        return f"{amount / 10_000:.0f} 万"
    return f"{amount:.0f}"


def market_sentiment_profile(candidates: list[dict]) -> dict:
    if not candidates:
        return {"label": "弱", "position": "2成", "entries": "3次", "summary": "候选不足，按弱情绪处理"}
    avg_score = sum(number(item.get("signal_score")) for item in candidates) / len(candidates)
    strong_count = sum(1 for item in candidates if number(item.get("change_percent")) >= 7)
    if avg_score >= 75 or strong_count >= 2:
        return {"label": "强", "position": "8成", "entries": "10次", "summary": f"平均信号分 {avg_score:.0f}，强势候选 {strong_count} 只"}
    if avg_score >= 65:
        return {"label": "中", "position": "5成", "entries": "5次", "summary": f"平均信号分 {avg_score:.0f}，题材有热度但未全面爆发"}
    return {"label": "弱", "position": "2成", "entries": "3次", "summary": f"平均信号分 {avg_score:.0f}，信号偏弱"}


def candidate_action(item: dict, sentiment: dict) -> dict:
    pct = number(item.get("change_percent"))
    turnover_rate = number(item.get("turnover_rate"))
    if pct >= 7:
        return {
            "label": "观望或极轻仓试错",
            "position": "不超过1成",
            "entries": "最多1次",
            "reason": "涨幅已经超过 7% 追高阈值，按系统风控规则降级",
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


def append_candidate_explanation(lines: list[str], item: dict, sentiment: dict) -> None:
    action = candidate_action(item, sentiment)
    pct = number(item.get("change_percent"))
    turnover_rate = number(item.get("turnover_rate"))
    turnover = number(item.get("turnover_cny"))
    pe = number(item.get("pe"))
    score = number(item.get("signal_score"))
    float_market_cap = number(item.get("float_market_cap_cny"))
    position_summary = price_position_summary(item.get("price_position"))
    ignition_summary = ignition_signal_summary(item.get("ignition_signal"))
    source = item.get("source") or "结构化行情"
    sector = item.get("sector") or item.get("board_name") or "未分类"
    reasons = item.get("machine_reasons") or []
    lines.extend(
        [
            f"### {item['name']} ({item['symbol']})",
            "**入选理由**",
            f"- 来自今日 {sector} 候选股池，实时数据源为{source}。",
            f"- 涨幅 {pct:.2f}%，换手率 {turnover_rate:.2f}%，成交额 {format_cny(turnover)}，机器信号分 {score:.0f}/100。",
            f"- 流通市值 {format_cny(float_market_cap)}，用于执行 20-100 亿中小盘筛选。",
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
            "- 当前版本已尝试接入 10 秒逐笔成交；集合竞价最后一分钟成交量、五档盘口吃单数据仍未稳定接入，不在本报告中伪造判断。",
            "**操作建议**",
            f"- 建议：{action['label']}。",
            f"- 单股仓位：{action['position']}；买入拆分：{action['entries']}。",
            "",
        ]
    )


def build_conservative_report(payload: dict, reason: str) -> str:
    candidates = payload.get("candidates") or []
    high_risk = [item for item in candidates if number(item.get("change_percent")) >= 7]
    moderate = [item for item in candidates if 0 <= number(item.get("change_percent")) < 7]
    selected = (high_risk[:3] + moderate[:2])[:5]
    sentiment = market_sentiment_profile(candidates)
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
        item.setdefault("board_name", payload.get("board_name") or "AI智能体")
        append_candidate_explanation(lines, item, sentiment)
    lines.extend(
        [
            "## 最终建议",
            "- 今日不建议追高，涨幅超过 7% 的候选只允许观望或极轻仓试错。",
            "- 真正买点必须等待 10 秒量价点火、均价线/开盘价/0轴上方；盘口大单被主动吃掉后卖盘变稀疏仍需稳定盘口源确认。",
            "- 仅供参考，不构成投资建议。",
        ]
    )
    return "\n".join(lines)
