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

RULESET_VERSION = "v2"

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
    """裸断言：claims 中强断言缺 source（无 / 空串 / 纯空白）即拦。

    影响面产出的 claims 用的是 `evidence` 键（可复原的逐字证据）而非 `source`，
    所以按 payload_kind 决定读哪个键——两种有一点相同：**没有引用就不许写强断言**。
    """
    claims = payload.get("claims") or []
    key = "evidence" if payload.get("payload_kind") == "impact" else "source"

    def _ok(claim: dict) -> bool:
        val = claim.get(key)
        if isinstance(val, str):
            return bool(val.strip())
        # impact 的 evidence 是结构化块 {item_id, doc_id, span, quote}
        return isinstance(val, dict) and bool((val.get("quote") or "").strip())

    missing = [
        str(c.get("text", "")).strip()
        for c in claims
        if isinstance(c, dict) and not _ok(c)
    ]
    if not missing:
        return None
    shown = "、".join(f"『{t[:24]}』" for t in missing[:3])
    more = f" 等 {len(missing)} 条" if len(missing) > 3 else ""
    return f"claims 中 {len(missing)} 条强断言缺 {key} 引用：{shown}{more}"


def _check_unbalanced(payload: dict) -> str | None:
    """平衡类：当代议题（is_contemporary）必须覆盖 ≥2 个对立学派视角。

    v1 判定口径（学派闭环产出）：views 去重后学派（school）≥2 且立场（stance）≥2
    ——同一学派复述两遍、或两个学派念同一套立场，都不算「对立」。

    **影响面产出（payload_kind="impact"）完全不适用本规则**——理由见下方分支
    注释。这条不适用声明是 change zhengxin-entity-and-impact-loops 的显式决定，
    有测试守着（test_guard.py 的 impact 用例）。

    非当代议题不启用本规则。
    """
    if not payload.get("is_contemporary"):
        return None

    if payload.get("payload_kind") == "impact":
        # 影响面**不适用**本规则。原因：R-BAL-001 管的是「呈现观点谱系时不许只给
        # 单方立场」——那是编辑选择。影响面转述的是一份文件的正文，一份部委通知
        # 只点名一类主体是**文件的事实**，不是我们的剪辑；用平衡规则拦它等于惩罚
        # 诚实。影响面的保护在别处：每条要素必须有逐字证据（R-BARE-002）、不得
        # 滑向后果断言（R-PREDICT-003）、不得裁定个人利害（R-RECO-003）。
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

# ---- v2 新增（影响面环）：从「文本写了什么」滑向「所以会怎样」 ----

# 预测类：影响面最危险的滑坡——作用面描述本身合规，一句「这将导致」就把它
# 变成了后果断言。模式只认因果+变化的组合，不误伤「本文件规定……将……」
_PREDICTION_IMPACT = (
    r"(将|会|必将)(直接|必然|因此|从而)?(导致|造成|引发|带来)[^。；？！\n]{0,20}"
    r"(上升|下降|收紧|放松|扩大|萎缩|增加|减少|下滑|增长)",
    r"(意味着|预示着)[^。；？！\n]{0,15}(上升|下降|收紧|放松|扩大|萎缩|增加|减少)",
    r"this\s+will\s+(cause|lead\s+to|result\s+in)",
    r"means\s+that[^.\n]{0,30}\s+will\s+(rise|fall|increase|decrease)",
)

# 推荐/评价类：把作用面直接说成对「你」的利害，或据此建议行动
_RECO_IMPACT = (
    r"对你(们)?(是|将)?(有利|不利|有益|有害|利好|利空)",
    r"(你|您)(应该|应当|最好|建议)(据此)?(调整|规避|关注|支持|反对|行动)",
    r"(建议|可以)(据此|因此)(调整|规避|配置|行动)",
    r"(你|您)(将|会)(受益|受损)",
    r"(is|will\s+be)\s+(good|bad)\s+for\s+you",
    r"you\s+should\s+(adjust|avoid|watch|support|oppose)",
)


def v2() -> list:
    """规则库 v2：v1 的 8 条 + 影响面环的 3 条（共 11 条，仍覆盖五类口径）。

    v2 的三条全部落进既有类别，不新增第六类——`CATEGORIES` 是五类闭集且被
    测试断言等值，新增类别等于把口径纪律往下调；这三条本质就是 prediction /
    recommendation / bare-assertion，归入既有类语义上站得住。

    规则只增不改：v1 的 8 条逐字保留（同 rule_id 的行为变更必须升版本并在
    变更说明中留痕——口径审计要可回放）。
    """
    return v1() + [
        RegexRule(
            "R-PREDICT-003", "prediction", Action.BLOCK,
            "影响面：从作用面滑向后果断言（这将导致/会造成/意味着…上升下降收紧）",
            _PREDICTION_IMPACT,
        ),
        RegexRule(
            "R-RECO-003", "recommendation", Action.BLOCK,
            "影响面：把作用面直接说成个人利害或据此建议行动（对你有利/你应当调整）",
            _RECO_IMPACT,
        ),
        StructuredRule(
            "R-BARE-002", "bare-assertion", Action.BLOCK,
            "影响面：claims 强断言缺逐字证据（evidence）",
            _check_bare_assertion,
        ),
    ]


# 默认规则库（guard.guard_output 不传 ruleset 时用它）
def v1_or_latest() -> list:
    """兼容别名：当前默认规则库 = v2。"""
    return v2()


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
