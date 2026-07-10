from __future__ import annotations

import json
import urllib.request
from typing import Callable

from .config import DEFAULT_LLM_TIMEOUT_SECONDS


def call_chat_completion(
    messages: list[dict[str, str]],
    *,
    base_url: str,
    model: str,
    api_key: str = "ollama",
    timeout: int = DEFAULT_LLM_TIMEOUT_SECONDS,
    urlopen_func: Callable | None = None,
) -> str:
    if not base_url:
        raise ValueError("STOCK_AGENT_LLM_BASE_URL is required for ai mode")
    endpoint = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 1200,
        "stream": False,
        "messages": messages,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    opener = urlopen_func or urllib.request.urlopen
    with opener(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


def call_llm_analysis(
    context: str,
    *,
    base_url: str,
    model: str,
    api_key: str = "ollama",
    timeout: int = DEFAULT_LLM_TIMEOUT_SECONDS,
    urlopen_func: Callable | None = None,
) -> str:
    return call_chat_completion(
        [
            {
                "role": "system",
                "content": "你是谨慎的A股短线分析agent。只基于用户提供的结构化行情数据分析，不联网，不编造数据。",
            },
            {"role": "user", "content": context},
        ],
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout=timeout,
        urlopen_func=urlopen_func,
    )
