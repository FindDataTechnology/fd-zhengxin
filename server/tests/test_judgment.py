"""AI 判断三层 + 判断信封 + 判断通道守卫。

口径线（本文件守的是「判断必须可追溯、可反驳」）：
- 信封缺 may_be_wrong / would_overturn / evidence → 不产出（不是补默认值）；
- 统计判断无数据 → unavailable，且**不产生任何数值**（不用 LLM 估计冒充）；
- 处境为空 → 通用判断，且结论里必须写明「不是对你个人的判断」（默认全开 ×
  处境默认空的必然形态，不能静默冒充个人判断）；
- 判断通道仍受群体倡导禁令约束，与开关状态无关。
"""

from __future__ import annotations

import pytest

from fd_zhengxin_api import judgment as J


ELEM = {"item_id": "i1", "value": "新能源汽车整车制造",
        "quote": "适用于新能源汽车整车制造企业", "element": "适用对象"}
SIT = [{"id": "s1", "kind": "industry", "value": "新能源汽车"}]


# ---------------------------------------------------------------- 信封准入

def test_missing_may_be_wrong_rejected() -> None:
    j = J.Judgment(conclusion="x", evidence=[{"ref": "r"}], confidence="low",
                   may_be_wrong="", would_overturn="y", layer="personal")
    with pytest.raises(J.JudgmentRejected, match="我可能错在哪"):
        J.validate_judgment(j)


def test_missing_would_overturn_rejected() -> None:
    j = J.Judgment(conclusion="x", evidence=[{"ref": "r"}], confidence="low",
                   may_be_wrong="m", would_overturn="  ", layer="personal")
    with pytest.raises(J.JudgmentRejected, match="什么会推翻它"):
        J.validate_judgment(j)


def test_empty_evidence_rejected() -> None:
    j = J.Judgment(conclusion="x", evidence=[], confidence="low",
                   may_be_wrong="m", would_overturn="w", layer="means")
    with pytest.raises(J.JudgmentRejected, match="依据链"):
        J.validate_judgment(j)


def test_evidence_without_ref_rejected() -> None:
    j = J.Judgment(conclusion="x", evidence=[{"note": "无 ref"}], confidence="low",
                   may_be_wrong="m", would_overturn="w", layer="means")
    with pytest.raises(J.JudgmentRejected, match="缺 ref"):
        J.validate_judgment(j)


def test_confidence_closed_set() -> None:
    j = J.Judgment(conclusion="x", evidence=[{"ref": "r"}], confidence="0.8",
                   may_be_wrong="m", would_overturn="w", layer="means")
    with pytest.raises(J.JudgmentRejected, match="置信度"):
        J.validate_judgment(j)


# ---------------------------------------------------------------- 统计层

def test_statistical_unavailable_has_no_numeric_output() -> None:
    """统计判断无数据 → unavailable，且产出里没有任何数值字段。"""
    layer = J.build_statistical_layer(enabled=True, series_available=False)
    assert layer["status"] == J.STATUS_UNAVAILABLE
    assert layer["items"] == []
    assert "序列" in layer["why"]
    # 不含任何数值键（防止有人加个 estimated_effect 回去）
    assert not any(isinstance(v, (int, float)) for v in layer.values())


def test_statistical_disabled_is_not_unavailable() -> None:
    """禁用 ≠ 能力缺失：两者必须可区分。"""
    assert J.build_statistical_layer(enabled=False)["status"] == J.STATUS_DISABLED
    assert J.build_statistical_layer(enabled=True, series_available=False)["status"] \
        == J.STATUS_UNAVAILABLE


# ---------------------------------------------------------------- 对我判断层

def test_personal_layer_without_situation_is_generic_and_says_so() -> None:
    """默认全开 × 处境默认空：给通用判断，且明说不是个人判断。"""
    layer = J.build_personal_layer(enabled=True, elements=[ELEM], situation=[])
    assert layer["situation"] == "absent"
    assert layer["status"] == J.STATUS_OK
    concl = layer["items"][0]["conclusion"]
    assert "不是对你个人的判断" in concl
    assert layer["items"][0]["confidence"] == "low"


def test_personal_layer_with_situation_hit() -> None:
    layer = J.build_personal_layer(enabled=True, elements=[ELEM], situation=SIT)
    assert layer["situation"] == "declared"
    item = layer["items"][0]
    assert "字面相交" in item["conclusion"]
    refs = [e["ref"] for e in item["evidence"]]
    assert any(r.startswith("situation:") for r in refs)


def test_personal_layer_no_literal_overlap() -> None:
    layer = J.build_personal_layer(
        enabled=True, elements=[ELEM],
        situation=[{"id": "s2", "kind": "region", "value": "广东省"}])
    assert "字面不相交" in layer["items"][0]["conclusion"]


def test_personal_layer_disabled() -> None:
    layer = J.build_personal_layer(enabled=False, elements=[ELEM], situation=SIT)
    assert layer == {"status": J.STATUS_DISABLED, "items": []}


# ---------------------------------------------------------------- 手段判断层

def test_means_layer_needs_goal_and_options() -> None:
    assert J.build_means_layer(enabled=True, goal=None)["status"] == J.STATUS_UNAVAILABLE
    assert J.build_means_layer(enabled=True, goal="X", options=[])["status"] \
        == J.STATUS_UNAVAILABLE


def test_means_layer_produces_envelope() -> None:
    layer = J.build_means_layer(
        enabled=True, goal="扩大出口",
        options=[{"ref": "item:a", "label": "路径A", "note": "依据A"}])
    assert layer["status"] == J.STATUS_OK
    item = layer["items"][0]
    for k in ("conclusion", "evidence", "confidence", "may_be_wrong",
              "would_overturn", "layer"):
        assert item.get(k), f"信封缺 {k}"


def test_means_layer_drops_option_without_ref() -> None:
    layer = J.build_means_layer(
        enabled=True, goal="X", options=[{"label": "无 ref 的路径"}])
    assert layer["items"] == []
    assert layer["status"] == J.STATUS_UNAVAILABLE


# ---------------------------------------------------------------- 判断通道守卫

def _good_item(**over) -> dict:
    item = {
        "conclusion": "这份文件对某类主体是收紧方向。",
        "evidence": [{"ref": "item:i1", "quote": "禁止…"}],
        "confidence": "medium",
        "may_be_wrong": "字面同义不等于同对象",
        "would_overturn": "另有条款点名相反方向",
        "layer": "personal",
    }
    item.update(over)
    return item


def _block(items: list[dict], layer="personal") -> dict:
    return {"enabled": True, "layers": {layer: {"status": "ok", "items": items,
                                                 "situation": "declared"}}}


def test_gate_lets_good_judgment_through() -> None:
    b = _block([_good_item()])
    v = J.judgment_gate(b)
    assert v.ok and v.reasons == []
    assert len(b["layers"]["personal"]["items"]) == 1


def test_gate_drops_item_missing_envelope_field() -> None:
    b = _block([_good_item(may_be_wrong="")])
    v = J.judgment_gate(b)
    assert not v.ok
    assert v.reasons[0]["rule_id"] == "J-ENVELOPE-001"
    assert b["layers"]["personal"]["items"] == []
    assert b["layers"]["personal"]["status"] == J.STATUS_UNAVAILABLE


def test_gate_drops_item_with_unrefed_evidence() -> None:
    b = _block([_good_item(evidence=[{"note": "无 ref"}])])
    v = J.judgment_gate(b)
    assert v.reasons[0]["rule_id"] == "J-EVIDENCE-001"


def test_gate_blocks_group_advocacy_regardless_of_switch() -> None:
    """群体倡导在判断通道仍被拦——与开关状态无关。"""
    b = _block([_good_item(conclusion="我们应当共同抵制这项政策。")])
    v = J.judgment_gate(b)
    assert not v.ok
    assert v.reasons[0]["rule_id"] == "J-ADVOCACY-001"


@pytest.mark.parametrize("sentence", [
    "我们应当反对", "我们必须支持", "必须捍卫这项权利",
    "共同抵制", "号召全体行动起来",
])
def test_group_advocacy_detector_covers_phrases(sentence: str) -> None:
    assert J.has_group_advocacy(sentence) is not None


def test_personal_judgment_is_not_group_advocacy() -> None:
    """面向个人的建议不算群体倡导。"""
    assert J.has_group_advocacy("基于这些，我认为这对你不利。") is None


def test_gate_statistical_numeric_needs_sample() -> None:
    b = _block([_good_item(layer="statistical",
                           conclusion="这类政策对该行业影响 r=0.12，不显著。")],
               layer="statistical")
    v = J.judgment_gate(b)
    assert not v.ok
    assert v.reasons[0]["rule_id"] == "J-EXPERIMENTAL-001"


def test_gate_statistical_numeric_ok_with_sample() -> None:
    b = _block([_good_item(layer="statistical",
                           conclusion="这类政策对该行业影响 r=0.12，不显著。",
                           sample_size=6, window="2019-2024")],
               layer="statistical")
    v = J.judgment_gate(b)
    assert v.ok, v.reasons


# ---------------------------------------------------------------- 判断偏好 API

from fastapi.testclient import TestClient  # noqa: E402
from fd_zhengxin_api.main import app  # noqa: E402

_client = TestClient(app)
AUTH = {"Authorization": "Bearer dev-tok-alice"}


@pytest.fixture(autouse=False)
def _api_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FDZX__DB", str(tmp_path / "personal.db"))
    monkeypatch.setenv("FDZX__AUTH_DEV_TOKENS", "dev-tok-alice=alice")


def test_prefs_api_default_all_on(_api_env) -> None:
    r = _client.get("/api/v1/judgment/prefs", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["prefs"] == {"statistical": True, "means": True, "personal": True}


def test_prefs_api_partial_update(_api_env) -> None:
    r = _client.put("/api/v1/judgment/prefs", json={"statistical": False}, headers=AUTH)
    assert r.json()["prefs"] == {"statistical": False, "means": True, "personal": True}
    assert _client.get("/api/v1/judgment/prefs", headers=AUTH).json()["prefs"]["statistical"] is False


def test_prefs_api_rejects_unknown_field(_api_env) -> None:
    r = _client.put("/api/v1/judgment/prefs", json={"bogus": True}, headers=AUTH)
    assert r.status_code == 422


def test_prefs_api_requires_auth() -> None:
    assert _client.get("/api/v1/judgment/prefs").status_code == 401
    assert _client.put("/api/v1/judgment/prefs", json={}).status_code == 401


# ---------------------------------------------------------------- 作用面 vs 文件元数据（走查撞到）

def test_metadata_never_becomes_personal_judgment() -> None:
    """文号 / 成文日期 / 文种 / 发文机关不是作用面，不产生「对我判断」。

    浏览器走查 2026-11-22 实测：一份公告的四个元数据要素生成了四张
    「「2026年第32号」是这份文件点名的作用面」的卡片——与「我」毫无关系，
    且四张一个样。这比不产出更糟：它让判断层看起来在工作，实际在复读元数据。
    """
    meta = [
        {"item_id": "i1", "element": "文号", "value": "2026年第32号", "quote": "2026年第32号"},
        {"item_id": "i1", "element": "成文日期", "value": "2026-11-13", "quote": "2026年11月13日"},
        {"item_id": "i1", "element": "文种", "value": "公告", "quote": "公告"},
        {"item_id": "i1", "element": "发布机构", "value": "自然资源部", "quote": "自然资源部"},
        {"item_id": "i1", "element": "发文机关", "value": "自然资源部", "quote": "自然资源部"},
    ]
    layer = J.build_personal_layer(enabled=True, elements=meta, situation=[])
    assert layer["items"] == []
    assert layer["status"] == J.STATUS_UNAVAILABLE
    assert "没有点明适用的行业/地区/主体" in layer["why"]


def test_scope_element_still_produces_judgment() -> None:
    """有真正的适用对象时照常判断（修复没把好路径一起关掉）。"""
    layer = J.build_personal_layer(enabled=True, elements=[ELEM], situation=[])
    assert layer["status"] == J.STATUS_OK
    assert len(layer["items"]) == 1


def test_mixed_elements_only_scope_ones_judged() -> None:
    """元数据与作用面混在一起时，只判作用面。"""
    meta = {"item_id": "i1", "element": "文号", "value": "2026年第32号", "quote": "2026年第32号"}
    layer = J.build_personal_layer(enabled=True, elements=[meta, ELEM], situation=[])
    assert len(layer["items"]) == 1
    assert "新能源汽车整车制造" in layer["items"][0]["conclusion"]
