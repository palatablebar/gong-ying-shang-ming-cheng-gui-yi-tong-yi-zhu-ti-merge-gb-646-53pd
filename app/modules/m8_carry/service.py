"""M8 跨批次承接：把上一版确认过的关系与主体，按业务身份映射到新批次。

三类去向：
- 沿用（carried）：两侧身份键都在、关键字段未变，旧结论继续生效；
- 待复核（pending）：关键字段变化、身份冲突或主体新增成员，旧结论不再自动生效；
- 退出（exited）：上一版的关系或主体在当批已找不到对应身份键。

承接结果整批替换；同一批重跑不重复记账。比较基准固定为"上一已发布版本"：
当批尚未发布时取最新已发布版本，已发布时取它的父版本，避免拿自己跟自己比。
"""
from __future__ import annotations

import hashlib
from collections import defaultdict

from ... import store
from ...config import IDENTITY_KEY_FIELDS
from ...manifest import utc_iso, utc_now
from ...reasons import (
    R_CARRIED,
    R_EXITED,
    R_PENDING_IDENTITY,
    R_PENDING_KEY_FIELD,
    R_PENDING_NEW_MEMBER,
    R_SUBJECT_CHANGED,
    R_SUBJECT_NEW,
)
from . import identity

RELATION_MERGE = "merge"
RELATION_KEEP = "keep"


# ---- 比较基准 ----

def source_version_for(run_id: str):
    """当批未发布时对最新已发布版本；已发布时对它的父版本。"""
    published = store.version_by_run(run_id)
    if published:
        parent = published.get("parent_version") or ""
        return store.get_version(parent) if parent else None
    return store.latest_version("published")


def _prev_members(source) -> list:
    return store.version_members(source["version_id"]) if source else []


def _prev_decisions(source) -> list:
    return store.version_decisions(source["version_id"]) if source else []


def _snapshot_of(member) -> dict:
    return {field: str(member.get(field) or "") for field in IDENTITY_KEY_FIELDS}


def _snapshot(record) -> dict:
    return identity.key_snapshot(record)


# ---- 批次内索引 ----

def _identity_index(records) -> dict:
    index = defaultdict(list)
    for record in records.values():
        index[identity.identity_key(record)].append(record)
    for key in index:
        index[key].sort(key=lambda item: item["supplier_id"])
    return dict(index)


def _pair_index(run_id, records) -> dict:
    pairs = {}
    for pair in store.list_pairs(run_id):
        left, right = records.get(pair["left_id"]), records.get(pair["right_id"])
        if left is None or right is None:
            continue
        key = identity.relation_key(identity.identity_key(left), identity.identity_key(right))
        pairs.setdefault(key, []).append(pair)
    return pairs


def _cluster_index(run_id, records) -> dict:
    clusters = {row["cluster_id"]: row for row in store.list_clusters(run_id)}
    members = defaultdict(list)
    for row in store.list_cluster_members(run_id):
        record = records.get(row["supplier_id"])
        if record is None:
            continue
        members[row["cluster_id"]].append({
            "supplier_id": row["supplier_id"],
            "identity_key": identity.identity_key(record),
            "share_amount": row["share_amount"],
        })
    return {cluster_id: {"row": row, "members": members.get(cluster_id, []),
                         "keys": {member["identity_key"] for member in members.get(cluster_id, [])}}
            for cluster_id, row in clusters.items()}


# ---- 关系承接 ----

def synthetic_pair_id(relation_key: str) -> str:
    digest = hashlib.sha256(relation_key.encode("utf-8")).hexdigest()[:10].upper()
    return "PC-" + digest


def build_relations(run_id: str, source) -> tuple:
    """返回 (entries, current_pairs)；entries 同时决定候选对的分档覆盖。"""
    records = {record["supplier_id"]: record for record in store.list_records(run_id)}
    identity_index = _identity_index(records)
    pairs_by_keys = _pair_index(run_id, records)
    prev_members = {member["identity_key"]: member for member in _prev_members(source)}

    entries = []
    for decision in _prev_decisions(source):
        prev_left, prev_right = decision["left_key"], decision["right_key"]
        relation_key = identity.relation_key(prev_left, prev_right)
        left_records = identity_index.get(prev_left, [])
        right_records = identity_index.get(prev_right, [])
        evidence = {
            "source_version": source["version_id"],
            "prev_pair_id": decision["pair_id"],
            "decision": decision["decision"],
            "reason_code": decision.get("reason_code", ""),
            "score": decision.get("score", ""),
            "operator": decision.get("operator", ""),
            "decided_at": decision.get("decided_at", ""),
            "row_version": decision.get("row_version", 0),
        }

        status, code, detail = "carried", R_CARRIED, ""
        if not left_records or not right_records:
            missing = ",".join(key for key, rows in ((prev_left, left_records),
                                                     (prev_right, right_records)) if not rows)
            status, code, detail = "exited", R_EXITED, f"身份键已不在本批次: {missing}"
        elif len(left_records) > 1 or len(right_records) > 1:
            status, code, detail = "pending", R_PENDING_IDENTITY, "身份键在本批次不唯一"
        else:
            changed = []
            for key, rows in ((prev_left, left_records), (prev_right, right_records)):
                before = prev_members.get(key)
                if before is None:
                    continue
                evidence.setdefault("prev_keys", {})[key] = _snapshot_of(before)
                evidence.setdefault("current_keys", {})[key] = _snapshot(rows[0])
                changed.extend(f"{field}" for field in
                               identity.changed_fields(_snapshot_of(before), _snapshot(rows[0])))
            if changed:
                status = "pending"
                code = R_PENDING_IDENTITY if len(changed) > 1 else R_PENDING_KEY_FIELD
                detail = "关键字段变化: " + ",".join(sorted(set(changed)))

        # 当批没有自然候选对时给一个稳定编号，让沿用结论仍有落点。
        current_pair = None
        pair_id = ""
        if len(left_records) == 1 and len(right_records) == 1:
            candidates = pairs_by_keys.get(relation_key) or []
            same_sides = [pair for pair in candidates
                          if {pair["left_id"], pair["right_id"]} ==
                          {left_records[0]["supplier_id"], right_records[0]["supplier_id"]}]
            current_pair = (same_sides or candidates or [None])[0]
            pair_id = current_pair["pair_id"] if current_pair else synthetic_pair_id(relation_key)
            evidence["current_pair_id"] = pair_id

        entries.append({
            "relation_key": relation_key,
            "source_version": source["version_id"],
            "prev_left_key": prev_left,
            "prev_right_key": prev_right,
            "left_key": identity.identity_key(left_records[0]) if len(left_records) == 1 else prev_left,
            "right_key": identity.identity_key(right_records[0]) if len(right_records) == 1
            else prev_right,
            "left_id": left_records[0]["supplier_id"] if len(left_records) == 1 else "",
            "right_id": right_records[0]["supplier_id"] if len(right_records) == 1 else "",
            "pair_id": pair_id, "synthetic": current_pair is None and bool(pair_id),
            "status": status,
            "code": code,
            "detail": detail,
            "score": decision.get("score", ""),
            "reason_code": decision.get("reason_code", ""),
            "prev_decision": decision["decision"],
            "operator": decision.get("operator", ""),
            "decided_at": decision.get("decided_at", ""),
            "evidence": evidence,
        })
    return entries, pairs_by_keys


def manual_verdict(run_id: str, pair_id: str) -> str:
    record = store.get_decision(run_id, pair_id)
    if not record or record.get("origin") == "carried":
        return "reject"
    return "auto" if record["decision"] == "merge" else "reject"


def relation_carry(entries, pairs_by_keys, decided_run_id: str = "") -> list:
    """承接状态转成候选对上的分档覆盖：沿用旧结论，待复核则拦进复核队列。

    当批已有人工判定的候选对不再被承接覆盖，人工结论优先。
    """
    manual = set()
    if decided_run_id:
        manual = {row["pair_id"] for row in store.list_decisions(decided_run_id)
                  if row.get("origin") != "carried"}
    carried_merge = {entry["relation_key"] for entry in entries
                     if entry["status"] == "carried" and entry["prev_decision"] == RELATION_MERGE}
    carried_keep = {entry["relation_key"] for entry in entries
                    if entry["status"] == "carried" and entry["prev_decision"] == RELATION_KEEP}
    pending_notes = {entry["relation_key"]: entry for entry in entries
                     if entry["status"] == "pending"}

    updates = []
    for relation_key, pairs in pairs_by_keys.items():
        for pair in pairs:
            if pair["pair_id"] in manual:
                # 人工已在本批重新判定：标记为重新确认，不再显示待复核。
                if relation_key in carried_merge or relation_key in carried_keep:
                    updates.append({"pair_id": pair["pair_id"],
                                    "verdict": manual_verdict(decided_run_id, pair["pair_id"]),
                                    "carry_status": "carried", "carry_code": R_RECONFIRMED,
                                    "carry_detail": "本批已重新确认"})
                continue
            if relation_key in carried_merge:
                if pair["reason_code"] == "P_TAX_CONFLICT":
                    # 上一版允许合并、当批却出现税号硬冲突：身份不再相容，退回复核。
                    updates.append({"pair_id": pair["pair_id"], "verdict": "review",
                                    "carry_status": "pending",
                                    "carry_code": R_PENDING_IDENTITY,
                                    "carry_detail": "沿用结论与当批税号冲突矛盾"})
                    continue
                updates.append({"pair_id": pair["pair_id"], "verdict": "auto",
                                "carry_status": "carried", "carry_code": R_CARRIED,
                                "carry_detail": "沿用上一版已确认合并"})
            elif relation_key in carried_keep:
                updates.append({"pair_id": pair["pair_id"], "verdict": "reject",
                                "carry_status": "carried", "carry_code": R_CARRIED,
                                "carry_detail": "沿用上一版已确认保留"})
            elif relation_key in pending_notes:
                entry = pending_notes[relation_key]
                updates.append({"pair_id": pair["pair_id"], "verdict": "review",
                                "carry_status": "pending", "carry_code": entry["code"],
                                "carry_detail": entry["detail"]})
    return updates


def build_subjects(run_id: str, source) -> list:
    if source is None:
        return []
    records = {record["supplier_id"]: record for record in store.list_records(run_id)}
    previous = {}
    prev_members = defaultdict(list)
    for member in _prev_members(source):
        prev_members[member["cluster_id"]].append(member)
    for cluster in store.version_clusters(source["version_id"]):
        previous[cluster["cluster_id"]] = {
            "cluster": cluster,
            "keys": {member["identity_key"] for member in prev_members.get(cluster["cluster_id"], [])},
            "members": prev_members.get(cluster["cluster_id"], []),
        }
    current = _cluster_index(run_id, records)

    used, entries = set(), []
    for prev_id in sorted(previous):
        payload = previous[prev_id]
        best, best_overlap = None, 0
        for cluster_id in sorted(current):
            if cluster_id in used:
                continue
            overlap = len(current[cluster_id]["keys"] & payload["keys"])
            if overlap > best_overlap:
                best, best_overlap = cluster_id, overlap
        added = removed = set()
        status, code, detail = "exited", R_EXITED, "当批找不到对应主体"
        if best:
            used.add(best)
            cur_keys = current[best]["keys"]
            added, removed = cur_keys - payload["keys"], payload["keys"] - cur_keys
            if cur_keys == payload["keys"]:
                status, code, detail = "carried", R_CARRIED, "成员与上一版一致"
            elif added and not removed:
                status, code = "pending", R_PENDING_NEW_MEMBER
                detail = "新增成员: " + ",".join(sorted(added))
            else:
                # 主体仍在（部分成员有重叠），但成员减少或增减并存：构成变化，
                # 旧主体不能直接沿用，交人工确认；只有找不到对应主体才算退出。
                status, code = "pending", R_SUBJECT_CHANGED
                parts = []
                if added:
                    parts.append("新增 " + ",".join(sorted(added)))
                if removed:
                    parts.append("成员退出 " + ",".join(sorted(removed)))
                detail = "；".join(parts)
        entries.append({
            "relation_key": f"SUBJ:P:{prev_id}",
            "source_version": source["version_id"],
            "prev_left_key": prev_id,
            "prev_right_key": payload["cluster"]["rep_key"],
            "left_key": best or "",
            "right_key": current[best]["row"]["rep_id"] if best else "",
            "left_id": current[best]["row"]["rep_id"] if best else "",
            "right_id": "",
            "pair_id": "",
            "status": status,
            "code": code,
            "detail": detail,
            "score": "",
            "reason_code": "",
            "prev_decision": "",
            "operator": "",
            "decided_at": "",
            "evidence": {
                "source_version": source["version_id"],
                "prev_cluster_id": prev_id,
                "prev_total": str(payload["cluster"]["total_amount"]),
                "prev_member_keys": sorted(payload["keys"]),
                "current_cluster_id": best or "",
                "current_member_keys": sorted(current[best]["keys"]) if best else [],
                "added_keys": sorted(added),
                "removed_keys": sorted(removed),
            },
        })
    for cluster_id in sorted(current):
        if cluster_id in used:
            continue
        entries.append({
            "relation_key": f"SUBJ:N:{cluster_id}",
            "source_version": source["version_id"],
            "prev_left_key": "",
            "prev_right_key": "",
            "left_key": cluster_id,
            "right_key": current[cluster_id]["row"]["rep_id"],
            "left_id": current[cluster_id]["row"]["rep_id"],
            "right_id": "",
            "pair_id": "",
            "status": "new",
            "code": R_SUBJECT_NEW,
            "detail": "上一版无对应主体",
            "score": "",
            "reason_code": "",
            "prev_decision": "",
            "operator": "",
            "decided_at": "",
            "evidence": {"source_version": source["version_id"],
                         "current_member_keys": sorted(current[cluster_id]["keys"])},
        })
    return entries


def _counts(relations, subjects, resolved=()) -> dict:
    return {
        "relations": len(relations),
        "relations_carried": sum(1 for e in relations if e["status"] == "carried"),
        "relations_reconfirmed": len(resolved),
        "relations_pending": sum(1 for e in relations if e["status"] == "pending"),
        "relations_exited": sum(1 for e in relations if e["status"] == "exited"),
        "subjects": len(subjects),
        "subjects_carried": sum(1 for e in subjects if e["status"] == "carried"),
        "subjects_pending": sum(1 for e in subjects if e["status"] == "pending"),
        "subjects_exited": sum(1 for e in subjects if e["status"] == "exited"),
        "subjects_new": sum(1 for e in subjects if e["status"] == "new"),
    }


# ---- 编排 ----

def resolved_relation_keys(run_id: str) -> set:
    """待复核承接关系里，当批已有人工判定的那些。"""
    manual = {row["pair_id"] for row in store.list_decisions(run_id)
              if row.get("origin") != "carried"}
    return {entry["relation_key"] for entry in store.list_carry_entries(run_id)
            if entry["kind"] == "relation" and entry["status"] == "pending"
            and entry["pair_id"] in manual}


def ensure_relations(run_id: str) -> dict:
    """关系承接：依赖采信、候选对与上一版快照；阶段内幂等。"""
    source = source_version_for(run_id)
    relations, pairs_by_keys = build_relations(run_id, source)
    updates = relation_carry(relations, pairs_by_keys, run_id)
    if updates:
        store.update_pair_carry(run_id, updates)
    store.save_carry_entries(run_id, "relation", relations)
    materialize_carried(run_id)
    subjects = [entry for entry in store.list_carry_entries(run_id) if entry["kind"] == "subject"]
    store.save_carry_meta(run_id, source["version_id"] if source else "", utc_iso(utc_now()),
                          _counts(relations, subjects, resolved_relation_keys(run_id)))
    return {"run_id": run_id, "source_version": source["version_id"] if source else "",
            "relations": len(relations), "pair_updates": len(updates)}


def refresh_subjects(run_id: str) -> dict:
    """主体承接：簇算好后再刷新；关系台账保持不动。"""
    if not store.list_clusters(run_id):
        return {"run_id": run_id, "subjects": 0, "skipped": "clusters-missing"}
    source = source_version_for(run_id)
    relations = [entry for entry in store.list_carry_entries(run_id) if entry["kind"] == "relation"]
    subjects = build_subjects(run_id, source)
    store.save_carry_entries(run_id, "subject", subjects)
    meta = store.get_carry_run(run_id) or {}
    store.save_carry_meta(run_id, source["version_id"] if source else "",
                          meta.get("computed_at") or utc_iso(utc_now()),
                          _counts(relations, subjects, resolved_relation_keys(run_id)))
    return {"run_id": run_id, "source_version": source["version_id"] if source else "",
            "subjects": len(subjects)}


def compute(run_id: str) -> dict:
    """一次性算全：关系 + 主体；簇未算时不写主体台账，避免误判全部退出。"""
    ensure_relations(run_id)
    return refresh_subjects(run_id)


def counts(run_id: str) -> dict:
    meta = store.get_carry_run(run_id)
    if meta is None:
        compute(run_id)
        meta = store.get_carry_run(run_id)
    return meta["counts"] if meta else {}


def summary(run_id: str) -> dict:
    meta = store.get_carry_run(run_id)
    if meta is None:
        compute(run_id)
        meta = store.get_carry_run(run_id)
    return {
        "run_id": run_id,
        "source_version": meta["previous_version"],
        "computed_at": meta["computed_at"],
        "counts": meta["counts"],
        "entries": store.list_carry_entries(run_id),
        "pending": pending_entries(run_id),
        "relations": relations_for_display(run_id),
        "subjects": subjects_for_display(run_id),
    }


# ---- 供队列与页面使用 ----

def pending_entries(run_id: str) -> list:
    """承接台账里待复核的关系，附当批是否已人工判定。"""
    decisions = {row["pair_id"]: row for row in store.list_decisions(run_id)
                 if row.get("origin") != "carried"}
    items = []
    for entry in store.list_carry_entries(run_id):
        if entry["kind"] != "relation" or entry["status"] != "pending":
            continue
        pair_id = entry["pair_id"]
        current = decisions.get(pair_id) if pair_id else None
        items.append({
            **entry,
            "current_decision": current["decision"] if current else "",
            "current_version": current["version"] if current else 0,
            "resolved": current is not None,
        })
    return items


def relations_for_display(run_id: str) -> list:
    records = {record["supplier_id"]: record for record in store.list_records(run_id)}
    rows = []
    for entry in store.list_carry_entries(run_id):
        if entry["kind"] != "relation":
            continue
        row = dict(entry)
        row["left_name"] = records.get(entry["left_id"], {}).get("raw_name", "")
        row["right_name"] = records.get(entry["right_id"], {}).get("raw_name", "")
        row["left_tax"] = records.get(entry["left_id"], {}).get("tax_id", "")
        row["right_tax"] = records.get(entry["right_id"], {}).get("tax_id", "")
        rows.append(row)
    return rows


def subjects_for_display(run_id: str) -> list:
    clusters = {row["cluster_id"]: row for row in store.list_clusters(run_id)}
    rows = []
    for entry in store.list_carry_entries(run_id):
        if entry["kind"] != "subject":
            continue
        row = dict(entry)
        current = clusters.get(entry["left_key"])
        row["current_rep"] = current["rep_id"] if current else ""
        row["member_count"] = current["member_count"] if current else 0
        row["total_amount"] = current["total_amount"] if current else \
            entry["evidence"].get("prev_total", "0.00")
        row["prev_cluster"] = entry["prev_left_key"]
        row["prev_member_keys"] = entry["evidence"].get("prev_member_keys", [])
        row["current_member_keys"] = entry["evidence"].get("current_member_keys", [])
        row["added_keys"] = entry["evidence"].get("added_keys", [])
        row["removed_keys"] = entry["evidence"].get("removed_keys", [])
        rows.append(row)
    return rows


def materialize_carried(run_id: str) -> int:
    """把"沿用"的关系写成当批决定行（origin=carried），发布时随版本一起固化。

    只补不覆盖：当批已有人工决定的关系保持人工版本，避免重跑改写版本号。
    每次重算前先清掉上一轮自动生成的沿用行，保证同一批重跑结果一致。
    """
    records = {record["supplier_id"]: record for record in store.list_records(run_id)}
    pairs = {pair["pair_id"]: pair for pair in store.list_pairs(run_id)}
    manual = {row["pair_id"]: row for row in store.list_decisions(run_id)
              if row.get("origin") != "carried"}
    created = 0
    with store.connect() as conn:
        conn.execute("DELETE FROM decisions WHERE run_id = ? AND origin = 'carried'", (run_id,))
        for entry in store.list_carry_entries(run_id):
            if entry["kind"] != "relation" or entry["status"] != "carried":
                continue
            pair_id = entry["pair_id"]
            if not pair_id or pair_id in manual:
                continue
            pair = pairs.get(pair_id)
            if pair is None or pair["verdict"] not in ("auto", "reject"):
                continue
            conn.execute(
                "INSERT OR REPLACE INTO decisions (run_id, pair_id, decision, expected_version,"
                " version, operator, decided_at, origin, source_version, carry_code)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, pair_id,
                 "merge" if pair["verdict"] == "auto" else "keep",
                 0, int(entry["evidence"].get("row_version") or 1),
                 entry["evidence"].get("operator") or "采购主数据组",
                 entry["evidence"].get("decided_at") or utc_iso(utc_now()),
                 "carried", entry["source_version"], R_CARRIED),
            )
            created += 1
        conn.commit()
    return created


# ---- 新增成员 / 关键字段变化：强制回到复核 ----

def forced_review(run_id: str) -> dict:
    """当批候选对里，涉及"新增成员"或"关键字段变化"的一律拦进复核。

    规则（按优先级）：
    - 身份键在本批不唯一 -> R_PENDING_IDENTITY；
    - 一侧是上一版已存在的主体、另一侧是本批新增记录的候选对 -> R_PENDING_NEW_MEMBER；
    - 任一侧记录的关键字段相对上一版发生变化 -> R_PENDING_KEY_FIELD；
    已经有人工判定的候选对不再覆盖。
    """
    source = source_version_for(run_id)
    if source is None:
        return {"run_id": run_id, "forced": 0}
    records = {record["supplier_id"]: record for record in store.list_records(run_id)}
    prev_members = {member["identity_key"]: member for member in _prev_members(source)}
    identity_index = _identity_index(records)
    # 只有人工判定优先；系统沿用行不能挡住"必须复核"的判断。
    decided = {row["pair_id"] for row in store.list_decisions(run_id)
               if row.get("origin") != "carried"}
    carry_entries = {entry["relation_key"]: entry for entry in store.list_carry_entries(run_id)
                     if entry["kind"] == "relation"}

    changed_cache = {}
    for key, rows in identity_index.items():
        before = prev_members.get(key)
        if before is None or len(rows) != 1:
            continue
        fields = identity.changed_fields(_snapshot_of(before), _snapshot(rows[0]))
        if fields:
            changed_cache[key] = fields

    updates = []
    for pair in store.list_pairs(run_id):
        if pair["pair_id"] in decided or pair["verdict"] != "auto":
            continue
        left = records.get(pair["left_id"])
        right = records.get(pair["right_id"])
        if left is None or right is None:
            continue
        left_key, right_key = identity.identity_key(left), identity.identity_key(right)
        code = detail = ""
        if len(identity_index.get(left_key, [])) > 1 or len(identity_index.get(right_key, [])) > 1:
            code, detail = R_PENDING_IDENTITY, "身份键在本批次不唯一"
        elif (left_key in prev_members) != (right_key in prev_members):
            new_key = right_key if right_key not in prev_members else left_key
            code = R_PENDING_NEW_MEMBER
            detail = f"新增成员 {new_key} 并入上一版已存在主体"
        else:
            changed = [f for key in (left_key, right_key)
                       for f in changed_cache.get(key, [])]
            if changed:
                fields = sorted(set(changed))
                # 两个以上关键字段同时变化视为身份冲突，比单字段变更更需人工核对。
                code = R_PENDING_IDENTITY if len(fields) > 1 else R_PENDING_KEY_FIELD
                detail = "关键字段变化: " + ",".join(fields)
        if not code:
            continue
        if pair["reason_code"] == "P_TAX_CONFLICT":
            continue
        updates.append({"pair_id": pair["pair_id"], "verdict": "review",
                        "carry_status": "pending", "carry_code": code,
                        "carry_detail": detail})
    if updates:
        store.update_pair_carry(run_id, updates)
        # 被拦回复核的承接关系不能再显示为"已沿用"，撤掉它的系统决定行。
        store.delete_carried_decisions(run_id, [item["pair_id"] for item in updates])
    return {"run_id": run_id, "forced": len(updates)}
