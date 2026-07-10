from __future__ import annotations

import re
from datetime import datetime
from typing import Callable

from .config import DEFAULT_BOARD_CODE, DEFAULT_BOARD_NAME, DEFAULT_LLM_TIMEOUT_SECONDS
from .context import extract_market_payload, generate_agent_context, prepare_candidates
from .data_sources import fallback_quotes, fetch_board_quotes, fetch_sina_fallback_quotes
from .llm import call_llm_analysis
from .parameters import chase_risk_threshold, load_strategy_config
from .selection import analyze, filter_candidates
from .utils import beijing_now, number


def generate_report(
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
) -> str:
    report_time = beijing_now(now)
    fetcher = board_fetcher or fetch_board_quotes
    try:
        rows, error = fetcher(board_code, board_name=board_name)
    except Exception as exc:
        rows, error = [], str(exc)

    strategy = strategy or load_strategy_config()
    filtered, enrichment_error = prepare_candidates(
        rows,
        strategy=strategy,
        history_fetcher=history_fetcher,
        financial_fetcher=financial_fetcher,
        enrich_limit=enrich_limit,
    )
    if enrichment_error:
        error = enrichment_error
    if not rows:
        fallback = fallback_fetcher or fetch_sina_fallback_quotes
        try:
            fallback_rows, fallback_error = fallback(board_name=board_name)
        except Exception as exc:
            fallback_rows, fallback_error = [], str(exc)
        filtered, enrichment_error = prepare_candidates(
            fallback_rows,
            strategy=strategy,
            history_fetcher=history_fetcher,
            financial_fetcher=financial_fetcher,
            enrich_limit=enrich_limit,
        )
        if fallback_rows and not enrichment_error:
            error = None
        elif not fallback_rows:
            filtered = fallback_quotes(report_time, error or fallback_error or "未获得有效行情")
        elif enrichment_error:
            error = enrichment_error

    if not filtered:
        return "\n".join(
            [
                f"📊 **股票每日推荐** ({report_time.strftime('%Y 年%m月%d日')})",
                "",
                "当前策略没有匹配股票。",
                f"数据状态：{error or '行情正常，筛选条件未命中'}",
                "",
                "仅供参考，不构成投资建议。",
            ]
        )

    analyses = [analyze(row, strategy=strategy) for row in filtered]
    analyses.sort(key=lambda item: (item["score"], number(item.get("percent"))), reverse=True)
    top = analyses[:top_n]
    avg_score = sum(item["score"] for item in top) / len(top)
    sources = sorted({item["source"] for item in top})

    report = [
        f"📊 **股票每日推荐** ({report_time.strftime('%Y 年%m月%d日')})",
        "=" * 30,
        "",
        f"🎯 **{board_name}板块精选 {len(top)} 只值得关注的股票**",
        "",
    ]

    for index, stock in enumerate(top, 1):
        report.extend(
            [
                f"{stock['rating_emoji']} **【推荐 #{index}】{stock['name']} ({stock['symbol']})**",
                f"  🎯 **评级**: {stock['rating']} (评分：{stock['score']}/100)",
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
            "  • 任务频率：北京时间每个自然日 09:00 播报一次",
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
    board_fetcher: Callable | None = None,
    fallback_fetcher: Callable | None = None,
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
) -> str:
    context = generate_agent_context(
        now=now,
        board_code=board_code,
        board_name=board_name,
        candidate_limit=candidate_limit,
        board_fetcher=board_fetcher,
        fallback_fetcher=fallback_fetcher,
        enable_tick=enable_tick,
        tick_fetcher=tick_fetcher,
        tick_limit=tick_limit,
        history_fetcher=history_fetcher,
        financial_fetcher=financial_fetcher,
        enrich_limit=enrich_limit,
        strategy=strategy,
    )
    payload = extract_market_payload(context) or {}
    if payload.get("fetch_error") or any("估算兜底" in source for source in payload.get("source", [])):
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
    if not payload.get("candidates"):
        return "⚪ **当前策略无匹配股票**\n\n行情数据正常，但没有股票同时满足已启用条件。\n\n仅供参考，不构成投资建议。"
    client = llm_client or call_llm_analysis
    try:
        analysis = client(
            context,
            base_url=llm_base_url,
            model=llm_model,
            api_key=llm_api_key,
            timeout=llm_timeout,
        )
        guarded = apply_risk_guard(context, analysis, strategy=strategy)
        return guarded
    except Exception as exc:
        fallback = generate_report(
            now=now,
            board_code=board_code,
            board_name=board_name,
            top_n=min(3, candidate_limit),
            board_fetcher=board_fetcher,
            fallback_fetcher=fallback_fetcher,
            history_fetcher=history_fetcher,
            financial_fetcher=financial_fetcher,
            enrich_limit=enrich_limit,
            strategy=strategy,
        )
        return f"⚠️ AI 分析失败：{exc}\n\n以下为脚本兜底报告：\n\n{fallback}"


def apply_risk_guard(context: str, analysis: str, *, strategy: dict | None = None) -> str:
    payload = extract_market_payload(context)
    if not payload:
        return analysis
    candidates = payload.get("candidates") or []
    known_symbols = {str(item.get("symbol")) for item in candidates}
    mentioned_symbols = set(re.findall(r"\b\d{6}\b", analysis))
    unknown_symbols = mentioned_symbols - known_symbols
    if unknown_symbols:
        return build_conservative_report(payload, "数据一致性校验失败：AI 输出包含非候选股票代码", strategy=strategy)

    has_high_risk = any(number(item.get("change_percent")) >= chase_risk_threshold(strategy) for item in candidates)
    unsafe_position = any(term in analysis for term in ["重仓", "中仓", "满仓", "30-40", "30%", "40%"])
    if not has_high_risk or not unsafe_position:
        return analysis

    return build_conservative_report(payload, "AI 原始输出包含对高涨幅股票的过高仓位建议", strategy=strategy)


def format_cny(value: float) -> str:
    amount = number(value)
    if amount >= 100_000_000:
        return f"{amount / 100_000_000:.1f} 亿"
    if amount >= 10_000:
        return f"{amount / 10_000:.0f} 万"
    return f"{amount:.0f}"


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


def append_candidate_explanation(lines: list[str], item: dict, sentiment: dict, *, strategy: dict | None = None) -> None:
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
    lines.extend(
        [
            f"### {item['name']} ({item['symbol']})",
            "**入选理由**",
            f"- 来自今日 {item.get('board_name', 'AI智能体')} 热点股池，实时数据源为{source}。",
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


def build_conservative_report(payload: dict, reason: str, *, strategy: dict | None = None) -> str:
    candidates = payload.get("candidates") or []
    threshold = chase_risk_threshold(strategy)
    high_risk = [item for item in candidates if number(item.get("change_percent")) >= threshold]
    moderate = [item for item in candidates if 0 <= number(item.get("change_percent")) < threshold]
    selected = (high_risk[:3] + moderate[:2])[:5]
    sentiment = market_sentiment_profile(candidates, strategy=strategy)
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
        append_candidate_explanation(lines, item, sentiment, strategy=strategy)
    lines.extend(
        [
            "## 最终建议",
            f"- 今日不建议追高，涨幅超过 {threshold:g}% 的候选只允许观望或极轻仓试错。",
            "- 真正买点必须等待 10 秒量价点火、均价线/开盘价/0轴上方；盘口大单被主动吃掉后卖盘变稀疏仍需稳定盘口源确认。",
            "- 仅供参考，不构成投资建议。",
        ]
    )
    return "\n".join(lines)
