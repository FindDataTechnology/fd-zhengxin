"""定位闭环（task 4.2）：事件/政策/概念描述进，参照系坐标出。

三段式（照 fd-zhesi loops.py 先例：参照系召回 → LLM 重排 → 引用校验）：
1. 概念锚定：looputil 确定性词表命中 → reference_mcp.concept_lookup 归一
   （定义摘要 + SEP 链接 + 实体 qid），查不到不编造；
2. 谱系定位：relation_query 沿极性边（influenced_by / critical）找学派归属
   （离散谱系——归派结论一律挂参照系边 id，不做连续谱打分）；当代对象
   （is_contemporary）另补 ideology_query 坐标（P1142/P1387/MARPOR）；
3. 历史先例链：带年代的边按年排序成链，每环 人物/年代/关键文本 + 参照系
   条目 id；缺年代的边不入链（不编造年代）。

铁律：
- 不硬归派：最强候选强度（学派最强边权重）低于阈值 MIN_POSITION_STRENGTH
  → positioned=false + 缺口说明，不拿弱证据凑数；
- 引用校验：每个归派结论的依据必须指向参照系条目 id（候选集内），LLM 给的
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
POLARITIES = ("influenced_by", "critical")  # 谱系定位沿这两类极性边找学派归属
MAX_TERMS = 3
MAX_QIDS = 2


class LocateIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)   # 事件/政策/概念描述
    is_contemporary: bool = False                       # 当代对象 → 补 ideology_query 坐标


# ---------------------------------------------------------------------------
# 参照系召回（第一段）
# ---------------------------------------------------------------------------


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
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            ref = str(entry["id"])
            if ref in seen:
                continue
            seen.add(ref)
            anchors.append({
                "term": str(entry.get("term") or term),
                "ref": ref,
                "title": str(entry.get("title") or ""),
                "definition": str(entry.get("definition") or ""),
                "url": str(entry.get("url") or ""),
                "source": str(entry.get("source") or ""),
                "qid": str(entry.get("qid") or ""),
            })
    return anchors, gaps


def _norm_year(value: object) -> int | None:
    try:
        year = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return year if -3000 <= year <= 2100 else None


def _norm_weight(value: object) -> float:
    try:
        w = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, w))


def _norm_edge(entry: dict, qid: str, polarity: str) -> dict:
    """参照系边 → 归一结构（weight 夹 0..1；year 缺/非法记 None 不编造）。"""
    return {
        "id": str(entry.get("id") or ""),
        "qid": qid,
        "target_qid": str(entry.get("target_qid") or ""),
        "polarity": str(entry.get("polarity") or polarity),
        "pid": str(entry.get("pid") or ""),
        "weight": _norm_weight(entry.get("weight")),
        "school": str(entry.get("school") or ""),
        "target_name": str(entry.get("target_name") or ""),
        "target_kind": str(entry.get("target_kind") or "person"),
        "year": _norm_year(entry.get("year")),
        "key_text": str(entry.get("key_text") or ""),
        "stance": str(entry.get("stance") or ""),
    }


def _recall_edges(
    rc: reference_mcp.ReferenceClient, qids: list[str],
    polarities: tuple[str, ...] = POLARITIES,
) -> tuple[list[dict], list[dict]]:
    """谱系定位召回：对每个 qid 沿给定极性边拉关系边。返回 (edges, gaps)。"""
    edges: list[dict] = []
    gaps: list[dict] = []
    seen: set[str] = set()
    for qid in qids:
        for polarity in polarities:
            r = rc.relation_query(qid, polarity)
            if not r.ok:
                gaps.append({
                    "stage": "reference", "reason": r.reason,
                    "detail": f"relation_query({qid}, {polarity})：{r.detail}",
                })
                continue
            for entry in r.data.get("results") or []:
                if not isinstance(entry, dict) or not entry.get("id"):
                    continue
                eid = str(entry["id"])
                if eid in seen:
                    continue
                seen.add(eid)
                edges.append(_norm_edge(entry, qid, polarity))
    return edges, gaps


# ---------------------------------------------------------------------------
# 规则打分 + LLM 重排（第二段）
# ---------------------------------------------------------------------------


def _edge_stance(edge: dict) -> str:
    """边的立场描述：优先参照系自带 stance，否则由极性+关键文本转述。"""
    if edge["stance"]:
        return edge["stance"]
    if edge["polarity"] == "critical":
        return f"批判立场（{edge['key_text'] or '见参照系边'}）"
    return f"承袭立场（{edge['key_text'] or '见参照系边'}）"


def school_candidates(edges: list[dict]) -> list[dict]:
    """确定性规则打分：按学派聚合边，强度 = 最强边权重（离散谱系，不打连续分）。

    排序：强度降序 → 支持边数 → 召回序（无 KEY 时的确定性候选序）。
    """
    by_school: dict[str, dict] = {}
    for order, edge in enumerate(edges):
        if not edge["school"] or not edge["id"]:
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

    qids = [a["qid"] for a in anchors if a["qid"]]
    qids = list(dict.fromkeys(qids))[:MAX_QIDS]
    if anchors and not qids:
        gaps.append({
            "stage": "positioning", "reason": "no_entity_alignment",
            "detail": "概念锚无实体 qid，参照系关系层不可查（离散谱系需要实体对齐）",
        })
    edges, edge_gaps = _recall_edges(rc, qids)
    gaps.extend(edge_gaps)

    # ---- 候选打分 → 阈值门 → LLM 重排（不越阈值） ----
    candidates = school_candidates(edges)
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

    # ---- 引用校验：归派结论的依据必须指向候选集内条目 id，无锚不出 ----
    edge_ids = {e["id"] for e in edges}
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

    # ---- 当代对象补 ideology_query 坐标 ----
    coordinates: list[dict] = []
    if body.is_contemporary:
        for qid in qids:
            r = rc.ideology_query(qid)
            if not r.ok:
                gaps.append({
                    "stage": "contemporary", "reason": r.reason,
                    "detail": f"ideology_query({qid})：{r.detail}",
                })
                continue
            for entry in r.data.get("results") or []:
                if isinstance(entry, dict) and entry.get("id"):
                    coordinates.append({
                        "qid": str(entry.get("qid") or qid),
                        "name": str(entry.get("name") or ""),
                        "ref": str(entry["id"]),
                        "anchors": [
                            {"axis": a.get("axis"), "value": a.get("value"), "basis": a.get("basis")}
                            for a in (entry.get("anchors") or [])
                            if isinstance(a, dict)
                        ] or [{"axis": "", "value": "", "basis": ""}],
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
