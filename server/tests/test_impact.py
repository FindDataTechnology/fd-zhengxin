"""影响面环：作用面逐字抽取 / 名义方向三值 / 待检验清单 tested 恒空 / 处境交叠三值。

口径线（本文件守的是「不越权」那一侧）：
- 每个要素的 quote 必须与正文逐字一致，不一致即丢弃（宁删勿假）；
- `tested` 递归断言恒 None——正心自己永不填传导结论；
- 处境交叠只做字面比较，不推断上下游；
- 产出正文不得出现利害裁定（由 guard R-RECO-003 / R-PREDICT-003 兜底）。

全离线：fd-info 走 ScriptedTransport 桩（照 test_pulse 口径），参照系走
假 RefResult（entity 展开面另测）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fd_zhengxin_api import impact, reference_mcp
from fd_zhengxin_api.infostream_client import InfoStreamClient
from fd_zhengxin_api.main import app

client = TestClient(app)
AUTH = {"Authorization": "Bearer dev-tok-alice"}

BODY = (
    "文号：财政部令第110号  发文机关：财政部  成文日期：2022-01-14  文种：命令\n"
    "《政府采购框架协议采购方式管理暂行办法》已经部务会议审议通过，现予公布，"
    "自2022年3月1日起施行。\n"
    "第一条 为了规范多频次、小额度采购活动。\n"
    "各省、自治区、直辖市人民政府，国务院各部委、各直属机构：\n"
    "本办法适用于各级国家机关、事业单位和团体组织。\n"
    "第六条 禁止以不合理条件限制或者排斥潜在供应商。\n"
)


class FakeTransport:
    """fd-info 桩：按工具名回放。记录调用，供只读性断言。"""

    def __init__(self, items: dict, bodies: dict, health: dict | None = None):
        self.items = items
        self.bodies = bodies
        self.health = health or {"sources": [{"source": "cn-gov", "status": "ok"}]}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool: str, arguments: dict, timeout: float) -> dict:
        self.calls.append((tool, arguments))
        if tool == "stream_health":
            return self.health
        if tool == "get_item":
            item = self.items.get(arguments["item_id"])
            return item if item else {"error": "not found"}
        if tool == "get_body":
            body = self.bodies.get(arguments["doc_id"])
            if body is None:
                return {"ok": False, "error": "missing"}
            return {"ok": True, "body": body}
        if tool == "search_info":
            return {"items": [], "count": 0, "mode": "index"}
        raise AssertionError(f"非预期工具调用：{tool}")


def _fake_client(transport) -> InfoStreamClient:
    return InfoStreamClient(base_url="http://fd-info.test/mcp",
                            transport=transport, backoff_base=0.0)


def _item(item_id="i1", doc_id="cn-gov:x", status="stored") -> dict:
    return {
        "item_id": item_id, "doc_id": doc_id, "title": "政府采购框架协议采购方式管理暂行办法",
        "published_at": "2022-01-14T00:00:00Z", "body_status": status,
        "source": "cn-gov", "source_type": "ministry",
    }


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FDZX__DB", str(tmp_path / "personal.db"))
    monkeypatch.setenv("FDZX__AUTH_DEV_TOKENS", "dev-tok-alice=alice")
    monkeypatch.delenv("FDZX__INFOSTREAM_BASE_URL", raising=False)
    monkeypatch.delenv("FDZX__REFERENCE_BASE_URL", raising=False)


def _patch_infostream(monkeypatch, transport) -> None:
    monkeypatch.setattr(impact, "InfoStreamClient", lambda: _fake_client(transport))


# ---------------------------------------------------------------- L1 要素抽取（纯函数）

def test_extract_elements_from_real_shaped_body() -> None:
    els = impact.extract_elements(BODY)
    by = {e["element"]: e for e in els}
    assert by["发文机关"]["value"] == "财政部"
    assert by["文号"]["value"] == "财政部令第110号"
    assert by["成文日期"]["value"] == "2022-01-14"
    assert by["生效日期"]["value"] == "2022-03-01"
    for e in els:
        assert BODY[e["span"][0]:e["span"][1]] == e["quote"]


def test_verify_elements_drops_mismatched_quote() -> None:
    els = [{"element": "发文机关", "value": "财政部", "span": [0, 5], "quote": "假的引文"}]
    kept, dropped = impact.verify_elements(els, BODY)
    assert kept == []
    assert dropped[0]["reason"] == "quote_mismatch"


def test_elements_absent_are_not_fabricated() -> None:
    """正文没有的要素不出现——不是空值字段，是它根本不在这份文件里。"""
    els = impact.extract_elements("这是一段没有任何政策要素的普通文字。")
    assert els == []


# ---------------------------------------------------------------- L2 方向三值

def test_direction_three_valued() -> None:
    assert impact.infer_direction("第六条 禁止以不合理条件限制供应商")[0] == "收紧"
    assert impact.infer_direction("对符合条件的企业免征增值税")[0] == "放宽"


def test_direction_undetermined_is_not_neutral() -> None:
    """无措辞依据 = undetermined，不是「中性」（那是判断，这是判断不了）。"""
    d, hits = impact.infer_direction("本办法自发布之日起施行。")
    assert d == impact.DIRECTION_UNDETERMINED
    assert hits == []


def test_direction_neutral_when_both_present() -> None:
    d, _ = impact.infer_direction("既要严格监管，也要鼓励创新")
    assert d == "中性"


# ---------------------------------------------------------------- L3 tested 恒空（核心）

def _walk_tested(obj):
    out = []
    if isinstance(obj, dict):
        if "tested" in obj:
            out.append(obj["tested"])
        for v in obj.values():
            out += _walk_tested(v)
    elif isinstance(obj, list):
        for it in obj:
            out += _walk_tested(it)
    return out


def test_transmission_test_candidates_tested_always_none(monkeypatch) -> None:
    """递归断言：产出里每个 tested 都是 None——正心永不自己填传导结论。

    这条测试的意义是：将来有人想「顺手接一下柏讯」，这里先红。
    """
    tr = FakeTransport({"i1": _item()}, {"cn-gov:x": BODY})
    _patch_infostream(monkeypatch, tr)

    r = client.post("/api/v1/loops/impact",
                    json={"target_kind": "claim", "target_ref": "i1"}, headers=AUTH)
    body = r.json()
    tested_values = _walk_tested(body)
    assert tested_values, "产出里应有 transmission_candidates"
    assert all(v is None for v in tested_values), tested_values


def test_transmission_candidate_shape() -> None:
    cand = impact.transmission_candidate(
        {"element": "发文机关", "value": "财政部", "span": [0, 5], "quote": "财政部令"}
    )
    assert cand["tested"] is None
    assert cand["evidence"]["quote"] == "财政部令"


# ---------------------------------------------------------------- L4 处境交叠三值

def test_overlap_hit_and_no_overlap() -> None:
    els = [{"element": "适用范围", "value": "新能源汽车整车制造"},
           {"element": "适用范围", "value": "煤炭开采"}]
    sit = [{"kind": "industry", "value": "新能源汽车"}]
    out = impact.situation_overlap(els, sit)
    assert out[0]["relevance"] == "hit"          # 互为子串
    assert out[1]["relevance"] == "no_literal_overlap"


def test_overlap_undetermined_without_situation() -> None:
    els = [{"element": "适用范围", "value": "X"}]
    out = impact.situation_overlap(els, [])
    assert out[0]["relevance"] == "undetermined"


def test_overlap_never_says_weakly_related() -> None:
    els = [{"element": "适用范围", "value": "上游锂矿"}]
    sit = [{"kind": "industry", "value": "动力电池"}]
    out = impact.situation_overlap(els, sit)
    text = str(out)
    for banned in ("弱相关", "可能相关", "潜在相关", "间接相关"):
        assert banned not in text


# ---------------------------------------------------------------- 端到端

def test_impact_end_to_end_with_body(monkeypatch) -> None:
    tr = FakeTransport({"i1": _item()}, {"cn-gov:x": BODY})
    _patch_infostream(monkeypatch, tr)
    client.post("/api/v1/situation",
                json={"kind": "industry", "value": "政府采购"}, headers=AUTH)

    r = client.post("/api/v1/loops/impact",
                    json={"target_kind": "claim", "target_ref": "i1"}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["claims"][0]["body_available"] is True
    assert body["claims"][0]["elements"], "应从正文抽出要素"
    assert body["situation_overlap"]
    assert body["guard"]["action"] in (None, "grey")
    assert "不裁定" in body["notice"]
    # 只读性：不出现任何写方法
    assert {c[0] for c in tr.calls} <= {
        "stream_health", "get_item", "get_body", "search_info"}


def test_body_unavailable_is_reported_not_failed(monkeypatch) -> None:
    """正文不可得按 body_status 原值记 gap（no_text/oversize/failed 语义不同）。"""
    tr = FakeTransport({"i1": _item(status="no_text")}, {})
    _patch_infostream(monkeypatch, tr)
    r = client.post("/api/v1/loops/impact",
                    json={"target_kind": "claim", "target_ref": "i1"}, headers=AUTH)
    body = r.json()
    assert body["claims"][0]["body_available"] is False
    gap = next(g for g in body["gaps"] if g["stage"] == "body")
    assert gap["body_unavailable"] == "no_text"


def test_upstream_unconfigured_degrades_with_reason(monkeypatch) -> None:
    """fd-info 未配置：冻结降级并带结构化原因，不静默空转、不误判为失衡。"""
    r = client.post("/api/v1/loops/impact",
                    json={"target_kind": "claim", "target_ref": "i1"}, headers=AUTH)
    assert r.status_code == 200, "上游不可用不该被平衡规则判成 422"
    body = r.json()
    assert any(g["reason"] == "not_configured" for g in body["gaps"])


def test_guard_blocks_benefit_language(monkeypatch) -> None:
    """把利害裁定塞进正文 → R-RECO-003 拦（不呈现）。"""
    poisoned = BODY + "\n这份文件对你有利，你应当据此调整。\n"
    tr = FakeTransport({"i1": _item()}, {"cn-gov:x": poisoned})
    _patch_infostream(monkeypatch, tr)
    r = client.post("/api/v1/loops/impact",
                    json={"target_kind": "claim", "target_ref": "i1"}, headers=AUTH)
    # 正文只取要素不整段转述，所以这里验的是规则本身在 impact payload 上生效
    from fd_zhengxin_api.guard import Action, guard_output

    verdict = guard_output({
        "payload_kind": "impact", "topic": "x", "is_contemporary": True,
        "claims": [], "elements": [{"target": "a", "direction": "收紧"},
                                   {"target": "b", "direction": "放宽"}],
        "views": [], "body": "这份文件对你有利，你应当据此调整。",
    })
    assert verdict.action is Action.BLOCK
    assert any(x.rule_id == "R-RECO-003" for x in verdict.reasons)


def test_impact_unauthorized_401() -> None:
    r = client.post("/api/v1/loops/impact",
                    json={"target_kind": "claim", "target_ref": "i1"})
    assert r.status_code == 401


def test_invalid_target_kind_422() -> None:
    r = client.post("/api/v1/loops/impact",
                    json={"target_kind": "person", "target_ref": "x"}, headers=AUTH)
    assert r.status_code == 422


# ---------------------------------------------------------------- HTML 正文（实测缺陷回归）

# 实测：fd-info 的 get_body 对 ministry 条目吐原始 HTML（spool 95/95 条如此）
HTML_BODY = (
    '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>x</title>'
    '<script>var a=1;</script><style>.a{color:red}</style></head><body>'
    '<div class="nav">首页 机构 公开 服务 文号 机构 正文 全部</div>'
    '<p>发文机关：财政部</p><p>文  号：财政部令第110号</p>'
    '<p>成文日期：2022-01-14</p>'
    '<p>自2022年3月1日起施行。</p>'
    '<p>第六条 禁止以不合理条件限制供应商。</p>'
    '</body></html>'
)


def test_strip_html_removes_tags_and_scripts() -> None:
    text = impact._strip_html(HTML_BODY)
    assert "<" not in text and ">" not in text
    assert "var a=1" not in text
    assert "财政部令第110号" in text


def test_nav_bar_noise_not_extracted_as_elements() -> None:
    """导航栏里的「文号」「机构」不得被当成政策要素——比抽不出更糟。"""
    els = impact.extract_elements(impact._strip_html(HTML_BODY))
    values = {e["element"]: e["value"] for e in els}
    assert values.get("发文机关") == "财政部"
    assert values.get("文号") == "财政部令第110号"
    assert values.get("成文日期") == "2022-01-14"
    assert all(v not in impact._NAV_NOISE for v in values.values())


def test_end_to_end_with_html_body(monkeypatch) -> None:
    """端到端：get_body 吐 HTML 时链路仍能抽出并逐字校验要素。"""
    tr = FakeTransport({"i1": _item()}, {"cn-gov:x": HTML_BODY})
    _patch_infostream(monkeypatch, tr)
    r = client.post("/api/v1/loops/impact",
                    json={"target_kind": "claim", "target_ref": "i1"}, headers=AUTH)
    assert r.status_code == 200, r.text
    els = r.json()["claims"][0]["elements"]
    assert any(e["element"] == "发文机关" for e in els)
    assert any(e["element"] == "文号" for e in els)


# ---------------------------------------------------------------- 实体 → claim 展开（缺口②）

class _FakeRef:
    """参照系桩：entity_dossier 返回可查实体。"""

    def __init__(self, coverage="available", label="习近平", ref="Q15031"):
        self._cov, self._label, self._ref = coverage, label, ref

    def entity_dossier(self, ref):
        from fd_zhengxin_api.reference_mcp import RefResult
        if self._cov != "available":
            return RefResult(False, self._cov, "实体面不可用")
        return RefResult(True, data={
            "identity": {"ref": self._ref, "label": self._label, "kind": "politician"},
            "coverage": {"status": "available", "detail": None},
            "relations": {"out": [], "in": []}, "relation_count": 0,
        })


class _SearchStub:
    def __init__(self, hits: dict, mode="text-core"):
        self.hits, self.mode = hits, mode
        self.calls = []

    def search_info(self, query, *, source_types=None, limit=None):
        self.calls.append((query, source_types))
        items = self.hits.get(query, [])
        return {"items": items, "count": len(items), "mode": self.mode}


def test_entity_expansion_uses_search_info() -> None:
    stub = _SearchStub({"习近平": [{"item_id": "abc"}, {"item_id": "def"}]})
    ids, cov = impact._claims_for_entity(_FakeRef(), "习近平", stub)
    assert ids == ["abc", "def"]
    assert cov["match_mode"] == "text-core"
    assert cov["status"] == "available"
    assert stub.calls[0][1] == ["ministry"]


def test_entity_expansion_unknown_entity_does_not_search() -> None:
    stub = _SearchStub({})
    ids, cov = impact._claims_for_entity(_FakeRef(coverage="not_ingested"), "X", stub)
    assert ids == []
    assert cov["status"] == "not_ingested"
    assert stub.calls == []


def test_text_core_zero_hits_is_flagged_not_silent() -> None:
    stub = _SearchStub({}, mode="text-core")
    ids, cov = impact._claims_for_entity(_FakeRef(), "习近平", stub)
    assert ids == []
    assert "不可当真无命中" in cov["detail"]


def test_entity_expansion_without_client_degrades() -> None:
    ids, cov = impact._claims_for_entity(_FakeRef(), "习近平", None)
    assert ids == [] and cov["match_mode"] == "none"


# ---------------------------------------------------------------- 判断层接入（change ai-judgment）

def _impact(monkeypatch, body_=BODY, prefs=None):
    tr = FakeTransport({"i1": _item()}, {"cn-gov:x": body_})
    _patch_infostream(monkeypatch, tr)
    if prefs:
        client.put("/api/v1/judgment/prefs", json=prefs, headers=AUTH)
    return client.post("/api/v1/loops/impact",
                       json={"target_kind": "claim", "target_ref": "i1"},
                       headers=AUTH).json()


def test_judgment_block_present_default_all_on(monkeypatch) -> None:
    """默认全开：judgment 块三层都在；统计层因无数据为 unavailable。"""
    j = _impact(monkeypatch)
    assert "judgment" in j
    layers = j["judgment"]["layers"]
    assert set(layers) == {"statistical", "means", "personal"}
    assert layers["statistical"]["status"] == "unavailable"
    assert layers["personal"]["situation"] == "absent"


def test_disabling_judgment_leaves_material_untouched(monkeypatch) -> None:
    """层全关 → 材料层字段逐字节不变（只允许 judgment 不同）。"""
    on = _impact(monkeypatch)
    client.put("/api/v1/judgment/prefs",
               json={"statistical": False, "means": False, "personal": False},
               headers=AUTH)
    off = _impact(monkeypatch)

    for k in ("claims", "transmission_candidates", "situation_overlap",
              "match_mode", "coverage", "target", "engine", "notice"):
        assert on[k] == off[k], f"材料层字段 {k} 被判断开关影响了"

    off_layers = off["judgment"]["layers"]
    for name in ("statistical", "means", "personal"):
        assert off_layers[name]["status"] == "disabled"
        assert off_layers[name]["items"] == []


def test_judgment_situation_declared_changes_personal_layer(monkeypatch) -> None:
    """声明处境后，对我判断从「通用判断」变成字面交叠判断。"""
    client.post("/api/v1/situation",
                json={"kind": "industry", "value": "新能源汽车"}, headers=AUTH)
    j = _impact(monkeypatch)
    personal_layer = j["judgment"]["layers"]["personal"]
    assert personal_layer["situation"] == "declared"
    assert personal_layer["status"] == "ok"
    # 每个要素各一条判断：发文机关「财政部」与「新能源汽车」不相交是**正确**的，
    # 断言的是「不再出现『这是通用判断』」——即处境声明确实参与了判断。
    concl = [it["conclusion"] for it in personal_layer["items"]]
    assert all("不是对你个人的判断" not in c for c in concl)
    assert any("字面" in c for c in concl)


# ---------------------------------------------------------------- 要素假阳性（浏览器走查撞到）

def test_element_equal_to_title_is_dropped() -> None:
    """要素值 == 条目标题 → 不是要素（实测：会议报道里「文号」抽到了标题行）。"""
    body = "文号：某某会议精神\n各省、自治区人民政府：\n"
    els = impact.extract_elements(body, title="某某会议精神")
    assert all(e["value"] != "某某会议精神" for e in els)


def test_docno_without_number_marker_is_dropped() -> None:
    """文号必须长得像文号（含「号」或〔〕）；否则是正文里的普通句子。"""
    body = "文号  国家外汇管理局党组传达学习党的二十届五中全会精神\n"
    assert impact.extract_elements(body) == []
    body2 = "文号：自然资发〔2026〕147号\n"
    els = impact.extract_elements(body2)
    assert els and els[0]["element"] == "文号"
    assert "147" in els[0]["value"]
