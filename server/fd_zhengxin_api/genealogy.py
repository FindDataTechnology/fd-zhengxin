"""谱系闭环（task 4.4）：政治概念/思潮进，按年代排序的演变链出。

三段式（照 fd-zhesi loops.py 先例：参照系召回 → LLM 重排 → 引用校验）：
1. 参照系召回：概念锚定（词表归一 → concept_lookup）→ relation_query 沿
   influenced_by 边拉演变环节（hop1 概念实体；hop2 按权重下钻前几个环节的
   实体，深度有界防漫游）；
2. LLM 重排：只从候选环清单里选环（index-only，链保持年代序——LLM 不能改
   变年序、不能发明环）；无 KEY / LLM 失败 → 确定性路径（召回序 + 规则
   打分：边权重），engine=deterministic；
3. 引用校验：每环来源（source）必须指向参照系条目 id（候选集内），无锚
   的环不出现在输出。

演变链每环：人物/流派（kind）、年代（year）、关键文本（key_text）、所属
学派（school）、来源（ref=参照系条目 id），按年代升序。

铁律：
- 参照系断链处标注 gap 不编造环节——三类断链如实可见：
  ① 边缺年代 → 不入链（no_year）；② 下钻空结果/失败 → 该环节后断链
  （reason 透传参照系口径：not_ingested/unreachable/…）；③ 相邻两环年代
  跨度超 GAP_YEARS → 标注中间无参照系环节（reference_break）；
- 产出过 guard_output 才可返回（publish 门在 looputil.publish_gate）；
- LLM 调用经 llm_ledger.chat_logged(surface="genealogy") 记账。

main.py 挂载（接线时两行）：from .genealogy import router as genealogy_router;
app.include_router(genealogy_router)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from . import llm_ledger, looputil, personal, reference_mcp
from .auth import require_user
from .guard import Action
from .llm import LLMError
from .locate import _recall_edges

router = APIRouter(prefix="/api/v1/loops", tags=["loops"])

HOP2_TARGETS = 3     # hop1 后按权重下钻的环节数（深度有界）
GAP_YEARS = 300      # 相邻环年代跨度超过此值标注「中间无参照系环节」
MAX_RINGS = 12


class GenealogyIn(BaseModel):
    term: str = Field(min_length=1, max_length=400)   # 概念/思潮（中文别名或规范词）


# ---------------------------------------------------------------------------
# 参照系召回：概念锚 + 两跳 influenced_by 边（第一段）
# ---------------------------------------------------------------------------


def _anchor_term(term: str) -> str:
    """词表归一：命中 LEXICON 取规范查询词，未命中原词直查（由参照系判空）。"""
    picked = looputil.extract_terms(term, cap=1)
    return picked[0] if picked else term.strip()


def _lookup_anchor(
    rc: reference_mcp.ReferenceClient, term: str
) -> tuple[dict | None, list[dict]]:
    """concept_lookup → 概念锚（优先带 qid 的结果）。返回 (anchor|None, gaps)。"""
    r = rc.concept_lookup(term)
    if not r.ok:
        return None, [{
            "stage": "reference", "reason": r.reason,
            "detail": f"concept_lookup({term!r})：{r.detail}",
        }]
    results = [e for e in (r.data.get("results") or []) if isinstance(e, dict) and e.get("id")]
    if not results:
        return None, [{
            "stage": "reference", "reason": "empty_concept",
            "detail": f"concept_lookup({term!r}) 返回空（参照系无该概念锚）",
        }]
    entry = next((e for e in results if e.get("qid")), results[0])
    return {
        "term": str(entry.get("term") or term),
        "ref": str(entry["id"]),
        "title": str(entry.get("title") or ""),
        "url": str(entry.get("url") or ""),
        "qid": str(entry.get("qid") or ""),
    }, []


def recall_rings(
    rc: reference_mcp.ReferenceClient, qid: str,
) -> tuple[list[dict], list[dict]]:
    """两跳召回演变环节：hop1 概念实体的 influenced_by 边 + hop2 按权重下钻。

    返回 (edges, gaps)；edges 全局按 id 去重。断链处（下钻空结果/失败）在
    gaps 里标注该环节名与原因，不编造后续环节。
    """
    gaps: list[dict] = []
    edges, hop1_gaps = _recall_edges(rc, [qid], polarities=("influenced_by",))
    gaps.extend(hop1_gaps)

    targets = sorted(
        (e for e in edges if e["target_qid"]),
        key=lambda e: (-e["weight"], e["id"]),
    )[:HOP2_TARGETS]
    seen = {e["id"] for e in edges}
    for edge in targets:
        hop2, hop2_gaps = _recall_edges(
            rc, [edge["target_qid"]], polarities=("influenced_by",)
        )
        for g in hop2_gaps:
            gaps.append({
                "stage": "chain", "reason": g["reason"],
                "detail": (
                    f"「{edge['target_name'] or edge['target_qid']}」下钻断链：{g['detail']}"
                ),
            })
        for e in hop2:
            if e["id"] not in seen:
                seen.add(e["id"])
                edges.append(e)
    return edges, gaps


def build_chain(edges: list[dict]) -> tuple[list[dict], list[dict]]:
    """边 → 年代升序演变链 + 断链/缺口标注。返回 (chain, gaps)。"""
    gaps: list[dict] = []
    rings: list[dict] = []
    for edge in edges:
        if edge["year"] is None:
            continue  # 缺年代的环不入链（下面统一记缺口）
        rings.append({
            "name": edge["target_name"] or edge["school"] or edge["id"],
            "kind": edge["target_kind"] or "person",
            "year": edge["year"],
            "key_text": edge["key_text"],
            "school": edge["school"],
            "weight": edge["weight"],
            "ref": edge["id"],
        })
    yearless = [e for e in edges if e["year"] is None and e["id"]]
    if yearless:
        gaps.append({
            "stage": "chain", "reason": "no_year",
            "detail": (
                f"{len(yearless)} 个环节缺年代未入链："
                + "、".join(e["target_name"] or e["id"] for e in yearless[:3])
                + "（不编造年代）"
            ),
        })
    rings.sort(key=lambda r: (r["year"], r["ref"]))
    rings = rings[:MAX_RINGS]
    for prev, nxt in zip(rings, rings[1:]):
        if nxt["year"] - prev["year"] > GAP_YEARS:
            gaps.append({
                "stage": "chain", "reason": "reference_break",
                "detail": (
                    f"{prev['year']}–{nxt['year']} 年间（跨度 "
                    f"{nxt['year'] - prev['year']} 年 > {GAP_YEARS}）参照系无中间"
                    f"环节，不编造「{prev['name']}→{nxt['name']}」之间的链环"
                ),
            })
    return rings, gaps


# ---------------------------------------------------------------------------
# LLM 重排（第二段）：index-only 选环，链保持年代序
# ---------------------------------------------------------------------------


def _llm_select(term: str, rings: list[dict]) -> list[int] | None:
    """LLM 从候选环清单选核心环节（1-based 编号，不许发明）。

    返回 None=无 KEY；非法编号按 LLMError 处理（调用方降级确定性路径）。
    """
    lines = "\n".join(
        f"{i}. {r['name']}（{r['year']}）{'（' + r['key_text'] + '）' if r['key_text'] else ''}"
        f" 权重 {r['weight']} 依据 {r['ref']}"
        for i, r in enumerate(rings, 1)
    )
    raw = llm_ledger.chat_logged(
        [
            {"role": "system", "content": (
                "你是政治思想史导读员，从参照系给出的演变环清单里挑出该概念/思潮"
                "谱系的核心环节。只能从候选清单里选，绝不发明环节或年代，不得改动"
                "年代顺序。只输出 JSON。"
            )},
            {"role": "user", "content": (
                f"概念/思潮：{term}\n\n候选环（按年代序）：\n{lines}\n\n"
                f"选出核心环节（至少 {min(len(rings), 2)} 个，至多全部）。"
                '只输出 JSON：{"indices": [编号, ...]}'
            )},
        ],
        surface="genealogy",
        temperature=0.0,
        max_tokens=200,
    )
    if raw is None:
        return None
    data = looputil.parse_llm_json(raw)
    idx = data.get("indices") if isinstance(data, dict) else None
    if not isinstance(idx, list) or not idx or not all(
        isinstance(i, int) and not isinstance(i, bool) and 1 <= i <= len(rings)
        for i in idx
    ):
        raise LLMError("genealogy 重排返回的 indices 非法")
    picked: list[int] = []
    for i in idx:
        if i not in picked:
            picked.append(i)
    return picked  # 调用方按年代重排（LLM 无权改年序）


def _render_body(term: str, chain: list[dict]) -> str:
    """转述性正文：链环年代序渲染，不带口径风险句式。"""
    if not chain:
        return f"「{term[:60]}」暂无参照系演变链（缺口见 gaps，不编造环节）。"
    parts = "→".join(
        f"{r['name']}（{r['year']}{('，' + r['key_text']) if r['key_text'] else ''}）"
        for r in chain
    )
    return f"「{term[:60]}」演变链（按年代序，参照系转述）：{parts}。"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@router.post("/genealogy")
def genealogy(body: GenealogyIn, user_id: str = Depends(require_user)) -> dict:
    """谱系闭环：概念/思潮 → 按年代排序的演变链（断链处如实标注 gap）。"""
    rc = reference_mcp.client()
    gaps: list[dict] = []

    term = _anchor_term(body.term)
    anchor, anchor_gaps = _lookup_anchor(rc, term)
    gaps.extend(anchor_gaps)

    edges: list[dict] = []
    if anchor and anchor["qid"]:
        edges, recall_gaps = recall_rings(rc, anchor["qid"])
        gaps.extend(recall_gaps)
    elif anchor:
        gaps.append({
            "stage": "chain", "reason": "no_entity_alignment",
            "detail": "概念锚无实体 qid，参照系关系层不可查，无法建链",
        })

    rings, chain_gaps = build_chain(edges)
    gaps.extend(chain_gaps)

    # ---- LLM 重排（选环不改年序）；无 KEY 确定性路径 = 全部候选环 ----
    chain = list(rings)
    engine = "deterministic"
    if rings:
        try:
            picks = _llm_select(term, rings)
            if picks is not None:
                engine = "llm"
                chosen = {rings[i - 1]["ref"] for i in picks}
                chain = [r for r in rings if r["ref"] in chosen]
        except LLMError:
            gaps.append({
                "stage": "llm", "reason": "llm_fallback",
                "detail": "LLM 选环失败，回退确定性路径（召回序+规则打分）",
            })

    # ---- 引用校验：每环来源必须指向候选集内条目 id，无锚不出 ----
    edge_ids = {e["id"] for e in edges}
    chain = [r for r in chain if r["ref"] in edge_ids]

    body_text = _render_body(term, chain)

    # ---- publish 门：产出过 guard_output 才可返回 ----
    payload = {
        "topic": term[:200],
        "is_contemporary": False,  # 谱系是历史脉络，平衡呈现规则不适用
        "claims": [
            {
                "text": f"{r['name']}（{r['year']}）{r['key_text']}".strip(),
                "source": r["ref"],
            }
            for r in chain
        ],
        "views": [],
        "body": body_text,
    }
    gdict, verdict = looputil.publish_gate(payload)

    conn = personal.connect()
    try:
        personal.migrate(conn)
        personal.audit(
            conn, user_id, "loops.genealogy", None, "allowed",
            detail=(
                f"engine={engine} rings={len(chain)} gaps={len(gaps)}"
                f" guard={verdict.action.value if verdict.action else 'pass'}"
            ),
        )
    finally:
        conn.close()

    if verdict.action is Action.BLOCK:
        raise looputil.block_exc(verdict, gaps=gaps)

    return {
        "term": body.term,
        "resolved_term": term,
        "anchor": anchor,
        "engine": engine,
        "chain": chain,
        "gap_years": GAP_YEARS,
        "gaps": gaps,
        "guard": gdict,
    }
