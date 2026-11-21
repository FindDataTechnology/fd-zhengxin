"""客户侧规则叠加（私有化部署，design.md D5）：只能收紧，不能放宽。

overlay 是私有化客户自带的一份 JSON 规则文件（路径配置 FDZX__GUARD_
OVERLAY，见 config.guard_overlay_path），与基线规则库（guard_rules.v1）
组合成生效规则集。允许的操作只有两种：
- 新增确定性正则规则（severity 起步 block 或 grey 均可，新增不是放宽）；
- 把已有 grey 规则收紧为 block（同 severity 幂等重放也允许）。

一律抛 OverlayError 拒绝（宁可整份不叠加，也不放宽一格）：
- 把已有 block 规则降为 grey（放宽）；
- 删除已有规则（remove / deleted）；
- 改写已有规则的 pattern / category（基线规则只增不改，见 guard_rules）；
- 坏格式：缺 rule_id / severity 拼错 / 新增规则缺 pattern 或 category /
  pattern 不是合法正则 / 文件不存在或不是合法 JSON。

overlay 文件格式：
{"rules": [
    {"rule_id": "R-PREDICT-002", "severity": "block"},
    {"rule_id": "R-CUSTOM-001", "category": "prediction",
     "pattern": "客户自留风险词", "severity": "grey"}
]}
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from . import config, guard_rules
from .guard import Action
from .guard_rules import RegexRule

SEVERITIES = {"block": Action.BLOCK, "grey": Action.GREY}


class OverlayError(ValueError):
    """overlay 非法（放宽 / 删规则 / 改基线 / 坏格式）：拒绝加载。"""


def load_ruleset(base: list | None = None, overlay_path: str | None = None) -> list:
    """基线规则库 + 客户侧 overlay → 生效规则集（只收紧）。

    base 缺省用 guard_rules.v2()；overlay_path 缺省读 config.guard_
    overlay_path()（未配置即无叠加，原样返回基线）。任何放宽意图抛
    OverlayError，绝不静默降级。
    """
    if base is None:
        base = guard_rules.v2()
    rules = list(base)
    if overlay_path is None:
        overlay_path = config.guard_overlay_path()
    if not overlay_path:
        return rules

    by_id = {r.rule_id: r for r in rules}
    order = [r.rule_id for r in rules]
    for entry in _entries(overlay_path):
        order, by_id = _apply_entry(order, by_id, entry)
    return [by_id[rid] for rid in order]


def _entries(overlay_path: str) -> list[dict]:
    path = Path(overlay_path)
    if not path.is_file():
        raise OverlayError(f"叠加文件不存在或不是文件：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OverlayError(f"叠加文件读取/解析失败（{path}）：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise OverlayError("叠加文件格式：顶层应为 {{\"rules\": [...]}}")
    for i, entry in enumerate(data["rules"]):
        if not isinstance(entry, dict):
            raise OverlayError(f"叠加 rules[{i}] 不是对象：{entry!r}")
    return data["rules"]


def _apply_entry(order: list[str], by_id: dict, entry: dict) -> tuple[list[str], dict]:
    rid = entry.get("rule_id")
    if not isinstance(rid, str) or not rid.strip():
        raise OverlayError(f"overlay 条目缺 rule_id：{entry!r}")
    rid = rid.strip()
    if entry.get("remove") or entry.get("deleted"):
        raise OverlayError(f"不允许删除规则（{rid}）：客户侧只能新增或收紧")

    severity = entry.get("severity")
    if severity not in SEVERITIES:
        raise OverlayError(
            f"规则 {rid} 的 severity 只能是 block/grey，收到 {severity!r}"
        )
    action = SEVERITIES[severity]

    if rid in by_id:  # 已有规则：只允许收紧 severity（grey→block）
        if "pattern" in entry or "category" in entry:
            raise OverlayError(
                f"不允许改写已有规则 {rid} 的 pattern/category（只能收紧 severity）"
            )
        current = by_id[rid]
        if current.action is Action.BLOCK and action is Action.GREY:
            raise OverlayError(
                f"不允许放宽：规则 {rid} 已是 block，不可降为 grey"
            )
        if current.action is Action.GREY and action is Action.BLOCK:
            by_id[rid] = replace(current, action=Action.BLOCK)  # 收紧
        # 同 severity：幂等重放，原样保留
        return order, by_id

    # 新增规则：必须有 category 与可编译的 pattern
    category = entry.get("category")
    pattern = entry.get("pattern")
    if not isinstance(category, str) or not category.strip():
        raise OverlayError(f"新增规则 {rid} 缺 category（口径类别）")
    if not isinstance(pattern, str) or not pattern:
        raise OverlayError(f"新增规则 {rid} 缺 pattern（正则模式）")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise OverlayError(f"新增规则 {rid} 的 pattern 不是合法正则：{exc}") from exc
    by_id[rid] = RegexRule(
        rid, category.strip(), action,
        "客户侧叠加规则（overlay 新增）", (pattern,),
    )
    order.append(rid)
    return order, by_id
