"""内容口径守卫规则库 v1（确定性规则集，design.md D5）。

规则四要素：rule_id（R-<类>-<序号>）/ category（五类口径之一）/
severity（block=拦 / grey=标灰，落到 guard.Action）/ 模式（正文正则
RegexRule，或结构化断言检查 StructuredRule）/ description（人读说明）。

五类口径：
- prediction      预测类：结果预言句式（将会赢得 / 预测…当选 / 必然走向 / X 年内会）
- recommendation  推荐/评价类：立场推荐与优劣评价句式（应该支持 / 更优的立场是 / 明智的选择是）
- advocacy        倡导类：行动号召句式（我们应当 / 必须反对 / 捍卫X）
- bare-assertion  裸断言类：claims 强断言缺 source 引用（结构化检查）
- unbalanced      平衡类：当代议题 views 未覆盖 ≥2 个对立学派视角（结构化检查）

扫描面：正则规则只扫 payload.body（topic 是用户的理解对象，不是产出）；
结构化规则检查 claims / views / is_contemporary 字段。

版本化：RULESET_VERSION 标识整库版本；规则只增不改——同 rule_id 的行为
变更必须升版本并在变更说明中留痕（口径审计要可回放）。客户侧叠加
（guard_overlay）基于本库做「只收紧」的组合，不回写本文件。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .guard import Action, Reason

RULESET_VERSION = "v1"

CATEGORIES = frozenset(
    {"prediction", "recommendation", "advocacy", "bare-assertion", "unbalanced"}
)


# ---- 规则载体 ----


@dataclass
class RegexRule:
    """正文正则规则：sources 为人读模式源串（审计可回放），命中正文 body 即出 Reason。"""

    rule_id: str
    category: str
    action: Action
    description: str
    sources: tuple[str, ...]

    def __post_init__(self) -> None:
        self.patterns = tuple(re.compile(p, re.IGNORECASE) for p in self.sources)

    def match(self, text: str) -> Reason | None:
        for pattern in self.patterns:
            m = pattern.search(text)
            if m:
                return Reason(
                    self.rule_id, self.category,
                    f"{self.description}（命中『{m.group(0)}』）",
                )
        return None

    def apply(self, payload: dict) -> Reason | None:
        body = (payload or {}).get("body")
        text = body if isinstance(body, str) else ("" if body is None else str(body))
        return self.match(text)


@dataclass(frozen=True)
class StructuredRule:
    """结构化断言规则：check(payload) 返回 detail 字符串即命中，None 即未命中。"""

    rule_id: str
    category: str
    action: Action
    description: str
    check: Callable[[dict], "str | None"]

    def apply(self, payload: dict) -> Reason | None:
        detail = self.check(payload or {})
        if detail is None:
            return None
        return Reason(self.rule_id, self.category, f"{self.description}——{detail}")

    def match(self, text: str) -> Reason | None:  # 结构化规则不参与纯文本检查
        return None


# ---- 结构化检查函数 ----


def _source_ok(claim: dict) -> bool:
    src = claim.get("source")
    return isinstance(src, str) and bool(src.strip())


def _check_bare_assertion(payload: dict) -> str | None:
    """裸断言：claims 中强断言缺 source（无 / 空串 / 纯空白）即拦。"""
    claims = payload.get("claims") or []
    missing = [
        str(c.get("text", "")).strip()
        for c in claims
        if isinstance(c, dict) and not _source_ok(c)
    ]
    if not missing:
        return None
    shown = "、".join(f"『{t[:24]}』" for t in missing[:3])
    more = f" 等 {len(missing)} 条" if len(missing) > 3 else ""
    return f"claims 中 {len(missing)} 条强断言缺 source 引用：{shown}{more}"


def _check_unbalanced(payload: dict) -> str | None:
    """平衡类：当代议题（is_contemporary）必须覆盖 ≥2 个对立学派视角。

    v1 判定口径：views 去重后学派（school）≥2 且立场（stance）≥2——
    同一学派复述两遍、或两个学派念同一套立场，都不算「对立」。
    非当代议题不启用本规则。
    """
    if not payload.get("is_contemporary"):
        return None
    views = payload.get("views") or []
    schools, stances = set(), set()
    for v in views:
        if not isinstance(v, dict):
            continue
        if isinstance(v.get("school"), str) and v["school"].strip():
            schools.add(v["school"].strip())
        if isinstance(v.get("stance"), str) and v["stance"].strip():
            stances.add(v["stance"].strip())
    if len(schools) >= 2 and len(stances) >= 2:
        return None
    return (
        f"当代议题视角失衡：去重后学派 {len(schools)} 个、立场 {len(stances)} 种"
        "（要求 ≥2 个对立学派视角）"
    )


# ---- 模式集（中文 + 常见英文变体；只增不改） ----

_PREDICTION_BLOCK = (
    r"将会(赢得|获胜|胜出)",
    r"预测[^。；？！\n]{0,20}(当选|胜选|赢得|获胜|落败|败选)",
    r"必然走向",
    r"[0-9一二两三四五六七八九十百]+\s*年内[^。；？！\n]{0,10}会",
    r"will\s+(win|prevail|be\s+elected)",
    r"is\s+(bound|certain|destined)\s+to",
    r"within\s+\d+\s+years[^.。\n]{0,40}\s+will",
)
_PREDICTION_GREY = (
    r"大概率[^。；？！\n]{0,12}会",
    r"(几乎|差不多)可以肯定",
    r"注定会",
    r"胜算(明显)?更大",
    r"almost\s+certainly",
    r"is\s+likely\s+to\s+(win|prevail|be\s+elected)",
)
_RECO_BLOCK = (
    r"应该支持",
    r"应该反对",
    r"更优(的立场|的选择)?是",
    r"明智的选择是",
    r"(值得|应当)拥护",
    r"you\s+should\s+support",
    r"the\s+(better|wiser)\s+(position|choice|side)\s+is",
    r"the\s+wise\s+choice\s+is",
)
_RECO_GREY = (
    r"更值得认同",
    r"显然更有道理",
    r"更站得住脚",
    r"the\s+more\s+reasonable\s+side",
)
_ADVO_BLOCK = (
    r"我们应当",
    r"我们必须",
    r"必须(反对|支持|捍卫)",
    r"捍卫[^\s，。；、！？]{1,12}",
    r"we\s+must\s+(oppose|support|defend)",
    r"rally\s+(against|for)",
)
_ADVO_GREY = (
    r"共同抵制",
    r"一起(来)?推动",
    r"join\s+the\s+(movement|cause)",
    r"stand\s+with\s+(them|us|the\s+people)",
)


def v1() -> list:
    """规则库 v1：确定性规则全集（8 条，覆盖五类口径）。"""
    return [
        RegexRule(
            "R-PREDICT-001", "prediction", Action.BLOCK,
            "预测类：结果预言句式（将会赢得 / 预测…当选 / 必然走向 / X 年内会）",
            _PREDICTION_BLOCK,
        ),
        RegexRule(
            "R-PREDICT-002", "prediction", Action.GREY,
            "预测类：倾向性概率断言（大概率会 / 注定会 / almost certainly）",
            _PREDICTION_GREY,
        ),
        RegexRule(
            "R-RECO-001", "recommendation", Action.BLOCK,
            "推荐/评价类：立场推荐句式（应该支持 / 更优的立场是 / 明智的选择是）",
            _RECO_BLOCK,
        ),
        RegexRule(
            "R-RECO-002", "recommendation", Action.GREY,
            "推荐/评价类：立场优劣评价句式（更值得认同 / 显然更有道理）",
            _RECO_GREY,
        ),
        RegexRule(
            "R-ADVO-001", "advocacy", Action.BLOCK,
            "倡导类：行动号召句式（我们应当 / 必须反对 / 捍卫X）",
            _ADVO_BLOCK,
        ),
        RegexRule(
            "R-ADVO-002", "advocacy", Action.GREY,
            "倡导类：动员式号召句式（共同抵制 / join the movement）",
            _ADVO_GREY,
        ),
        StructuredRule(
            "R-BARE-001", "bare-assertion", Action.BLOCK,
            "裸断言类：claims 强断言缺 source 引用", _check_bare_assertion,
        ),
        StructuredRule(
            "R-BAL-001", "unbalanced", Action.BLOCK,
            "平衡类：当代议题未覆盖 ≥2 个对立学派视角", _check_unbalanced,
        ),
    ]
