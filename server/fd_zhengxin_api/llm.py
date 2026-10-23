"""LLM 网关客户端（骨架期，照 fd-zhesi_api/llm.py 模式）。

OpenAI 兼容网关（默认 token.finddatatech.cloud，配置见 config.py）：
- 不设 FDZX__LLM_API_KEY → chat() 返回 None，调用方走确定性回退；
- 设了 KEY → POST {base}/chat/completions，超时 20s；
- 任何失败（网络/非 2xx/坏负载）抛 LLMError，由调用方捕获降级。

铁律：不自研重试堆栈，失败就降级。
"""

from __future__ import annotations

from typing import Any

from . import config


class LLMError(RuntimeError):
    """网关调用失败（网络/HTTP/负载解析）。调用方捕获后降级，不重试。"""


def is_configured() -> bool:
    return bool(config.llm_api_key())


def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 900,
    timeout: float = 20.0,
) -> str | None:
    """单次补全调用。无 KEY → None；失败 → LLMError（不重试）。"""
    key = config.llm_api_key()
    if not key:
        return None
    import httpx  # 延迟导入：环境缺 httpx 时仅 LLM 增强不可用，确定性层不受影响

    base = config.llm_base_url().rstrip("/")
    payload: dict[str, Any] = {
        "model": model or config.llm_model(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        resp = httpx.post(
            f"{base}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty content")
        return content.strip()
    except Exception as exc:  # noqa: BLE001 —— 单次失败即抛，绝不重试
        raise LLMError(f"LLM 网关调用失败: {type(exc).__name__}: {exc}") from exc


def chat_with_usage(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 1024,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """chat() 的台账形态：补全文本 + 网关回报的 usage（无则 None）。

    台账（llm_ledger）用 usage 记实测 token（usage_method="usage"）；网关不回报
    时返回 usage=None，由台账退回估算口径。失败口径与 chat() 一致。
    """
    import httpx  # 延迟导入：与 chat() 同习惯

    key = config.llm_api_key()
    if not key:
        return {"content": None, "usage": None}
    base = config.llm_base_url().rstrip("/")
    payload: dict[str, Any] = {
        "model": model or config.llm_model(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        resp = httpx.post(
            f"{base}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty content")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        return {"content": content.strip(), "usage": usage}
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"LLM 网关调用失败: {type(exc).__name__}: {exc}") from exc
