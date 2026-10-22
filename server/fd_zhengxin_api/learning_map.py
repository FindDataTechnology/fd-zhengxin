"""学习地图 API（task 5.4）：按「已理解」卡在参照系坐标系合成覆盖度，版本化保留历史。

坐标系（学派/思潮维度）优先取参照系（reference_client.fetch_region_taxonomy），
按卡 topic 锚点归类（归一化后互为子串即同区域）。**参照系未配置/不可达时
按卡自身 topic 锚点降级合成并标注**（degraded=true + degradation_reasons，
此时「未触及区域」不可知，如实置空不猜测——照「缺口如实可见」纪律）。

三桶覆盖度（spec「覆盖度视图」）：
- understood：区域内有「已理解」active 卡；
- in_progress：区域内只有复述中/搁置的卡；
- untouched：参照系坐标上有、用户一张卡都没有的区域（降级模式下不可知）。

每次合成都存新版本（personal.save_map，版本号自增），旧版本全保留、
可查可对比（spec「地图更新保留历史」）。全路由要求用户授权；数据只落
个人层自有库。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from . import personal, reference_client
from .auth import require_user
from .cards_api import normalize_text

router = APIRouter(prefix="/api/v1/map", tags=["learning-map"])

_DEGRADE_NOTES = {
    "not_configured": (
        "参照系未配置（FDZX__REFERENCE_BASE_URL）：按卡自身 topic 锚点归类"
        "降级合成，未触及区域不可知"
    ),
    "empty_taxonomy": "参照系返回空坐标系：按卡自身 topic 锚点归类降级合成",
}


def _degradation_reasons(reason: str | None) -> list[str]:
    if reason is None:
        return []
    if reason.startswith("unreachable:"):
        return [f"参照系不可达（{reason}）：按卡自身 topic 锚点归类降级合成，"
                "未触及区域不可知"]
    return [_DEGRADE_NOTES.get(reason, f"参照系不可用（{reason}）：按卡自身 topic"
                                        " 锚点归类降级合成")]


def _region_status(entry: dict) -> str:
    return "understood" if entry["understood"] > 0 else "in_progress"


def build_snapshot(
    cards: list[dict], taxonomy: list[str] | None, degrade_reason: str | None, at: str,
) -> dict:
    """纯函数：active 卡 + 坐标系 → 覆盖度快照（测试直调）。"""
    counts = {"total": len(cards), "understood": 0, "restating": 0, "shelved": 0}
    for c in cards:
        counts[c["state"]] = counts.get(c["state"], 0) + 1

    regions: dict[str, dict] = {}
    unclassified: list[dict] = []

    def _bucket(name: str, card: dict) -> None:
        entry = regions.setdefault(
            name, {"name": name, "understood": 0, "restating": 0,
                   "shelved": 0, "card_ids": []},
        )
        entry[card["state"]] += 1
        entry["card_ids"].append(card["id"])

    if taxonomy is not None:
        # 参照系坐标系：卡 topic 锚点归入学派/思潮区域（归一化互为子串）
        norm_tax = [(name, normalize_text(name)) for name in taxonomy]
        for card in cards:
            t_n = normalize_text(card["topic"])
            hit = next(
                (name for name, n in norm_tax if n and (n in t_n or t_n in n)),
                None,
            )
            if hit:
                _bucket(hit, card)
            else:
                unclassified.append({"card_id": card["id"], "topic": card["topic"]})
        untouched = [name for name in taxonomy if name not in regions]
        coordinate_source, degraded = "reference", False
    else:
        # 降级：按卡自身 topic 锚点归类（同题合并），未触及不可知
        display: dict[str, str] = {}
        for card in cards:
            key = normalize_text(card["topic"])
            display.setdefault(key, card["topic"])
            _bucket(key, card)
        regions = {
            display[k]: {**v, "name": display[k]} for k, v in regions.items()
        }
        untouched = []
        coordinate_source, degraded = "card-anchors", True

    region_list = [regions[n] for n in sorted(regions)]
    for entry in region_list:
        entry["status"] = _region_status(entry)
    return {
        "coordinate_source": coordinate_source,
        "degraded": degraded,
        "degradation_reasons": _degradation_reasons(degrade_reason),
        "regions": region_list,
        "understood_regions": [e["name"] for e in region_list
                               if e["status"] == "understood"],
        "in_progress_regions": [e["name"] for e in region_list
                                if e["status"] == "in_progress"],
        "untouched_regions": untouched,
        "unclassified_cards": unclassified,
        "counts": counts,
        "based_on_cards": [c["id"] for c in cards],
        "synthesized_at": at,
    }


def _db():
    conn = personal.connect()
    personal.migrate(conn)
    return conn


@router.post("/synthesize", status_code=201)
def synthesize(user_id: str = Depends(require_user)) -> dict:
    """按当前 active 理解卡合成学习地图新版本；旧版本保留可对比。"""
    conn = _db()
    try:
        cards = personal.list_cards(conn, user_id, "active")
        taxonomy, reason = reference_client.fetch_region_taxonomy()
        snapshot = build_snapshot(cards, taxonomy, reason, personal.now())
        saved = personal.save_map(conn, user_id, snapshot)
        personal.audit(conn, user_id, "map.synthesize", saved["id"], "allowed",
                       detail=(f"v{saved['version']}, degraded={snapshot['degraded']},"
                               f" cards={snapshot['counts']['total']}"))
        return saved
    finally:
        conn.close()


@router.get("")
def latest(user_id: str = Depends(require_user)) -> dict:
    """当前（最新版本）学习地图；尚未合成过则 404。"""
    conn = _db()
    try:
        out = personal.latest_map(conn, user_id)
        if not out:
            raise HTTPException(404, "尚未合成学习地图：先 POST /api/v1/map/synthesize")
        return out
    finally:
        conn.close()


@router.get("/history")
def history(user_id: str = Depends(require_user)) -> dict:
    """全部历史版本（最新在前，含标题与时间，不含快照本体）。"""
    conn = _db()
    try:
        return {"results": personal.list_map_versions(conn, user_id)}
    finally:
        conn.close()


@router.get("/history/{version}")
def history_version(version: int, user_id: str = Depends(require_user)) -> dict:
    """取某一版本的完整快照（新旧版本对比的查询面）。"""
    conn = _db()
    try:
        out = personal.get_map_version(conn, user_id, version)
        if not out:
            raise HTTPException(404, f"版本 v{version} 不存在（或已超出最新版本）")
        return out
    finally:
        conn.close()
