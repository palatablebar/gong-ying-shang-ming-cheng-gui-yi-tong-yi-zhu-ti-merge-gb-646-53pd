"""M4 相似判定编排：从候选对算分、落库、按结论聚合。

候选对分两段：先按名称得分与税号规则给出当批分档，再由 M8 承接台账用
上一版确认结论覆盖（沿用 / 拦回复核）。分档一旦重算就要重做承接覆盖，
否则沿用结论会被下一次重建冲掉。
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal

from ... import store
from ...errors import NotFound
from ...reasons import V_UNKNOWN_RUN
from ..m3_blocks import service as block_service
from . import conflict, similarity, threshold

STAGE = "pairs"


def _signature(run_id: str) -> str:
    """决定分档结果的输入：候选对集合与承接基准版本。"""
    from ..m8_carry import service as carry_service

    source = carry_service.source_version_for(run_id)
    return "|".join([
        f"block_pairs:{store.count(run_id, 'block_pairs')}",
        "source:" + (source["version_id"] if source else "-"),
    ])


def ensure_pairs(run_id: str) -> dict:
    """分档 + 承接覆盖；同批同基准下只算一次，重跑不新增记录。"""
    block_service.ensure_blocks(run_id)
    signature = _signature(run_id)
    if store.stage_signature(run_id, STAGE) == signature and store.count(run_id, "pair_scores"):
        return verdict_stats(run_id)
    rebuild(run_id)
    store.mark_stage(run_id, STAGE, signature)
    return verdict_stats(run_id)


def rebuild(run_id: str) -> dict:
    """强制按名称得分重建分档，然后重新施加承接覆盖。"""
    records = {record["supplier_id"]: record for record in store.list_records(run_id)}
    if not records:
        raise NotFound(V_UNKNOWN_RUN, run_id)
    pairs = []
    for item in store.list_block_pairs(run_id):
        left = records.get(item["left_id"])
        right = records.get(item["right_id"])
        if left is None or right is None:
            continue
        scored = similarity.score(left["norm_name"], right["norm_name"])
        verdict = conflict.decide(left, right, scored["score"])
        pairs.append({
            "pair_id": item["pair_id"],
            "left_id": item["left_id"],
            "right_id": item["right_id"],
            "dice": scored["dice"],
            "ratio": scored["ratio"],
            "score": scored["score"],
            "verdict": verdict["verdict"],
            "reason_code": verdict["reason_code"],
            "detail": verdict["detail"],
            "rules": verdict["rules"],
        })
    store.save_pairs(run_id, pairs)
    apply_carry(run_id, records)
    return verdict_stats(run_id)


def decided_choice(run_id: str, pair_id: str) -> str:
    record = store.get_decision(run_id, pair_id)
    return record["decision"] if record else ""


def apply_carry(run_id: str, records=None) -> dict:
    """把 M8 的关系承接施加到候选对；承接关系没有自然候选对时补一条。"""
    from ..m8_carry import service as carry_service

    store.clear_stages(run_id, (STAGE,))
    carry_service.ensure_relations(run_id)
    records = records or {record["supplier_id"]: record
                          for record in store.list_records(run_id)}
    decided = {row["pair_id"] for row in store.list_decisions(run_id)
               if row.get("origin") != "carried"}
    pairs = {pair["pair_id"]: pair for pair in store.list_pairs(run_id)}
    for entry in store.list_carry_entries(run_id):
        if entry["kind"] != "relation" or not entry["pair_id"]:
            continue
        if entry["pair_id"] in pairs:
            continue
        left = records.get(entry["left_id"])
        right = records.get(entry["right_id"])
        if left is None or right is None:
            continue
        scored = similarity.score(left["norm_name"], right["norm_name"])
        verdict = "auto" if entry["prev_decision"] == "merge" else "reject"
        if entry["pair_id"] in decided:
            verdict = "auto" if decided_choice(run_id, entry["pair_id"]) == "merge" else "reject"
        store.upsert_pair(run_id, {
            "pair_id": entry["pair_id"], "left_id": left["supplier_id"],
            "right_id": right["supplier_id"], "dice": scored["dice"],
            "ratio": scored["ratio"], "score": scored["score"], "verdict": verdict,
            "reason_code": "P_AUTO" if verdict == "auto" else "P_REJECT",
            "detail": "沿用上一版结论，" + f"score={scored['score']:.4f}",
            "carry_status": "carried",
            "carry_code": "R_CARRIED" if entry["pair_id"] not in decided else "R_RECONFIRMED",
            "carry_detail": "沿用上一版" + ("已确认合并" if verdict == "auto" else "已确认保留"),
        })
    carry_service.forced_review(run_id)
    # 强制复核后重新生成沿用决定，避免"已沿用"残留。
    carry_service.materialize_carried(run_id)
    return {"run_id": run_id, "pairs": store.count(run_id, "pair_scores")}


def verdict_stats(run_id: str) -> dict:
    pairs = store.list_pairs(run_id)
    counter = Counter(pair["verdict"] for pair in pairs)
    carry = Counter(pair["carry_status"] for pair in pairs if pair.get("carry_status"))
    codes = Counter(pair["reason_code"] for pair in pairs)
    scores = [Decimal(pair["score"]) for pair in pairs]
    return {
        "total": len(pairs),
        "auto": counter.get("auto", 0),
        "review": counter.get("review", 0),
        "reject": counter.get("reject", 0),
        "carried": carry.get("carried", 0),
        "pending_carry": carry.get("pending", 0),
        "max_score": str(max(scores)) if scores else "0.000000",
        "min_score": str(min(scores)) if scores else "0.000000",
        "reason_codes": [{"code": code, "count": count} for code, count in codes.most_common()],
        "thresholds": threshold.describe(),
    }


def details(run_id: str, verdict: str = "") -> list:
    records = {record["supplier_id"]: record for record in store.list_records(run_id)}
    rows = []
    for pair in store.list_pairs(run_id, verdict):
        left = records.get(pair["left_id"], {})
        right = records.get(pair["right_id"], {})
        rows.append({
            **pair,
            "left_name": left.get("raw_name", ""),
            "right_name": right.get("raw_name", ""),
            "left_norm": left.get("norm_name", ""),
            "right_norm": right.get("norm_name", ""),
            "left_tax": left.get("tax_id", ""),
            "right_tax": right.get("tax_id", ""),
            "left_city": left.get("city", ""),
            "right_city": right.get("city", ""),
        })
    return rows
