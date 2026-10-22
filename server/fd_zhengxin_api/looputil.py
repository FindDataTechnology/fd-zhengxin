"""三闭环共享底座：确定性锚定词表、当代议题判定、LLM JSON 解析、publish 守卫门。

定位（locate）/多视角（perspectives）/谱系（genealogy）三闭环共用的小件：

- LEXICON / extract_terms：确定性概念锚定词表（中文子串 + 英文词边界，长词
  优先），查询词面与 fd-zhengxin-reference 的 SEP CANONICAL 对齐——这是三
  段式第一段「参照系召回」的本地召回层，LLM 缺席也能锚定；
- CONTEMPORARY_MARKERS / detect_contemporary：当代议题判定（显式入参优先，
  词面标记兜底，默认非当代——保守取向，宁可漏判交给调用方显式补，不冒进）；
- parse_llm_json：LLM 返回的宽松 JSON 解析（代码围栏 / 前后噪声容错，照
  fd-zhesi loops._parse_json 先例）；
- keep_valid_refs：引用校验——候选集内引用过滤（宁删勿假，无锚不出）；
- publish_gate：全部闭环产出的统一 publish 门——payload 过 guard_output，
  block 时调用方拿 HTTPException(422) 结构化拦截（不呈现），grey 附原因
  放行（界面置灰降权），干净放行不带任何推荐性标注（放行不是动作）。
"""

from __future__ import annotations

import json
import re
from typing import Iterable

from fastapi import HTTPException

from .guard import Action, Verdict, guard_output

# ---------------------------------------------------------------------------
# 确定性锚定词表（中文别名 → 参照系查询词）
#
# 查询词面与 fd-zhengxin-reference/fd_zhengxin_reference/sep_politics.py 的
# CANONICAL 26 词对齐（英文词即该表的键）。中文子串匹配、英文按词边界
# （大小写不敏感）；命中按关键词长度降序（越长越具体），照 fd-zhesi
# QUESTION_DOMAINS 的 map_domains 手法。
# ---------------------------------------------------------------------------

LEXICON: list[dict] = [
    {"term": "social contract",
     "keywords": ["社会契约", "契约论", "社会约定", "社会契约论", "social contract"]},
    {"term": "justice", "keywords": ["正义", "公正", "分配正义", "justice"]},
    {"term": "liberty",
     "keywords": ["自由", "自由权", "消极自由", "积极自由", "liberty", "freedom"]},
    {"term": "democracy", "keywords": ["民主", "民主制", "democracy"]},
    {"term": "sovereignty", "keywords": ["主权", "人民主权", "sovereignty"]},
    {"term": "legitimacy",
     "keywords": ["正当性", "合法性", "政治正当", "legitimacy"]},
    {"term": "authority", "keywords": ["权威", "政治权威", "authority"]},
    {"term": "rights", "keywords": ["权利", "人权", "自然权利", "rights"]},
    {"term": "equality", "keywords": ["平等", "平等主义", "equality"]},
    {"term": "property",
     "keywords": ["财产", "财产权", "所有权", "私有制", "property"]},
    {"term": "revolution", "keywords": ["革命", "革命权", "revolution"]},
    {"term": "liberalism", "keywords": ["自由主义", "liberalism"]},
    {"term": "conservatism", "keywords": ["保守主义", "conservatism"]},
    {"term": "socialism", "keywords": ["社会主义", "socialism"]},
    {"term": "nationalism", "keywords": ["民族主义", "nationalism"]},
    {"term": "republicanism", "keywords": ["共和主义", "共和", "republicanism"]},
    {"term": "constitutionalism",
     "keywords": ["宪政", "立宪主义", "宪法主义", "constitutionalism"]},
    {"term": "utilitarianism", "keywords": ["功利主义", "utilitarianism"]},
    {"term": "political representation",
     "keywords": ["政治代表", "代表制", "代议", "political representation"]},
    {"term": "civil disobedience",
     "keywords": ["公民不服从", "非暴力反抗", "公民抗命", "civil disobedience"]},
    {"term": "legalism (chinese philosophy)",
     "keywords": ["法家", "韩非", "商鞅", "legalism"]},
    {"term": "confucian political thought",
     "keywords": ["儒家", "儒学", "孔孟", "仁政", "德治", "confucian"]},
    {"term": "chinese political philosophy",
     "keywords": ["中国政治思想", "中国政治哲学", "先秦诸子", "chinese political philosophy"]},
    {"term": "confucius", "keywords": ["孔子", "论语", "confucius"]},
    {"term": "mencius", "keywords": ["孟子", "mencius"]},
]

_KW_CACHE: dict[str, re.Pattern[str]] = {}


def _kw_pattern(kw: str) -> re.Pattern[str]:
    pat = _KW_CACHE.get(kw)
    if pat is None:
        pat = re.compile(rf"(?<![a-z0-9]){re.escape(kw.lower())}(?![a-z0-9])")
        _KW_CACHE[kw] = pat
    return pat


def _kw_hit(kw: str, raw: str, low: str) -> bool:
    """中文关键词按子串、英文关键词按词边界（大小写不敏感）匹配。"""
    if any("\u4e00" <= ch <= "\u9fff" for ch in kw):
        return kw in raw
    return bool(_kw_pattern(kw).search(low))


def extract_terms(text: str, cap: int = 3) -> list[str]:
    """确定性锚定：文本 → 命中的参照系查询词（长词优先，去重截 cap 个）。"""
    raw = (text or "").strip()
    low = raw.lower()
    scored: list[tuple[int, int, str]] = []
    for order, entry in enumerate(LEXICON):
        best = max(
            (len(k) for k in entry["keywords"] if _kw_hit(k, raw, low)), default=0
        )
        if best:
            scored.append((-best, order, entry["term"]))
    scored.sort()
    out: list[str] = []
    for _, _, term in scored:
        if term not in out:
            out.append(term)
        if len(out) >= cap:
            break
    return out


# ---------------------------------------------------------------------------
# 当代议题判定：显式入参优先；词面标记兜底（保守——默认非当代）
# ---------------------------------------------------------------------------

CONTEMPORARY_MARKERS: tuple[str, ...] = (
    "当代", "当今", "现今", "当下", "近年", "近年来", "当前", "眼前",
    "今年", "去年", "现行", "现任", "执政", "在野", "大选", "选举", "公投",
    "改选", "民意调查", "民调", "执政党", "反对党", "现政府",
    "current", "today", "this year", "last year", "election", "referendum",
    "incumbent", "in office", "latest",
)

_RECENT_YEAR_RE = re.compile(r"\b20\d{2}\b")


def detect_contemporary(text: str, explicit: bool | None = None) -> bool:
    """当代议题判定：explicit 非 None 直接采信；否则词面标记（含 20xx 年份）
    命中即当代，默认非当代（保守取向：误判当代会触发强制多视角拦截，
    宁可让调用方显式声明，不冒进）。"""
    if explicit is not None:
        return bool(explicit)
    raw = (text or "")
    low = raw.lower()
    if any(m in raw for m in CONTEMPORARY_MARKERS if not m.isascii()):
        return True
    if any(m in low for m in CONTEMPORARY_MARKERS if m.isascii()):
        return True
    return bool(_RECENT_YEAR_RE.search(raw))


# ---------------------------------------------------------------------------
# LLM JSON 解析（宽松：代码围栏 / 前后噪声；照 fd-zhesi loops._parse_json）
# ---------------------------------------------------------------------------


def parse_llm_json(content: str) -> object | None:
    s = (content or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
        return None


# ---------------------------------------------------------------------------
# 引用校验：候选集内引用过滤（宁删勿假）
# ---------------------------------------------------------------------------


def keep_valid_refs(refs: Iterable, valid: set[str]) -> list[str]:
    """过滤引用列表：只留候选集内的 id，去重保序。LLM 编造的引用一律剥除。"""
    out: list[str] = []
    seen: set[str] = set()
    for r in refs:
        if isinstance(r, str) and r in valid and r not in seen:
            seen.add(r)
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# publish 守卫门：全部闭环产出过 guard_output 才可返回
# ---------------------------------------------------------------------------


def guard_dict(verdict: Verdict) -> dict:
    """裁决 → 可附到响应的字典（照 pulse.get_spectrum_guarded 的 guard 形态）。"""
    return {
        "action": verdict.action.value if verdict.action else None,
        "reasons": [
            {"rule_id": r.rule_id, "category": r.category, "detail": r.detail}
            for r in verdict.reasons
        ],
        "ruleset_version": verdict.ruleset_version,
    }


def publish_gate(
    payload: dict, *, blocked_detail: dict | None = None,
) -> tuple[dict, Verdict]:
    """闭环产出的 publish 门：payload 过 guard_output，返回 (裁决字典, 裁决)。

    不在此抛错——调用方先审计、再按动作处置：BLOCK → HTTPException(422,
    {"reason": "guard_blocked", "guard": …, **blocked_detail})（不呈现）；
    GREY → 正常返回但响应附 guard（action=grey，界面置灰降权）；None → 干净
    放行（guard.action=None，无推荐性标注）。
    """
    verdict = guard_output(payload)
    return guard_dict(verdict), verdict


def block_exc(verdict: Verdict, **extra: dict) -> HTTPException:
    """BLOCK 处置：422 结构化拦截，extra（如 gaps）并入 detail 供排查。"""
    detail = {"reason": "guard_blocked", "guard": guard_dict(verdict)}
    detail.update(extra)
    return HTTPException(status_code=422, detail=detail)


__all__ = [
    "LEXICON", "extract_terms", "detect_contemporary", "CONTEMPORARY_MARKERS",
    "parse_llm_json", "keep_valid_refs", "publish_gate", "guard_dict",
    "block_exc", "Action",
]
