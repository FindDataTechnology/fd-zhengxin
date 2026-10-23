"""理解卡 API（task 5.2/5.3）：建卡 → 复述校验 → 状态流转 → 理解年轮。

全路由要求用户授权（auth.require_user，未授权 401 并审计留痕——task 5.3
的验证线在 tests/test_cards_lifecycle.py）；数据只落个人层自有库（物理分库）。

口径纪律（API 层）：
- 理解卡**不含也不接受任何表态字段**：请求体 extra=forbid（多投的
  立场/赞同类字段直接 422），响应里只有理解事实（主题/复述/要点/进度）。
- 复述校验（verify）只判「是否覆盖该论证的关键前提」：确定性覆盖比对
  （归一化子串 / 字符二元组 / 关键词包含，保守打分宁低勿高），输出
  coverage_ratio + 未覆盖前提清单 + 差距说明，**无任何立场评价字段**。
  LLM 只做可选的同义判别增强（llm.py 有 KEY 时），输出标 engine。

前提集来源：建卡声明的 key_points 优先；卡未声明时尝试参照系概念锚
（reference_client，未配置/不可达则如实声明无可对照前提，不猜测）。

状态机：restating（复述中）→ 校验达 VERIFY_THRESHOLD 才可 understood
（已理解）；shelved（搁置）。重复提交/搁置走「旧状态封存入年轮
（append-only），新状态开卡」（personal.supersede_card）；封存后的卡
拒绝一切改动（404，照 fd-zhesi 先例）。
"""

from __future__ import annotations

import re
import unicodedata

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import config, llm, llm_ledger, personal, reference_client
from .auth import require_user
from .llm import LLMError

router = APIRouter(prefix="/api/v1/cards", tags=["understanding-cards"])

VERIFY_THRESHOLD = 0.8  # 前提覆盖度达线才可转「已理解」（宁低勿高的保守线）

_KEEP_RE = re.compile(r"[a-z0-9\u4e00-\u9fff]+")
_LATIN_WORD_RE = re.compile(r"[a-z]{2,}")
_STOPWORDS = {
    "the", "a", "an", "of", "to", "and", "or", "is", "are", "in", "on",
    "for", "with", "that", "this", "it", "as", "by", "be",
}


# ---- 复述校验：确定性覆盖比对（纯函数，测试直调） ----

def normalize_text(text: str) -> str:
    """归一化：NFKC（全角→半角）+ casefold + 只留字母/数字/CJK，去空白标点。"""
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(_KEEP_RE.findall(folded))


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set()


def _content_words(text: str) -> list[str]:
    """拉丁词级内容词（去停用词）；中文无空格分词，走二元组路径。"""
    return [w for w in _LATIN_WORD_RE.findall(str(text or "").casefold())
            if w not in _STOPWORDS]


def premise_covered(premise: str, digest: str) -> bool:
    """单前提覆盖判定（保守，宁低勿高）：

    1. 归一化子串命中（含标点/全角/大小写差异的逐字覆盖）；
    2. 拉丁内容词全包含（词序无关的关键词覆盖，前提须有内容词）；
    3. 字符二元组重叠 ≥ 2/3（容忍少量虚词插入，如「让渡范围」vs
       「让渡的范围」；语义相左的改写（如「全部」vs「部分」）会跌破线）。
    单字符前提只认子串（不放宽）。
    """
    p_n, d_n = normalize_text(premise), normalize_text(digest)
    if not p_n or not d_n:
        return False
    if p_n in d_n:
        return True
    p_words = _content_words(premise)
    if p_words and set(p_words) <= set(_content_words(digest)):
        return True
    p_bi = _bigrams(p_n)
    if len(p_bi) >= 2:
        hit = len(p_bi & _bigrams(d_n))
        return hit * 3 >= len(p_bi) * 2  # ≥ 2/3，整数比较避开浮点边界
    return False


def _llm_messages(topic: str, premise: str, digest: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是复述覆盖度判别器。只判断「复述是否覆盖了给定前提的关键含义」，"
                "不评价复述或前提的立场、倾向、好坏。只输出 JSON："
                '{"covered": true} 或 {"covered": false}。'
            ),
        },
        {
            "role": "user",
            "content": f"主题：{topic}\n前提：{premise}\n复述：{digest}\n"
                       "该复述是否覆盖了这条前提的关键含义？",
        },
    ]


def _llm_says_covered(answer: str) -> bool:
    m = re.search(r'"?\s*covered"?\s*[:=]\s*(true|false)', answer.casefold())
    return bool(m and m.group(1) == "true")


def _premises_for(topic: str, key_points: list[str]) -> tuple[list[str], str, str | None]:
    """前提集：卡声明的 key_points 优先；为空时取参照系概念锚（如实标注来源）。"""
    declared = [str(k).strip() for k in key_points or [] if str(k).strip()]
    if declared:
        return declared, "card", None
    if config.reference_base_url():
        anchors, reason = reference_client.fetch_concept_anchor(topic)
        if anchors:
            return anchors, "reference", None
        return [], "none", f"reference:{reason}"
    return [], "none", "no_key_points"


def verify_coverage(topic: str, digest: str, key_points: list[str]) -> dict:
    """复述校验（spec「建卡与校验」）：前提覆盖度 + 差距说明，不含立场评价。

    输出键固定：premise_source / premises / covered_premises /
    uncovered_premises / coverage_ratio / threshold / passed / gaps / engine。
    engine：deterministic（无 KEY 或无待判前提）| deterministic+llm（LLM
    同义判别生效）| deterministic+llm-fallback（LLM 配了但调用失败，回退）。
    """
    premises, source, source_note = _premises_for(topic, key_points)
    if not premises:
        return {
            "premise_source": source,
            "premises": [],
            "covered_premises": [],
            "uncovered_premises": [],
            "coverage_ratio": None,
            "threshold": VERIFY_THRESHOLD,
            "passed": False,
            "gaps": [f"无可对照的关键前提（{source_note}）：请在建卡时声明"
                     " key_points，或接通参照系概念锚后再校验"],
            "engine": "deterministic",
        }

    covered = [p for p in premises if premise_covered(p, digest)]
    uncovered = [p for p in premises if p not in covered]

    engine = "deterministic"
    if uncovered and llm.is_configured():
        promoted: list[str] = []
        failed = False
        for p in uncovered:
            try:
                answer = llm_ledger.chat_logged(_llm_messages(topic, p, digest),
                                  surface="personal-verify",
                                  max_tokens=50, timeout=10.0)
            except LLMError:
                failed = True
                break  # 不重试：剩余前提回退确定性判定
            if answer is not None and _llm_says_covered(answer):
                promoted.append(p)
        if promoted:
            uncovered = [p for p in uncovered if p not in promoted]
            covered = covered + promoted
            engine = "deterministic+llm"
        if failed:
            engine = "deterministic+llm-fallback"

    ratio = round(len(covered) / len(premises), 4)
    passed = ratio >= VERIFY_THRESHOLD
    gaps: list[str] = []
    for p in uncovered:
        gaps.append(f"复述未覆盖关键前提「{p}」：归一化比对（子串/关键词/二元组）"
                    "均未达覆盖线，保守判为未覆盖")
    if engine == "deterministic+llm-fallback":
        gaps.append("LLM 同义判别不可用，已回退纯确定性判定（覆盖度只低不高）")
    if not passed:
        gaps.append(f"覆盖度 {len(covered)}/{len(premises)}（{ratio:.0%}）"
                    f"未达阈值 {VERIFY_THRESHOLD:.0%}，暂不能转「已理解」")
    return {
        "premise_source": source,
        "premises": premises,
        "covered_premises": covered,
        "uncovered_premises": uncovered,
        "coverage_ratio": ratio,
        "threshold": VERIFY_THRESHOLD,
        "passed": passed,
        "gaps": gaps,
        "engine": engine,
    }


# ---- API 面 ----

class CardCreate(BaseModel):
    """建卡请求。extra=forbid：表态类字段（立场/赞同/倾向）一律 422 拒收。"""

    model_config = ConfigDict(extra="forbid")
    topic: str = Field(min_length=1)            # 理解对象（学派/论证锚点）
    digest: str = Field(min_length=1)           # 我的复述（用自己的话）
    key_points: list[str] = Field(default_factory=list)  # 声明的关键前提
    refs: list[dict] = Field(default_factory=list)       # 来源引用（参照系坐标）


class DigestSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest: str = Field(min_length=1)
    key_points: list[str] | None = None         # 不填则沿用旧卡声明


def _db():
    conn = personal.connect()
    personal.migrate(conn)
    return conn


def _get_active_or_404(conn, user_id: str, card_id: str) -> dict:
    card = personal.get_card(conn, user_id, card_id)
    if not card or card["status"] != "active":
        raise HTTPException(404, "卡不存在或已封存")
    return card


@router.post("", status_code=201)
def create_card(body: CardCreate, user_id: str = Depends(require_user)) -> dict:
    """建卡 + 即时复述校验：达阈值直接「已理解」，否则「复述中」。"""
    verification = verify_coverage(body.topic, body.digest, body.key_points)
    state = "understood" if verification["passed"] else "restating"
    conn = _db()
    try:
        card = personal.create_card(
            conn, user_id, body.topic, body.digest,
            list(body.key_points), list(body.refs), state=state,
        )
        personal.audit(conn, user_id, "card.create", card["id"], "allowed",
                       detail=f"state={state},engine={verification['engine']}")
        verification = {**verification, "card_id": card["id"]}
        return {"card": card, "verification": verification}
    finally:
        conn.close()


@router.get("")
def list_cards(
    status: str = "active", state: str | None = None,
    user_id: str = Depends(require_user),
) -> dict:
    if status not in ("active", "sealed"):
        raise HTTPException(422, "status 只支持 active/sealed")
    if state and state not in personal.CARD_STATES:
        raise HTTPException(422, f"state 只支持 {list(personal.CARD_STATES)}")
    conn = _db()
    try:
        return {"results": personal.list_cards(conn, user_id, status, state)}
    finally:
        conn.close()


@router.get("/annals")
def annals(user_id: str = Depends(require_user)) -> dict:
    """理解年轮查询：封存快照按时间排列（append-only，历史不覆盖）。"""
    conn = _db()
    try:
        return {"results": personal.list_annals(conn, user_id)}
    finally:
        conn.close()


@router.get("/{card_id}")
def get_card(card_id: str, user_id: str = Depends(require_user)) -> dict:
    conn = _db()
    try:
        card = personal.get_card(conn, user_id, card_id)
        if not card:
            raise HTTPException(404, "卡不存在或不属于该用户")
        return card
    finally:
        conn.close()


@router.post("/{card_id}/verify")
def verify_digest(card_id: str, user_id: str = Depends(require_user)) -> dict:
    """复述校验（只判前提覆盖，不评立场）：达阈值且未「已理解」时转已理解。"""
    conn = _db()
    try:
        card = _get_active_or_404(conn, user_id, card_id)
        verification = verify_coverage(card["topic"], card["digest"], card["key_points"])
        if verification["passed"] and card["state"] != "understood":
            card = personal.set_card_state(conn, user_id, card_id, "understood") or card
        personal.audit(conn, user_id, "card.verify", card_id, "allowed",
                       detail=(f"coverage={verification['coverage_ratio']},"
                               f"passed={verification['passed']},"
                               f"engine={verification['engine']}"))
        verification = {**verification, "card_id": card_id}
        return {"card": card, "verification": verification}
    finally:
        conn.close()


@router.post("/{card_id}/resubmit", status_code=201)
def resubmit(card_id: str, body: DigestSubmit, user_id: str = Depends(require_user)) -> dict:
    """重复提交新复述：旧卡封存入年轮（superseded），新卡即时校验开卡。"""
    conn = _db()
    try:
        old = _get_active_or_404(conn, user_id, card_id)
        new_kp = body.key_points if body.key_points is not None else old["key_points"]
        verification = verify_coverage(old["topic"], body.digest, new_kp)
        state = "understood" if verification["passed"] else "restating"
        new_card = personal.supersede_card(
            conn, user_id, card_id, kind="superseded", state=state,
            digest=body.digest, key_points=new_kp,
        )
        personal.audit(conn, user_id, "card.resubmit", card_id, "allowed",
                       detail=f"old={card_id},new={new_card['id']},state={state}")
        verification = {**verification, "card_id": new_card["id"]}
        return {"card": new_card, "verification": verification}
    finally:
        conn.close()


@router.post("/{card_id}/shelve", status_code=201)
def shelve(card_id: str, user_id: str = Depends(require_user)) -> dict:
    """搁置：旧状态封存入年轮（sealed），同题开一张 shelved 新卡（可回溯再启）。"""
    conn = _db()
    try:
        _get_active_or_404(conn, user_id, card_id)
        new_card = personal.supersede_card(
            conn, user_id, card_id, kind="sealed", state="shelved",
        )
        personal.audit(conn, user_id, "card.shelve", card_id, "allowed",
                       detail=f"sealed={card_id},new={new_card['id']}")
        return {"card": new_card}
    finally:
        conn.close()
