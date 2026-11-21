"""影响面环：某个主张/事件/人物 → 它点名了什么、往哪个方向、哪些环节未检验、
与你的处境是否字面相交。

这条环要回答的是「这份文件对**我**意味着什么」，而这件事有一条硬边界：
**政心不裁定利害。** 它能做的是把「文本直接说了什么」摊开（作用面）、
给出名义方向（收紧/放宽/中性）、标出待检验的传导环节（不自己检验）、
并把作用面与用户自己声明的处境做**字面**交叠。到「所以对你有利/不利」
那一步就不走了——那一步需要知道你的价值权重，而那只有你知道。

四层，每层都有一条不许越的线：

  L1 作用面    从正文逐字抽取要素，每个要素带 {item_id, doc_id, span, quote}
               且必须过 pulse.verify_evidence（quote ≡ body[start:end]）
               取不到的要素标「正文未载明」，不臆造

  L2 名义方向  每要素一个三值（收紧/放宽/中性），无措辞依据则 undetermined
               **不给强度分**——与柏讯「只有两档置信度、不写 0.8」同一条纪律

  L3 传导      **只出待检验清单**，tested 恒 None。
               柏讯的 policy-mapping/policy-transmission 是 SKILL.md 提示词、
               不是服务端点——正心运行期无可调之物，假装调了就是编造。
               `tested` 只能由外部回填，由测试递归断言锁死。

  L4 利害      **不产出**。系统不写「对你有利」「你应当调整」。

产出过 guard_output 才可返回（payload_kind="impact"，R-BAL-001 走另一套判据）。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from . import looputil, personal, reference_mcp
from .auth import require_user
from .guard import Action
from .infostream_client import InfoStreamClient, InfoStreamUnavailable
from .pulse import verify_evidence

router = APIRouter(prefix="/api/v1/loops", tags=["loops"])

MAX_CLAIMS_PER_TARGET = 20   # 实体 → 关联 claim 的有界展开

# 要素抽取的确定性规则：标签 → 正则。
# 实测形状（reports/baseline-2026-11-21.md 第二次实测）：ministry 正文剥 HTML 后是
# **标签一行、值在下一行**的政府网站布局，且日期是「2026年11月13日」不是 ISO；
# 早期「标签：值」同行形态也存在（财政部令样张）。两种都认。
# 导航噪音门槛：值不得是纯导航词（导航栏里同样出现「文号」「机构」）。
_NAV_NOISE = frozenset({
    "全部", "机构", "文号", "标题", "正文", "索引号", "名称", "主题", "首页",
    "公开", "服务", "互动", "数据", "专题", "政府", "网站", "当前位置", "其他",
    "动态", "体裁", "机构", "发文机关",
})
# 值形态：同行冒号后 / 下一行（标签独占一行时）
# 值形态：同行冒号后 / 下一行（标签独占一行时）。
# 上界是「撞上下一个已知标签」——同行混排时（「发文机关：财政部  成文日期：…」）
# 值必须是标签之间的那一段，否则会吞掉后半行。
_VAL = (
    r"(?:\s*[：:]\s*|\s*\n\s*)"
    r"(.{1,60}?)(?=\s{2,}(?:发文机关|发布机构|成文日期|实施日期|文\s*种|体\s*裁"
    r"|发文字号|文\s*号|索\s*引\s*号|名\s*称|主\s*题)\s*[：:]|\n|\Z)"
)

# 已知标签名：值撞上它就是「下一行的下一个标签」，说明本要素实际无值
_LABEL_WORDS = frozenset({
    "发布机构", "发文机关", "成文日期", "实施日期", "废止/失效日期",
    "索引号", "主题", "体裁", "文号", "发文字号", "名称", "正文",
})

_ELEMENT_PATTERNS: dict[str, re.Pattern[str]] = {
    "发文机关": re.compile(r"发文机关" + _VAL),
    "文号": re.compile(r"(?:发文字号|文\s*号)" + _VAL),
    "成文日期": re.compile(r"成文日期" + _VAL),
    "文种": re.compile(r"(?:文\s*种|体\s*裁)" + _VAL),
    "发布机构": re.compile(r"发布机构" + _VAL),
}


def _norm_date(value: str) -> str | None:
    """「2026年11月13日」/「2026-11-13」/「2026/11/13」→ ISO 日期；认不出返回 None。"""
    m = re.search(r"(\d{4})\s*[年\-/]\s*(\d{1,2})\s*[月\-/]\s*(\d{1,2})", value)
    if not m:
        return None
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"
# 「自 XXXX 年 X 月 X 日起施行/执行」——生效时间
_EFFECTIVE_RE = re.compile(
    r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日起\s*(施行|执行|实施)"
)
# 适用对象/范围：「各省、自治区、直辖市…」「本办法适用于…」
_SCOPE_RE = re.compile(r"(本办法|本规定|本通知|本意见|本条例)[^。\n]{0,20}(适用|施行)")
_ADDRESSEE_RE = re.compile(r"^(各省|各地区|各有关部门|各直属机构)[^\n]{0,60}", re.MULTILINE)

# 名义方向词表（政策措辞，与 pulse.STANCE_LEXICON 是两回事——那是舆论立场）。
# 三值闭集 + undetermined，**不给强度**。
DIRECTION_LEXICON: dict[str, tuple[str, ...]] = {
    "收紧": ("严格", "禁止", "不得", "限制", "取消", "停止", "处罚", "暂停", "从严", "清退"),
    "放宽": ("鼓励", "支持", "简化", "放宽", "下放", "优化", "免征", "减征", "补贴", "奖补"),
}
DIRECTION_UNDETERMINED = "undetermined"


class ImpactIn(BaseModel):
    target_kind: str = Field(pattern="^(claim|entity)$")
    target_ref: str = Field(min_length=1, max_length=500)
    is_contemporary: bool = True   # 政策文件天然当代；R-BAL-001 走 impact 判据


# ---------------------------------------------------------------- target 展开


def _claims_for_entity(
    rc: reference_mcp.ReferenceClient, ref: str,
    client: InfoStreamClient | None = None,
) -> tuple[list[str], dict]:
    """实体 → 关联资讯条目的有界展开。返回 (item_ids, coverage)。

    三级匹配，逐级退化，`match_mode` 如实记在 coverage 里：
    1. `InfoItem.entities` 元组命中 —— **实测对政治面恒不命中**
       （0/93，字段装的是个股代码，见 reports/baseline-2026-11-21.md）；
    2. 标题子串 —— 弱信号，但可靠；
    3. `search_info` 全文检索 —— 最弱，且必须读 mode（`mode="text-core"` 且
       count=0 不等于真的没有，probe 实测过的缺口）。

    实体本身查不到（coverage != available）时不展开，如实返回原因。
    """
    dossier = rc.entity_dossier(ref)
    if not dossier.ok:
        return [], {"status": dossier.reason, "detail": dossier.detail}
    data = dossier.data or {}
    cov = (data.get("coverage") or {}).get("status")
    if cov != "available":
        return [], {"status": cov or "unavailable",
                    "detail": (data.get("coverage") or {}).get("detail")}

    identity = data.get("identity") or {}
    names = {identity.get("label"), identity.get("ref")}
    names = {n for n in names if isinstance(n, str) and n and not n.startswith("seed:")}
    if not names:
        return [], {"status": "available", "match_mode": "none",
                    "detail": "实体可查但没有可用于检索的标签"}

    if client is None:
        return [], {"status": "available", "match_mode": "none",
                    "detail": "fd-info 客户端不可用（未配置或未注入）"}

    item_ids: list[str] = []
    modes: list[str] = []
    try:
        for name in list(names)[:2]:          # 最多两个名字（中/英各一次）
            r = client.search_info(name, source_types=["ministry"], limit=MAX_CLAIMS_PER_TARGET)
            mode = r.get("mode") or "unknown"
            modes.append(mode)
            for it in r.get("items") or []:
                iid = it.get("item_id")
                if iid and iid not in item_ids:
                    item_ids.append(iid)
    except InfoStreamUnavailable as exc:
        return [], {"status": exc.reason.code, "detail": exc.reason.detail}

    match_mode = "+".join(sorted(set(modes))) or "none"
    detail = f"经 search_info 检索（mode={match_mode}）命中 {len(item_ids)} 条"
    if "text-core" in match_mode and not item_ids:
        detail += "；text-core 模式 count=0 不可当真无命中（已知缺口）"
    return item_ids[:MAX_CLAIMS_PER_TARGET], {
        "status": "available", "match_mode": match_mode, "detail": detail,
    }


def _strip_html(text: str) -> str:
    """HTML → 纯文本（不引新依赖：标准库够用）。

    实测（reports/baseline-2026-11-21.md）：fd-info 的 `get_body` 对 ministry
    条目吐的是**原始 HTML**（spool 里 95/95 条如此），不剥标签会把导航栏里的
    「文号」「机构」当成政策要素抽出来——比抽不出更糟。

    shortcut: 正则剥标签、不做 DOM 解析，对畸形 HTML 可能残留少量标签文本；
    若正文来源改为 text-core 的已清洗语料（corpus_documents.content）则可整段
    去掉本函数。
    """
    if not text or "<" not in text:
        return text or ""
    t = re.sub(r"<script[^>]*>.*?</script>|<style[^>]*>.*?</style>", " ", text,
               flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</p>|</div>|</tr>|</h[1-6]>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    t = re.sub(r"[ \t　]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


def _fetch_claim_body(client: InfoStreamClient, item: dict) -> tuple[str | None, dict]:
    """取条目正文 + 返回抓取状态（不归一：no_text/oversize/failed 语义不同）。"""
    status = item.get("body_status")
    if status != "stored":
        return None, {"body_unavailable": status or "unknown"}
    doc_id = item.get("doc_id")
    if not doc_id:
        return None, {"body_unavailable": "no_doc_id"}
    try:
        payload = client.get_body(doc_id)
    except InfoStreamUnavailable as exc:
        return None, {"body_unavailable": f"upstream:{exc.reason.code}"}
    if not payload.get("ok", True) or payload.get("error"):
        return None, {"body_unavailable": "fetch_failed"}
    raw = payload.get("body") or payload.get("content") or ""
    body = _strip_html(raw)
    return (body or None), ({} if body else {"body_unavailable": "empty_body"})


# ---------------------------------------------------------------- L1 作用面


def extract_elements(body: str) -> list[dict]:
    """从正文确定性抽取作用要素。每个要素带 span，供 verify_evidence 校验。

    取不到的要素不出现（正文未载明不是「空值字段」，是它根本不在这份文件里）。
    """
    out: list[dict] = []
    for label, pat in _ELEMENT_PATTERNS.items():
        for m in pat.finditer(body):
            value = m.group(1).strip()
            if label in ("成文日期",):
                iso = _norm_date(value)
                if not iso:
                    continue
                value = iso
            if value in _NAV_NOISE or len(value) < 2:
                continue   # 导航栏噪音，不是要素
            # 「标签一行、值在下一行」形态下，空值会让**下一个标签**被当成值
            # （实测：文号='发布机构'）。值是已知标签名 = 该要素实际无值。
            if value in _LABEL_WORDS:
                continue
            if label == "发布机构" and any(
                e["element"] == "发文机关" and e["value"] == value for e in out
            ):
                continue   # 与发文机关重复时只留一条
            out.append({"element": label, "value": value,
                        "span": [m.start(1), m.end(1)], "quote": m.group(1)})
            break
    m = _EFFECTIVE_RE.search(body)
    if m:
        out.append({
            "element": "生效日期",
            "value": f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}",
            "span": [m.start(), m.end()], "quote": m.group(0),
        })
    heads = _ADDRESSEE_RE.findall(body)
    if heads:
        m = _ADDRESSEE_RE.search(body)
        out.append({"element": "主送/适用对象", "value": m.group(0).strip(),
                    "span": [m.start(), m.end()], "quote": m.group(0)})
    return out


def verify_elements(elements: list[dict], body: str) -> tuple[list[dict], list[dict]]:
    """每个要素过 verify_evidence；不过的丢弃并记 gap（宁删勿假）。"""
    kept, dropped = [], []
    for el in elements:
        ev = {"span": el["span"], "quote": el["quote"]}
        if verify_evidence(ev, body):
            kept.append(el)
        else:
            dropped.append({"element": el["element"],
                            "reason": "quote_mismatch",
                            "detail": "引用与正文不逐字一致，已丢弃"})
    return kept, dropped


# ---------------------------------------------------------------- L2 名义方向


def infer_direction(text: str) -> tuple[str, list[str]]:
    """名义方向三值 + undetermined。返回 (方向, 命中的措辞)。

    无措辞依据时返回 undetermined——**不是「中性」**。中性是一个判断（有措辞
    但两向都不占优），undetermined 是「判断不了」，两者不是同一件事。
    """
    hits = {d: [w for w in words if w in text] for d, words in DIRECTION_LEXICON.items()}
    upward, downward = len(hits.get("收紧", [])), len(hits.get("放宽", []))
    if upward == 0 and downward == 0:
        return DIRECTION_UNDETERMINED, []
    if upward > downward:
        return "收紧", hits["收紧"]
    if downward > upward:
        return "放宽", hits["放宽"]
    return "中性", hits["收紧"] + hits["放宽"]   # 双向均出现 → 中性（有依据的判断）


# ---------------------------------------------------------------- L3 传导（只出清单）


def transmission_candidate(element: dict) -> dict:
    """作用要素 → 待检验条目。`tested` **恒 None**，只能由外部回填。"""
    return {
        "element": element["element"],
        "value": element["value"],
        "evidence": {
            "span": element["span"],
            "quote": element["quote"],
        },
        "direction": element.get("direction"),
        "tested": None,
    }


# ---------------------------------------------------------------- L4 处境交叠


def situation_overlap(
    elements: list[dict], situation: list[dict],
) -> list[dict]:
    """作用面 × 有效处境 → 字面交叠三值（hit / no_literal_overlap / undetermined）。

    **互为子串**允许（「新能源汽车」vs「新能源汽车整车制造」——这是字面关系）；
    **上下游不推断**（正心没有供应链语义，有也不该有）。
    """
    if not situation:
        return [{"element": e["element"], "relevance": "undetermined",
                 "detail": "无有效处境声明（声明处境可获得交叠结果）"} for e in elements]

    out = []
    for e in elements:
        value = e["value"]
        matched = None
        for s in situation:
            sv = (s.get("value") or "").strip()
            if not sv:
                continue
            if sv in value or value in sv:
                matched = s
                break
        if matched:
            out.append({"element": e["element"], "relevance": "hit",
                        "situation_kind": matched["kind"],
                        "situation_value": matched["value"],
                        "detail": f"作用面点名对象与你的 {matched['kind']} 声明字面相交"})
        else:
            out.append({"element": e["element"], "relevance": "no_literal_overlap",
                        "detail": "作用面点名对象与你声明的处境字面不同"
                                  "（不推断上下游）"})
    return out


# ---------------------------------------------------------------- API


@router.post("/impact")
def impact(body: ImpactIn, user_id: str = Depends(require_user)) -> dict:
    """影响面环：作用面 → 名义方向 → 待检验清单 → 处境交叠。不裁定利害。"""
    from . import config

    rc = reference_mcp.client()
    client = InfoStreamClient()
    gaps: list[dict] = []

    # ---- 解析目标为 claim 集合 ----
    if body.target_kind == "claim":
        item_ids = [body.target_ref]
        match_mode = "direct"
        coverage = {"status": "available", "detail": "按 item_id 直接指定"}
    else:
        item_ids, coverage = _claims_for_entity(rc, body.target_ref, client)
        match_mode = coverage.get("match_mode") or "entity_expansion"
        if coverage.get("status") != "available":
            gaps.append({"stage": "target", "reason": coverage.get("status"),
                         "detail": coverage.get("detail")})
        else:
            gaps.append({
                "stage": "target", "reason": "entity_claim_link_unavailable",
                "detail": (
                    "实体 → 资讯条目的关联面尚未接线：fd-info 的 entities 元组对"
                    "政治信息面填充率为 0/93（实测），标题/全文匹配需 fd-info 检索面"
                    "接线后启用。当前实体目标只返回实体本身，无关联 claim。"
                ),
            })

    claims_out: list[dict] = []
    all_elements: list[dict] = []
    if item_ids:
        try:
            health = client.stream_health()
        except InfoStreamUnavailable as exc:
            gaps.append({"stage": "upstream", "reason": exc.reason.code,
                         "detail": exc.reason.detail})
            health = None
        ctx = (health or {}).get("sources") or (health or {}).get("data") or {}
        for item_id in item_ids[:MAX_CLAIMS_PER_TARGET]:
            try:
                item = client.get_item(item_id)
            except InfoStreamUnavailable as exc:
                gaps.append({"stage": "upstream", "reason": exc.reason.code,
                             "detail": f"get_item({item_id})：{exc.reason.detail}"})
                continue
            if item.get("error"):
                gaps.append({"stage": "claim", "reason": "not_found",
                             "detail": f"item {item_id} 不存在"})
                continue
            body_text, body_gap = _fetch_claim_body(client, item)
            if body_gap:
                gaps.append({"stage": "body", "item_id": item_id, **body_gap,
                             "detail": f"正文不可得（{body_gap['body_unavailable']}），"
                                       "只做标题层登记"})
                claims_out.append({
                    "item_id": item_id,
                    "title": item.get("title"),
                    "published_at": item.get("published_at"),
                    "body_available": False,
                    "elements": [], "direction": None,
                })
                continue

            raw_elements = extract_elements(body_text)
            elements, dropped = verify_elements(raw_elements, body_text)
            for d in dropped:
                gaps.append({"stage": "elements", "item_id": item_id, **d})
            for el in elements:
                el["direction"] = infer_direction(el["value"])[0]
                el["item_id"] = item_id
            all_elements.extend(elements)
            claims_out.append({
                "item_id": item_id,
                "title": item.get("title"),
                "published_at": item.get("published_at"),
                "doc_id": item.get("doc_id"),
                "body_available": True,
                "elements": elements,
            })

    # ---- L3：待检验清单（tested 恒 None） ----
    candidates = [transmission_candidate(e) for e in all_elements]

    # ---- L4：处境交叠 ----
    conn = personal.connect()
    try:
        conn_ok = True
        situation = personal.list_situation(conn, user_id)
    finally:
        conn.close()
    overlaps = situation_overlap(all_elements, situation)

    # ---- publish 门（payload_kind="impact"：R-BAL-001 走作用对象/方向判据） ----
    payload = {
        "payload_kind": "impact",
        "topic": f"{body.target_kind}:{body.target_ref}"[:200],
        "is_contemporary": body.is_contemporary,
        "claims": [
            {"text": f"{c['title']}", "evidence": {"quote": c.get("title") or ""}}
            for c in claims_out
        ],
        "elements": [
            {"target": e["value"], "direction": e.get("direction")}
            for e in all_elements
        ],
        "views": [],
        "body": _render_body(claims_out),
    }
    gdict, verdict = looputil.publish_gate(payload)

    conn = personal.connect()
    try:
        personal.audit(
            conn, user_id, "loops.impact", body.target_ref, "allowed",
            detail=(f"claims={len(claims_out)} elements={len(all_elements)}"
                    f" candidates={len(candidates)} overlaps={len(overlaps)}"
                    f" guard={verdict.action.value if verdict.action else 'pass'}"),
        )
    finally:
        conn.close()

    if verdict.action is Action.BLOCK:
        raise looputil.block_exc(verdict, gaps=gaps)

    return {
        "target": {"kind": body.target_kind, "ref": body.target_ref},
        "match_mode": match_mode,
        "coverage": coverage,
        "claims": claims_out,
        "transmission_candidates": candidates,
        "situation_overlap": overlaps,
        "engine": "deterministic",
        "gaps": gaps,
        "guard": gdict,
        "notice": (
            "本环呈现文本作用面与待检验环节，不裁定对你有利或不利，"
            "也不给行动建议；传导环节须由外部检验方回填结论。"
        ),
    }


def _render_body(claims: list[dict]) -> str:
    """转述性正文：只陈述文本点名了什么，不写后果、不写建议。"""
    if not claims:
        return "未取到可呈现的作用要素（缺口见 gaps）。"
    parts = []
    for c in claims[:5]:
        els = c.get("elements") or []
        if els:
            parts.append(f"《{c['title']}》载明：" + "；".join(
                f"{e['element']}={e['value']}" for e in els[:4]))
        else:
            parts.append(f"《{c['title']}》（正文不可得，仅标题层）")
    return "。".join(parts) + "。以上为正文要素转述，不作利害裁定。"
