"""内容口径守卫（骨架）：呈现内容只有两个处置动作——拦 / 标灰。

口径纪律（政心产品红线，详见仓根 README）：
- 政心做「理解」不做「表态」：呈现层转述带来源、只呈现不裁断；
- 越线内容（无来源断言、立场诱导、时政口径风险等）不允许原样呈现。

守卫动作枚举（只有两个）：
- Action.BLOCK（拦）：不呈现，调用方拿到拦截原因结构；
- Action.GREY（标灰）：可呈现但置灰降权，附原因供界面标注。

规则库留空接口：rules() 骨架期返回空表（全部放行），接线期逐条填充；
规则只增不改，每条规则命中即产出 Reason（结构化拦截原因）。
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
