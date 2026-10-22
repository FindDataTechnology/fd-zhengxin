"""跨仓契约测试（D2 修复的防漂移闸）：参照系真实工具返回样例 → 主服务解析层。

fixture 取自 fd-zhengxin-reference 真实服务（tests/fixtures/reference_mcp_
contract.json，抓取方式见其 _meta）：种子概念行/近现代思潮行/空结果口径/
种子关系行/思潮坐标行——把这些 payload 经注入 transport 喂给三闭环的解析
与消费层，断言字段映射（ref=provenance.stable_id、school/figures/weight、
target_name/target_years/key_text）与端到端产出（带源定位/带源视角/谱系链
或如实缺口）。参照侧行结构变更而 fixture 未更新时，本文件先红——两仓契约
不再静默漂移。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fd_zhengxin_api import genealogy, locate, perspectives, reference_mcp
from fd_zhengxin_api.reference_mcp import ReferenceClient

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "reference_mcp_contract.json").read_text("utf-8")
)

app = FastAPI()
app.include_router(locate.router)
app.include_router(perspectives.router)
app.include_router(genealogy.router)
client = TestClient(app)
AUTH = {"Authorization": "Bearer dev-tok-alice"}

BASE_URL = "http://reference.test/mcp"

# fixture 键 → (tool, 排序参数对) 路由：真实服务同参同返回
def _args_key(arguments: dict) -> tuple:
    return tuple(sorted(
        (k, tuple(v) if isinstance(v, list) else v) for k, v in arguments.items()
    ))


CONTRACT_ROUTES = {
    ("concept_lookup", _args_key({"term": "仁政"})): FIXTURE["concept_lookup_renzheng"],
    ("concept_lookup", _args_key({"term": "自由主义思潮"})): FIXTURE["concept_lookup_liberalism"],
    ("concept_lookup", _args_key({"term": "螺旋史学本体论"})): FIXTURE["concept_lookup_miss"],
    ("concept_lookup", _args_key({"term": "忠恕"})): FIXTURE["concept_lookup_zhongshu"],
    ("relation_query", _args_key({"source": "孔子", "target": "曾参（曾子）"})):
        FIXTURE["relation_query_kongzi_zengzi"],
    ("relation_query", _args_key({"source": "孟轲（孟子）", "target": "孔伋（子思）"})):
        FIXTURE["relation_query_mengzi_sikong"],
    ("ideology_query", _args_key({"name": "Q42"})): FIXTURE["ideology_query_not_ingested"],
    ("source_status", _args_key({})): FIXTURE["source_status"],
}


def _contract_transport(tool: str, arguments: dict, timeout: float) -> dict:
    payload = CONTRACT_ROUTES.get((tool, _args_key(arguments)))
    if payload is not None:
        return payload
    return {"results": [], "reason": "not_ingested",
            "hint": f"契约 fixture 未路由 {tool} {arguments}"}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FDZX__DB", str(tmp_path / "personal.db"))
    monkeypatch.setenv("FDZX__LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("FDZX__AUTH_DEV_TOKENS", "dev-tok-alice=alice")
    monkeypatch.setenv("FDZX__REFERENCE_BASE_URL", BASE_URL)
    monkeypatch.delenv("FDZX__LLM_API_KEY", raising=False)
    monkeypatch.setattr(
        reference_mcp, "client",
        lambda: ReferenceClient(base_url=BASE_URL, transport=_contract_transport),
    )


# ---------------------------------------------------------------- 行结构契约


def test_fixture_row_structure_anchors_parse() -> None:
    """种子概念行/近现代思潮行的字段映射（locate.anchor_from_row 消费面）。"""
    seed_row = FIXTURE["concept_lookup_renzheng"]["results"][0]
    a = locate.anchor_from_row(seed_row, "仁政")
    assert a["ref"] == "china-seed:concept:仁政"        # id 锚 = stable_id（D2）
    assert a["school"] == "儒家" and a["weight"] == 1.0  # curated 直挂
    assert [f["name"] for f in a["figures"]] == ["孟轲（孟子）"]
    assert "不忍人之政" in a["definition"]               # snippet → definition
    assert a["url"].startswith("https://zh.wikipedia.org/wiki/")

    modern_row = FIXTURE["concept_lookup_liberalism"]["results"][0]
    b = locate.anchor_from_row(modern_row, "自由主义思潮")
    assert b["ref"] == "modern:china/current/liberalism"
    assert b["term"] == "自由主义思潮"                    # label → term
    assert b["school"] == "自由主义思潮" and b["weight"] == 1.0
    assert "人权论战" in b["definition"]                 # summary → definition


def test_fixture_row_structure_edge_parses() -> None:
    """种子关系行的字段映射（locate._norm_edge 消费面）+ 年代串保守解析。"""
    edge_row = FIXTURE["relation_query_kongzi_zengzi"]["results"][0]
    e = locate._norm_edge(edge_row, None)
    assert e["id"].startswith("china-seed:relation:seed:china/thinker/kongzi>")
    assert e["target_name"] == "曾参（曾子）" and e["school"] == "儒家"
    assert e["polarity"] == "positive" and e["weight"] == 1.0
    assert e["key_text"].startswith("「参乎")
    assert e["year"] == -505                              # 约前505–前435 → -505


def test_fixture_miss_reason_is_taxonomy() -> None:
    """空结果口径：断网形态下 miss 行 reason=unreachable（taxonomy 内），
    客户端归一为结构化降级而非静默。"""
    c = ReferenceClient(base_url=BASE_URL, transport=_contract_transport)
    r = c.concept_lookup("螺旋史学本体论")
    assert not r.ok and r.reason == "unreachable"
    assert "螺旋史学本体论" in r.detail
    # source_status 台账与思潮坐标行也是真实形态
    assert c.status().ok
    iq = c.ideology_query("Q42")
    assert not iq.ok and iq.reason == "not_ingested"


# ---------------------------------------------------------------- 端到端（三闭环吃真实契约）


def test_contract_locate_renzheng_sourced_attribution() -> None:
    """locate 仁政：概念行直挂归派儒家，依据=概念行 stable_id（带源结论）。"""
    r = client.post("/api/v1/loops/locate", json={"text": "仁政"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["positioned"] is True
    assert [a["school"] for a in out["attributions"]] == ["儒家"]
    assert out["attributions"][0]["evidence"] == ["china-seed:concept:仁政"]
    assert out["anchors"][0]["ref"] == "china-seed:concept:仁政"
    assert out["engine"] == "deterministic"


def test_contract_locate_modern_current() -> None:
    """locate 自由主义思潮：近现代思潮行直挂定位（modern-index 锚）。"""
    r = client.post("/api/v1/loops/locate", json={"text": "自由主义思潮"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["positioned"] is True
    assert [a["school"] for a in out["attributions"]] == ["自由主义思潮"]
    assert out["attributions"][0]["evidence"] == ["modern:china/current/liberalism"]
    assert out["anchors"][0]["source"] == "modern-index"


def test_contract_locate_fabricated_term_not_positioned() -> None:
    """生造词：词表不命中/参照系 miss → positioned=false，缺口可见不编造。"""
    r = client.post("/api/v1/loops/locate", json={"text": "螺旋史学本体论"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["positioned"] is False and out["attributions"] == []


def test_contract_perspectives_sourced_view() -> None:
    """perspectives 先秦概念：概念行直挂学派出带源视角（views≥1，锚=stable_id）。"""
    r = client.post("/api/v1/loops/perspectives", json={"topic": "仁政"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert len(out["views"]) >= 1
    v = out["views"][0]
    assert v["school"] == "儒家" and v["form"] == "sourced-stance"
    assert v["anchors"] == ["china-seed:concept:仁政"]


def test_contract_genealogy_chain_from_seed_edges() -> None:
    """genealogy 忠恕：概念人物对（孔子×曾参）种子边 → 年代链（-505）。"""
    r = client.post("/api/v1/loops/genealogy", json={"term": "忠恕"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["anchor"]["ref"] == "china-seed:concept:忠恕"
    assert [(ring["name"], ring["year"]) for ring in out["chain"]] == [("曾参（曾子）", -505)]
    assert all(ring["key_text"] for ring in out["chain"])
    assert out["chain"][0]["ref"].startswith("china-seed:relation:seed:china/thinker/kongzi>")


def test_contract_genealogy_single_figure_honest_gap() -> None:
    """genealogy 仁政：单人物概念锚无对可配 → 空链 + no_pair_to_query 缺口（如实）。"""
    r = client.post("/api/v1/loops/genealogy", json={"term": "仁政"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["chain"] == []
    assert any(g["reason"] == "no_pair_to_query" and "孟轲（孟子）" in g["detail"]
               for g in out["gaps"])
