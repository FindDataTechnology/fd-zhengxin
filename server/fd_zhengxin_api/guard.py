"""内容口径守卫（骨架）：呈现内容只有两个处置动作——拦 / 标灰。

口径纪律（政心产品红线，详见仓根 README）：
- 政心做「理解」不做「表态」：呈现层转述带来源、只呈现不裁断；
- 越线内容（无来源断言、立场诱导、时政口径风险等）不允许原样呈现。

守卫动作枚举（只有两个）：
- Action.BLOCK（拦）：不呈现，调用方拿到拦截原因结构；
- Action.GREY（标灰）：可呈现但置灰降权，附原因供界面标注。

两个面：
- check(text) / rules()：文本级骨架接口（既有口径，测试守住空表放行）；
- guard_output(payload, ruleset)：publish 前统一拦截面——四闭环候选产出
  （topic / is_contemporary / claims / views / body）过守卫，规则库 v1
  见 guard_rules.py，动作非 None 即不得原样呈现（None = 干净放行，
  放行不是动作、更不得伪装成推荐）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class Action(str, Enum):
    """守卫动作：拦 / 标灰。"""

    BLOCK = "block"  # 拦：不呈现
    GREY = "grey"    # 标灰：呈现但置灰降权


@dataclass
class Reason:
    """拦截/标灰原因结构（界面与审计共用）。"""

    rule_id: str            # 命中规则 id（如 "no-source-assertion"）
    category: str           # 口径类别（如 sourcing / neutrality / sensitivity）
    detail: str             # 人读说明：为什么命中


@dataclass
class Verdict:
    """守卫裁决：action 为 None 即放行（无需呈现任何标注）。"""

    action: Action | None = None
    reasons: list[Reason] = field(default_factory=list)
    ruleset_version: str = ""   # 裁决所用规则库版本（口径审计回放用）


class Rule(Protocol):
    """规则库条目接口（接线期填充实现）：命中返回 Reason，未命中返回 None。"""

    rule_id: str
    category: str
    action: Action

    def match(self, text: str) -> Reason | None: ...  # pragma: no cover


def rules() -> list[Rule]:
    """规则库：骨架期留空（空表 = 全部放行），接线期逐条填充。"""
    return []


_SEVERITY = {Action.BLOCK: 2, Action.GREY: 1}


def check(text: str) -> Verdict:
    """文本过守卫：逐条规则匹配；多条命中取最严动作（拦 > 标灰）。

    规则库为空时返回放行裁决（action=None, reasons=[]）。
    """
    verdict = Verdict()
    for rule in rules():
        reason = rule.match(text)
        if reason is None:
            continue
        verdict.reasons.append(reason)
        if verdict.action is None or _SEVERITY[rule.action] > _SEVERITY[verdict.action]:
            verdict.action = rule.action
    return verdict


def guard_output(payload: dict, ruleset: list | None = None) -> Verdict:
    """publish 前统一拦截面：四闭环候选产出过守卫。

    payload 是候选产出的统一结构：{topic, is_contemporary, claims[{text,
    source?}], views[{school, stance}], body}；正则规则扫 body，结构化规则
    检查 claims/views。动作只有两种：block（拦，不呈现）/ grey（标灰，
    呈现但置灰降权）；无命中时 action=None（干净放行——放行不是动作）。
    多条命中取最严（block > grey）并聚合全部 Reason。

    ruleset 缺省用当前规则库（guard_rules.v2()，版本号随裁决带回）；传
    自定义规则集（如 guard_overlay.load_ruleset 的收紧组合）时版本号留空，
    由调用方自行标注。
    """
    from . import guard_rules  # 延迟导入：guard_rules 复用本模块的 Action/Reason

    if ruleset is None:
        ruleset = guard_rules.v2()
        version = guard_rules.RULESET_VERSION
    else:
        version = ""
    verdict = Verdict(ruleset_version=version)
    for rule in ruleset:
        reason = rule.apply(payload or {})
        if reason is None:
            continue
        verdict.reasons.append(reason)
        if verdict.action is None or _SEVERITY[rule.action] > _SEVERITY[verdict.action]:
            verdict.action = rule.action
    return verdict
