from __future__ import annotations

import json
import os
import re
from typing import Callable

from .config import DEFAULT_LLM_TIMEOUT_SECONDS
from .llm import call_chat_completion
from .parameters import PARAMETER_CATALOG, convert_strategy_text


VALID_ROLES = {"user", "assistant"}
CONFIRM_PATTERN = re.compile(r"(?:确认(?:生成|应用|创建)?|确定(?:生成|应用)?|生成策略|按此生成|就这样)")
NEGATED_CONFIRM_PATTERN = re.compile(r"(?:不|别|不要|暂不|先不).{0,6}(?:确认|确定|生成)")


def _is_explicit_confirmation(text: str) -> bool:
    return bool(CONFIRM_PATTERN.search(text)) and not bool(NEGATED_CONFIRM_PATTERN.search(text))


def _normalize_messages(messages: object) -> list[dict[str, str]]:
    if not isinstance(messages, list):
        raise ValueError("messages 必须是数组")
    normalized = []
    for item in messages[-30:]:
        if not isinstance(item, dict) or item.get("role") not in VALID_ROLES:
            raise ValueError("消息角色必须是 user 或 assistant")
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        normalized.append({"role": item["role"], "content": content[:4000]})
    if not normalized or normalized[-1]["role"] != "user":
        raise ValueError("最后一条消息必须来自用户")
    return normalized


def _catalog_summary() -> str:
    return "\n".join(
        f"- {item['id']} | {item['label']} | {item['operator']} | {item['kind']} | {item['status']}"
        for item in PARAMETER_CATALOG
    )


def _system_prompt(strategy_name: str) -> str:
    return f"""你是A股选股策略配置助手，当前策略名为“{strategy_name or '未命名策略'}”。你的职责是通过对话把用户的投资想法澄清成筛选条件，不提供个股推荐，也不承诺收益。

工作规则：
1. 根据上下文主动追问缺失或含糊的信息，每次最多问两个最关键的问题。优先澄清持有周期、股票范围、风险偏好、流动性/市值、技术面或基本面取向。
2. 信息足够时先给出简洁完整的策略摘要，并明确询问“确认生成策略吗？”，此时 status=review。
3. 只有用户在最后一条消息明确确认后才能返回 status=confirmed。确认前绝不能生成参数草案。
4. confirmed 时，strategy_text 必须是适合规则解析的完整中文条件，包含所有已确认的数字、单位和条件词，例如“不低于”“不超过”“20到100亿”。
5. 只输出一个 JSON 对象，不要 Markdown，不要附加文字：
{{"status":"question|review|confirmed","message":"给用户的回复","summary":"当前策略摘要","strategy_text":"确认后用于解析的完整策略文本"}}

可配置参数目录：
{_catalog_summary()}"""


def _parse_model_response(content: str) -> dict:
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("模型响应不是 JSON 对象")
    status = payload.get("status")
    if status not in {"question", "review", "confirmed"}:
        raise ValueError("模型响应状态无效")
    return {
        "status": status,
        "message": str(payload.get("message") or "").strip()[:4000],
        "summary": str(payload.get("summary") or "").strip()[:4000],
        "strategy_text": str(payload.get("strategy_text") or "").strip()[:8000],
    }


def _user_strategy_text(messages: list[dict[str, str]]) -> str:
    parts = [item["content"] for item in messages if item["role"] == "user" and not CONFIRM_PATTERN.fullmatch(item["content"])]
    return "；".join(parts)


def _fallback_response(messages: list[dict[str, str]]) -> dict:
    user_text = _user_strategy_text(messages)
    last_text = messages[-1]["content"]
    confirmed = _is_explicit_confirmation(last_text)
    dimensions = [
        (r"短线|中线|长线|日内|持有|周期", "你的计划持有周期是短线、中线还是长线？"),
        (r"A股|沪深|创业板|科创板|北交所|板块|行业|概念", "股票范围怎么限定，例如全 A 股、某个市场、行业或概念板块？"),
        (r"风险|追高|涨幅|波动|回撤|涨停", "你的风险偏好如何？请给出可接受的追高或波动阈值。"),
        (r"市值|换手|成交额|量比|流动性", "对市值和流动性有什么要求？可以说明市值区间、成交额或换手率。"),
        (r"技术|均线|MACD|RSI|突破|估值|PE|PB|成长|ROE|分红|基本面", "更看重技术面、估值、成长、质量还是分红？请补充关键条件。"),
    ]
    if not confirmed:
        for pattern, question in dimensions:
            if not re.search(pattern, user_text, flags=re.I):
                return {"status": "question", "message": question, "summary": user_text, "strategy_text": "", "draft": None, "provider": "fallback"}
        return {
            "status": "review",
            "message": f"我已整理为：{user_text}\n\n确认生成策略吗？",
            "summary": user_text,
            "strategy_text": user_text,
            "draft": None,
            "provider": "fallback",
        }
    draft = convert_strategy_text(user_text)
    return {
        "status": "confirmed",
        "message": draft["message"],
        "summary": user_text,
        "strategy_text": user_text,
        "draft": draft,
        "provider": "fallback",
    }


def chat_strategy(
    messages: object,
    *,
    strategy_name: str = "",
    llm_func: Callable[..., str] | None = None,
) -> dict:
    normalized = _normalize_messages(messages)
    base_url = os.getenv("STOCK_AGENT_LLM_BASE_URL", "").strip()
    if not base_url and llm_func is None:
        return _fallback_response(normalized)

    caller = llm_func or call_chat_completion
    try:
        raw = caller(
            [{"role": "system", "content": _system_prompt(strategy_name)}, *normalized],
            base_url=base_url,
            model=os.getenv("STOCK_AGENT_LLM_MODEL", "codex-worker"),
            api_key=os.getenv("STOCK_AGENT_LLM_API_KEY", "ollama"),
            timeout=int(os.getenv("STOCK_AGENT_LLM_TIMEOUT", str(DEFAULT_LLM_TIMEOUT_SECONDS))),
        )
        response = _parse_model_response(raw)
    except Exception:
        return _fallback_response(normalized)

    explicitly_confirmed = _is_explicit_confirmation(normalized[-1]["content"])
    if response["status"] == "confirmed" and not explicitly_confirmed:
        response["status"] = "review"
        response["message"] = (response["message"] or response["summary"]) + "\n\n确认生成策略吗？"

    response["draft"] = None
    response["provider"] = "ai"
    if response["status"] == "confirmed":
        user_text = _user_strategy_text(normalized)
        model_text = response["strategy_text"]
        strategy_text = "；".join(part for part in [model_text, user_text] if part)
        response["strategy_text"] = strategy_text
        response["draft"] = convert_strategy_text(strategy_text)
        response["message"] = response["draft"]["message"]
    return response
