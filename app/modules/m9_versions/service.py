"""M9 版本发布：正式主数据与复核决定作为同一版本一次性切换。

发布语义：
- 内容摘要包含批次、规则版本、主体/成员分摊、全部复核决定与承接台账；
  同摘要已发布则直接复用，同一批重跑不新增版本；
- 版本头、版本主体、版本成员、版本决定在同一事务写入，提交前做对账自检，
  自检失败或写入异常都清掉半成品，上一已发布版本保持原样；
- 同一批次只保留一个已发布版本：再次发布时旧版本降为 superseded。
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from decimal import Decimal

from ... import store
from ...config import VERSION_PREFIX
from ...errors import DomainError, NotFound
from ...manifest import utc_iso, utc_now
from ...reasons import V_NO_RUN, V_PUBLISH_FAILED, V_UNKNOWN_VERSION
from ..m5_clusters import service as cluster_service
from ..m8_carry import identity
from ..m8_carry import service as carry_service

CENT = Decimal("0.01")


def _amount(value) -> str:
    return str(Decimal(str(value or "0")).quantize(CENT))


def _cluster_payload(run_id: str) -> tuple:
    records = {record["supplier_id"]: record for record in store.list_records(run_id)}
    clusters = store.list_clusters(run_id)
    members = store.list_cluster_members(run_id)
    by_cluster = {}
    for member in members:
        record = records.get(member["supplier_id"], {})
        by_cluster.setdefault(member["cluster_id"], []).append({
            "cluster_id": member["cluster_id"],
            "supplier_id": member["supplier_id"],
            "identity_key": identity.identity_key(record),
            "tax_id": record.get("tax_id", ""),
            "norm_name": record.get("norm_name", ""),
            "city": record.get("city", ""),
            "share_amount": _amount(member["share_amount"]),
        })
    cluster_rows = []
    for cluster in clusters:
        rows = sorted(by_cluster.get(cluster["cluster_id"], []),
                      key=lambda item: item["supplier_id"])
        cluster_rows.append({
            "cluster_id": cluster["cluster_id"],
            "rep_id": cluster["rep_id"],
            "rep_key": identity.identity_key(records.get(cluster["rep_id"], {})),
            "tax_id": cluster["tax_id"],
            "city": cluster["city"],
            "member_count": cluster["member_count"],
            "total_amount": _amount(cluster["total_amount"]),
            "conflict": bool(cluster["conflict"]),
            "member_keys": [row["identity_key"] for row in rows],
        })
    return cluster_rows, [row for rows in by_cluster.values() for row in rows]


def _decision_payload(run_id: str, cluster_rows) -> list:
    records = {record["supplier_id"]: record for record in store.list_records(run_id)}
    pairs = {pair["pair_id"]: pair for pair in store.list_pairs(run_id)}
    rows = []
    for decision in store.list_decisions(run_id):
        pair = pairs.get(decision["pair_id"], {})
        left = records.get(pair.get("left_id", ""), {})
        right = records.get(pair.get("right_id", ""), {})
        rows.append({
            "pair_id": decision["pair_id"],
            "left_key": identity.identity_key(left),
            "right_key": identity.identity_key(right),
            "left_id": pair.get("left_id", ""),
            "right_id": pair.get("right_id", ""),
            "decision": decision["decision"],
            "reason_code": pair.get("reason_code", ""),
            "score": str(pair.get("score", "")),
            "operator": decision["operator"],
            "decided_at": decision["decided_at"],
            "row_version": int(decision["version"]),
            "origin": decision.get("origin") or (
                "carried" if pair.get("carry_status") == "carried" else "manual"),
            "source_version": decision.get("source_version", ""),
            "carry_code": decision.get("carry_code") or pair.get("carry_code", ""),
        })
    return rows


def content_digest(run_id: str, cluster_rows, member_rows, decision_rows) -> str:
    """只摘要正式内容：批次、规则版本、主体、成员分摊与决定。

    不纳入承接台账等中间状态，否则每次重算都会得到新摘要、重复发版。
    """
    payload = {
        "run_id": run_id,
        "rule_version": store.get_run(run_id)["rule_version"],
        "clusters": cluster_rows,
        "members": member_rows,
        "decisions": decision_rows,
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _next_seq() -> int:
    latest = store.latest_version("")
    return (latest["seq"] + 1) if latest else 1


def _self_check(run_id: str, cluster_rows, member_rows, ledger) -> list:
    checks = []
    accepted = len(store.list_records(run_id))
    members_total = Decimal("0")
    for row in member_rows:
        members_total += Decimal(row["share_amount"])
    clusters_total = sum((Decimal(row["total_amount"]) for row in cluster_rows), Decimal("0"))
    checks.append({
        "name": "主体拆分", "left": accepted,
        "right": sum(row["member_count"] for row in cluster_rows),
        "ok": accepted == sum(row["member_count"] for row in cluster_rows),
    })
    checks.append({
        "name": "分摊守恒", "left": str(clusters_total), "right": str(members_total),
        "ok": clusters_total == members_total,
    })
    checks.append({
        "name": "对账通过", "left": ledger.get("accepted", 0),
        "right": ledger.get("subjects", 0), "ok": bool(ledger.get("ok")),
    })
    return checks


def publish(run_id: str) -> dict:
    """发布当前批次为一个新版本；内容未变则返回已有版本。"""
    run = store.get_run(run_id)
    if not run:
        raise NotFound(V_NO_RUN, run_id)
    cluster_service.ensure_clusters(run_id)
    carry_service.compute(run_id)
    if not store.list_clusters(run_id):
        raise DomainError(V_PUBLISH_FAILED, "当前批次没有可发布的主体")

    cluster_rows, member_rows = _cluster_payload(run_id)
    decision_rows = _decision_payload(run_id, cluster_rows)
    digest = content_digest(run_id, cluster_rows, member_rows, decision_rows)
    existing = store.get_version_by_digest(digest)
    if existing:
        # 内容完全相同不新增版本。若同内容的旧版本已被更晚的版本取代，
        # 仍以当前正式版本为准，不把旧版本重新激活。
        current = store.latest_version("published")
        sealed = existing["status"] != "published"
        payload = {**store.get_version(existing["version_id"]), "created": False, "checks": []}
        if sealed:
            payload["superseded_by"] = current["version_id"] if current else ""
        return payload

    parent = store.latest_version("published")
    seq = _next_seq()
    version_id = f"{VERSION_PREFIX}{seq:04d}"
    ledger = cluster_service.reconcile(run_id)
    checks = _self_check(run_id, cluster_rows, member_rows, ledger)
    counts = {
        "subjects": len(cluster_rows),
        "members": len(member_rows),
        "decisions": len(decision_rows),
        "amount": str(sum((Decimal(row["total_amount"]) for row in cluster_rows), Decimal("0"))),
        "created_at_run": run["created_at"],
    }
    version = {
        "version_id": version_id,
        "seq": seq,
        "run_id": run_id,
        "status": "published",
        "rule_version": run["rule_version"],
        "content_digest": digest,
        "parent_version": parent["version_id"] if parent else "",
        "created_at": run["created_at"],
        "published_at": utc_iso(utc_now()),
        "counts_json": json.dumps(counts, ensure_ascii=False, sort_keys=True),
    }
    try:
        if not all(check["ok"] for check in checks):
            raise ValueError("对账自检未通过")
        store.publish_version(version, cluster_rows, member_rows, decision_rows)
    except Exception as error:  # pragma: no cover - 触发事务回滚
        store.delete_version(version_id)
        raise DomainError(V_PUBLISH_FAILED, str(error) or "写入失败") from error

    # 正式主数据只认一个当前版本：新版本发布后，此前已发布的版本一并降为
    # superseded，历史快照仍可查询与导出，用于发布前后对比。
    for other in store.list_versions("published", limit=500):
        if other["version_id"] != version_id:
            store.mark_version_status(other["version_id"], "superseded")
    return {**store.get_version(version_id), "created": True, "checks": checks}


def build_from_run(run_id: str) -> dict:
    """未发布批次也能给出"候选版本"内容，用于发布前预览。"""
    cluster_service.ensure_clusters(run_id)
    cluster_rows, member_rows = _cluster_payload(run_id)
    decision_rows = _decision_payload(run_id, cluster_rows)
    return {"clusters": cluster_rows, "members": member_rows, "decisions": decision_rows}


# ---- 查询 ----

def summary_of(version) -> dict:
    if not version:
        return {}
    counts = json.loads(version.get("counts_json") or "{}")
    return {**version, "counts": counts}


def latest_published() -> dict:
    return summary_of(store.latest_version("published"))


def current_for_run(run_id: str) -> dict:
    return summary_of(store.version_by_run(run_id))


def get(version_id: str) -> dict:
    version = store.get_version(version_id)
    if not version:
        raise NotFound(V_UNKNOWN_VERSION, version_id)
    return summary_of(version)


def decisions_of(version_id: str) -> list:
    return store.version_decisions(version_id)


def _member_sets(version_id: str) -> dict:
    grouped = {}
    for member in store.version_members(version_id):
        grouped.setdefault(member["cluster_id"], set()).add(member["identity_key"])
    return grouped


def match_subjects(before_rows, after_rows, before_members, after_members) -> dict:
    """按成员身份键重叠度一对一配对；簇编号每批会重排，不能当身份。"""
    used, pairs = set(), []
    ordered = sorted(after_rows, key=lambda row: (row["rep_key"] or "\uffff", row["cluster_id"]))
    for after in ordered:
        after_keys = after_members.get(after["cluster_id"], set())
        best, best_overlap = None, 0
        for before in before_rows:
            if before["cluster_id"] in used:
                continue
            overlap = len(before_members.get(before["cluster_id"], set()) & after_keys)
            if overlap > best_overlap:
                best, best_overlap = before, overlap
        if best is not None and best_overlap:
            used.add(best["cluster_id"])
        pairs.append({"before": best, "after": after,
                      "before_keys": before_members.get(best["cluster_id"], set()) if best else set(),
                      "after_keys": after_keys})
    for before in sorted(before_rows, key=lambda row: row["cluster_id"]):
        if before["cluster_id"] not in used:
            pairs.append({"before": before, "after": None,
                          "before_keys": before_members.get(before["cluster_id"], set()),
                          "after_keys": set()})
    return {"pairs": pairs}


def _classify(pair) -> str:
    before, after = pair["before"], pair["after"]
    if after is None:
        return "removed"
    if before is None:
        return "added"
    if pair["before_keys"] == pair["after_keys"] and before["total_amount"] == after["total_amount"]:
        return "same"
    return "changed"


def compare(before_id: str, after_id: str) -> dict:
    """两个版本的差异：主体构成、金额与复核决定逐项对齐。

    主体按成员身份键配对（簇编号每批重排，不作为身份）；复核决定按两侧
    身份键配对（候选对编号同理）。
    """
    before = store.get_version(before_id)
    after = store.get_version(after_id)
    if not before:
        raise NotFound(V_UNKNOWN_VERSION, before_id)
    if not after:
        raise NotFound(V_UNKNOWN_VERSION, after_id)
    before_clusters = store.version_clusters(before_id)
    after_clusters = store.version_clusters(after_id)
    before_members = _member_sets(before_id)
    after_members = _member_sets(after_id)
    matched = match_subjects(before_clusters, after_clusters, before_members, after_members)

    buckets = {"added": [], "removed": [], "changed": [], "same": []}
    for pair in matched["pairs"]:
        kind = _classify(pair)
        item = dict(pair)
        item["added_keys"] = sorted(pair["after_keys"] - pair["before_keys"])
        item["removed_keys"] = sorted(pair["before_keys"] - pair["after_keys"])
        item["cluster_from"] = pair["before"]["cluster_id"] if pair["before"] else ""
        item["cluster_to"] = pair["after"]["cluster_id"] if pair["after"] else ""
        buckets[kind].append(item)
    for key in buckets:
        buckets[key].sort(key=lambda item: item["cluster_to"] or item["cluster_from"])

    def decision_key(row):
        return "|".join(sorted((row["left_key"], row["right_key"])))

    before_decisions = {decision_key(row): row for row in store.version_decisions(before_id)}
    after_decisions = {decision_key(row): row for row in store.version_decisions(after_id)}
    decision_added = [row for key, row in after_decisions.items() if key not in before_decisions]
    decision_removed = [row for key, row in before_decisions.items() if key not in after_decisions]
    decision_changed = [
        {"before": before_decisions[key], "after": after_decisions[key]}
        for key in after_decisions
        if key in before_decisions
        and before_decisions[key]["decision"] != after_decisions[key]["decision"]
    ]

    def total(rows) -> str:
        return str(sum((Decimal(row["total_amount"]) for row in rows), Decimal("0")))

    return {
        "before": summary_of(before),
        "after": summary_of(after),
        "subjects": buckets,
        "decisions": {
            "added": sorted(decision_added, key=lambda item: item["pair_id"]),
            "removed": sorted(decision_removed, key=lambda item: item["pair_id"]),
            "changed": sorted(decision_changed, key=lambda item: item["after"]["pair_id"]),
        },
        "amount": {"before": total(before_clusters), "after": total(after_clusters)},
        "counts": {
            "subjects_added": len(buckets["added"]),
            "subjects_removed": len(buckets["removed"]),
            "subjects_changed": len(buckets["changed"]),
            "subjects_same": len(buckets["same"]),
            "decisions_added": len(decision_added),
            "decisions_removed": len(decision_removed),
            "decisions_changed": len(decision_changed),
        },
    }


def preview(run_id: str) -> dict:
    """发布前差异：当前批次 vs 上一已发布版本。"""
    source = carry_service.source_version_for(run_id)
    current = build_from_run(run_id)
    carry_meta = store.get_carry_run(run_id)
    payload = {
        "run_id": run_id,
        "source_version": source["version_id"] if source else "",
        "counts": {
            "subjects": len(current["clusters"]),
            "members": len(current["members"]),
            "decisions": len(current["decisions"]),
        },
        "carry": carry_meta["counts"] if carry_meta else {},
    }
    if not source:
        # 首次发布：全部主体都是"新增"，保持与后续预览相同的条目结构。
        payload["subjects"] = {
            "added": [{"before": None, "after": row, "before_keys": set(),
                       "after_keys": set(row["member_keys"]), "added_keys": row["member_keys"],
                       "removed_keys": [], "cluster_from": "", "cluster_to": row["cluster_id"]}
                      for row in current["clusters"]],
            "removed": [], "changed": [], "same": [],
        }
        payload["decisions"] = {"added": current["decisions"], "removed": [], "changed": []}
        payload["first_release"] = True
        return payload

    grouped = {}
    for member in current["members"]:
        grouped.setdefault(member["cluster_id"], set()).add(member["identity_key"])
    before_clusters = store.version_clusters(source["version_id"])
    matched = match_subjects(before_clusters, current["clusters"],
                             _member_sets(source["version_id"]), grouped)
    buckets = {"added": [], "removed": [], "changed": [], "same": []}
    for pair in matched["pairs"]:
        kind = _classify(pair)
        item = dict(pair)
        item["added_keys"] = sorted(pair["after_keys"] - pair["before_keys"])
        item["removed_keys"] = sorted(pair["before_keys"] - pair["after_keys"])
        item["cluster_from"] = pair["before"]["cluster_id"] if pair["before"] else ""
        item["cluster_to"] = pair["after"]["cluster_id"] if pair["after"] else ""
        buckets[kind].append(item)
    payload["subjects"] = buckets
    payload["first_release"] = False

    def decision_key(row):
        return "|".join(sorted((row["left_key"], row["right_key"])))

    prev_decisions = {decision_key(row): row
                      for row in store.version_decisions(source["version_id"])}
    now_decisions = {decision_key(row): row for row in current["decisions"]}
    payload["decisions"] = {
        "added": [row for key, row in now_decisions.items() if key not in prev_decisions],
        "removed": [row for key, row in prev_decisions.items() if key not in now_decisions],
        "changed": [{"before": prev_decisions[key], "after": row}
                    for key, row in now_decisions.items()
                    if key in prev_decisions
                    and prev_decisions[key]["decision"] != row["decision"]],
    }
    return payload


# ---- 版本导出：以发布快照为唯一来源 ----

def version_master_csv(version_id: str) -> str:
    """主体主数据：一主体一行，成员与决定都取自同一版本快照。"""
    import csv
    import io

    version = get(version_id)
    clusters = store.version_clusters(version_id)
    members = defaultdict(list)
    for member in store.version_members(version_id):
        members[member["cluster_id"]].append(member)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(("版本", "主体编号", "代表供应商号", "税号", "城市", "成员数",
                     "本年累计(元)", "簇状态", "成员供应商号", "成员业务身份"))
    for cluster in clusters:
        rows = sorted(members.get(cluster["cluster_id"], []),
                      key=lambda item: item["supplier_id"])
        status = "税号冲突" if cluster["conflict"] else (
            "单记录" if cluster["member_count"] == 1 else "已合并")
        writer.writerow([
            version["version_id"], cluster["cluster_id"], cluster["rep_id"],
            cluster["tax_id"], cluster["city"], cluster["member_count"],
            f"{Decimal(cluster['total_amount']):.2f}", status,
            " ".join(row["supplier_id"] for row in rows),
            " ".join(row["identity_key"] for row in rows),
        ])
    return buffer.getvalue()


def version_decisions_csv(version_id: str) -> str:
    import csv
    import io

    version = get(version_id)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(("版本", "候选对", "左侧身份", "右侧身份", "结论", "来源",
                     "原因码", "得分", "操作人", "决定时间", "行版本"))
    for row in store.version_decisions(version_id):
        writer.writerow([
            version["version_id"], row["pair_id"], row["left_key"], row["right_key"],
            "合并" if row["decision"] == "merge" else "保留",
            "沿用" if row["origin"] == "carried" else "人工",
            row["reason_code"], row["score"], row["operator"], row["decided_at"],
            row["row_version"],
        ])
    return buffer.getvalue()
