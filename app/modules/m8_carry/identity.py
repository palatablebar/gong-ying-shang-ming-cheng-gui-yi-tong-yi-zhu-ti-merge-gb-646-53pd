"""业务身份键：跨批次承接的唯一锚点。

集团编码（supplier_no）在同一集团内稳定，优先作为身份键；个别来源缺编码时
退回供应商编码。税号、归一名与城市属于"关键字段"，任一变化都要重新复核。
"""
from __future__ import annotations

from ...config import IDENTITY_KEY_FIELDS


def identity_key(record) -> str:
    number = str(record.get("supplier_no") or "").strip()
    if number:
        return f"NO:{number}"
    return f"ID:{str(record.get('supplier_id') or '').strip()}"


def relation_key(left_key: str, right_key: str) -> str:
    first, second = sorted((left_key, right_key))
    return f"{first}|{second}"


def key_snapshot(record) -> dict:
    return {field: str(record.get(field) or "") for field in IDENTITY_KEY_FIELDS}


def changed_fields(before: dict, after: dict) -> list:
    return [field for field in IDENTITY_KEY_FIELDS
            if str(before.get(field, "")) != str(after.get(field, ""))]
