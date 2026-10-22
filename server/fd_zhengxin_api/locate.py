"""定位闭环（task 4.2）：事件/政策/概念描述进，参照系坐标出。

三段式（照 fd-zhesi loops.py 先例：参照系召回 → LLM 重排 → 引用校验）：
1. 概念锚定：looputil 确定性词表命中 → reference_mcp.concept_lookup 归一
   （定义摘要 + 来源链接 + provenance.stable_id 引用锚），查不到不编造；
2. 谱系定位：概念行自带 school/weight 的直接归派（curated 直挂，锚=概念行
   stable_id）+ 概念关联人物对（figures）经 relation_query 拉师承/批判边
   （边行 school/weight/key_text/target_years，离散谱系——归派结论一律挂
   参照系行 stable_id，不做连续谱打分）；当代对象（is_contemporary）另补
   ideology_query 坐标（P1142/P1387/MARPOR，参数 name=QID 或标签）；
3. 历史先例链：带年代的边按年排序成链，每环 人物/年代/关键文本 + 参照系
   行 stable_id；缺年代的边不入链（不编造年代）。

D2 契约对齐（以参照系 tools_placeholder.py 实测行结构为准）：
- 概念行 id 锚 = provenance.stable_id；definition 取 snippet/summary；
  qid 非通用键（种子/近现代行没有）——关系召回改按 figures 人物标签对查询；
- 边行 id 锚 = provenance.stable_id；school/target_name/weight/key_text 从
  行结构映射，target_years 为 curated 年代串（「约前372–前289」），本地只做
  保守解析（取首个「前N/N」为年代，负数=公元前），解析不出不入链。

铁律：
- 不硬归派：最强候选强度（学派最强边/锚权重）低于阈值 MIN_POSITION_STRENGTH
  → positioned=false + 缺口说明，不拿弱证据凑数；
- 引用校验：每个归派结论的依据必须指向参照系行 stable_id（候选集内），LLM 给的
  引用一律过滤，无锚的结论不出现在输出；
- LLM 只做重排：从过了阈值的候选清单里选/排序（index-only），不能发明候选、
  不能越阈值归派；无 KEY / LLM 失败 → 确定性路径（召回序 + 规则打分），
  engine=deterministic；LLM 调用经 llm_ledger.chat_logged(surface="locate")；
- 产出过 guard_output 才可返回（publish 门在 looputil.publish_gate）：当代
  议题 views<2 学派会被 R-BAL-001 拦（422 结构化返回，不呈现）。

main.py 挂载（接线时两行）：from .locate import router as locate_router;
app.include_router(locate_router)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from . import llm_ledger, looputil, personal, reference_mcp
from .auth import require_user
from .guard import Action
from .llm import LLMError

router = APIRouter(prefix="/api/v1/loops", tags=["loops"])

MIN_POSITION_STRENGTH = 0.30  # 最强边权重低于此值不归派（照 fd-zhesi steelman 门槛口径）
MAX_TERMS = 3
MAX_ANCHORS_FOR_PAIRS = 3    # 参与人物对查询的概念锚数上限（每锚人物两两配对）
MAX_PAIRS_PER_ROUND = 8      # 每锚人物对查询上限（深度有界防漫游）


class LocateIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)   # 事件/政策/概念描述
    is_contemporary: bool = False                       # 当代对象 → 补 ideology_query 坐标


# ---------------------------------------------------------------------------
# 参照系召回（第一段）：概念锚定 + 字段映射（D2 契约对齐）
# ---------------------------------------------------------------------------


def _stable_id(entry: dict) -> str:
    """参照系行的引用锚：provenance.stable_id（D2；行内不再有裸 id 键）。"""
    prov = entry.get("provenance")
    return str((prov or {}).get("stable_id") or entry.get("id") or "")


def anchor_from_row(entry: dict, term: str) -> dict | None:
    """参照系概念行 → 概念锚（三闭环共用的字段映射层）。

    宽容映射（各源行字段不同，逐一回退，不编造）：
    - ref：provenance.stable_id（无锚的行丢弃——引用校验的需要）；
    - term：term → label（近现代行用 label）→ 入参词；
    - definition：definition → snippet（种子行一句话界定+原典锚）→ summary（近现代）；
    - school/weight：种子概念行/近现代思潮行的 curated 直挂归派（无则空）；
    - figures：种子概念行的关联人物 [{name, years}]（关系面人物对查询的种子）。
    """
    if not isinstance(entry, dict):
        return None
    ref = _stable_id(entry)
    if not ref:
        return None
    out_term = str(entry.get("term") or entry.get("label") or term)
    return {
        "term": out_term,
        "ref": ref,
        "title": str(entry.get("title") or out_term),
        "definition": str(
            entry.get("definition") or entry.get("snippet") or entry.get("summary") or ""
        ),
        "url": str(entry.get("url") or ""),
        "source": str(entry.get("source") or (entry.get("provenance") or {}).get("source") or ""),
        "qid": str(entry.get("qid") or ""),
        "school": str(entry.get("school") or ""),
        "weight": _norm_weight(entry.get("weight")),
        "figures": [
            {"name": str(f.get("name") or ""), "years": str(f.get("years") or "")}
            for f in entry.get("figures") or []
            if isinstance(f, dict) and f.get("name")
        ],
    }


def _anchor_concepts(rc: reference_mcp.ReferenceClient, text: str) -> tuple[list[dict], list[dict]]:
    """概念锚定：词表命中 → concept_lookup 归一。返回 (anchors, gaps)。"""
    anchors: list[dict] = []
    gaps: list[dict] = []
    terms = looputil.extract_terms(text, cap=MAX_TERMS)
    if not terms:
        gaps.append({
            "stage": "anchoring", "reason": "no_term_match",
            "detail": "输入未命中确定性锚定词表（中文子串/英文词边界），无概念锚——"
                      "不凭空定义",
        })
        return anchors, gaps
    seen: set[str] = set()
    for term in terms:
        r = rc.concept_lookup(term)
        if not r.ok:
            gaps.append({
                "stage": "reference", "reason": r.reason,
                "detail": f"concept_lookup({term!r})：{r.detail}",
            })
            continue
        for entry in r.data.get("results") or []:
            anchor = anchor_from_row(entry, term)
            if anchor is None or anchor["ref"] in seen:
                continue
            seen.add(anchor["ref"])
            anchors.append(anchor)
    return anchors, gaps


def _norm_year(value: object) -> int | None:
    try:
        year = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return year if -3000 <= year <= 2100 else None


_YEAR_RANGE_RE = None  # 延迟编译（模块导入零成本）


def year_from_range(text: str) -> int | None:
    """curated 年代串 → 保守年代值：「约前372–前289」→ -372（首个年份，
    前=负）；「前507–?」→ -507；无年份数字（如「?」）→ None 不编造。"""
    global _YEAR_RANGE_RE
    import re

    if _YEAR_RANGE_RE is None:
        _YEAR_RANGE_RE = re.compile(r"(前)?(\d{1,4})")
    m = _YEAR_RANGE_RE.search(str(text or ""))
    if m is None:
        return None
    year = int(m.group(2))
    if m.group(1):  # 「前」= 公元前
        year = -year
    return year if -3000 <= year <= 2100 else None


def _norm_weight(value: object) -> float:
    try:
        w = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, w))


def _norm_edge(entry: dict, anchor: dict | None) -> dict:
    """参照系关系行 → 归一结构（D2：行锚=stable_id；weight 夹 0..1；
    year 从 curated 年代串保守解析，解析不出记 None 不编造）。"""
    if not isinstance(entry, dict):
        return {}
    prov = entry.get("provenance") or {}
    target_name = str(entry.get("target_name") or entry.get("label_target") or "")
    return {
        "id": _stable_id(entry),
        "anchor_ref": (anchor or {}).get("ref", ""),
        "qid": str(entry.get("qid") or ""),
        "target_qid": str(entry.get("target_qid") or entry.get("target_id") or ""),
        "target_name": target_name,
        "target_kind": str(entry.get("target_kind") or "person"),
        "polarity": str(entry.get("polarity") or ""),
        "pid": str(entry.get("property_pid") or entry.get("pid") or ""),
        "weight": _norm_weight(entry.get("weight")),
        "school": str(entry.get("school") or entry.get("target_school") or ""),
        "year": _norm_year(entry.get("year"))
        if entry.get("year") is not None
        else year_from_range(str(entry.get("target_years") or "")),
        "key_text": str(
            entry.get("key_text") or entry.get("evidence") or entry.get("kind") or ""
        ),
        "stance": str(entry.get("stance") or ""),
    }


def _figure_pairs(anchors: list[dict]) -> list[tuple[dict, str, str]]:
    """概念锚 → 人物对（锚内 figures 两两；每锚上限 MAX_PAIRS_PER_ROUND）。"""
    pairs: list[tuple[dict, str, str]] = []
    for anchor in anchors[:MAX_ANCHORS_FOR_PAIRS]:
        names = [f["name"] for f in anchor.get("figures") or [] if f.get("name")]
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                pairs.append((anchor, names[i], names[j]))
                if len(pairs) >= MAX_PAIRS_PER_ROUND:
                    return pairs
    return pairs


def _recall_edges(
    rc: reference_mcp.ReferenceClient, anchors: list[dict],
) -> tuple[list[dict], list[dict]]:
    """谱系定位召回（D2）：对概念锚关联人物做 relation_query 人物对查询。

    参照系关系面是人物对（source+target）查询而非「某实体全部边」——召回
    种子即概念行 figures。返回 (edges, gaps)；edges 按 stable_id 去重。
    """
    edges: list[dict] = []
    gaps: list[dict] = []
    seen: set[str] = set()
    queried = False
    for anchor, src, tgt in _figure_pairs(anchors):
        queried = True
        r = rc.relation_query(src, tgt)
        if not r.ok:
            gaps.append({
                "stage": "reference", "reason": r.reason,
                "detail": f"relation_query({src!r}, {tgt!r})：{r.detail}",
            })
            continue
        for entry in r.data.get("results") or []:
            edge = _norm_edge(entry, anchor)
            if not edge["id"] or edge["id"] in seen:
                continue
            seen.add(edge["id"])
            edges.append(edge)
    if anchors and not queried:
        # 概念锚单人物/无人物：参照系关系面为人物对查询，无对可配——如实记缺口
        figures = [f["name"] for a in anchors for f in a.get("figures") or [] if f.get("name")]
        gaps.append({
            "stage": "positioning", "reason": "no_pair_to_query",
            "detail": (
                "概念锚关联人物 {} 个（{}），参照系关系面为人物对查询，无可配对——"
                "关系边召回为空（不编造邻接）".format(
                    len(figures), "、".join(figures[:3]) or "无"
                )
            ),
        })
    return edges, gaps


# ---------------------------------------------------------------------------
# 规则打分 + LLM 重排（第二段）
# ---------------------------------------------------------------------------


def _edge_stance(edge: dict) -> str:
    """边的立场描述：优先参照系自带 stance，否则由极性+关键文本转述。"""
    if edge["stance"]:
        return edge["stance"]
    if edge["polarity"] in ("critical", "critique", "opposition"):
        return f"批判立场（{edge['key_text'] or '见参照系边'}）"
    return f"承袭立场（{edge['key_text'] or '见参照系边'}）"


def anchor_candidates(anchors: list[dict]) -> list[dict]:
    """概念锚直出的归派候选（D2）：参照系概念行自带 school+weight（curated
    直挂），锚 stable_id 即引用依据——离散归派的最短证据路径。

    同学派多锚合并（evidence 并集，强度取最大，stance 取最强锚的界定）。
    """
    by_school: dict[str, dict] = {}
    for order, a in enumerate(anchors):
        if not a["school"] or not a["ref"] or a["weight"] <= 0:
            continue
        cand = by_school.setdefault(
            a["school"],
            {"school": a["school"], "evidence": [], "strength": 0.0,
             "stance": "", "order": order},
        )
        if a["ref"] not in cand["evidence"]:
            cand["evidence"].append(a["ref"])
        if a["weight"] > cand["strength"]:
            cand["strength"] = a["weight"]
            cand["stance"] = a["definition"] or f"参照系概念锚（{a['source']}）"
    return sorted(by_school.values(), key=lambda c: (-c["strength"], c["order"]))


def merge_candidates(*groups: list[dict]) -> list[dict]:
    """多来源候选按学派合并：evidence 并集、强度取最大（stance 随最强者）。

    概念锚直挂与关系边聚合可能对同学派各出一份证据（如法家概念 + 法家师承
    边）——呈现层一个学派一条结论，证据合并。
    """
    by_school: dict[str, dict] = {}
    for group in groups:
        for cand in group:
            cur = by_school.setdefault(cand["school"], {**cand, "evidence": list(cand["evidence"])})
            for e in cand["evidence"]:
                if e not in cur["evidence"]:
                    cur["evidence"].append(e)
            if cand["strength"] > cur["strength"]:
                cur["strength"] = cand["strength"]
                cur["stance"] = cand["stance"]
    return sorted(by_school.values(), key=lambda c: (-c["strength"], -len(c["evidence"]), c["order"]))


def school_candidates(edges: list[dict]) -> list[dict]:
    """确定性规则打分：按学派聚合边，强度 = 最强边权重（离散谱系，不打连续分）。

    排序：强度降序 → 支持边数 → 召回序（无 KEY 时的确定性候选序）。
    """
    by_school: dict[str, dict] = {}
    for order, edge in enumerate(edges):
        if not edge.get("school") or not edge.get("id"):
            continue
        cand = by_school.setdefault(
            edge["school"],
            {"school": edge["school"], "evidence": [], "strength": 0.0,
             "stance": "", "order": order},
        )
        if edge["id"] not in cand["evidence"]:
            cand["evidence"].append(edge["id"])
        if edge["weight"] > cand["strength"]:
            cand["strength"] = edge["weight"]
            cand["stance"] = _edge_stance(edge)
    out = sorted(by_school.values(), key=lambda c: (-c["strength"], -len(c["evidence"]), c["order"]))
    return out


def _llm_rerank(text: str, eligible: list[dict]) -> list[int] | None:
    """LLM 重排：从过了阈值的候选清单严格选 1-3 个（index-only，不许发明）。

    返回 None 表示无 KEY；返回 1-based 编号列表。非法编号按 LLMError 处理
    （调用方降级确定性路径）。
    """
    lines = "\n".join(
        f"{i}. 学派「{c['school']}」 强度 {c['strength']} 依据 {', '.join(c['evidence'][:2])}"
        for i, c in enumerate(eligible, 1)
    )
    raw = llm_ledger.chat_logged(
        [
            {"role": "system", "content": (
                "你是政治思想定位导航员，为一段事件/政策/概念描述挑选最站得住的学派"
                "归属。只能从候选清单里选，绝不发明新学派，不得选清单外的编号。"
                "只输出 JSON。"
            )},
            {"role": "user", "content": (
                f"输入描述：{text}\n\n过了证据阈值的候选学派：\n{lines}\n\n"
                "选出最贴近的 1-3 个学派并按贴切度排序。"
                '只输出 JSON：{"indices": [编号, ...]}'
            )},
        ],
        surface="locate",
        temperature=0.0,
        max_tokens=200,
    )
    if raw is None:
        return None
    data = looputil.parse_llm_json(raw)
    idx = data.get("indices") if isinstance(data, dict) else None
    if not isinstance(idx, list) or not idx or not all(
        isinstance(i, int) and not isinstance(i, bool) and 1 <= i <= len(eligible)
        for i in idx
    ):
        raise LLMError("locate 重排返回的 indices 非法")
    picked: list[int] = []
    for i in idx:
        if i not in picked:
            picked.append(i)
    return picked


# ---------------------------------------------------------------------------
# 历史先例链（第三段素材）与正文渲染
# ---------------------------------------------------------------------------


def precedent_chain(edges: list[dict]) -> list[dict]:
    """带年代的边 → 年份升序链；缺年代不入链（调用方记缺口，不编造年代）。"""
    rings = [
        {
            "name": e["target_name"] or e["school"] or e["id"],
            "year": e["year"],
            "note": e["key_text"],
            "school": e["school"],
            "ref": e["id"],
        }
        for e in edges
        if e["year"] is not None and e["id"]
    ]
    rings.sort(key=lambda r: (r["year"], r["ref"]))
    return rings


def _render_body(text: str, attributions: list[dict], chain: list[dict]) -> str:
    """转述性正文：只拼已过引用校验的结论，不用口径风险句式。"""
    parts: list[str] = []
    if attributions:
        parts.append("谱系定位：" + "、".join(
            f"{a['school']}（依据 {a['evidence'][0]}）" for a in attributions
        ))
    if chain:
        parts.append("历史先例链：" + "→".join(
            f"{r['name']}（{r['year']}）" for r in chain[:6]
        ))
    if not parts:
        return f"「{text[:60]}」暂无参照系定位结论（缺口见 gaps，不硬归派）。"
    return f"「{text[:60]}」" + "；".join(parts) + "。以上为参照系条目转述，不作裁断。"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@router.post("/locate")
def locate(body: LocateIn, user_id: str = Depends(require_user)) -> dict:
    """定位闭环：概念锚定 → 谱系定位（+当代坐标）→ 先例链，三段式产出。"""
    rc = reference_mcp.client()
    gaps: list[dict] = []

    anchors, anchor_gaps = _anchor_concepts(rc, body.text)
    gaps.extend(anchor_gaps)

    edges, edge_gaps = _recall_edges(rc, anchors)
    gaps.extend(edge_gaps)

    # ---- 候选打分（概念锚直挂 + 关系边聚合，按学派合并）→ 阈值门 → LLM 重排 ----
    candidates = merge_candidates(anchor_candidates(anchors), school_candidates(edges))
    eligible = [c for c in candidates if c["strength"] >= MIN_POSITION_STRENGTH]
    if candidates and not eligible:
        gaps.append({
            "stage": "positioning", "reason": "insufficient_evidence",
            "detail": (
                f"最强候选强度 {candidates[0]['strength']} < 阈值 "
                f"{MIN_POSITION_STRENGTH}（参照系边权重不足），不硬归派"
            ),
        })
    attributions = list(eligible)
    engine = "deterministic"
    if eligible:
        try:
            picks = _llm_rerank(body.text, eligible)
            if picks is not None:  # None=无 KEY；列表=重排成功
                engine = "llm"
                attributions = [eligible[i - 1] for i in picks]
        except LLMError:
            gaps.append({
                "stage": "llm", "reason": "llm_fallback",
                "detail": "LLM 重排失败，回退确定性路径（召回序+规则打分）",
            })

    # ---- 引用校验：归派结论的依据必须指向候选集内行 stable_id，无锚不出 ----
    edge_ids = {e["id"] for e in edges} | {a["ref"] for a in anchors}
    validated: list[dict] = []
    for cand in attributions:
        evidence = looputil.keep_valid_refs(cand["evidence"], edge_ids)
        if not evidence:
            continue
        validated.append({**cand, "evidence": evidence})

    chain = precedent_chain(edges)
    yearless = [e for e in edges if e["year"] is None and e["id"]]
    if yearless:
        gaps.append({
            "stage": "chain", "reason": "no_year",
            "detail": f"{len(yearless)} 条关系边缺年代，未入先例链（不编造年代）",
        })

    # ---- 当代对象补 ideology_query 坐标（参数 name：qid 或标签） ----
    coordinates: list[dict] = []
    if body.is_contemporary:
        names = [a["qid"] or a["term"] for a in anchors][:MAX_TERMS]
        for name in list(dict.fromkeys(names)):
            r = rc.ideology_query(name)
            if not r.ok:
                gaps.append({
                    "stage": "contemporary", "reason": r.reason,
                    "detail": f"ideology_query({name!r})：{r.detail}",
                })
                continue
            for entry in r.data.get("results") or []:
                if not isinstance(entry, dict):
                    continue
                ref = _stable_id(entry)
                if not ref:
                    continue
                coordinates.append({
                    "qid": str(entry.get("qid") or name),
                    "name": str(entry.get("label_source") or entry.get("anchor_label") or ""),
                    "ref": ref,
                    "anchors": [
                        {
                            "axis": str(entry.get("anchor_pid") or ""),
                            "value": str(entry.get("anchor_label") or ""),
                            "basis": str(entry.get("anchor_pid") or ""),
                        },
                    ],
                })

    positioned = bool(validated)
    body_text = _render_body(body.text, validated, chain)

    # ---- publish 门：产出过 guard_output 才可返回 ----
    payload = {
        "topic": body.text[:200],
        "is_contemporary": body.is_contemporary,
        "claims": [
            {"text": f"归派「{a['school']}」：{a['stance']}", "source": a["evidence"][0]}
            for a in validated
        ],
        "views": [{"school": a["school"], "stance": a["stance"]} for a in validated],
        "body": body_text,
    }
    gdict, verdict = looputil.publish_gate(payload)

    conn = personal.connect()
    try:
        personal.migrate(conn)
        personal.audit(
            conn, user_id, "loops.locate", None, "allowed",
            detail=(
                f"engine={engine} positioned={positioned} schools={len(validated)}"
                f" chain={len(chain)} gaps={len(gaps)}"
                f" guard={verdict.action.value if verdict.action else 'pass'}"
            ),
        )
    finally:
        conn.close()

    if verdict.action is Action.BLOCK:
        raise looputil.block_exc(verdict, gaps=gaps)

    return {
        "text": body.text,
        "is_contemporary": body.is_contemporary,
        "engine": engine,
        "positioned": positioned,
        "anchors": anchors,
        "attributions": validated,
        "min_position_strength": MIN_POSITION_STRENGTH,
        "coordinates": coordinates,
        "precedent_chain": chain,
        "gaps": gaps,
        "guard": gdict,
    }
