"""多视角闭环（task 4.3）：已定位议题进，各学派 steel-man 解读 + 分歧结构出。

三段式（照 fd-zhesi loops.py 先例：参照系召回 → LLM 构造 → 引用校验）：
1. 召回各学派最强论证锚点（D2 对齐后双来源）：议题锚定（looputil 词表 →
   concept_lookup，概念行自带 school/weight 时直挂学派锚）→ 概念关联人物对
   经 relation_query 拉各学派论证锚，按参照系行强度（权重）排序，每学派取最强；
2. steel-man 构造：有 KEY 时 LLM 只准引用候选锚点 id 写 steel-man（引用校验
   过滤编造引用，无有效引用的 LLM 视图不用）；无 KEY / LLM 失败走确定性
   模板——不出 steel-man，转述各学派出典立场（form=sourced-stance），
   engine=deterministic，模板只用参照系字段不编造论证；
3. 分歧点结构：共享前提 / 真正分歧 / 成立条件（LLM 给则用，给不出由各派
   出典立场词面并置确定性导出，标注非语义判定）。

铁律：
- 弱解读不得冒充 steel-man：学派全部锚点强度低于 STEELMAN_MIN_WEIGHT 时不
  呈现（记入 gaps），呈现的视图一律标注锚（参照系条目 id）；
- 当代议题强制多视角：is_contemporary（显式入参或词面判定）时 views<2 学派
  的产物被守卫 R-BAL-001 拦（422 结构化返回，不呈现）；
- 产出过 guard_output 才可返回（publish 门在 looputil.publish_gate）；
  steel-man 正文即呈现面，守卫正则照扫正文；
- LLM 调用经 llm_ledger.chat_logged(surface="perspectives") 记账。

main.py 挂载（接线时两行）：from .perspectives import router as
perspectives_router; app.include_router(perspectives_router)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from . import llm_ledger, looputil, personal, reference_mcp
from .auth import require_user
from .guard import Action
from .llm import LLMError
from .locate import _anchor_concepts, _edge_stance, _recall_edges

router = APIRouter(prefix="/api/v1/loops", tags=["loops"])

STEELMAN_MIN_WEIGHT = 0.30  # 学派最强锚点低于此强度不呈现（弱解读不冒充 steel-man）
MAX_VIEWS = 5
MAX_TERMS = 3


class PerspectivesIn(BaseModel):
    topic: str = Field(min_length=1, max_length=4000)   # 已定位议题
    is_contemporary: bool | None = None                 # None → 词面判定（保守默认非当代）


# ---------------------------------------------------------------------------
# 参照系召回：学派锚点分组（确定性，第一段）
# ---------------------------------------------------------------------------


def school_views(edges: list[dict], anchors: list[dict] | None = None) -> list[dict]:
    """学派锚点分组（D2 对齐后双来源）：

    - 概念锚直挂（anchors）：参照系概念行自带 school+weight（curated 直挂），
      锚 stable_id 即引用锚——无关系边的概念（如单人物先秦概念、近现代思潮）
      也能呈现带源学派视角；
    - 关系边聚合（edges）：锚点按强度（边权重）降序，学派按最强锚点强度降序。
    """
    groups: dict[str, dict] = {}
    for order, anchor in enumerate(anchors or []):
        if not anchor.get("school") or not anchor.get("ref"):
            continue
        group = groups.setdefault(
            anchor["school"],
            {"school": anchor["school"], "anchors": [], "stance": "", "order": order},
        )
        group["anchors"].append({
            "ref": anchor["ref"],
            "weight": anchor.get("weight", 0.0),
            "polarity": "",
            "key_text": anchor.get("definition") or "",
        })
    for order, edge in enumerate(edges):
        if not edge.get("school") or not edge.get("id"):
            continue
        group = groups.setdefault(
            edge["school"],
            {"school": edge["school"], "anchors": [], "stance": "", "order": order},
        )
        group["anchors"].append({
            "ref": edge["id"],
            "weight": edge["weight"],
            "polarity": edge["polarity"],
            "key_text": edge["key_text"],
        })
    out: list[dict] = []
    for school, group in groups.items():
        anchors_sorted = sorted(
            group["anchors"], key=lambda a: (-a["weight"], a["ref"])
        )
        best = anchors_sorted[0]
        stance_edge = next(
            (e for e in edges if e["id"] == best["ref"]), None,
        )
        stance_anchor = next(
            (a for a in (anchors or []) if a["ref"] == best["ref"]), None,
        )
        if stance_edge is not None:
            stance = _edge_stance(stance_edge)
        elif stance_anchor is not None:
            stance = (
                stance_anchor.get("definition")
                or f"参照系概念锚（{stance_anchor.get('source', '')}）"
            )
        else:
            stance = ""
        out.append({
            "school": school,
            "stance": stance,
            "anchors": anchors_sorted,
            "strength": best["weight"],
            "order": group["order"],
        })
    out.sort(key=lambda g: (-g["strength"], g["order"]))
    return out


# ---------------------------------------------------------------------------
# LLM steel-man 构造（第二段）；无 KEY 走确定性模板
# ---------------------------------------------------------------------------


def _llm_views(topic: str, eligible: list[dict]) -> dict | None:
    """LLM 为各学派构造 steel-man：只准引用候选锚点 id。返回 None=无 KEY。

    结构非法 / 空 steel-man 抛 LLMError（调用方降级模板）。
    """
    lines = "\n".join(
        f"{i}. 学派「{s['school']}」 出典立场：{s['stance']}\n"
        f"   可引用锚点："
        + "；".join(f"{a['ref']}（{a['key_text'] or a['polarity']}）" for a in s["anchors"][:3])
        for i, s in enumerate(eligible, 1)
    )
    raw = llm_ledger.chat_logged(
        [
            {"role": "system", "content": (
                "你是政治思想导读员，为议题构造各学派的 steel-man 解读（先把该学派"
                "立场按其最强形式呈现，不裁断、不比较优劣、不号召行动）。"
                "铁律：只引用给定锚点清单内的 id，不得编造引用；转述优先于评论。"
                "只输出 JSON。"
            )},
            {"role": "user", "content": (
                f"议题：{topic}\n\n候选学派及其锚点：\n{lines}\n\n"
                "为每个学派写一段 60-120 字的 steel-man 解读（中文，面向普通读者），"
                "并给出分歧点结构。\n"
                '只输出 JSON：{"views": [{"index": 编号, "steel_man": "...",'
                ' "citations": ["锚点 id", ...]}], "disagreement":'
                ' {"shared_premises": ["..."], "real_disagreement": ["..."],'
                ' "conditions": ["..."]}}'
            )},
        ],
        surface="perspectives",
        max_tokens=1200,
    )
    if raw is None:
        return None
    data = looputil.parse_llm_json(raw)
    if not isinstance(data, dict) or not isinstance(data.get("views"), list):
        raise LLMError("perspectives LLM 返回非法（缺 views）")
    return data


def _template_view(school: dict) -> dict:
    """确定性模板：转述学派出典立场（form=sourced-stance，不冒充 steel-man）。"""
    best = school["anchors"][0]
    key = best["key_text"] or "见参照系边"
    text = (
        f"「{school['school']}」出典立场（转述）：{school['stance']}——"
        f"参照系锚点 {best['ref']}（{key}）。此处为确定性模板转述，非 steel-man。"
    )
    return {
        "school": school["school"],
        "stance": school["stance"],
        "form": "sourced-stance",
        "text": text,
        "anchors": [a["ref"] for a in school["anchors"][:3]],
        "strength": school["strength"],
    }


def _deterministic_disagreement(topic: str, views: list[dict], anchor_terms: list[str]) -> dict:
    """分歧点确定性导出：各派出典立场词面并置（非语义判定，如实标注）。"""
    shared: list[str] = []
    if len(views) >= 2 and anchor_terms:
        shared.append(
            f"各学派都在回应议题所涉的同一组概念锚：{'、'.join(anchor_terms[:3])}"
        )
    real: list[str] = []
    conditions: list[str] = []
    for a, b in zip(views, views[1:3]):
        real.append(
            f"「{a['school']}」主张{a['stance']}；「{b['school']}」主张{b['stance']}"
            "（并置呈现，不裁断）"
        )
        conditions.append(
            f"若「{a['school']}」的前提成立，「{b['school']}」需正面回应其"
            f"{a['stance']}所涉论证（反之亦然）"
        )
    return {
        "shared_premises": shared,
        "real_disagreement": real,
        "conditions": conditions,
        "note": "分歧点为各派出典立场的词面并置结构，非语义判定",
    }


def _valid_llm_disagreement(data: dict) -> dict | None:
    """LLM 分歧点结构校验：三个键都是字符串列表才用，否则返回 None 走确定性。"""
    raw = data.get("disagreement")
    if not isinstance(raw, dict):
        return None
    out: dict[str, list[str]] = {}
    for key in ("shared_premises", "real_disagreement", "conditions"):
        vals = raw.get(key)
        if not isinstance(vals, list) or not all(isinstance(v, str) and v.strip() for v in vals):
            return None
        out[key] = [v.strip() for v in vals if v.strip()]
    return out


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@router.post("/perspectives")
def perspectives(body: PerspectivesIn, user_id: str = Depends(require_user)) -> dict:
    """多视角闭环：各学派 steel-man（或出典转述）+ 分歧点结构。"""
    is_contemp = looputil.detect_contemporary(body.topic, body.is_contemporary)
    rc = reference_mcp.client()
    gaps: list[dict] = []

    anchors, anchor_gaps = _anchor_concepts(rc, body.topic)
    gaps.extend(anchor_gaps)
    if anchors and not any(a.get("school") or a.get("figures") for a in anchors):
        gaps.append({
            "stage": "anchoring", "reason": "no_school_anchor",
            "detail": "议题概念锚既无学派直挂也无关联人物（如 SEP 引用行），"
                      "参照系无可呈现的学派锚点",
        })
    edges, edge_gaps = _recall_edges(rc, anchors)
    gaps.extend(edge_gaps)

    groups = school_views(edges, anchors)
    eligible = [g for g in groups if g["strength"] >= STEELMAN_MIN_WEIGHT][:MAX_VIEWS]
    for weak in groups:
        if weak["strength"] < STEELMAN_MIN_WEIGHT:
            gaps.append({
                "stage": "steelman", "reason": "weak_anchor",
                "detail": (
                    f"学派「{weak['school']}」最强锚点强度 {weak['strength']} < "
                    f"{STEELMAN_MIN_WEIGHT}，弱解读不冒充 steel-man，未呈现"
                ),
            })
    if not eligible and not anchor_gaps and not edge_gaps:
        gaps.append({
            "stage": "anchoring", "reason": "no_school_anchor",
            "detail": "议题在参照系无可呈现的学派锚点（可能未摄取该维度）",
        })

    # ---- steel-man 构造：LLM（引用校验）或确定性模板 ----
    valid_refs = {a["ref"] for g in eligible for a in g["anchors"]}
    llm_texts: dict[str, tuple[str, list[str]]] = {}
    llm_disagreement: dict | None = None
    if eligible:
        try:
            data = _llm_views(body.topic, eligible)
            if data is not None:
                for item in data["views"]:
                    if not isinstance(item, dict):
                        continue
                    index = item.get("index")
                    steel = item.get("steel_man")
                    if not isinstance(index, int) or isinstance(index, bool):
                        continue
                    if not (1 <= index <= len(eligible)) or not isinstance(steel, str):
                        continue
                    if not steel.strip():
                        continue
                    # 引用校验：编造引用剥除；无有效锚的 LLM 视图不用（宁模板勿假）
                    citations = looputil.keep_valid_refs(
                        item.get("citations") or [], valid_refs
                    )
                    if not citations:
                        continue
                    llm_texts.setdefault(eligible[index - 1]["school"], (steel.strip(), citations))
                llm_disagreement = _valid_llm_disagreement(data)
        except LLMError:
            gaps.append({
                "stage": "llm", "reason": "llm_fallback",
                "detail": "LLM steel-man 构造失败，回退确定性出典转述模板",
            })

    views: list[dict] = []
    for school in eligible:
        got = llm_texts.get(school["school"])
        if got is not None:
            text, citations = got
            views.append({
                "school": school["school"],
                "stance": school["stance"],
                "form": "steel-man",
                "text": text,
                "anchors": citations,
                "strength": school["strength"],
            })
        else:
            views.append(_template_view(school))
    engine = "llm" if any(v["form"] == "steel-man" for v in views) else "deterministic"

    disagreement = llm_disagreement or _deterministic_disagreement(
        body.topic, views, [a["term"] for a in anchors]
    )

    # ---- publish 门：steel-man 正文即呈现面，当代议题强制多视角在此拦 ----
    body_text = (
        "；".join(v["text"] for v in views)
        or f"议题「{body.topic[:60]}」暂无可呈现的学派视角（缺口见 gaps）。"
    )
    payload = {
        "topic": body.topic[:200],
        "is_contemporary": is_contemp,
        "claims": [
            {"text": f"「{v['school']}」：{v['stance']}", "source": v["anchors"][0]}
            for v in views
            if v["anchors"]
        ],
        "views": [{"school": v["school"], "stance": v["stance"]} for v in views],
        "body": body_text,
    }
    gdict, verdict = looputil.publish_gate(payload)

    conn = personal.connect()
    try:
        personal.migrate(conn)
        personal.audit(
            conn, user_id, "loops.perspectives", None, "allowed",
            detail=(
                f"engine={engine} views={len(views)} contemporary={is_contemp}"
                f" gaps={len(gaps)}"
                f" guard={verdict.action.value if verdict.action else 'pass'}"
            ),
        )
    finally:
        conn.close()

    if verdict.action is Action.BLOCK:
        raise looputil.block_exc(verdict, gaps=gaps, is_contemporary=is_contemp)

    return {
        "topic": body.topic,
        "is_contemporary": is_contemp,
        "engine": engine,
        "views": views,
        "steelman_min_weight": STEELMAN_MIN_WEIGHT,
        "disagreement": disagreement,
        "gaps": gaps,
        "guard": gdict,
    }
