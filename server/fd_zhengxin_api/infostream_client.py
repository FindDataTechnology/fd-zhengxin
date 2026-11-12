"""info-stream（fd-info）只读客户端：脉搏环（pulse）的上游取数面。

依据 docs/info-stream-probe.md 实测结论封装三接口（+健康度）：
- list_since：键集分页（published_at DESC, item_id DESC），游标 base64(JSON)，
  翻页只信 next_cursor，不用 OFFSET 语义；
- get_item：条目不存在时 HTTP 200 + payload 带 "error" 键——不是错误，
  不重试，原样返回由调用方判读；
- search_info：必带 mode 字段（text-core / index）；mode:"text-core" 且
  count:0 不可当真无命中（probe 实测缺口），调用方自行判读；
- stream_health：D7 纪律「先查健康度再取数」——list_since 空结果本体
  不带原因，必须交叉健康度才能解读。

超时口径（probe §5 建议）：stream_health 2s / list_since·get_item 5s /
search_info 10s，连接级 3s。重试：短退避 ×2（默认 0.25s → 1.0s），
重试后仍失败 → 抛结构化 InfoStreamUnavailable（unreachable /
upstream_error / protocol_error），由调用方（pulse.freeze）冻结降级，
**绝不静默返回空结果**。

纯只读铁律：本客户端不含任何写方法——READ_METHODS 即全部公开面，
fd-info 侧 5 工具也都声明 readOnlyHint: true。

传输形态：MCP streamable-http（POST {base}/mcp，JSON-RPC 2.0，
initialize → notifications/initialized → tools/call，会话头
Mcp-Session-Id）——会话传输实现已提取到 mcp_http.py（与 reference_mcp 共用，
D1 修复产物），本模块只保留 fd-info 侧的端点拼装与错误归类。fd-info 未上产：
FDZX__INFOSTREAM_BASE_URL 未设时任何调用立即抛 not_configured。测试可注入假
transport（可调用对象 (tool, arguments, timeout) -> payload dict）离线回放。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from . import config
from .mcp_http import (  # noqa: F401 —— 兼容 re-export（test_pulse 直调旧名）
    MCP_HEADERS as _MCP_HEADERS,
    parse_sse_text as _parse_sse_text,
    unwrap_envelope as _unwrap_envelope,
)
from .mcp_http import MCPSessionTransport

# ---- 结构化冻结原因（绝不静默空） ----


@dataclass(frozen=True)
class FrozenReason:
    """上游不可用原因结构（pulse 冻结降级如实记录用）。"""

    code: str      # not_configured | unreachable | upstream_error | protocol_error
    detail: str    # 人读原文（错误类型+信息 / 缺的配置项）

    def to_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail}


class InfoStreamUnavailable(Exception):
    """上游不可达 / 未配置 / 坏响应：带结构化原因抛出，绝不静默空。"""

    def __init__(self, reason: FrozenReason):
        super().__init__(f"[{reason.code}] {reason.detail}")
        self.reason = reason


# ---- 超时与退避（probe §5 口径） ----

TIMEOUTS: dict[str, float] = {
    "stream_health": 2.0,
    "list_since": 5.0,
    "get_item": 5.0,
    "search_info": 10.0,
    "get_body": 5.0,
}
CONNECT_TIMEOUT = 3.0
RETRIES = 2                     # 短退避 ×2（初始 1 次 + 重试 2 次 = 3 次尝试）
RETRY_BACKOFF_BASE = 0.25       # 退避 0.25s → 1.0s（指数 ×4），测试可归零

READ_METHODS = ("list_since", "get_item", "search_info", "stream_health", "get_body")


def _classify(exc: Exception) -> FrozenReason:
    """把传输层异常归类为结构化冻结原因。"""
    import httpx  # 延迟导入：与 llm.py 同习惯

    if isinstance(exc, (httpx.TransportError, OSError)):
        return FrozenReason("unreachable", f"{type(exc).__name__}: {exc}")
    if isinstance(exc, httpx.HTTPStatusError):
        return FrozenReason("upstream_error", f"HTTP {exc.response.status_code}: {exc}")
    return FrozenReason("protocol_error", f"{type(exc).__name__}: {exc}")


# ---- MCP streamable-http 传输（实现已提取至 mcp_http.py，此处仅 fd-info 拼装） ----


class MCPHTTPTransport(MCPSessionTransport):
    """fd-info 形态的真实传输：base_url 是服务根，端点 = {base}/mcp。

    会话握手/双响应体解析/envelope 解包全部继承 mcp_http.MCPSessionTransport
    （D1：勿在各客户端重抄会话头）。

    token（8.6）：fd-info 开共享门之后必须带同一把令牌；`None`=按配置即时读
    （`FDZX__INFOSTREAM_TOKEN`），空串=不发该头（与接入前逐字节一致）。
    """

    def __init__(
        self,
        base_url: str,
        connect_timeout: float = CONNECT_TIMEOUT,
        token: str | None = None,
    ):
        base = base_url.rstrip("/")
        endpoint = base if base.endswith("/mcp") else base + "/mcp"
        super().__init__(
            endpoint, connect_timeout=connect_timeout,
            client_name="fd-zhengxin-infostream-client",
            token=config.infostream_token() if token is None else token,
        )


Transport = Callable[[str, dict, float], dict]


class InfoStreamClient:
    """fd-info 只读客户端：三接口封装 + 健康度；无任何写方法。"""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        transport: Transport | None = None,
        connect_timeout: float = CONNECT_TIMEOUT,
        backoff_base: float = RETRY_BACKOFF_BASE,
        retries: int = RETRIES,
    ):
        self._base_url = (base_url if base_url is not None else config.infostream_base_url()).strip()
        self._transport: Transport = transport or MCPHTTPTransport(
            self._base_url, connect_timeout=connect_timeout
        )
        self._backoff_base = backoff_base
        self._retries = retries
        self.attempts = 0  # 最近一次工具调用的实际尝试次数（审计/测试用）

    def is_configured(self) -> bool:
        return bool(self._base_url)

    # ---- 内部：超时 + 短退避 ×N，失败抛结构化原因 ----

    def _call(self, tool: str, arguments: dict, timeout: float) -> dict:
        if not self.is_configured():
            raise InfoStreamUnavailable(
                FrozenReason(
                    "not_configured",
                    "FDZX__INFOSTREAM_BASE_URL 未配置（fd-info 未上产，端点可配置）",
                )
            )
        last: FrozenReason | None = None
        for attempt in range(self._retries + 1):  # 初始 1 次 + 重试 ×retries
            self.attempts = attempt + 1
            try:
                return self._transport(tool, arguments, timeout)
            except Exception as exc:  # noqa: BLE001 —— 归类为结构化原因再决定降级
                last = _classify(exc)
                if attempt < self._retries:
                    time.sleep(self._backoff_base * (4 ** attempt))
        assert last is not None  # pragma: no cover —— 循环至少跑一次
        raise InfoStreamUnavailable(last)

    # ---- 三接口（只读）+ 健康度 ----

    def list_since(
        self,
        *,
        cursor: str | None = None,
        source_types: list[str] | None = None,
        published_after: str | None = None,
        limit: int | None = None,
    ) -> dict:
        """增量拉取：{items, next_cursor, has_more}；翻页只信 next_cursor。"""
        args: dict[str, Any] = {}
        if cursor is not None:
            args["cursor"] = cursor
        if source_types is not None:
            args["source_types"] = source_types
        if published_after is not None:
            args["published_after"] = published_after
        if limit is not None:
            args["limit"] = limit
        return self._call("list_since", args, TIMEOUTS["list_since"])

    def get_item(self, item_id: str) -> dict:
        """条目详情（含 doc_id / body_status；不内联正文）。不存在 → payload 带
        "error" 键原样返回（不是异常，不要重试）。"""
        return self._call("get_item", {"item_id": item_id}, TIMEOUTS["get_item"])

    def get_body(self, doc_id: str) -> dict:
        """条目全文（get_item 不内联正文后的补位读取面）。缺失/failed →
        payload 带 "ok": false 与 "error" 键原样返回（不是异常，不要重试）。"""
        return self._call("get_body", {"doc_id": doc_id}, TIMEOUTS["get_body"])

    def search_info(
        self, query: str, *, source_types: list[str] | None = None, limit: int | None = None,
    ) -> dict:
        """检索：{items, count, mode}。必读 mode——mode:"text-core" 且
        count:0 不可当真无命中（probe 实测缺口）；mode:"index" 仅标题/摘要
        关键字弱信号。"""
        args: dict[str, Any] = {"query": query}
        if source_types is not None:
            args["source_types"] = source_types
        if limit is not None:
            args["limit"] = limit
        return self._call("search_info", args, TIMEOUTS["search_info"])

    def stream_health(self) -> dict:
        """源健康度（D7：先健康度再取数）。政治源 cn-gov 不 ok 时
        list_since 的空结果是「源不可用」不是「无信息」。"""
        return self._call("stream_health", {}, TIMEOUTS["stream_health"])
