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
# D4 对齐后词表有三段来源（共 115 词条，静态固化——运行时不读对方仓文件）：
#
# 1. 近现代五思潮词（5 条）：fd-zhengxin-reference 的 china_lineage.
#    MODERN_CURRENTS 标签（维新变法思潮/辛亥革命思潮/自由主义思潮/文化保守
#    主义（学衡派/新儒家早期）/社会主义论战），term=思潮全称（参照系按标签
#    精确命中）；
# 2. 先秦种子谱系（86 条）：fd-zhengxin-reference/seed/china_seed.json 的
#    概念名单（46 条，term=种子标签精确值）与人物名单（40 条，term=人物
#    全名标签——参照系关系面按精确全名匹配，简称不入词表）；
# 3. SEP CANONICAL 26 词的中文别名（24 条，原有词表）。
#
# 再生成方式（种子/近现代名单变更后重跑，产出替换第 1/2 段）：
#     python3 - <<'EOF'
#     import json
#     data = json.load(open("<参照系仓>/seed/china_seed.json"))
#     for c in data["concepts"]:
#         if len(c["label"]) >= 2:   # 单字概念（仁/礼/势/术）子串误命中率高，不入
#             print(json.dumps({"term": c["label"], "keywords": [c["label"]]},
#                              ensure_ascii=False))
#     for f in data["figures"]:
#         print(json.dumps({"term": f["label"], "keywords": [f["label"]]},
#                          ensure_ascii=False))
#     EOF
# 近现代五词手写（china_lineage.MODERN_CURRENTS 的标签 + 常用简称别名）。
#
# 匹配纪律：中文关键词按子串、英文按词边界（大小写不敏感）；命中按关键词
# 长度降序（越长越具体——「自由主义思潮」先于「自由主义」/liberalism，
# 种子直查优先于 SEP 伞形词），照 fd-zhesi QUESTION_DOMAINS 的 map_domains
# 手法。种子/近现代词条置于 SEP 别名段之前：等长并列时参照系库内直查胜出。
# ---------------------------------------------------------------------------

LEXICON: list[dict] = [
    # ---- 1) 近现代五思潮（term=思潮全称，参照系 currents 标签精确命中） ----
    {"term": "维新变法思潮", "keywords": ["维新变法思潮", "维新变法", "维新"]},
    {"term": "辛亥革命思潮", "keywords": ["辛亥革命思潮", "辛亥革命"]},
    {"term": "自由主义思潮", "keywords": ["自由主义思潮", "自由主义"]},
    {"term": "文化保守主义（学衡派/新儒家早期）",
     "keywords": ["文化保守主义（学衡派/新儒家早期）", "文化保守主义"]},
    {"term": "社会主义论战", "keywords": ["社会主义论战"]},
    # ---- 2) 先秦种子概念（term=种子标签，参照系 concepts 表精确命中） ----
    {"term": "仁政", "keywords": ["仁政"]},
    {"term": "王道", "keywords": ["王道"]},
    {"term": "霸道", "keywords": ["霸道"]},
    {"term": "德治", "keywords": ["德治"]},
    {"term": "民本", "keywords": ["民本"]},
    {"term": "正名", "keywords": ["正名"]},
    {"term": "大同", "keywords": ["大同"]},
    {"term": "小康", "keywords": ["小康"]},
    {"term": "性善论", "keywords": ["性善论"]},
    {"term": "性恶论", "keywords": ["性恶论"]},
    {"term": "忠恕", "keywords": ["忠恕"]},
    {"term": "中庸", "keywords": ["中庸"]},
    {"term": "天命", "keywords": ["天命"]},
    {"term": "有教无类", "keywords": ["有教无类"]},
    {"term": "义利之辨", "keywords": ["义利之辨"]},
    {"term": "无为", "keywords": ["无为"]},
    {"term": "道法自然", "keywords": ["道法自然"]},
    {"term": "小国寡民", "keywords": ["小国寡民"]},
    {"term": "柔弱胜刚强", "keywords": ["柔弱胜刚强"]},
    {"term": "齐物", "keywords": ["齐物"]},
    {"term": "逍遥", "keywords": ["逍遥"]},
    {"term": "绝圣弃智", "keywords": ["绝圣弃智"]},
    {"term": "内圣外王", "keywords": ["内圣外王"]},
    {"term": "法治（以法为本）", "keywords": ["法治（以法为本）", "法治", "以法为本"]},
    {"term": "法不阿贵", "keywords": ["法不阿贵"]},
    {"term": "耕战", "keywords": ["耕战"]},
    {"term": "以刑去刑", "keywords": ["以刑去刑"]},
    {"term": "兼爱", "keywords": ["兼爱"]},
    {"term": "非攻", "keywords": ["非攻"]},
    {"term": "尚贤", "keywords": ["尚贤"]},
    {"term": "尚同", "keywords": ["尚同"]},
    {"term": "节用", "keywords": ["节用"]},
    {"term": "天志", "keywords": ["天志"]},
    {"term": "非命", "keywords": ["非命"]},
    {"term": "明鬼", "keywords": ["明鬼"]},
    {"term": "白马非马", "keywords": ["白马非马"]},
    {"term": "离坚白", "keywords": ["离坚白"]},
    {"term": "合同异", "keywords": ["合同异"]},
    {"term": "五行", "keywords": ["五行"]},
    {"term": "阴阳", "keywords": ["阴阳"]},
    {"term": "五德终始", "keywords": ["五德终始"]},
    {"term": "不战而屈人之兵", "keywords": ["不战而屈人之兵"]},
    {"term": "知己知彼", "keywords": ["知己知彼"]},
    {"term": "上兵伐谋", "keywords": ["上兵伐谋"]},
    {"term": "合纵", "keywords": ["合纵"]},
    {"term": "连横", "keywords": ["连横"]},
    # ---- 2b) 先秦种子人物（term=人物全名标签；关系面按精确全名匹配） ----
    {"term": "孔子", "keywords": ["孔子"]},
    {"term": "曾参（曾子）", "keywords": ["曾参（曾子）"]},
    {"term": "孔伋（子思）", "keywords": ["孔伋（子思）"]},
    {"term": "卜商（子夏）", "keywords": ["卜商（子夏）"]},
    {"term": "言偃（子游）", "keywords": ["言偃（子游）"]},
    {"term": "孟轲（孟子）", "keywords": ["孟轲（孟子）"]},
    {"term": "告子", "keywords": ["告子"]},
    {"term": "荀况（荀子）", "keywords": ["荀况（荀子）"]},
    {"term": "老聃（老子）", "keywords": ["老聃（老子）"]},
    {"term": "关尹（尹喜）", "keywords": ["关尹（尹喜）"]},
    {"term": "杨朱", "keywords": ["杨朱"]},
    {"term": "列御寇（列子）", "keywords": ["列御寇（列子）"]},
    {"term": "文子", "keywords": ["文子"]},
    {"term": "庄周（庄子）", "keywords": ["庄周（庄子）"]},
    {"term": "田骈", "keywords": ["田骈"]},
    {"term": "环渊", "keywords": ["环渊"]},
    {"term": "管夷吾（管仲）", "keywords": ["管夷吾（管仲）"]},
    {"term": "李悝（李克）", "keywords": ["李悝（李克）"]},
    {"term": "卫鞅（商鞅）", "keywords": ["卫鞅（商鞅）"]},
    {"term": "申不害", "keywords": ["申不害"]},
    {"term": "慎到", "keywords": ["慎到"]},
    {"term": "韩非", "keywords": ["韩非"]},
    {"term": "李斯", "keywords": ["李斯"]},
    {"term": "墨翟（墨子）", "keywords": ["墨翟（墨子）"]},
    {"term": "禽滑釐", "keywords": ["禽滑釐"]},
    {"term": "孟胜", "keywords": ["孟胜"]},
    {"term": "宋钘", "keywords": ["宋钘"]},
    {"term": "邓析", "keywords": ["邓析"]},
    {"term": "惠施", "keywords": ["惠施"]},
    {"term": "公孙龙", "keywords": ["公孙龙"]},
    {"term": "尹文", "keywords": ["尹文"]},
    {"term": "邹衍", "keywords": ["邹衍"]},
    {"term": "邹奭", "keywords": ["邹奭"]},
    {"term": "孙武", "keywords": ["孙武"]},
    {"term": "吴起", "keywords": ["吴起"]},
    {"term": "孙膑", "keywords": ["孙膑"]},
    {"term": "鬼谷子（王诩）", "keywords": ["鬼谷子（王诩）"]},
    {"term": "张仪", "keywords": ["张仪"]},
    {"term": "苏秦", "keywords": ["苏秦"]},
    {"term": "公孙衍（犀首）", "keywords": ["公孙衍（犀首）"]},
    # ---- 3) SEP CANONICAL 26 词的中文别名（原有 24 词） ----
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
    {"term": "liberalism", "keywords": ["liberalism"]},
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
     "keywords": ["法家", "商鞅", "legalism"]},
    {"term": "confucian political thought",
     "keywords": ["儒家", "儒学", "孔孟", "confucian"]},
    {"term": "chinese political philosophy",
     "keywords": ["中国政治思想", "中国政治哲学", "先秦诸子", "chinese political philosophy"]},
    {"term": "confucius", "keywords": ["论语", "confucius"]},
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
