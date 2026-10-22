"""MCP streamable-http 会话传输（reference_mcp 与 infostream_client 的共用底座）。

D1 教训（docs/e2e-local-report.md 缺陷 D1）：对 FastMCP streamable-http 端点单发
tools/call——不做 initialize 握手、不带 Mcp-Session-Id 头——会被服务端一律
400（"Missing session ID"）。本模块把「每次工具调用一条完整 MCP 会话」的传输
形态固化为一处，两个上游消费客户端共用（勿再各抄一份漏掉会话头）：

    initialize →（服务端给了会话头则）notifications/initialized → tools/call

- 会话头逐次建立：只读低频查询场景无需常驻会话，失败无残留状态；
- 双响应体兼容：application/json 与 text/event-stream（SSE data: 帧）都能解
  （按 request id 对帧，initialize=id1、tools/call=id2）；
- envelope 解包：result.content[0].text（工具的 JSON 输出）→ payload dict；
  JSON-RPC error / isError / 非 200 一律抛错（由调用方归一为结构化降级原因）。

测试可注入替身：本模块只承载**默认传输**，客户端层各自保留
(tool, arguments, timeout) -> payload 的可注入签名。
"""

from __future__ import annotations

import json

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

DEFAULT_CONNECT_TIMEOUT = 3.0


def resolve_endpoint(base_url: str) -> str:
    """服务地址 → MCP streamable-http 端点。

    配置可能是端点全路径（参照系口径：FDZX__REFERENCE_BASE_URL=…/mcp）或
    服务根（fd-info 口径：…/ 根，端点=根+/mcp）：路径为空或仅 / 时补 /mcp，
    其余按原样使用（已是端点）。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return base
    path = base.split("://", 1)[-1]
    if "/" not in path:  # 形如 http://host:8000 → http://host:8000/mcp
        return base + "/mcp"
    return base


def parse_sse_text(text: str, request_id: int) -> dict:
    """从 text/event-stream 帧里解出对应 request id 的 JSON-RPC 消息。"""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload:
            continue
        try:
            msg = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict) and msg.get("id") == request_id and "result" in msg:
            return msg
    raise ValueError(f"SSE 流中无 id={request_id} 的 JSON-RPC 结果帧")


def unwrap_envelope(envelope: dict) -> dict:
    """tools/call 结果 envelope → payload dict：result.content[0].text 解包。

    只解 JSON-RPC 层：JSON-RPC error / isError / 无 content 一律 ValueError
    （上游侧错误落在 payload 的键里，由调用方判读）。
    """
    if not isinstance(envelope, dict):
        raise ValueError(f"非对象 JSON-RPC 响应：{envelope!r}")
    if envelope.get("error") is not None:
        raise ValueError(f"JSON-RPC 错误响应：{envelope['error']!r}")
    result = envelope.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        raise ValueError(f"tools/call isError 响应：{result!r}")
    content = result.get("content")
    if not isinstance(content, list) or not content:
        raise ValueError(f"tools/call 无 content：{result!r}")
    text = content[0].get("text") if isinstance(content[0], dict) else None
    if not isinstance(text, str):
        raise ValueError(f"content[0] 无 text：{content[0]!r}")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError(f"工具返回非对象 payload：{payload!r}")
    return payload


class MCPSessionTransport:
    """默认传输：每次工具调用一条完整 MCP 会话（initialize → tools/call）。

    签名 (tool, arguments, timeout) -> payload dict；httpx 网络异常原样抛出
    （调用方归一为 unreachable/upstream），协议层异常抛 ValueError。
    """

    def __init__(
        self,
        endpoint_url: str,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        client_name: str = "fd-zhengxin-mcp-client",
    ):
        self._endpoint = resolve_endpoint(endpoint_url)
        self._connect_timeout = connect_timeout
        self._client_name = client_name

    def __call__(self, tool: str, arguments: dict, timeout: float) -> dict:
        import httpx

        to = httpx.Timeout(timeout, connect=self._connect_timeout)
        with httpx.Client(timeout=to) as http:
            session = self._initialize(http)
            if session:
                # notifications/initialized：服务端 202/204 皆可，不校验
                http.post(
                    self._endpoint,
                    headers={**MCP_HEADERS, "Mcp-Session-Id": session},
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
            headers = {**MCP_HEADERS, **({"Mcp-Session-Id": session} if session else {})}
            resp = http.post(
                self._endpoint,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                },
            )
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if "text/event-stream" in ctype:
                envelope = parse_sse_text(resp.text, 2)
            else:
                envelope = resp.json()
        return unwrap_envelope(envelope)

    def _initialize(self, http) -> str | None:
        resp = http.post(
            self._endpoint,
            headers=MCP_HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": self._client_name, "version": "0.1"},
                },
            },
        )
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        envelope = (
            parse_sse_text(resp.text, 1) if "text/event-stream" in ctype else resp.json()
        )
        if not (isinstance(envelope, dict) and isinstance(envelope.get("result"), dict)):
            raise ValueError(f"initialize 坏响应：{envelope!r}")
        return resp.headers.get("mcp-session-id")


__all__ = [
    "MCP_HEADERS",
    "MCPSessionTransport",
    "parse_sse_text",
    "resolve_endpoint",
    "unwrap_envelope",
]
