"""守卫全套测试：两动作口径 + 规则库 v1 + 正反样本集 + LLM 灰区判别器 + 客户侧叠加。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from fd_zhengxin_api import config, guard_llm, guard_overlay, guard_rules
from fd_zhengxin_api.guard import (
    Action, Reason, Verdict, check, guard_output, rules,
)

SAMPLES_DIR = Path(__file__).parent / "guard_samples"


def _jsonl(name: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (SAMPLES_DIR / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_empty_rules_allow_everything() -> None:
    """规则库留空接口：rules() 为空表时 check() 全放行。"""
    assert rules() == []
    v = check("任何内容")
    assert isinstance(v, Verdict)
    assert v.action is None and v.reasons == []


def test_actions_are_exactly_block_and_grey() -> None:
    """动作枚举只有两个：拦（block）/ 标灰（grey）。"""
    assert {a.name for a in Action} == {"BLOCK", "GREY"}
    assert {a.value for a in Action} == {"block", "grey"}


@dataclass
class _FakeRule:
    rule_id: str
    category: str
    action: Action
    hit: str

    def match(self, text: str) -> Reason | None:
        if self.hit in text:
            return Reason(self.rule_id, self.category, f"命中 {self.hit}")
        return None


def test_check_picks_stricter_action(monkeypatch) -> None:
    """多规则命中取最严（拦 > 标灰），原因结构聚合。"""
    import fd_zhengxin_api.guard as g

    monkeypatch.setattr(
        g, "rules",
        lambda: [
            _FakeRule("r-grey", "neutrality", Action.GREY, "诱导"),
            _FakeRule("r-block", "sourcing", Action.BLOCK, "无来源断言"),
        ],
    )
    v = g.check("这段无来源断言且带立场诱导")
    assert v.action is Action.BLOCK  # 拦最严
    assert {r.rule_id for r in v.reasons} == {"r-grey", "r-block"}
    assert all(isinstance(r, Reason) for r in v.reasons)

    v2 = g.check("只有立场诱导")
    assert v2.action is Action.GREY and len(v2.reasons) == 1

    v3 = g.check("干净的转述")
    assert v3.action is None and v3.reasons == []


# ---- 规则库 v1 ----


def test_ruleset_v1_catalog() -> None:
    """v1 规则目录：rule_id 唯一、五类口径全覆盖、severity 只有 block/grey。"""
    rs = guard_rules.v1()
    assert guard_rules.RULESET_VERSION == "v1"
    ids = [r.rule_id for r in rs]
    assert len(ids) == len(set(ids))
    assert {r.category for r in rs} == set(guard_rules.CATEGORIES)
    for r in rs:
        assert r.category in guard_rules.CATEGORIES
        assert r.action in (Action.BLOCK, Action.GREY)
        assert r.description


# ---- 正反样本集（全量跑） ----


def test_positive_samples_all_block() -> None:
    """正例（应拦）≥30 条：全 block，且期望 rule_id 确实命中。"""
    samples = _jsonl("positives.jsonl")
    assert len(samples) >= 30
    for i, s in enumerate(samples, 1):
        v = guard_output(s["payload"])
        assert v.action is Action.BLOCK, f"positives[{i}] 未拦: {s['payload']}"
        assert v.ruleset_version == guard_rules.RULESET_VERSION
        hits = {r.rule_id for r in v.reasons}
        expected = set(s["expect_rule_ids"])
        assert expected and expected <= hits, (
            f"positives[{i}] 期望 {sorted(expected)} 未全命中（实际 {sorted(hits)}）"
        )
        assert all(isinstance(r, Reason) for r in v.reasons)
    # 五类口径都有正例覆盖
    covered = set()
    for s in samples:
        for r in guard_rules.v1():
            if r.rule_id in s["expect_rule_ids"]:
                covered.add(r.category)
    assert covered == set(guard_rules.CATEGORIES)


def test_negative_samples_all_pass() -> None:
    """负例（应过）≥30 条：全部干净放行（action=None、无任何 Reason）。"""
    samples = _jsonl("negatives.jsonl")
    assert len(samples) >= 30
    for i, s in enumerate(samples, 1):
        v = guard_output(s["payload"])
        assert v.action is None and v.reasons == [], (
            f"negatives[{i}] 被误拦/误标灰: {[r.rule_id for r in v.reasons]}"
        )


def test_guard_output_multi_hit_strictest() -> None:
    """多命中取最严：仅灰级命中 → grey；灰+拦并存 → block 且原因聚合。"""
    grey_only = {
        "topic": "走势讨论", "is_contemporary": False, "claims": [], "views": [],
        "body": "这项调整大概率会引发新一轮讨论。",
    }
    v = guard_output(grey_only)
    assert v.action is Action.GREY
    assert {r.rule_id for r in v.reasons} == {"R-PREDICT-002"}

    both = {
        "topic": "走势讨论", "is_contemporary": False, "claims": [], "views": [],
        "body": "这项调整大概率会引发反弹，并且事态必然走向公开化。",
    }
    v2 = guard_output(both)
    assert v2.action is Action.BLOCK  # 拦最严
    assert {r.rule_id for r in v2.reasons} == {"R-PREDICT-001", "R-PREDICT-002"}

    clean = {
        "topic": "概念卡", "is_contemporary": False, "claims": [], "views": [],
        "body": "此处仅转述文本原意。",
    }
    v3 = guard_output(clean)
    assert v3.action is None and v3.reasons == []


# ---- LLM 灰区判别器 ----


def test_classify_grey_key_missing(monkeypatch) -> None:
    """KEY 缺失：动作仍标灰，reason=llm_unavailable，绝不放行。"""
    monkeypatch.delenv("FDZX__LLM_API_KEY", raising=False)
    d = guard_llm.classify_grey({"topic": "t", "body": "灰区内容"})
    assert d.action is Action.GREY
    assert d.llm_opinion == "unavailable"
    assert d.reason_code == "llm_unavailable"


def test_classify_grey_never_passes(monkeypatch) -> None:
    """LLM 说 clean 也只是参考意见；失败/坏响应一律 grey。"""
    monkeypatch.delenv("FDZX__LLM_API_KEY", raising=False)
    payload = {"topic": "t", "is_contemporary": False, "claims": [], "views": [],
               "body": "灰区内容"}

    monkeypatch.setattr(guard_llm.llm, "is_configured", lambda: True)
    monkeypatch.setattr(guard_llm.llm_ledger, "chat_logged", lambda *a, **k: "clean")
    d = guard_llm.classify_grey(payload)
    assert d.action is Action.GREY and d.llm_opinion == "clean" and d.reason_code == ""

    monkeypatch.setattr(guard_llm.llm_ledger, "chat_logged", lambda *a, **k: "risky")
    d2 = guard_llm.classify_grey(payload)
    assert d2.action is Action.GREY and d2.llm_opinion == "risky"

    def _boom(*a, **k):
        raise guard_llm.llm.LLMError("timeout")

    monkeypatch.setattr(guard_llm.llm_ledger, "chat_logged", _boom)
    d3 = guard_llm.classify_grey(payload)
    assert d3.action is Action.GREY and d3.reason_code == "llm_error"

    monkeypatch.setattr(guard_llm.llm_ledger, "chat_logged", lambda *a, **k: "无法判断")
    d4 = guard_llm.classify_grey(payload)
    assert d4.action is Action.GREY and d4.reason_code == "llm_unparseable"

    monkeypatch.setattr(guard_llm.llm, "chat", lambda *a, **k: None)
    d5 = guard_llm.classify_grey(payload)
    assert d5.action is Action.GREY and d5.reason_code == "llm_unparseable"


def test_grey_queue_roundtrip(tmp_path, monkeypatch) -> None:
    """标灰落 grey_queue（pending），payload 快照可回放。"""
    monkeypatch.delenv("FDZX__LLM_API_KEY", raising=False)
    conn = sqlite3.connect(tmp_path / "guard.db")
    try:
        payload = {"topic": "灰区议题", "is_contemporary": True, "claims": [],
                   "views": [], "body": "待人工分诊的灰区正文。"}
        d = guard_llm.classify_grey(payload, conn=conn, ruleset_version="v1")
        assert d.reason_code == "llm_unavailable"
        rows = guard_llm.list_grey_queue(conn)
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "pending"
        assert row["llm_opinion"] == "unavailable"
        assert row["ruleset_version"] == "v1"
        assert row["payload"]["topic"] == "灰区议题"  # JSON 快照回放

        assert guard_llm.list_grey_queue(conn, status="resolved") == []
    finally:
        conn.close()


# ---- 客户侧规则叠加 ----


def test_overlay_tighten_and_add(tmp_path) -> None:
    """合法 overlay：grey→block 收紧 + 新增规则；收紧后同 payload 变拦。"""
    ov = tmp_path / "overlay.json"
    ov.write_text(json.dumps({"rules": [
        {"rule_id": "R-PREDICT-002", "severity": "block"},
        {"rule_id": "R-RECO-001", "severity": "block"},   # 同 severity 幂等重放
        {"rule_id": "R-CUSTOM-001", "category": "prediction",
         "pattern": "口径自留风险词", "severity": "grey"},
    ]}, ensure_ascii=False), encoding="utf-8")

    base = guard_rules.v1()
    merged = guard_overlay.load_ruleset(base, str(ov))
    by_id = {r.rule_id: r for r in merged}
    assert len(merged) == len(base) + 1
    assert by_id["R-PREDICT-002"].action is Action.BLOCK  # 收紧生效
    assert by_id["R-CUSTOM-001"].category == "prediction"

    grey_body = {"topic": "t", "is_contemporary": False, "claims": [], "views": [],
                 "body": "这项调整大概率会引发讨论。"}
    assert guard_output(grey_body, base).action is Action.GREY      # 基线：灰
    assert guard_output(grey_body, merged).action is Action.BLOCK   # 收紧后：拦

    custom_body = {"topic": "t", "is_contemporary": False, "claims": [], "views": [],
                   "body": "正文里出现口径自留风险词。"}
    assert guard_output(custom_body, base).action is None           # 基线：干净
    assert guard_output(custom_body, merged).action is Action.GREY  # 新增：灰


def test_overlay_rejects_relax(tmp_path) -> None:
    """放宽一律拒绝：block→grey、删规则、改基线 pattern、坏格式、文件缺失。"""
    base = guard_rules.v1()

    def _load(rules: list) -> None:
        f = tmp_path / "ov.json"
        f.write_text(json.dumps({"rules": rules}, ensure_ascii=False), encoding="utf-8")
        guard_overlay.load_ruleset(base, str(f))

    with pytest.raises(guard_overlay.OverlayError):  # block → grey（放宽）
        _load([{"rule_id": "R-PREDICT-001", "severity": "grey"}])
    with pytest.raises(guard_overlay.OverlayError):  # 删除规则
        _load([{"rule_id": "R-BARE-001", "remove": True}])
    with pytest.raises(guard_overlay.OverlayError):  # 改写基线 pattern
        _load([{"rule_id": "R-ADVO-001", "pattern": "x", "severity": "block"}])
    with pytest.raises(guard_overlay.OverlayError):  # 坏 severity
        _load([{"rule_id": "R-CUSTOM-9", "category": "c", "pattern": "p",
                "severity": "warn"}])
    with pytest.raises(guard_overlay.OverlayError):  # 新增缺 pattern
        _load([{"rule_id": "R-CUSTOM-9", "category": "c", "severity": "grey"}])
    with pytest.raises(guard_overlay.OverlayError):  # 新增 pattern 非法正则
        _load([{"rule_id": "R-CUSTOM-9", "category": "c", "pattern": "([",
                "severity": "grey"}])
    with pytest.raises(guard_overlay.OverlayError):  # 缺 rule_id
        _load([{"severity": "grey"}])
    with pytest.raises(guard_overlay.OverlayError):  # 文件不存在
        guard_overlay.load_ruleset(base, str(tmp_path / "nope.json"))

    bad = tmp_path / "bad.json"
    bad.write_text("{不是 json", encoding="utf-8")
    with pytest.raises(guard_overlay.OverlayError):  # 坏 JSON
        guard_overlay.load_ruleset(base, str(bad))


def test_overlay_path_from_config(tmp_path, monkeypatch) -> None:
    """配置项 FDZX__GUARD_OVERLAY：设了即叠加，未设原样返回基线 v1。"""
    f = tmp_path / "overlay.json"
    f.write_text(json.dumps({"rules": [
        {"rule_id": "R-CUSTOM-002", "category": "advocacy",
         "pattern": "客户禁语", "severity": "block"},
    ]}, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setenv("FDZX__GUARD_OVERLAY", str(f))
    assert config.guard_overlay_path() == str(f)
    ids = {r.rule_id for r in guard_overlay.load_ruleset()}
    assert "R-CUSTOM-002" in ids and "R-PREDICT-001" in ids  # 基线仍在

    body = {"topic": "t", "is_contemporary": False, "claims": [], "views": [],
            "body": "正文出现客户禁语。"}
    assert guard_output(body, guard_overlay.load_ruleset()).action is Action.BLOCK

    monkeypatch.delenv("FDZX__GUARD_OVERLAY", raising=False)
    assert config.guard_overlay_path() == ""
    assert guard_overlay.load_ruleset() == guard_rules.v1()  # 未设：基线原样
