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


# ── 能力快照自查（词元 GET /v1/ciyuan/capabilities）────────────────────────
#
# 只读、只回答一个问题：「我配置的默认模型，此刻在这把 key 的 serving 里吗？」
#
# 为什么需要：/v1/models 列的是「认得」的 id，不等于「能服务」；而本模块的
# LLM_DEFAULT_MODEL（config.py）是硬编码的。词元侧 model id 会双向漂移
# （带前缀 ↔ 裸名），漂移发生时真实调用拿 404/400，但代码里看不出任何异常。
# 这个自查把那种静默失败变成一句可读的诊断，交给部署时或排障时手动跑。
#
# 纪律：本组函数**绝不改变**调用行为，也绝不抛——快照不可用就返回 checked=False，
# 调用方照旧用配置的默认模型。环境变量名只走 config 访问器，不在本模块重复。
CAPABILITIES_PATH = "/ciyuan/capabilities"
CAPABILITIES_SCHEMA = "ciyuan.capabilities/v1"
CAPABILITIES_TIMEOUT = 3.0


def _effective_base_url() -> str:
    return config.llm_base_url().rstrip("/")


def _effective_model() -> str:
    return config.llm_model()


def capabilities(*, timeout: float = CAPABILITIES_TIMEOUT) -> dict[str, Any] | None:
    """读一次 key-scoped 能力快照。任何失败（无 key / 无 httpx / 网络 / 非 2xx /
    schema 不符 / 形状不符）都返回 None，绝不抛、绝不记 key。"""
    key = config.llm_api_key()
    if not key:
        return None
    try:
        import httpx  # 延迟导入：与 chat() 同策略，缺 httpx 只让自查不可用
    except Exception:  # noqa: BLE001
        return None
    try:
        resp = httpx.get(
            f"{_effective_base_url()}{CAPABILITIES_PATH}",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        doc = resp.json()
        if not isinstance(doc, dict) or doc.get("schema") != CAPABILITIES_SCHEMA:
            return None
        models = doc.get("models")
        if not isinstance(models, dict):
            return None
        if not isinstance(models.get("serving"), list):
            return None
        if not isinstance(models.get("permitted_only"), list):
            return None
        return doc
    except Exception:  # noqa: BLE001 —— 自查绝不影响主路径
        return None


def _ids(entries: list[Any]) -> set[str]:
    out: set[str] = set()
    for item in entries:
        mid = item.get("id") if isinstance(item, dict) else item
        if isinstance(mid, str) and mid.strip():
            out.add(mid.strip().lower())
    return out


def model_verdict(
    model: str | None = None, *, timeout: float = CAPABILITIES_TIMEOUT
) -> dict[str, Any]:
    """判断默认模型此刻是否可服务。只读，不改变任何调用行为。

    返回：
      checked            是否真的问到了词元（False ⇒ 其余判断不可用）
      model              被判定的 id
      in_serving         在 serving 里（True/False/None=未知）
      in_permitted_only  在 permitted_only 里（「白名单有、账号没有」的典型症状）
      serving_count      serving 条目数（量级参考）
      suggestions        serving 里前几个 id（排障时看形状差异）
      note               一句话结论
    """
    target = (model or _effective_model()).strip()
    doc = capabilities(timeout=timeout)
    if doc is None:
        return {
            "checked": False,
            "model": target,
            "in_serving": None,
            "in_permitted_only": None,
            "serving_count": None,
            "suggestions": [],
            "note": "能力快照不可用（未配 key / 端点缺失 / 超时 / 形状不符）——调用行为不受影响，仍用配置的默认模型",
        }

    models = doc["models"]
    serving = _ids(models["serving"])
    permitted = _ids(models.get("permitted_only") or [])
    lowered = target.lower()

    in_serving = lowered in serving
    in_permitted = lowered in permitted
    suggestions = sorted(serving)[:5]

    if in_serving:
        note = "默认模型在 serving 里，可正常服务"
    elif in_permitted:
        note = (
            "默认模型只在 permitted_only 里：白名单放行了，但当前没有可调度账号能服务它"
            "——真实调用会失败，应改配置或补账号"
        )
    else:
        note = "默认模型既不在 serving 也不在 permitted_only：这把 key 当前看不到它（多半是 id 形状漂移或权益变更）"

    return {
        "checked": True,
        "model": target,
        "in_serving": in_serving,
        "in_permitted_only": in_permitted,
        "serving_count": len(serving),
        "suggestions": suggestions,
        "note": note,
    }


def main(argv: list[str] | None = None) -> int:
    """运维自查入口：python -m fd_zhengxin_api.llm --check-model"""
    import json
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if "--check-model" not in args:
        print("用法: python -m fd_zhengxin_api.llm --check-model", file=sys.stderr)
        return 2
    print(json.dumps(model_verdict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
