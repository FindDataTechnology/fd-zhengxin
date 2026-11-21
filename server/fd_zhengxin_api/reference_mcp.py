"""参照系 MCP 客户端（任务 4.2/4.3/4.4 三闭环的参照系消费面，与 LLM KEY 无关）。

命名说明：reference_client.py 已被个人层消费面（任务 5.2/5.4，regions/anchor
的 GET JSON 形态）占用，本模块承载三闭环的 MCP 四工具契约，名字分开、互不
覆盖——两块消费面契约不同（GET JSON vs MCP tools/call）。

契约（参照系 = fd-zhengxin-reference 仓的独立部署，本仓只消费不实现；D2 修复
后以参照系 tools_placeholder.py 的真实签名为准，勿再漂移）：
- 地址 FDZX__REFERENCE_BASE_URL（MCP streamable-http 端点，如 http://…/mcp；
  配服务根亦可，mcp_http.resolve_endpoint 自动补 /mcp）；
- 五工具（tools/list 实测签名）：
    concept_lookup(term)                     概念（种子/近现代/SEP，行带 provenance）
    ideology_query(name)                     思潮坐标锚（name 为 QID 或 label）
    relation_query(source, target, polarity) 关系边（两端名/QID + 可选极性过滤）
    regime_timeseries(country, indicator, years) 政体时序
    source_status()                          台账
- 行结构：id 锚在 provenance.stable_id（种子概念行另带 school/figures/weight，
  种子边行带 source_name/target_name/school/weight/key_text/target_years——
  详见 locate.py 的字段映射）；qid 不是通用键（种子/近现代行无 qid）。

D1 修复（docs/e2e-local-report.md）：传输层不再单发 tools/call——改走
mcp_http.MCPSessionTransport（initialize → notifications/initialized →
tools/call，会话头 Mcp-Session-Id，与 infostream_client 共用同一实现），
双响应体（application/json 与 SSE 帧）由共用传输解包为 payload dict。

消费纪律照 docs/info-stream-probe.md 的实跑结论：
- 空结果必带原因：参照系契约（errors taxonomy：blocked/upstream/unreachable/
  not_ingested）保证空结果带 reason，本客户端把「传输失败」与「工具级空结果」
  都归一为 RefResult(ok=False, reason=...)，绝不静默为空；
- 超时预算：tools/call 默认 8s、source_status 4s、连接级 3s（参照系是内网
  服务，预算比 fd-info 公网口径略紧）；
- 不自研重试堆栈（照 llm.py 铁律）：失败即降级，由调用方带原因返回；
- FDZX__REFERENCE_BASE_URL 未配置是本地前置缺口（reason=not_configured），
  不冒充远端故障；
- 无需 health-first 预检：参照系每个工具空结果自带 reason；status() 仅供
  诊断与展示。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from . import config
from .mcp_http import MCPSessionTransport

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


# transport 签名：(tool, arguments, timeout) -> 工具 payload dict（envelope
# 已由 mcp_http.MCPSessionTransport 解包；测试注入桩同签名）
Transport = Callable[[str, dict, float], dict]


def _from_payload(tool: str, payload: Any) -> RefResult:
    """工具 payload → RefResult。

    三种形态：
    1. `results` 非空列表 → 成功（四工具与台账的形态）；
    2. **有 `results` 键但为空**，或带 taxonomy reason → 降级（空结果必带原因）；
    3. **没有 `results` 键的对象形态**（`entity_dossier` 是 identity/relations/
       coverage 而非 results 数组）→ 只要不是错误对象就算成功，原样透传。
       少了这条分支，`entity_dossier` 的成功响应会被判成「空结果未带 reason」
       的上游异常——实测撞过（test_reference_contract 的 entity_dossier 用例）。
    """
    if not isinstance(payload, dict):
        return RefResult(False, UPSTREAM, f"{tool}: 工具输出不是对象")
    reason = payload.get("reason")
    results = payload.get("results")
    if isinstance(results, list) and results:
        return RefResult(True, data=payload)
    if "results" not in payload and reason is None and "error" not in payload:
        return RefResult(True, data=payload)   # 对象形态成功响应
    if isinstance(reason, str) and reason in TAXONOMY:
        return RefResult(False, reason, str(payload.get("hint") or reason))
    return RefResult(
        False, UPSTREAM,
        f"{tool}: 空结果未带合法 reason（taxonomy 外口径 {reason!r} 视为上游异常）",
    )


def _classify_raw(tool: str, exc: Exception) -> RefResult:
    """共用传输抛出的原始异常（httpx/ValueError）→ RefResult 分类归一。"""
    import httpx  # 延迟导入：与 llm.py 同习惯

    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, OSError)):
        return RefResult(False, UNREACHABLE, f"参照系不可达（{tool}）：{exc}")
    if isinstance(exc, httpx.HTTPStatusError):
        return RefResult(False, UPSTREAM, f"参照系响应异常（{tool}）：HTTP {exc.response.status_code}")
    if isinstance(exc, ValueError):
        # 协议层（坏 envelope/坏 SSE/JSON-RPC error）→ 可达但响应异常
        return RefResult(False, UPSTREAM, f"参照系响应异常（{tool}）：{exc}")
    return RefResult(False, UNREACHABLE, f"参照系传输失败（{tool}）：{type(exc).__name__}: {exc}")


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
        self._transport = transport or MCPSessionTransport(
            self.base_url,
            connect_timeout=CONNECT_TIMEOUT,
            client_name="fd-zhengxin-reference-mcp",
            # 8.6：参照系开共享门之后必须带同一把令牌；未配=不发该头（逐字节同前）
            token=config.reference_token(),
        )
        self.timeout = timeout
        self.status_timeout = status_timeout

    def _call(self, tool: str, args: dict, timeout: float | None = None) -> RefResult:
        if not self.base_url:
            return RefResult(
                False, NOT_CONFIGURED,
                "FDZX__REFERENCE_BASE_URL 未配置：参照系地址是硬前置，"
                "未配置即冻结本闭环（不冒充远端故障）",
            )
        try:
            payload = self._transport(tool, args, timeout or self.timeout)
        except RefUnreachable as exc:
            return RefResult(False, UNREACHABLE, f"参照系不可达（{tool}）：{exc}")
        except RefUpstream as exc:
            return RefResult(False, UPSTREAM, f"参照系响应异常（{tool}）：{exc}")
        except Exception as exc:  # noqa: BLE001 —— 桩/网络/协议失败统一归一降级
            return _classify_raw(tool, exc)
        return _from_payload(tool, payload)

    # ---- 契约面（五工具，参数名照 tools/list 实测签名） ----

    def status(self) -> RefResult:
        """源状态台账（诊断/展示用；数据工具自带结构化 reason，无需预检）。"""
        return self._call("source_status", {}, self.status_timeout)

    def concept_lookup(self, term: str) -> RefResult:
        """概念：定义摘要 + 来源链接（行锚 = provenance.stable_id）。"""
        return self._call("concept_lookup", {"term": term})

    def ideology_query(self, name: str) -> RefResult:
        """思潮坐标锚：P1142（意识形态）/P1387（政治取向）/MARPOR；name 为
        QID 或 label（D2：参数名 qid→name，照参照系实测签名）。"""
        return self._call("ideology_query", {"name": name})

    def relation_query(
        self, source: str, target: str, polarity: str | None = None,
    ) -> RefResult:
        """关系边：两端（人物/实体标签或 QID）之间的师承/批判/论战边。

        D2：参照系关系面是**人物对**查询（source+target，双向收敛），不是
        「某实体的全部边」；polarity 可选（inheritance/alliance/opposition/
        critique / 种子 positive/critical），不传即全量。
        """
        args: dict[str, Any] = {"source": source, "target": target}
        if polarity is not None:
            args["polarity"] = polarity
        return self._call("relation_query", args)

    def entity_dossier(self, ref: str) -> RefResult:
        """单实体读面：人物/政党/事件/运动/学派 → 身份 + 学派 + 出入关系边全集。

        与 `relation_query` 的区别：那是两两配对查询，本工具答「这个实体的全部
        关系」。响应第一层带 `coverage` 三态（available / not_in_snapshot /
        not_ingested）——调用方必须读它，空关系边不等于实体不存在。
        """
        return self._call("entity_dossier", {"ref": ref})

    def regime_timeseries(
        self, country: str, years: list[int] | None = None, indicator: str | None = None,
    ) -> RefResult:
        """政体时序（V-Dem/MARPOR 指标；脉搏环消费，本三闭环暂不调）。"""
        args: dict[str, Any] = {"country": country}
        if years is not None:
            args["years"] = years
        if indicator is not None:
            args["indicator"] = indicator
        return self._call("regime_timeseries", args)


def client() -> ReferenceClient:
    """按 config 建 client（每次新建不缓存——与 config 即时读取习惯一致）。"""
    return ReferenceClient()
