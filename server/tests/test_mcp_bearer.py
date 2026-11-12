"""上游共享 bearer 的判据（change converge-line-identity-and-keys 8.6）。

参照系与 fd-info 开了线自有共享 bearer 门之后，本仓发往它们的**每个**请求都要带
同一把令牌。这里只看"我们发了哪个头"，不起真上游：

1. 未配令牌 ⇒ 三个请求都不带 Authorization（与接入前逐字节一致，这是默认态）；
2. 配了 ⇒ `Authorization: Bearer <token>`，且 initialize 与后两个请求**都**带
   （后两个用拼装的会话头，最容易在实现里漏）；
3. 空白令牌视同未配（别让一个空 secret 变成 `Bearer ` 空头）；
4. 两个客户端各自的配置入口（`FDZX__REFERENCE_TOKEN` / `FDZX__INFOSTREAM_TOKEN`）
   真的接到了传输上——否则改对了实现却没人读，正是"声明≠挂载"。

选 MockTransport 而不是起真服务：判据的对象是**我们发的头**，真进程能证明"通了"
却证明不了"带了哪个头"，方向是反的。
"""
from __future__ import annotations

import json

import httpx
import pytest

from fd_zhengxin_api import config, infostream_client, mcp_http, reference_mcp

SID = "sid-1"

#: 先留住真类：下面的工厂会覆盖 httpx.Client，工厂内部必须用未被打补丁的那个。
_REAL_CLIENT = httpx.Client


def _install_mock(monkeypatch, records: list[dict]):
    """把 httpx.Client 换成挂 MockTransport 的实例工厂，记录每次请求的头。"""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        records.append(
            {"method": payload.get("method"), "headers": dict(request.headers)}
        )
        if payload.get("method") == "initialize":
            return httpx.Response(
                200,
                headers={"mcp-session-id": SID, "content-type": "application/json"},
                json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}},
            )
        if payload.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"content": [{"type": "text", "text": '{"ok": true}'}]},
            },
        )

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return _REAL_CLIENT(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "Client", factory)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("FDZX__REFERENCE_TOKEN", raising=False)
    monkeypatch.delenv("FDZX__INFOSTREAM_TOKEN", raising=False)


def _no_auth(recs):
    for rec in recs:
        assert "authorization" not in {k.lower() for k in rec["headers"]}, rec


def test_no_token_sends_no_authorization(monkeypatch):
    """未配令牌 = 不发这个头：门没开时行为与接入前逐字节一致。"""
    records: list[dict] = []
    _install_mock(monkeypatch, records)
    transport = mcp_http.MCPSessionTransport("http://ref.local/mcp")
    transport("concept_lookup", {"term": "民主"}, 5.0)
    assert [r["method"] for r in records][:1] == ["initialize"]
    assert len(records) == 3, [r["method"] for r in records]
    _no_auth(records)


def test_token_is_sent_on_every_request(monkeypatch):
    """配了令牌 ⇒ 三个请求都带（后两个经会话头拼装，最容易漏）。"""
    records: list[dict] = []
    _install_mock(monkeypatch, records)
    transport = mcp_http.MCPSessionTransport("http://ref.local/mcp", token="lmc_test_token")
    transport("concept_lookup", {"term": "民主"}, 5.0)
    assert [r["method"] for r in records] == [
        "initialize", "notifications/initialized", "tools/call",
    ]
    for rec in records:
        assert rec["headers"].get("authorization") == "Bearer lmc_test_token", rec


def test_blank_token_is_treated_as_unset(monkeypatch):
    """空串/空白不算配置——否则会发出一个空的 `Bearer ` 头被上游判非法。"""
    records: list[dict] = []
    _install_mock(monkeypatch, records)
    transport = mcp_http.MCPSessionTransport("http://ref.local/mcp", token="   ")
    transport("concept_lookup", {"term": "民主"}, 5.0)
    _no_auth(records)


def test_reference_client_reads_its_own_env(monkeypatch):
    """参照系客户端的令牌来自 FDZX__REFERENCE_TOKEN（配置真的接到了传输上）。"""
    records: list[dict] = []
    _install_mock(monkeypatch, records)
    monkeypatch.setenv("FDZX__REFERENCE_TOKEN", "lmc_ref_token")
    monkeypatch.setenv("FDZX__REFERENCE_BASE_URL", "http://ref.local/mcp")
    client = reference_mcp.ReferenceClient()
    client._call("concept_lookup", {"term": "民主"})
    assert records, "应当至少发出 initialize"
    for rec in records:
        assert rec["headers"].get("authorization") == "Bearer lmc_ref_token", rec


def test_infostream_transport_reads_its_own_env(monkeypatch):
    """fd-info 传输的令牌来自 FDZX__INFOSTREAM_TOKEN；且与服务根拼端点互不影响。"""
    records: list[dict] = []
    _install_mock(monkeypatch, records)
    monkeypatch.setenv("FDZX__INFOSTREAM_TOKEN", "lmc_info_token")
    transport = infostream_client.MCPHTTPTransport("http://info.local:8000")
    assert transport._endpoint == "http://info.local:8000/mcp"
    transport("search_info", {"query": "经济"}, 5.0)
    assert records
    for rec in records:
        assert rec["headers"].get("authorization") == "Bearer lmc_info_token", rec


def test_config_token_getters_are_trimmed(monkeypatch):
    """两个配置入口都 trim：带空白的 secret 不该变成非法头。"""
    monkeypatch.setenv("FDZX__REFERENCE_TOKEN", "  abc  ")
    monkeypatch.setenv("FDZX__INFOSTREAM_TOKEN", "  ")
    assert config.reference_token() == "abc"
    assert config.infostream_token() == ""
