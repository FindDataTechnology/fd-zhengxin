"""参照系 MCP 客户端（任务 4.2/4.3/4.4 三闭环的参照系消费面，与 LLM KEY 无关）。

命名说明：reference_client.py 已被个人层消费面（任务 5.2/5.4，regions/anchor
的 GET JSON 形态）占用，本模块承载三闭环的 MCP 四工具契约，名字分开、互不
覆盖——两块消费面契约不同（GET JSON vs MCP tools/call），由主代理在接线期
决定是否收敛。

契约（参照系 = fd-zhengxin-reference 仓的独立部署，本仓只消费不实现）：
- 地址 FDZX__REFERENCE_BASE_URL（MCP streamable-http 端点，如 http://…/mcp）；
- 四工具：concept_lookup(term) 概念（定义摘要+来源+SEP 链接）/
  ideology_query(qid) 思潮坐标锚（P1142/P1387/MARPOR）/
  relation_query(qid, polarity) 关系边（极性 + PID 依据）/
  regime_timeseries(country, years) 政体时序；外加 source_status() 台账。

消费纪律照 docs/info-stream-probe.md 的实跑结论：
- 空结果必带原因：参照系契约（errors taxonomy：blocked/upstream/unreachable/
  not_ingested）保证空结果带 reason，本客户端把「传输失败」与「工具级空结果」
  都归一为 RefResult(ok=False, reason=...)，绝不静默为空；
- 超时预算：tools/call 默认 8s、source_status 4s、连接级 3s（参照系是内网
  服务，预算比 fd-info 公网口径略紧）；
- 不自研重试堆栈（照 llm.py 铁律）：失败即降级，由调用方带原因返回；
- FDZX__REFERENCE_BASE_URL 未配置是本地前置缺口（reason=not_configured），
  不冒充远端故障；
- 无需 health-first 预检：参照系每个工具空结果自带 reason，与 fd-info 的
  静默空列表不同（probe §5 差异点）；status() 仅供诊断与展示。

传输形态：POST JSON-RPC 2.0 tools/call，Accept 同时给 json 与 event-stream，
两种响应体（application/json 与 SSE data: 行）都能解。真实服务未部署期，
调用一律可用注入的 transport 桩模拟（测试即此形态）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from . import config

# ---- 失败口径（与 fd-zhengxin-reference errors.py taxonomy 对齐） ----

BLOCKED = "blocked"            # 出站被许可红线拦截（不可绕过）
UPSTREAM = "upstream"          # 源可达但响应异常（非 2xx/坏 JSON-RPC/坏负载）
UNREACHABLE = "unreachable"    # 源不可达（DNS/连接失败/超时）
NOT_INGESTED = "not_ingested"  # 源未过许可门或数据尚未摄取
NOT_CONFIGURED = "not_configured"  # 本地前置缺口：未配 FDZX__REFERENCE_BASE_URL

TAXONOMY = frozenset({BLOCKED, UPSTREAM, UNREACHABLE, NOT_INGESTED})

DEFAULT_TIMEOUT = 8.0   # 单次 tools/call 预算
STATUS_TIMEOUT = 4.0    # source_status 预算（台账查询更轻）
CONNECT_TIMEOUT = 3.0   # 连接级预算（probe §5 建议口径）


@dataclass(frozen=True)
class RefResult:
    """参照系调用结果：ok=False 时必带 reason（结构化降级原因）。"""

    ok: bool
    reason: str = ""
    detail: str = ""
    data: dict | None = None   # ok=True 时为远端 payload（含 results/provenance）

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "detail": self.detail,
            "data": self.data,
        }


class RefUnreachable(RuntimeError):
    """网络层失败（DNS/连接/超时）→ reason=unreachable。"""


class RefUpstream(RuntimeError):
    """可达但响应异常（非 200/坏体）→ reason=upstream。"""


# transport 签名：(url, jsonrpc_payload, timeout) -> JSON-RPC 响应 dict
Transport = Callable[[str, dict, float], dict]


def decode_body(text: str) -> dict:
    """解 JSON-RPC 响应体：application/json 或 SSE（data: 行）两种都认。

    SSE 取第一条带 result/error 的 data 行（MCP streamable-http 单响应场景）。
    两种都解不出抛 RefUpstream。
    """
    raw = (text or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise RefUpstream(f"JSON 响应体解析失败：{exc}") from exc
        if isinstance(data, dict):
            return data
        raise RefUpstream("JSON 响应体不是对象")
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[len("data:"):].strip()
        try:
            data = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(data, dict) and ("result" in data or "error" in data):
            return data
    raise RefUpstream("响应体既非 JSON 也无可解析的 SSE data 行")


def http_transport(url: str, payload: dict, timeout: float) -> dict:
    """默认传输：httpx 单次 POST，无重试（失败即抛，由 _call 归一降级）。"""
    import httpx  # 延迟导入：缺 httpx 时仅参照系调用不可用，不影响包导入

    try:
        resp = httpx.post(
            url,
            json=payload,
            timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT),
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
    except httpx.HTTPError as exc:  # 含 TimeoutException / TransportError
        raise RefUnreachable(f"{type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise RefUpstream(f"HTTP {resp.status_code}")
    return decode_body(resp.text)


def _first_text(result: dict) -> str | None:
    for content in result.get("content") or []:
        if (
            isinstance(content, dict)
            and content.get("type") == "text"
            and isinstance(content.get("text"), str)
        ):
            return content["text"]
    return None


def _from_jsonrpc(tool: str, raw: Any) -> RefResult:
    """JSON-RPC 响应 → RefResult：工具级空结果带 taxonomy reason 也归一为降级。"""
    if not isinstance(raw, dict):
        return RefResult(False, UPSTREAM, f"{tool}: 响应不是 JSON-RPC 对象")
    if raw.get("error"):
        err = raw["error"]
        return RefResult(
            False, UPSTREAM,
            f"{tool}: JSON-RPC 错误 {err.get('code')}: {err.get('message')}",
        )
    result = raw.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        text = _first_text(result) if isinstance(result, dict) else ""
        return RefResult(
            False, UPSTREAM,
            f"{tool}: 工具级错误：{text or '响应缺 result 对象'}",
        )
    text = _first_text(result)
    if text is None:
        return RefResult(False, UPSTREAM, f"{tool}: 结果无 text content")
    try:
        payload = json.loads(text)
    except ValueError:
        return RefResult(False, UPSTREAM, f"{tool}: 工具输出不是 JSON：{text[:120]}")
    if not isinstance(payload, dict):
        return RefResult(False, UPSTREAM, f"{tool}: 工具输出不是对象")
    results = payload.get("results")
    if isinstance(results, list) and results:
        return RefResult(True, data=payload)
    reason = payload.get("reason")
    if isinstance(reason, str) and reason in TAXONOMY:
        return RefResult(False, reason, str(payload.get("hint") or reason))
    return RefResult(
        False, UPSTREAM,
        f"{tool}: 空结果未带合法 reason（taxonomy 外口径 {reason!r} 视为上游异常）",
    )


class ReferenceClient:
    """参照系四工具 + 台账的最小客户端：每次调用独立归一为 RefResult。"""

    def __init__(
        self,
        base_url: str | None = None,
        transport: Transport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        status_timeout: float = STATUS_TIMEOUT,
    ) -> None:
        self.base_url = (
            base_url if base_url is not None else config.reference_base_url()
        ).strip()
        self._transport = transport or http_transport
        self.timeout = timeout
        self.status_timeout = status_timeout

    def _call(self, tool: str, args: dict, timeout: float | None = None) -> RefResult:
        if not self.base_url:
            return RefResult(
                False, NOT_CONFIGURED,
                "FDZX__REFERENCE_BASE_URL 未配置：参照系地址是硬前置，"
                "未配置即冻结本闭环（不冒充远端故障）",
            )
        envelope = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }
        try:
            raw = self._transport(self.base_url, envelope, timeout or self.timeout)
        except RefUnreachable as exc:
            return RefResult(False, UNREACHABLE, f"参照系不可达（{tool}）：{exc}")
        except RefUpstream as exc:
            return RefResult(False, UPSTREAM, f"参照系响应异常（{tool}）：{exc}")
        except Exception as exc:  # noqa: BLE001 —— 桩/序列化等未知失败，归一 unreachable
            return RefResult(
                False, UNREACHABLE,
                f"参照系传输失败（{tool}）：{type(exc).__name__}: {exc}",
            )
        return _from_jsonrpc(tool, raw)

    # ---- 契约面（四工具 + 台账） ----

    def status(self) -> RefResult:
        """源状态台账（诊断/展示用；数据工具自带结构化 reason，无需预检）。"""
        return self._call("source_status", {}, self.status_timeout)

    def concept_lookup(self, term: str) -> RefResult:
        """概念：定义摘要 + 来源 + SEP 链接（结果项带 id/qid，id 即引用锚）。"""
        return self._call("concept_lookup", {"term": term})

    def ideology_query(self, qid: str) -> RefResult:
        """思潮坐标锚：P1142（意识形态）/P1387（政治取向）/MARPOR 立场编码。"""
        return self._call("ideology_query", {"qid": qid})

    def relation_query(self, qid: str, polarity: str) -> RefResult:
        """关系边：带极性（influenced_by/critical/…）与 PID 依据的离散边。"""
        return self._call("relation_query", {"qid": qid, "polarity": polarity})

    def regime_timeseries(self, country: str, years: str) -> RefResult:
        """政体时序（V-Dem/MARPOR 指标；脉搏环消费，本三闭环暂不调）。"""
        return self._call("regime_timeseries", {"country": country, "years": years})


def client() -> ReferenceClient:
    """按 config 建 client（每次新建不缓存——与 config 即时读取习惯一致）。"""
    return ReferenceClient()
