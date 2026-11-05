"""脉搏环（任务 4.5）：议题立场光谱——info-stream 政治信息流的增量消费面。

模型：每议题一行 sqlite 快照（pulse_spectra）：
- spectrum：[{stance_label, evidence: [{item_id, doc_id, span:[start,end],
  quote}]}]——每次光谱移动都带依据（item_id + 正文 span + 逐字 quote）；
- cursor：list_since 键集游标，增量续拉（解冻后从断点继续，不重拉不漏）；
- frozen_reason：上游不可用时如实记录的结构化原因（JSON，NULL=未冻结）；
  冻结不清空快照——查询面照常可读旧光谱；
- dropped_no_evidence：累计丢弃的「缺证据强断言」数。

口径纪律：
- 缺证据的强断言不产出：stance 有断言（关键词/LLM 声称）但拿不到通过
  逐字校验的正文证据 → 该 stance 不进光谱，计入 dropped_no_evidence；
- span 逐字写校验：quote 必须与 get_item 返回正文（body）的
  body[start:end] 逐字一致，校验不过的证据一律丢弃（含 LLM 给的 span）；
- 立场判定以确定性关键词匹配为主，LLM 仅可选增强（无 KEY 跳过，其
  span 同样过逐字校验）；LLM 调用经 llm_ledger.chat_logged(surface=
  "pulse") 记账；
- 产物过 guard_output：当代议题光谱即当代议题产物（is_contemporary=True），
  平衡检查适用——单立场光谱会被 R-BAL-001 判 block，查询面带裁决返回。

上游消费顺序（probe D7 纪律）：先 stream_health（政治源 cn-gov 不 ok →
冻结并记录 last_error 原文），再 list_since(source_types=["ministry"])
增量拉取；上游 unreachable / 未配置 → 冻结降级，绝不静默空转。
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from . import config, llm, llm_ledger
from .auth import require_user
from .guard import guard_output
from .infostream_client import (
    FrozenReason, InfoStreamClient, InfoStreamUnavailable,
)

POLITICAL_SOURCE_TYPES = ["ministry"]   # 政治类条目 = 部委源（cn-gov）
POLITICAL_SOURCES = ("cn-gov",)         # stream_health 里的政治源名

MAX_PAGES_PER_INGEST = 50               # 单次摄取翻页上限（防失控循环）

# 确定性立场词表（只增不改；label 是光谱刻度，不做优劣评价）
STANCE_LEXICON: dict[str, tuple[str, ...]] = {
    "支持推进": ("支持", "赞成", "拥护", "推进", "贯彻落实", "出台"),
    "反对质疑": ("反对", "质疑", "担忧", "批评", "暂缓"),
    "谨慎观望": ("谨慎", "审慎", "观望", "稳妥", "逐步", "有待观察"),
}

_SENTENCE_BREAK = "。！？；\n"

PULSE_DDL = """
CREATE TABLE IF NOT EXISTS pulse_spectra (
    issue TEXT PRIMARY KEY,
    spectrum TEXT NOT NULL DEFAULT '[]',       -- JSON [{stance_label, evidence:[…]}]
    cursor TEXT,                               -- list_since 键集游标（增量续拉）
    last_item_id TEXT,                         -- 最近处理的 item_id（审计断点）
    dropped_no_evidence INTEGER NOT NULL DEFAULT 0,
    last_updated_at TEXT,                      -- 最近一次成功数据更新时间
    frozen_reason TEXT,                        -- JSON {code,detail}；NULL=未冻结
    updated_at TEXT NOT NULL                   -- 行最近变更时间（含冻结/解冻）
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:  # 预留：快照外显 id（当前以 issue 为主键）
    return uuid.uuid4().hex[:12]


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """脉搏环库连接（config.pulse_db_path；照 personal.connect 习惯）。"""
    path = db_path or config.pulse_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(PULSE_DDL)
    return conn


def _row(conn: sqlite3.Connection, issue: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM pulse_spectra WHERE issue=?", (issue,)).fetchone()


def _ensure_row(conn: sqlite3.Connection, issue: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO pulse_spectra (issue, spectrum, updated_at)"
        " VALUES (?, '[]', ?)",
        (issue, _now()),
    )


# ---- 证据逐字写校验（铁律：quote ≡ body[start:end]） ----


def _valid_span(span: object, body: str) -> bool:
    if (not isinstance(span, (list, tuple)) or len(span) != 2
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in span)):
        return False
    start, end = span
    return 0 <= start < end <= len(body)


def verify_evidence(evidence: dict, body: str | None) -> bool:
    """写校验：span 合法且 quote 与正文切片逐字一致，才允许落库。"""
    if not isinstance(body, str) or not body:
        return False
    if not _valid_span(evidence.get("span"), body):
        return False
    quote = evidence.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        return False
    start, end = evidence["span"]
    return body[start:end] == quote


# ---- 确定性抽取：议题关联 + 立场证据 ----


def _issue_terms(issue: str) -> list[str]:
    """议题关联词：议题名里的连续 CJK 段（≥2 字），无则退回整串。"""
    terms = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,}", issue)
    return terms or [issue.strip()]


def _is_related(item: dict, terms: list[str]) -> bool:
    haystack = " ".join(str(item.get(k) or "") for k in ("title", "summary"))
    return any(t and t in haystack for t in terms)


def _sentence_span(body: str, start: int, end: int) -> tuple[int, int]:
    """把命中区间扩展为所在句子区间（句末断点字符计入 quote）。"""
    s = max(body.rfind(b, 0, start) for b in _SENTENCE_BREAK)
    sent_start = 0 if s == -1 else s + 1
    nxt = [i for i in (body.find(b, end) for b in _SENTENCE_BREAK) if i != -1]
    e = min(nxt) if nxt else len(body)
    sent_end = len(body) if e == -1 else e + 1
    return max(sent_start, 0), min(sent_end, len(body))


def _detect_stances(body: str) -> dict[str, list[tuple[int, int]]]:
    """确定性立场检测：body 内每个立场关键词命中 → 所在句子 span。"""
    found: dict[str, list[tuple[int, int]]] = {}
    for label, keywords in STANCE_LEXICON.items():
        spans: set[tuple[int, int]] = set()
        for kw in keywords:
            for m in re.finditer(re.escape(kw), body):
                spans.add(_sentence_span(body, m.start(), m.end()))
        if spans:
            found[label] = sorted(spans)
    return found


def _asserted_in_metadata(item: dict, label: str) -> bool:
    """强断言判读：立场关键词出现在标题/摘要也算断言——但只有 get_item
    返回正文里的 span 才能成为逐字证据。"""
    keywords = STANCE_LEXICON.get(label, ())
    elsewhere = " ".join(str(item.get(k) or "") for k in ("title", "summary"))
    return any(kw in elsewhere for kw in keywords)


def _evidence_from_spans(
    item_id: str, doc_id: str | None, body: str, spans: list[tuple[int, int]],
) -> list[dict]:
    out = []
    for s, e in spans:
        ev = {"item_id": item_id, "doc_id": doc_id, "span": [s, e], "quote": body[s:e]}
        if verify_evidence(ev, body):  # 写校验：span 内文本 ≡ 正文逐字切片
            out.append(ev)
    return out


# ---- LLM 可选增强（无 KEY 跳过；span 同样过逐字校验） ----

_LLM_SYSTEM = (
    "你是「政心」脉搏环的立场证据抽取助手。给定议题与一条政治信息正文，"
    "只做转述性抽取：找出正文中表达明确立场倾向的句子，输出 JSON 数组，"
    "每个元素形如 {\"stance_label\": \"支持推进|反对质疑|谨慎观望 之一\","
    " \"start\": 句子起始偏移, \"end\": 句子结束偏移}。start/end 是正文"
    "字符偏移区间，必须使正文切片恰好等于该句原文。没有明确立场输出 []。"
    "不要输出任何其他内容。"
)


def _llm_stance_claims(issue: str, title: str, body: str) -> list[dict]:
    """LLM 增强：返回 [{stance_label, span}] 候选（未校验，调用方过滤）。

    记账走 llm_ledger 自管连接（surface="pulse"），不与脉搏环库混库。
    """
    if not llm.is_configured():
        return []
    prompt = json.dumps({"issue": issue, "title": title, "body": body}, ensure_ascii=False)
    try:
        raw = llm_ledger.chat_logged(
            [{"role": "system", "content": _LLM_SYSTEM},
             {"role": "user", "content": prompt}],
            surface="pulse", temperature=0.0, max_tokens=400,
        )
    except llm.LLMError:
        return []  # 增强失败降级为纯确定性抽取，不中断摄取
    if not raw:
        return []
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()).strip()
    try:
        claims = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(claims, list):
        return []
    return [c for c in claims if isinstance(c, dict)]


# ---- 摄取主流程 ----


@dataclass
class IngestReport:
    issue: str
    frozen: bool
    frozen_reason: dict | None
    pages: int
    items_seen: int
    items_processed: int
    items_skipped: int
    dropped_no_evidence: int   # 本次运行丢弃数（行内另存累计值）
    stances: list[str]         # 本次更新涉及的 stance_label
    spectrum_size: int
    updated_at: str

    def to_dict(self) -> dict:
        return {
            "issue": self.issue, "frozen": self.frozen,
            "frozen_reason": self.frozen_reason, "pages": self.pages,
            "items_seen": self.items_seen, "items_processed": self.items_processed,
            "items_skipped": self.items_skipped,
            "dropped_no_evidence": self.dropped_no_evidence,
            "stances": self.stances, "spectrum_size": self.spectrum_size,
            "updated_at": self.updated_at,
        }


def _health_freeze_reason(client: InfoStreamClient) -> dict | None:
    """D7：先健康度。政治源不 ok → 结构化冻结原因；健康面不可判读
    （未实现 / 无 cn-gov 条目）→ None 照常取数（取数自身会暴露可用性）。"""
    try:
        health = client.stream_health()
    except InfoStreamUnavailable as exc:
        return exc.reason.to_dict()
    if not isinstance(health, dict) or health.get("error"):
        return None
    for src in health.get("sources") or []:
        if not isinstance(src, dict) or src.get("source") not in POLITICAL_SOURCES:
            continue
        if src.get("status") == "ok":
            return None  # 政治源健康 → 正常取数
        detail = str(
            src.get("last_error") or src.get("reason") or f"status={src.get('status')}"
        )
        return {"code": "source_unavailable", "detail": detail}
    return None


def ingest_spectrum_update(
    issue: str,
    *,
    client: InfoStreamClient | None = None,
    conn: sqlite3.Connection | None = None,
    use_llm: bool = True,
) -> dict:
    """增量拉政治类条目 → 抽取议题立场证据 → 更新光谱快照。

    上游不可达/未配置 → 冻结（保留快照、记录结构化原因），下次调用
    上游恢复即自动解冻续拉（游标持久化）。返回 IngestReport.to_dict()。
    """
    own_conn = conn is None
    conn = conn or connect()
    client = client or InfoStreamClient()
    try:
        _ensure_row(conn, issue)
        row = _row(conn, issue)
        assert row is not None
        spectrum: list[dict] = json.loads(row["spectrum"] or "[]")
        cursor: str | None = row["cursor"]
        dropped_total: int = row["dropped_no_evidence"]

        # D7：健康度先行——政治源不 ok / 健康面不可达 → 冻结并返回
        health_reason = _health_freeze_reason(client)
        if health_reason is not None:
            updated_at = freeze(issue, reason=health_reason, conn=conn)
            return IngestReport(
                issue=issue, frozen=True, frozen_reason=health_reason,
                pages=0, items_seen=0, items_processed=0, items_skipped=0,
                dropped_no_evidence=0, stances=[], spectrum_size=len(spectrum),
                updated_at=updated_at,
            ).to_dict()

        terms = _issue_terms(issue)
        report = IngestReport(
            issue=issue, frozen=False, frozen_reason=None, pages=0,
            items_seen=0, items_processed=0, items_skipped=0,
            dropped_no_evidence=0, stances=[], spectrum_size=len(spectrum),
            updated_at="",
        )
        touched: set[str] = set()
        frozen_reason: dict | None = None
        last_seen_id: str | None = row["last_item_id"]

        try:
            for _page in range(MAX_PAGES_PER_INGEST):
                page = client.list_since(
                    cursor=cursor, source_types=POLITICAL_SOURCE_TYPES,
                )
                report.pages += 1
                items = page.get("items") or []
                report.items_seen += len(items)
                for item in items:
                    if not isinstance(item, dict) or not item.get("item_id"):
                        report.items_skipped += 1
                        continue
                    item_id = str(item["item_id"])
                    last_seen_id = item_id
                    # 同 item 修订（raw_hash 变）→ 先撤旧证据再补新（upsert 语义）
                    spectrum, removed = _drop_item_evidence(spectrum, item_id)
                    touched |= removed
                    if item.get("withdrawn") or not _is_related(item, terms):
                        report.items_skipped += 1
                        continue
                    full = client.get_item(item_id)
                    if not isinstance(full, dict) or full.get("error"):
                        report.items_skipped += 1
                        continue
                    doc_id = full.get("doc_id")
                    # get_item 不内联正文（fd-info 2026-10-23 收口契约）：正文走
                    # get_body 读取面；body_status 非 stored 的条目如实记跳过。
                    body: str | None = None
                    if doc_id and full.get("body_status") == "stored":
                        body_payload = client.get_body(str(doc_id))
                        if isinstance(body_payload, dict) and body_payload.get("ok"):
                            body = body_payload.get("body")
                    label_evs, dropped = _extract_item_stances(
                        issue, item_id, full, body, use_llm=use_llm,
                    )
                    report.dropped_no_evidence += dropped
                    dropped_total += dropped
                    for label, evs in label_evs.items():
                        touched.add(label)
                        _append_evidence(spectrum, label, evs)
                    report.items_processed += 1
                # 整页处理完才推进游标；部分处理不推进（重拉不漏）
                cursor = page.get("next_cursor") or None
                if not page.get("has_more") or not cursor:
                    break
        except InfoStreamUnavailable as exc:
            frozen_reason = exc.reason.to_dict()

        # 撤完证据变空的 stance 条目不留（无证据的强断言不允许留在光谱）
        spectrum = [e for e in spectrum if e.get("evidence")]
        ts = _now()
        data_updated = frozen_reason is None and report.pages > 0
        conn.execute(
            "UPDATE pulse_spectra SET spectrum=?, cursor=?, dropped_no_evidence=?,"
            " last_item_id=?, last_updated_at=?, frozen_reason=?, updated_at=?"
            " WHERE issue=?",
            (
                json.dumps(spectrum, ensure_ascii=False), cursor, dropped_total,
                last_seen_id,
                ts if data_updated else (row["last_updated_at"] or ts),
                json.dumps(frozen_reason, ensure_ascii=False) if frozen_reason else None,
                ts, issue,
            ),
        )
        conn.commit()
        report.stances = sorted(touched)
        report.spectrum_size = len(spectrum)
        if frozen_reason is not None:
            report.frozen = True
            report.frozen_reason = frozen_reason
        report.updated_at = ts
        return report.to_dict()
    finally:
        if own_conn:
            conn.close()


def _drop_item_evidence(spectrum: list[dict], item_id: str) -> tuple[list[dict], set[str]]:
    """撤销该条目在光谱里的全部旧证据；返回被触及的 label 集。"""
    touched: set[str] = set()
    for entry in spectrum:
        before = len(entry.get("evidence") or [])
        entry["evidence"] = [
            ev for ev in (entry.get("evidence") or []) if ev.get("item_id") != item_id
        ]
        if len(entry["evidence"]) != before:
            touched.add(entry["stance_label"])
    return spectrum, touched


def _append_evidence(spectrum: list[dict], label: str, evs: list[dict]) -> None:
    if not evs:
        return
    for entry in spectrum:
        if entry.get("stance_label") == label:
            existing = {(e["item_id"], tuple(e["span"])) for e in entry["evidence"]}
            entry["evidence"].extend(
                e for e in evs if (e["item_id"], tuple(e["span"])) not in existing
            )
            return
    spectrum.append({"stance_label": label, "evidence": list(evs)})


def _extract_item_stances(
    issue: str, item_id: str, full: dict, body: str | None, *, use_llm: bool = True,
) -> tuple[dict[str, list[dict]], int]:
    """单条目立场抽取：确定性关键词为主 + LLM 可选增强。

    断言来源三途：body 关键词命中（span 即证据）、标题/摘要命中（断言
    成立但 body 无证据）、LLM 声称（span 过逐字校验才算证据）。有断言
    而零证据的 label 丢弃并计数。返回 (label → 证据列表, 丢弃数)。
    """
    doc_id = full.get("doc_id")
    asserted: dict[str, list[dict]] = {}

    if isinstance(body, str) and body:
        for label, spans in _detect_stances(body).items():
            evs = _evidence_from_spans(item_id, doc_id, body, spans)
            asserted.setdefault(label, []).extend(evs)
        if use_llm:
            for claim in _llm_stance_claims(
                issue, str(full.get("title") or ""), body,
            ):
                label = claim.get("stance_label")
                if label not in STANCE_LEXICON or label in asserted:
                    continue
                span = claim.get("span")
                if _valid_span(span, body):
                    ev = {
                        "item_id": item_id, "doc_id": doc_id,
                        "span": [span[0], span[1]], "quote": body[span[0]:span[1]],
                    }
                    if verify_evidence(ev, body):
                        asserted.setdefault(label, []).append(ev)
                else:  # LLM 声称了立场但 span 非法 → 有断言无证据
                    asserted.setdefault(label, [])
    for label in STANCE_LEXICON:
        if label in asserted:
            continue
        if _asserted_in_metadata(full, label):
            asserted[label] = []  # 标题/摘要断言：等 body 证据，拿不到即丢

    label_evs = {label: evs for label, evs in asserted.items() if evs}
    dropped = len(asserted) - len(label_evs)
    return label_evs, dropped


# ---- 冻结降级 ----


def freeze(
    issue: str, reason: FrozenReason | dict, conn: sqlite3.Connection | None = None,
) -> str:
    """冻结光谱：只记 frozen_reason + updated_at，快照/游标/last_updated_at
    原样保留（如实：数据停在最后一次成功更新）。返回冻结时刻。"""
    own = conn is None
    conn = conn or connect()
    try:
        reason_dict = reason.to_dict() if isinstance(reason, FrozenReason) else dict(reason)
        _ensure_row(conn, issue)
        ts = _now()
        conn.execute(
            "UPDATE pulse_spectra SET frozen_reason=?, updated_at=? WHERE issue=?",
            (json.dumps(reason_dict, ensure_ascii=False), ts, issue),
        )
        conn.commit()
        return ts
    finally:
        if own:
            conn.close()


# ---- 查询面 ----


def get_spectrum(
    issue: str, conn: sqlite3.Connection | None = None,
) -> dict | None:
    """读光谱快照（冻结时照常可读旧光谱与原因）；未知议题返回 None。"""
    own = conn is None
    conn = conn or connect()
    try:
        row = _row(conn, issue)
        if row is None:
            return None
        reason = json.loads(row["frozen_reason"]) if row["frozen_reason"] else None
        return {
            "issue": row["issue"],
            "spectrum": json.loads(row["spectrum"] or "[]"),
            "dropped_no_evidence": row["dropped_no_evidence"],
            "last_updated_at": row["last_updated_at"],
            "updated_at": row["updated_at"],
            "frozen": reason is not None,
            "frozen_reason": reason,
        }
    finally:
        if own:
            conn.close()


def spectrum_guard_payload(issue: str, spectrum: list[dict]) -> dict:
    """光谱 → guard_output 统一 payload：当代议题产物，平衡检查适用。

    body 只做转述性描述（立场刻度 + 依据条数），不内嵌正文 quote——
    逐字片段随 claims 带 source（item_id）走结构化检查，不让来源原文
    的口径风险词误触正文正则。
    """
    views = [
        {"school": e.get("stance_label", ""), "stance": e.get("stance_label", "")}
        for e in spectrum
    ]
    claims = [
        {"text": ev.get("quote", ""), "source": ev.get("item_id", "")}
        for e in spectrum
        for ev in (e.get("evidence") or [])
    ]
    parts = "；".join(
        f"{e.get('stance_label', '')}（{len(e.get('evidence') or [])} 条依据）"
        for e in spectrum
    ) or "暂无立场证据"
    body = f"议题『{issue}』立场光谱（转述各来源立场，不裁断）：{parts}。"
    return {
        "topic": issue, "is_contemporary": True,
        "claims": claims, "views": views, "body": body,
    }


def get_spectrum_guarded(
    issue: str, conn: sqlite3.Connection | None = None,
) -> dict | None:
    """查询面数据 + 守卫裁决：动作非 None 时调用方不得原样呈现。"""
    snap = get_spectrum(issue, conn=conn)
    if snap is None:
        return None
    verdict = guard_output(spectrum_guard_payload(issue, snap["spectrum"]))
    snap["guard"] = {
        "action": verdict.action.value if verdict.action else None,
        "reasons": [
            {"rule_id": r.rule_id, "category": r.category, "detail": r.detail}
            for r in verdict.reasons
        ],
        "ruleset_version": verdict.ruleset_version,
    }
    return snap


# ---- API（/api/v1/pulse，带 auth 依赖） ----

router = APIRouter(prefix="/api/v1/pulse", tags=["pulse"])


@router.get("/spectrum/{issue}")
def read_spectrum(issue: str, user_id: str = Depends(require_user)) -> dict:
    """读议题立场光谱：快照 + 逐字证据 + 冻结原因 + 守卫裁决。"""
    snap = get_spectrum_guarded(issue)
    if snap is None:
        raise HTTPException(status_code=404, detail=f"未知议题：{issue}")
    return snap
