"""参照系只读客户端（task 5.2/5.4 的参照系消费面，留位期）。

参照系（政治思想参照系）是独立服务（地址 FDZX__REFERENCE_BASE_URL，见
config.reference_base_url），物理分库铁律下本模块对它**只读**、降级友好：

- 未配置地址 → 明确返回 (None, "not_configured")，调用方走降级路径并标注；
- 不可达 / 非 2xx / 坏负载 → (None, "unreachable:<原因类型>")，绝不静默为空
  （照参照系「失败口径」纪律：空结果必附原因）；
- 参照系服务尚未成型（task 2.x 接线期），因此负载解析保持宽容：
  认 regions / results / 裸列表，认 key_points / anchors / premises，
  条目可以是字符串或带 name/text 的对象。真实契约定型后在收敛点对齐。

超时短（默认 5s）、无重试：个人层体验优先，参照系慢即降级。
"""

from __future__ import annotations

from typing import Any

from . import config


def _get_json(path: str, timeout: float = 5.0) -> Any:
    """GET {base}{path} → JSON；未配置返回 None；任何失败抛异常（调用方归一）。"""
    base = config.reference_base_url().rstrip("/")
    if not base:
        return None
    import httpx  # 延迟导入：环境缺 httpx 时仅参照系消费不可用，降级不受影响

    resp = httpx.get(f"{base}{path}", timeout=timeout, headers={"accept": "application/json"})
    resp.raise_for_status()
    return resp.json()


def _as_list(data: Any, *keys: str) -> list[Any]:
    """从 {keys[0]: [...]} / {keys[1]: [...]} / 裸列表 里宽容取出条目列表。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def _item_text(item: Any, *keys: str) -> str | None:
    if isinstance(item, str) and item.strip():
        return item.strip()
    if isinstance(item, dict):
        for k in keys:
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def fetch_region_taxonomy() -> tuple[list[str] | None, str | None]:
    """取参照系学派/思潮维度（学习地图的坐标系）。

    返回 (区域名列表, None) 或 (None, 原因)。原因枚举：
    not_configured / unreachable:<ExcType> / empty_taxonomy。
    """
    if not config.reference_base_url():
        return None, "not_configured"
    try:
        data = _get_json("/api/v1/regions")
    except Exception as exc:  # noqa: BLE001 —— 任何失败归一为不可达，带原因返回
        return None, f"unreachable:{type(exc).__name__}"
    names = [
        t for t in (
            _item_text(item, "name", "name_zh", "title", "id")
            for item in _as_list(data, "regions", "results", "items")
        ) if t
    ]
    if not names:
        return None, "empty_taxonomy"
    return names, None


def fetch_concept_anchor(topic: str) -> tuple[list[str] | None, str | None]:
    """取某概念/论证在参照系的关键前提锚（复述校验的前提集来源之一）。

    返回 (前提列表, None) 或 (None, 原因)。原因枚举：
    not_configured / unreachable:<ExcType> / no_anchor。
    """
    if not config.reference_base_url():
        return None, "not_configured"
    from urllib.parse import quote

    try:
        data = _get_json(f"/api/v1/concepts/{quote(topic)}/anchor")
    except Exception as exc:  # noqa: BLE001
        return None, f"unreachable:{type(exc).__name__}"
    premises = [
        t for t in (
            _item_text(item, "text", "premise", "name", "label")
            for item in _as_list(data, "key_points", "anchors", "premises", "results")
        ) if t
    ]
    if not premises:
        return None, "no_anchor"
    return premises, None
