"""AI 判断三层 + 判断信封（change zhengxin-ai-judgment-and-workbench）。

存在理由：政心今天只给材料、不给判断。用户 2026-11-22 的主张成立——模型不存在
「纯粹属于自己的判断」，所以「因为是 AI 所以不许判断」这个理由不成立。但同一论证
反过来约束一件事：**一个融合了人的意志的判断，必须能指出它融合了谁的意志。**

因此本模块的产出形态是「判断信封」，不是一句结论。缺 `may_be_wrong` /
`would_overturn` 的候选**不产出**——无反证条件的断言无法被讨论，只能被服从，
而那不是政心做的产品。

三层（各自独立开关，默认全开，见 personal.judgment_prefs）：

    statistical  某类主张在历史上对某类主体的实际效果
                 首版恒 unavailable——政心没有面板数据序列，不用 LLM 生成
                 「大概的影响」冒充统计判断（那正是 R-PREDICT 本来要拦的东西）
    means        给定目标时哪条路径更可能达成。推理型，不依赖外部数据
    personal     结合用户声明的处境给出对用户的具体影响
                 处境为空时照给通用判断，但信封里必须写明这一点

判断通道与材料通道**并列、互不放宽**：材料层的 guard_output 一字不动，
判断层的规则在 judgment_gate（guard_rules 之外的独立规则集，见其 docstring）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

LAYERS = ("statistical", "means", "personal")

# 层状态四值：ok / disabled（用户关）/ unavailable（能力缺）/ error
STATUS_OK = "ok"
STATUS_DISABLED = "disabled"
STATUS_UNAVAILABLE = "unavailable"

CONFIDENCE_LEVELS = ("high", "medium", "low")

# 群体倡导（判断通道的独立副本，与材料通道 R-ADVO 用同一份正则源；
# 复制而非共享是因为两条通道演进节奏不同，见 design D3）
GROUP_ADVOCACY_PATTERNS = (
    r"我们应当",
    r"我们必须",
    r"必须(反对|支持|捍卫)",
    r"捍卫[^\s，。；、！？]{1,12}",
    r"共同抵制",
    r"号召[^\s，。；、]{1,16}",
    r"join\s+the\s+(movement|cause)",
    r"we\s+must\s+(oppose|support|defend)",
)
_GROUP_ADVOCACY_RE = tuple(re.compile(p, re.I) for p in GROUP_ADVOCACY_PATTERNS)


class JudgmentRejected(ValueError):
    """候选判断不满足准入条件（缺反证字段 / 依据链为空 / 群体倡导 / 数值缺样本）。"""


@dataclass(frozen=True)
class Judgment:
    """一条判断。六个字段缺一不可——缺则不产出，不是补默认值。"""

    conclusion: str                 # 结论
    evidence: list[dict]            # 依据链：[{ref, note}] 或 [{ref, quote}]
    confidence: str                 # high | medium | low
    may_be_wrong: str               # 我可能错在哪
    would_overturn: str             # 什么会推翻它
    layer: str                      # statistical | means | personal

    def to_dict(self) -> dict:
        return {
            "conclusion": self.conclusion,
            "evidence": self.evidence,
            "confidence": self.confidence,
            "may_be_wrong": self.may_be_wrong,
            "would_overturn": self.would_overturn,
            "layer": self.layer,
        }


def validate_judgment(j: Judgment) -> None:
    """准入校验：不满足即抛 JudgmentRejected（调用方记入 gaps，不产出）。

    四条都是硬条件，不是风格建议：
    - 结论非空；
    - 依据链非空且每条带 ref（没有依据的判断是断言）；
    - may_be_wrong / would_overturn 非空（无反证条件的断言无法被讨论）；
    - 置信度在闭集内（三档，不给分值——照柏讯「不写 0.8 这类伪精确分数」）。
    """
    if not (j.conclusion or "").strip():
        raise JudgmentRejected("结论为空")
    if not j.evidence:
        raise JudgmentRejected("依据链为空：没有依据的判断是断言，不是判断")
    for i, ev in enumerate(j.evidence):
        if not isinstance(ev, dict) or not (ev.get("ref") or "").strip():
            raise JudgmentRejected(f"依据链第 {i + 1} 条缺 ref（指向不可复核）")
    if not (j.may_be_wrong or "").strip():
        raise JudgmentRejected("缺「我可能错在哪」——无反证条件不得作为判断呈现")
    if not (j.would_overturn or "").strip():
        raise JudgmentRejected("缺「什么会推翻它」——无反证条件不得作为判断呈现")
    if j.confidence not in CONFIDENCE_LEVELS:
        raise JudgmentRejected(f"置信度 {j.confidence!r} 不在闭集 {CONFIDENCE_LEVELS} 内")
    if j.layer not in LAYERS:
        raise JudgmentRejected(f"未知判断层 {j.layer!r}")


def has_group_advocacy(text: str) -> str | None:
    """判断通道仍受群体倡导禁令约束（与开关状态无关）。返回命中的片段或 None。"""
    for pat in _GROUP_ADVOCACY_RE:
        m = pat.search(text or "")
        if m:
            return m.group(0)
    return None


# ---------------------------------------------------------------- 三层生成


def build_statistical_layer(*, enabled: bool, series_available: bool = False) -> dict:
    """统计判断层。**首版恒 unavailable**——政心没有面板数据序列。

    开关开 + 无数据 → unavailable（能力缺，不是用户选择）
    开关关          → disabled（用户选择）

    **绝不用 LLM 生成「大概的影响」冒充统计判断**：统计判断之所以能被允许，唯一
    理由是它有数据可指；没有数据时它退化成猜测，比不给更糟（那正是 R-PREDICT
    家族本来要拦的「从文本滑向后果」）。
    """
    if not enabled:
        return {"status": STATUS_DISABLED, "items": []}
    if not series_available:
        return {
            "status": STATUS_UNAVAILABLE,
            "items": [],
            "why": "未接入年鉴/城市/企业面板数据序列；统计判断需要可复核的序列与样本量，"
                   "不以语言模型估计替代。",
        }
    # 数据接入后在此生成；当前不实现（无数据可跑，写了也测不到真值）
    return {"status": STATUS_UNAVAILABLE, "items": [],
            "why": "数据序列已声明可用但生成路径未实现"}


def build_means_layer(
    *, enabled: bool, goal: str | None, options: list[dict] | None = None,
) -> dict:
    """手段判断层：给定目标，哪条路径更可能达成。

    推理型，不需要外部数据；但依据链必须指向可复核的材料（调用方给的 options
    里每条带 ref）。没有目标或候选时如实 unavailable，不凭空造路径。
    """
    if not enabled:
        return {"status": STATUS_DISABLED, "items": []}
    if not goal or not options:
        return {"status": STATUS_UNAVAILABLE, "items": [],
                "why": "缺目标或候选路径；手段判断需要明确的目标才谈得上「哪条更可能达成」"}
    items = []
    for opt in options:
        if not opt.get("ref"):
            continue
        try:
            j = Judgment(
                conclusion=opt.get("conclusion") or f"若目标是「{goal}」，"
                           f"路径「{opt.get('label', '未命名')}」相对更直接",
                evidence=[{"ref": opt["ref"],
                           "note": opt.get("note") or "候选路径依据"}],
                confidence=opt.get("confidence", "low"),
                may_be_wrong=opt.get("may_be_wrong")
                or "这是手段推断，不是对结果的保证；达成路径与目标之间可能还有未识别的环节",
                would_overturn=opt.get("would_overturn")
                or "出现反例路径在同类目标下更常达成，或目标本身被重新定义",
                layer="means",
            )
            validate_judgment(j)
        except JudgmentRejected:
            continue   # 不合格的候选不产出（宁缺勿滥）
        items.append(j.to_dict())
    return {"status": STATUS_OK if items else STATUS_UNAVAILABLE,
            "items": items,
            **({"why": "候选路径均不满足判断准入条件"} if not items else {})}


def build_personal_layer(
    *, enabled: bool, elements: list[dict], situation: list[dict],
) -> dict:
    """对我判断层：结合处境给出对用户的具体影响。

    **处境为空是必然的起点**（默认全开 + 处境默认空）：照给通用判断，但信封里
    必须写明 `situation="absent"` 且结论带上「这是通用判断，不是对你个人的判断」。
    静默降级冒充个人判断是最糟的形态。
    """
    if not enabled:
        return {"status": STATUS_DISABLED, "items": []}
    if not elements:
        return {"status": STATUS_UNAVAILABLE, "items": [],
                "why": "没有可供判断的作用要素（正文不可得或未抽出要素）"}

    declared = bool(situation)
    items = []
    for el in elements:
        if not el.get("item_id") or not el.get("quote"):
            continue
        value = el.get("value") or ""
        if declared:
            matches = [
                s for s in situation
                if (s.get("value") or "").strip()
                and ((s["value"] in value) or (value in s["value"]))
            ]
            if matches:
                concl = f"「{value}」与你声明的 {matches[0]['kind']}={matches[0]['value']} 字面相交"
                ev = [{"ref": f"item:{el['item_id']}", "quote": el["quote"]},
                      {"ref": f"situation:{matches[0].get('id', 'declared')}",
                       "note": matches[0]["value"]}]
                conf = "medium"
            else:
                concl = f"「{value}」与你声明的处境字面不相交"
                ev = [{"ref": f"item:{el['item_id']}", "quote": el["quote"]},
                      {"ref": "situation:declared", "note": "已声明处境，无字面交集"}]
                conf = "low"
            mw = "字面相交不等于实际影响——上下游关系不推断；同名字面可能是不同对象"
            wo = "你的处境声明变更，或文件另有条款点名你的具体类别"
        else:
            concl = (f"「{value}」是这份文件点名的作用面。"
                     "**你还没声明处境，这是通用判断，不是对你个人的判断。**")
            ev = [{"ref": f"item:{el['item_id']}", "quote": el["quote"]}]
            conf = "low"
            mw = "没有你的处境声明，无法判断这份文件是否触及你；这条只是复述作用面"
            wo = "声明处境后重跑，或文件点名对象与你的处境字面相交"
        try:
            j = Judgment(conclusion=concl, evidence=ev, confidence=conf,
                         may_be_wrong=mw, would_overturn=wo, layer="personal")
            validate_judgment(j)
        except JudgmentRejected:
            continue
        items.append(j.to_dict())
    return {
        "status": STATUS_OK if items else STATUS_UNAVAILABLE,
        "items": items,
        "situation": "declared" if declared else "absent",
        **({"why": "要素均不满足判断准入条件"} if not items else {}),
    }


def build_judgment_block(
    *, prefs: dict, elements: list[dict] | None = None,
    situation: list[dict] | None = None, goal: str | None = None,
    options: list[dict] | None = None, series_available: bool = False,
) -> dict:
    """三层合成一个 `judgment` 块（挂到既有产出的字段上）。"""
    return {
        "enabled": any(prefs.get(k, True) for k in LAYERS),
        "layers": {
            "statistical": build_statistical_layer(
                enabled=prefs.get("statistical", True),
                series_available=series_available,
            ),
            "means": build_means_layer(
                enabled=prefs.get("means", True), goal=goal, options=options,
            ),
            "personal": build_personal_layer(
                enabled=prefs.get("personal", True),
                elements=elements or [], situation=situation or [],
            ),
        },
    }


# ---------------------------------------------------------------- 判断通道守卫


@dataclass
class JudgmentVerdict:
    """判断通道裁决。action 为 None 即通过；非 None 即不呈现该条。"""

    ok: bool = True
    reasons: list[dict] = field(default_factory=list)
    ruleset_version: str = "j1"


JUDGMENT_RULESET_VERSION = "j1"


def judgment_gate(block: dict) -> JudgmentVerdict:
    """判断通道守卫：四条规则，作用于 `judgment` 块（不碰材料层）。

    与材料通道并列、互不放宽（见 design D3）。四条：

      J-ENVELOPE-001   信封缺 may_be_wrong / would_overturn / evidence
      J-EVIDENCE-001   依据链里有条目缺 ref（指向不可复核）
      J-ADVOCACY-001   面向群体的倡导或动员（与开关状态无关）
      J-EXPERIMENTAL-001 数值型结论未带样本量/窗口（统计判断的硬要求）

    通过的不改字段；命中的把该条从 items 移除并记 reason（宁可这一条不出，
    不整块不出——用户能看到「这条因何未呈现」比「什么都没有」有用）。
    """
    verdict = JudgmentVerdict(ruleset_version=JUDGMENT_RULESET_VERSION)
    if not isinstance(block, dict):
        return verdict

    for layer_name, layer in (block.get("layers") or {}).items():
        if not isinstance(layer, dict):
            continue
        kept = []
        for item in layer.get("items") or []:
            reason = _check_item(layer_name, item)
            if reason is None:
                kept.append(item)
            else:
                verdict.ok = False
                verdict.reasons.append(reason)
        if "items" in layer:
            layer["items"] = kept
            if not kept and layer.get("status") == STATUS_OK:
                layer["status"] = STATUS_UNAVAILABLE
                layer.setdefault("why", "全部判断未通过判断通道守卫")
    return verdict


def _check_item(layer_name: str, item: dict) -> dict | None:
    if not isinstance(item, dict):
        return {"rule_id": "J-ENVELOPE-001", "layer": layer_name,
                "detail": "判断条目不是对象"}
    # J-ENVELOPE-001
    for f in ("conclusion", "may_be_wrong", "would_overturn"):
        if not (item.get(f) or "").strip():
            return {"rule_id": "J-ENVELOPE-001", "layer": layer_name,
                    "detail": f"信封缺 {f}"}
    ev = item.get("evidence") or []
    if not ev:
        return {"rule_id": "J-ENVELOPE-001", "layer": layer_name,
                "detail": "信封缺依据链"}
    # J-EVIDENCE-001
    for i, e in enumerate(ev):
        if not isinstance(e, dict) or not (e.get("ref") or "").strip():
            return {"rule_id": "J-EVIDENCE-001", "layer": layer_name,
                    "detail": f"依据链第 {i + 1} 条缺 ref"}
    # J-ADVOCACY-001（群体倡导，与开关状态无关）
    hit = has_group_advocacy(item.get("conclusion") or "")
    if hit:
        return {"rule_id": "J-ADVOCACY-001", "layer": layer_name,
                "detail": f"面向群体的倡导或动员（命中『{hit}』）"}
    # J-EXPERIMENTAL-001：统计层给出数值必须带样本量与窗口
    if layer_name == "statistical":
        if re.search(r"\d+(\.\d+)?\s*%|\br\s*=\s*-?\d", item.get("conclusion") or ""):
            if not item.get("sample_size") or not item.get("window"):
                return {"rule_id": "J-EXPERIMENTAL-001", "layer": layer_name,
                        "detail": "数值型统计结论缺样本量或窗口"}
    return None
