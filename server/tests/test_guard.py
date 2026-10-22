"""骨架验证：口径守卫——两动作枚举、原因结构、空规则库放行。"""

from __future__ import annotations

from dataclasses import dataclass

from fd_zhengxin_api.guard import Action, Reason, Verdict, check, rules


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
