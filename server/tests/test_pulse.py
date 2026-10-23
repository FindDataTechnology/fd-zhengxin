"""脉搏环测试（任务 4.5）：历史条目回放驱动光谱更新（span 逐字校验）+
无证据 stance 丢弃 + 冻结降级（旧光谱可读、解冻自动续拉）+ 客户端只读性。
"""

from __future__ import annotations

import json
import threading
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from fd_zhengxin_api import infostream_client, llm, llm_ledger, pulse
from fd_zhengxin_api.infostream_client import InfoStreamClient, InfoStreamUnavailable
from fd_zhengxin_api.main import app

api = TestClient(app)
AUTH = {"Authorization": "Bearer dev-tok-alice"}
ISSUE = "延迟退休"

BODY_A = "关于延迟退休改革，文件明确表示支持渐进式实施，先在部分行业试点。有专家对基金可持续性表示担忧。"
BODY_B = "针对延迟退休年龄设置，部分地区建议审慎评估节奏，稳妥过渡。"
BODY_PLAIN = "会议纪要仅列议程与出席人员，未载明任何立场表述。"
BODY_LLM = "个别地区建议按年度评估节奏后再定实施力度。"


def _index_item(item_id: str, title: str, summary: str, withdrawn: bool = False) -> dict:
    return {
        "item_id": item_id, "source": "cn-gov", "source_type": "ministry",
        "published_at": "2026-10-2%dT00:00:00Z" % (1 + int(item_id[1:]) % 9),
        "title": title, "summary": summary, "doc_id": f"cn-gov:{item_id}",
        "body_status": "stored", "withdrawn": withdrawn, "raw_hash": f"h-{item_id}",
    }


def _full(item_id: str, title: str, summary: str, body: str | None) -> dict:
    d = _index_item(item_id, title, summary)
    d["body"] = body
    d["body_status"] = "stored" if body else "missing"
    return d


ITEM_A_IDX = _index_item("i1", "关于延迟退休改革方案的意见", "明确支持渐进实施")
ITEM_A = _full("i1", "关于延迟退休改革方案的意见", "明确支持渐进实施", BODY_A)
ITEM_B_IDX = _index_item("i2", "关于延迟退休年龄设置的建议", "建议审慎评估节奏")
ITEM_B = _full("i2", "关于延迟退休年龄设置的建议", "建议审慎评估节奏", BODY_B)


class ScriptedTransport:
    """回放传输：stream_health ok / list_since 逐页弹出 / get_item 查字典。"""

    def __init__(self, pages: list[dict], items: dict[str, dict]):
        self.pages = list(pages)
        self.items = dict(items)
        self.calls: list[tuple[str, dict]] = []
        self.health: dict = {"sources": [{"source": "cn-gov", "status": "ok",
                                          "consecutive_failures": 0}]}

    def __call__(self, tool: str, arguments: dict, timeout: float) -> dict:
        self.calls.append((tool, dict(arguments)))
        if tool == "stream_health":
            return self.health
        if tool == "list_since":
            return self.pages.pop(0) if self.pages else {
                "items": [], "next_cursor": None, "has_more": False,
            }
        if tool == "get_item":
            item = self.items.get(arguments["item_id"])
            return item if item is not None else {
                "error": f"'条目不存在：{arguments['item_id']}'",
            }
        if tool == "search_info":
            return {"items": [], "count": 0, "mode": "index"}
        raise AssertionError(f"只读客户端出现非预期工具调用：{tool}")


def make_client(pages: list[dict], items: dict[str, dict]) -> InfoStreamClient:
    return InfoStreamClient(
        base_url="http://fd-info.test/mcp",
        transport=ScriptedTransport(pages, items),
        backoff_base=0.0,
    )


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FDZX__DB", str(tmp_path / "personal.db"))
    monkeypatch.setenv("FDZX__AUTH_DEV_TOKENS", "dev-tok-alice=alice")
    monkeypatch.setenv("FDZX__PULSE_DB", str(tmp_path / "pulse.db"))
    monkeypatch.setenv("FDZX__LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.delenv("FDZX__LLM_API_KEY", raising=False)
    monkeypatch.delenv("FDZX__INFOSTREAM_BASE_URL", raising=False)


# ---- 客户端：只读性 ----


def test_client_is_purely_read_only() -> None:
    """只读铁律：公开方法恰为 READ_METHODS，无任何写动词命名的方法。"""
    client = InfoStreamClient(base_url="http://fd-info.test/mcp")
    public = [m for m in dir(client) if not m.startswith("_")]
    assert set(infostream_client.READ_METHODS) <= set(public)
    write_verbs = (
        "create", "update", "delete", "write", "ingest", "put", "post",
        "remove", "insert", "upsert", "publish", "push", "patch", "save",
    )
    offenders = [
        m for m in public
        if m in write_verbs or m.startswith(tuple(v + "_" for v in write_verbs))
    ]
    assert offenders == [], f"只读客户端出现疑似写方法：{offenders}"


def test_client_not_configured_structured_reason() -> None:
    """FDZX__INFOSTREAM_BASE_URL 未设：结构化 not_configured，绝不静默空。"""
    client = InfoStreamClient()
    assert client.is_configured() is False
    with pytest.raises(InfoStreamUnavailable) as ei:
        client.list_since(source_types=["ministry"])
    assert ei.value.reason.code == "not_configured"
    assert "FDZX__INFOSTREAM_BASE_URL" in ei.value.reason.detail
    for call in (
        lambda: client.get_item("x"),
        lambda: client.search_info("政策"),
        lambda: client.stream_health(),
    ):
        with pytest.raises(InfoStreamUnavailable) as e2:
            call()
        assert e2.value.reason.code == "not_configured"


def test_client_short_backoff_twice_then_unreachable() -> None:
    """超时+短退避×2：初始 1 次 + 重试 2 次 = 3 次尝试后结构化 unreachable。"""
    attempts = {"n": 0}

    def flaky(tool, arguments, timeout):
        attempts["n"] += 1
        raise OSError("connection refused")

    client = InfoStreamClient(base_url="http://fd-info.test/mcp",
                              transport=flaky, backoff_base=0.0)
    with pytest.raises(InfoStreamUnavailable) as ei:
        client.list_since()
    assert ei.value.reason.code == "unreachable"
    assert "connection refused" in ei.value.reason.detail
    assert attempts["n"] == 3 and client.attempts == 3


def test_client_three_interfaces_passthrough() -> None:
    """三接口封装：list_since 游标 / get_item 详情与不存在 / search_info mode。"""
    pages = [{"items": [dict(ITEM_A_IDX)], "next_cursor": "cur-1", "has_more": True}]
    client = make_client(pages, {"i1": dict(ITEM_A)})
    page = client.list_since(source_types=["ministry"])
    assert page["next_cursor"] == "cur-1" and page["has_more"] is True
    assert client._transport.calls[-1][1] == {"source_types": ["ministry"]}  # noqa: SLF001

    item = client.get_item("i1")
    assert item["item_id"] == "i1" and item["body"] == BODY_A

    missing = client.get_item("nope")          # 不存在：payload 带 error，不是异常
    assert "error" in missing

    res = client.search_info("延迟退休")
    assert res["mode"] == "index"               # mode 必带（text-core 空结果缺口防线）


def test_mcp_http_transport_jsonrpc_roundtrip() -> None:
    """真实传输：本地起最小 MCP streamable-http 服务，验证 JSON-RPC 解包
    与 initialize 会话头接线。"""
    import http.server
    import socketserver

    seen: list[tuple[str, str | None]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):  # 静音
            pass

        def _send(self, status, payload: bytes, headers: dict | None = None):
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def do_POST(self):  # noqa: N802 —— http.server 约定
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            seen.append((str(req.get("method")), self.headers.get("Mcp-Session-Id")))
            if req.get("method") == "initialize":
                body = json.dumps({
                    "jsonrpc": "2.0", "id": req["id"],
                    "result": {"protocolVersion": "2025-03-26", "capabilities": {},
                               "serverInfo": {"name": "t", "version": "0"}},
                }).encode()
                self._send(200, body, {"Content-Type": "application/json",
                                       "Mcp-Session-Id": "sess-42"})
            elif req.get("method") == "notifications/initialized":
                self._send(202, b"")
            else:  # tools/call
                inner = {"items": [{"item_id": "z1"}], "next_cursor": None,
                         "has_more": False}
                body = json.dumps({
                    "jsonrpc": "2.0", "id": req["id"],
                    "result": {"content": [{"type": "text",
                                            "text": json.dumps(inner, ensure_ascii=False)}],
                               "isError": False},
                }).encode()
                self._send(200, body, {"Content-Type": "application/json"})

    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = InfoStreamClient(
            base_url=f"http://127.0.0.1:{srv.server_address[1]}/mcp", backoff_base=0.0,
        )
        page = client.list_since(limit=5)
        assert page["items"][0]["item_id"] == "z1"
        methods = [m for m, _ in seen]
        assert methods[0] == "initialize"
        assert "notifications/initialized" in methods and "tools/call" in methods
        assert ("tools/call", "sess-42") in seen   # 会话头随 tools/call 回传
    finally:
        srv.shutdown()


def test_parse_sse_text_picks_matching_id() -> None:
    text = ('event: message\n'
            'data: {"jsonrpc":"2.0","id":1,"result":{"x":0}}\n\n'
            'data: {"jsonrpc":"2.0","id":2,"result":{"x":1}}\n')
    msg = infostream_client._parse_sse_text(text, 2)  # noqa: SLF001
    assert msg["result"] == {"x": 1}
    with pytest.raises(ValueError):
        infostream_client._parse_sse_text(text, 9)  # noqa: SLF001


# ---- 摄取：光谱更新 + span 逐字校验 ----


def test_ingest_builds_spectrum_with_literal_spans() -> None:
    """历史条目回放驱动光谱更新；每条证据的 span 内文本与 get_item 正文
    逐字一致（测试独立复算，不复信写端校验）。"""
    client = make_client(
        [{"items": [dict(ITEM_A_IDX), dict(ITEM_B_IDX)],
          "next_cursor": None, "has_more": False}],
        {"i1": dict(ITEM_A), "i2": dict(ITEM_B)},
    )
    report = pulse.ingest_spectrum_update(ISSUE, client=client)
    assert report["frozen"] is False and report["frozen_reason"] is None
    assert report["items_processed"] == 2 and report["dropped_no_evidence"] == 0

    snap = pulse.get_spectrum(ISSUE)
    assert snap is not None and snap["frozen"] is False
    labels = {e["stance_label"] for e in snap["spectrum"]}
    assert labels == {"支持推进", "反对质疑", "谨慎观望"}

    bodies = {"i1": BODY_A, "i2": BODY_B}
    for entry in snap["spectrum"]:
        assert entry["evidence"], "无证据 stance 不得出现在光谱"
        for ev in entry["evidence"]:
            body = bodies[ev["item_id"]]
            start, end = ev["span"]
            assert 0 <= start < end <= len(body)
            assert body[start:end] == ev["quote"], (
                f"span 逐字校验失败：{ev['quote']!r} != {body[start:end]!r}"
            )
            assert ev["doc_id"] == f"cn-gov:{ev['item_id']}"


def test_ingest_drops_stances_without_evidence() -> None:
    """缺证据的强断言不产出：标题/摘要断言立场但正文无对应依据 → 丢弃计数。"""
    idx_meta = _index_item("i3", "专家质疑延迟退休节奏过快", "各方对基金压力表示担忧")
    full_meta = _full("i3", "专家质疑延迟退休节奏过快",
                      "各方对基金压力表示担忧", BODY_PLAIN)   # 正文无立场词
    idx_nobody = _index_item("i4", "会议纪要涉及延迟退休议题", "有代表表达反对意见")
    full_nobody = _full("i4", "会议纪要涉及延迟退休议题", "有代表表达反对意见", None)

    client = make_client(
        [{"items": [idx_meta, idx_nobody], "next_cursor": None, "has_more": False}],
        {"i3": full_meta, "i4": full_nobody},
    )
    report = pulse.ingest_spectrum_update(ISSUE, client=client)
    assert report["dropped_no_evidence"] == 2  # i3 反对质疑 + i4 反对质疑
    snap = pulse.get_spectrum(ISSUE)
    assert snap["spectrum"] == []              # 无证据 stance 全部丢弃
    assert snap["dropped_no_evidence"] == 2    # 行内累计
    for entry in snap["spectrum"]:
        assert entry["evidence"]


def test_ingest_skips_unrelated_items() -> None:
    idx = _index_item("i9", "关于集成电路产业规划的通报", "产业政策例行通报")
    full = _full("i9", "关于集成电路产业规划的通报", "产业政策例行通报", BODY_PLAIN)
    client = make_client([{"items": [idx], "next_cursor": None, "has_more": False}],
                         {"i9": full})
    report = pulse.ingest_spectrum_update(ISSUE, client=client)
    assert report["items_processed"] == 0 and report["items_skipped"] == 1
    assert pulse.get_spectrum(ISSUE)["spectrum"] == []


def test_withdrawn_item_evidence_removed() -> None:
    """撤稿条目：旧证据撤销，撤空的光谱条目不留。"""
    client1 = make_client([{"items": [dict(ITEM_A_IDX)],
                            "next_cursor": None, "has_more": False}],
                          {"i1": dict(ITEM_A)})
    pulse.ingest_spectrum_update(ISSUE, client=client1)
    assert pulse.get_spectrum(ISSUE)["spectrum"]

    withdrawn = _index_item("i1", "关于延迟退休改革方案的意见",
                            "明确支持渐进实施", withdrawn=True)
    client2 = make_client([{"items": [withdrawn], "next_cursor": None, "has_more": False}],
                          {"i1": dict(ITEM_A)})
    pulse.ingest_spectrum_update(ISSUE, client=client2)
    snap = pulse.get_spectrum(ISSUE)
    ids = {ev["item_id"] for e in snap["spectrum"] for ev in e["evidence"]}
    assert "i1" not in ids and snap["spectrum"] == []


# ---- 冻结降级：unreachable → frozen_reason 记录 → 解冻自动续拉 ----


def test_freeze_degrade_and_auto_resume() -> None:
    client1 = make_client([{"items": [dict(ITEM_A_IDX)],
                            "next_cursor": "cur-1", "has_more": False}],
                          {"i1": dict(ITEM_A)})
    pulse.ingest_spectrum_update(ISSUE, client=client1)
    snap = pulse.get_spectrum(ISSUE)
    assert snap["frozen"] is False and snap["spectrum"]

    def dead(tool, arguments, timeout):
        raise OSError("connection refused")

    client2 = InfoStreamClient(base_url="http://fd-info.test/mcp",
                               transport=dead, backoff_base=0.0)
    report = pulse.ingest_spectrum_update(ISSUE, client=client2)
    assert report["frozen"] is True
    assert report["frozen_reason"]["code"] == "unreachable"
    snap = pulse.get_spectrum(ISSUE)
    assert snap["frozen"] is True
    assert snap["frozen_reason"]["code"] == "unreachable"
    assert "connection refused" in snap["frozen_reason"]["detail"]
    assert snap["spectrum"], "冻结不清空快照，查询仍可读旧光谱"
    assert snap["last_updated_at"], "last_updated_at 如实保留"

    # 上游恢复 → 自动解冻续拉：游标断点续传（旧条目不重拉），旧证据不丢
    client3 = make_client([{"items": [dict(ITEM_B_IDX)],
                            "next_cursor": None, "has_more": False}],
                          {"i2": dict(ITEM_B)})
    report3 = pulse.ingest_spectrum_update(ISSUE, client=client3)
    assert report3["frozen"] is False and report3["frozen_reason"] is None
    snap3 = pulse.get_spectrum(ISSUE)
    assert snap3["frozen"] is False and snap3["frozen_reason"] is None
    labels = {e["stance_label"] for e in snap3["spectrum"]}
    assert labels == {"支持推进", "反对质疑", "谨慎观望"}
    ids = {ev["item_id"] for e in snap3["spectrum"] for ev in e["evidence"]}
    assert ids == {"i1", "i2"}   # 冻结期间未丢 i1，恢复后并入 i2


def test_freeze_on_unhealthy_political_source() -> None:
    """D7 健康度先行：cn-gov 不 ok → 冻结并带 last_error 原文，不取数。"""
    tr = ScriptedTransport([], {})
    tr.health = {"sources": [
        {"source": "cn-gov", "status": "unavailable",
         "last_error": "No module named 'fd_cn_gov'（11 部委逐条错误）"},
    ]}
    client = InfoStreamClient(base_url="http://fd-info.test/mcp",
                              transport=tr, backoff_base=0.0)
    report = pulse.ingest_spectrum_update(ISSUE, client=client)
    assert report["frozen"] is True
    assert report["frozen_reason"]["code"] == "source_unavailable"
    assert "fd_cn_gov" in report["frozen_reason"]["detail"]
    assert [t for t, _ in tr.calls] == ["stream_health"]  # 健康度之后零取数


# ---- LLM 可选增强 ----


def test_llm_enhancement_spans_verified(monkeypatch) -> None:
    """LLM 给的 span 同样过逐字校验：有效保留、越界丢弃计数；调用入台账。"""
    monkeypatch.setattr(llm, "is_configured", lambda: True)
    valid_end = BODY_LLM.index("。") + 1
    claims = [
        {"stance_label": "谨慎观望", "span": [0, valid_end]},
        {"stance_label": "支持推进", "span": [0, len(BODY_LLM) + 50]},  # 越界
    ]
    monkeypatch.setattr(llm, "chat_with_usage",
                        lambda messages, **kw: {"content": json.dumps(claims, ensure_ascii=False), "usage": None})

    idx = _index_item("i5", "某地关于延迟退休实施节奏的意见", "按年度评估")
    full = _full("i5", "某地关于延迟退休实施节奏的意见", "按年度评估", BODY_LLM)
    client = make_client([{"items": [idx], "next_cursor": None, "has_more": False}],
                         {"i5": full})
    report = pulse.ingest_spectrum_update(ISSUE, client=client)
    assert report["dropped_no_evidence"] == 1          # 越界 span 的断言被丢
    snap = pulse.get_spectrum(ISSUE)
    labels = {e["stance_label"] for e in snap["spectrum"]}
    assert labels == {"谨慎观望"}
    ev = snap["spectrum"][0]["evidence"][0]
    assert ev["span"] == [0, valid_end]
    assert ev["quote"] == BODY_LLM[0:valid_end]        # 逐字一致
    summary = llm_ledger.ledger_summary(surface="pulse")   # 增强调用入台账
    assert summary["calls"] == 1 and summary["ok"] == 1


def test_llm_enhancement_skipped_without_key() -> None:
    """无 KEY：增强跳过（零台账记录），确定性抽取照常出光谱。"""
    assert llm.is_configured() is False
    client = make_client([{"items": [dict(ITEM_A_IDX)],
                           "next_cursor": None, "has_more": False}],
                         {"i1": dict(ITEM_A)})
    report = pulse.ingest_spectrum_update(ISSUE, client=client)  # use_llm 默认开
    assert report["dropped_no_evidence"] == 0
    labels = {e["stance_label"] for e in pulse.get_spectrum(ISSUE)["spectrum"]}
    assert {"支持推进", "反对质疑"} <= labels
    assert llm_ledger.ledger_summary(surface="pulse")["calls"] == 0


# ---- 产物过 guard_output（当代议题光谱即当代议题产物） ----


def test_spectrum_guard_balanced_and_unbalanced() -> None:
    client = make_client([{"items": [dict(ITEM_A_IDX), dict(ITEM_B_IDX)],
                           "next_cursor": None, "has_more": False}],
                         {"i1": dict(ITEM_A), "i2": dict(ITEM_B)})
    pulse.ingest_spectrum_update(ISSUE, client=client)
    snap = pulse.get_spectrum_guarded(ISSUE)
    assert snap["guard"]["action"] is None and snap["guard"]["reasons"] == []
    assert snap["guard"]["ruleset_version"]  # 裁决带规则库版本

    # 单立场光谱：当代议题平衡检查 → block（R-BAL-001），不得原样呈现
    body_single = "文件表示支持弹性实施方案。"
    idx = _index_item("i6", "关于延迟退休弹性实施的通知", "支持弹性实施")
    full = _full("i6", "关于延迟退休弹性实施的通知", "支持弹性实施", body_single)
    client2 = make_client([{"items": [idx], "next_cursor": None, "has_more": False}],
                          {"i6": full})
    pulse.ingest_spectrum_update("弹性实施", client=client2)
    snap2 = pulse.get_spectrum_guarded("弹性实施")
    assert snap2["guard"]["action"] == "block"
    assert any(r["rule_id"] == "R-BAL-001" for r in snap2["guard"]["reasons"])


# ---- API 查询面（/api/v1/pulse，带 auth 依赖） ----


def test_pulse_api_auth_404_and_200() -> None:
    quote = urllib.parse.quote(ISSUE)
    r = api.get(f"/api/v1/pulse/spectrum/{quote}")
    assert r.status_code == 401                       # 未授权拒绝

    r2 = api.get("/api/v1/pulse/spectrum/%E6%9C%AA%E7%9F%A5%E8%AE%AE%E9%A2%98",
                 headers=AUTH)
    assert r2.status_code == 404                       # 未知议题

    client = make_client([{"items": [dict(ITEM_A_IDX)],
                           "next_cursor": None, "has_more": False}],
                         {"i1": dict(ITEM_A)})
    pulse.ingest_spectrum_update(ISSUE, client=client)
    r3 = api.get(f"/api/v1/pulse/spectrum/{quote}", headers=AUTH)
    assert r3.status_code == 200
    body = r3.json()
    assert body["issue"] == ISSUE and body["spectrum"]
    assert body["frozen"] is False and "guard" in body
    for entry in body["spectrum"]:
        for ev in entry["evidence"]:
            assert len(ev["span"]) == 2 and ev["quote"]   # 证据随查询可回溯
