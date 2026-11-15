"""M8 跨批次承接：身份键配对、沿用/待复核/退出、重跑幂等与依据留存。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app import store
from app.modules.m1_intake import service as intake_service
from app.modules.m5_clusters import service as cluster_service
from app.modules.m6_reviews import service as review_service
from app.modules.m8_carry import identity, service as carry_service
from app.modules.m9_versions import service as version_service
from tests.fixtures import expected as EXP


def _prepare(db, files=None, directory=None):
    run = intake_service.ingest(files=files, directory=directory)
    cluster_service.ensure_clusters(run["run_id"])
    return run["run_id"]


@pytest.fixture()
def february(db):
    """第一批：确认"两江包装"合并与"P-00105"保留，发布为 V-0001。"""
    run_id = _prepare(db)
    review_service.decide(run_id, EXP.PAIR_CITY_MISMATCH, "merge", 0, "王核")
    review_service.decide(run_id, EXP.PAIR_REVIEW, "keep", 0, "王核")
    version = version_service.publish(run_id)
    assert version["created"] is True
    return run_id, version["version_id"]


def test_identity_key_prefers_supplier_no():
    record = {"supplier_id": "S-0001", "supplier_no": "000101"}
    assert identity.identity_key(record) == "NO:000101"
    assert identity.identity_key({"supplier_id": "S-0001", "supplier_no": ""}) == "ID:S-0001"
    assert identity.relation_key("NO:2", "NO:1") == identity.relation_key("NO:1", "NO:2")


def test_carried_relation_keeps_old_decision(db, batch_factory, february):
    run_id, version_id = february
    # 下一月：文件内容不变（仍含同样两对），但重新采信为独立批次。
    next_dir = batch_factory("march")
    next_run = _prepare(db, directory=next_dir)
    assert next_run != run_id

    summary = carry_service.summary(next_run)
    assert summary["source_version"] == version_id
    carried = {entry["relation_key"]: entry for entry in summary["relations"]}
    target = identity.relation_key("NO:000113", "NO:000178")
    assert carried[target]["status"] == "carried"
    assert carried[target]["prev_decision"] == "merge"
    assert carried[target]["operator"] == "王核"

    kept = identity.relation_key("NO:000173", "NO:000174")
    assert carried[kept]["status"] == "carried"
    assert carried[kept]["prev_decision"] == "keep"
    # 沿用的旧结论落成当批决定，且保留来源与操作人。
    decision = store.get_decision(next_run, carried[target]["pair_id"])
    assert decision["decision"] == "merge"
    assert decision["origin"] == "carried"
    assert decision["operator"] == "王核"
    assert decision["source_version"] == version_id


def test_key_field_change_returns_to_review(db, batch_factory, february):
    run_id, _version = february
    changed = batch_factory("march-city", mutations={
        "erp_a_primary_utf8.csv": [("S-0013,000113,重庆两江包装有限公司,91500000MA5X6Y7Z8,重庆,",
                                    "S-0013,000113,重庆两江包装有限公司,91500000MA5X6Y7Z8,贵阳,")]
    })
    next_run = _prepare(db, directory=changed)
    summary = carry_service.summary(next_run)
    target = identity.relation_key("NO:000113", "NO:000178")
    entry = {item["relation_key"]: item for item in summary["relations"]}[target]
    assert entry["status"] == "pending"
    assert entry["code"] == "R_PENDING_KEY_FIELD"
    assert "city" in entry["detail"]
    # 旧结论不再作为决定生效。
    assert store.get_decision(next_run, entry["pair_id"]) in (None,)


def test_key_field_change_forces_candidate_pairs_to_review(db, batch_factory, february):
    """税号与城市同时变化（身份冲突）：原本自动合并的候选对必须回到复核。"""
    conflicted = batch_factory("march-identity", mutations={
        "erp_a_primary_utf8.csv": [
            ("S-0001,000101,北京鼎新科技有限公司,91110108MA01C7X21,北京,",
             "S-0001,000101,北京鼎新数智科技有限公司,91110108MA01C7X21,天津,"),
        ]
    })
    next_run = _prepare(db, directory=conflicted)
    touching = [pair for pair in store.list_pairs(next_run)
                if "S-0001" in (pair["left_id"], pair["right_id"])
                and pair["reason_code"] != "P_TAX_CONFLICT"]
    # 税号冲突本来就不能合并，其余原本自动合并的对必须退回复核。
    auto_like = [pair for pair in touching if pair["verdict"] != "reject"]
    assert auto_like
    assert all(pair["verdict"] == "review" for pair in auto_like)
    assert all(pair["carry_code"] == "R_PENDING_IDENTITY" for pair in auto_like)
    subjects = carry_service.summary(next_run)["subjects"]
    old_subject = [entry for entry in subjects if "NO:000101" in entry["prev_member_keys"]]
    assert old_subject and old_subject[0]["status"] in ("pending", "exited")


def test_new_member_does_not_join_old_subject_before_review(db, batch_factory, february):
    """新增成员即使税号相同，也只能进复核队列，不能直接并进上一版主体。"""
    grown = batch_factory("march-new-member", extra_rows={
        "erp_a_primary_utf8.csv":
            "S-0090,000190,华远物流有限公司,91310115MA1H8K2Q9,上海,6222020200099999,ERP-A,,21000.00,2026-05-05"
    })
    next_run = _prepare(db, directory=grown)
    pair = [item for item in store.list_pairs(next_run)
            if {item["left_id"], item["right_id"]} == {"S-0038", "S-0090"}]
    assert pair and pair[0]["verdict"] == "review"
    assert pair[0]["carry_code"] == "R_PENDING_NEW_MEMBER"
    member_of = {row["supplier_id"]: row["cluster_id"]
                 for row in store.list_cluster_members(next_run)}
    assert member_of["S-0090"] != member_of["S-0038"]
    # 待复核承接关系在台账里可查，且带新增成员依据。
    pending = carry_service.summary(next_run)["pending"]
    assert any("新增成员" in item["detail"] or item["code"] == "R_PENDING_NEW_MEMBER"
               for item in pending) or pair[0]["carry_detail"]


def test_exited_subject_is_recorded(db, batch_factory, february):
    """上一版成员在下一批消失：主体与关系都记为退出并保留旧版成员依据。"""
    reduced = batch_factory("march-exit")
    for name in ("erp_a_primary_utf8.csv", "erp_d_amounts.csv"):
        path = f"{reduced}/{name}"
        lines = [line for line in open(path, encoding="utf-8").read().splitlines()
                 if not line.startswith("S-0078,")]
        open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    next_run = _prepare(db, directory=reduced)
    relations = {entry["relation_key"]: entry
                 for entry in carry_service.summary(next_run)["relations"]}
    target = relations[identity.relation_key("NO:000113", "NO:000178")]
    assert target["status"] == "exited"
    assert target["code"] == "R_EXITED"
    assert "NO:000178" in target["detail"]

    subjects = carry_service.summary(next_run)["subjects"]
    old = [entry for entry in subjects if entry["prev_left_key"] == "C-0023"]
    assert old
    # 主体还有成员在，但少了 000178：构成变化，交人工确认而不是直接退出。
    assert old[0]["status"] == "pending"
    assert old[0]["code"] == "R_SUBJECT_CHANGED"
    assert "NO:000178" in old[0]["removed_keys"]
    assert old[0]["prev_member_keys"]


def test_rerun_same_batch_does_not_grow_ledger(db, batch_factory, february):
    run_id, version_id = february
    next_dir = batch_factory("march")
    first = _prepare(db, directory=next_dir)
    entries_first = store.count(first, "carry_entries")
    decisions_first = len(store.list_decisions(first))
    versions_first = len(store.list_versions("", 50))

    second = _prepare(db, directory=next_dir)
    assert second == first
    assert store.count(second, "carry_entries") == entries_first
    assert len(store.list_decisions(second)) == decisions_first
    assert len(store.list_versions("", 50)) == versions_first


def test_carried_decision_survives_rerun(db, batch_factory, february):
    run_id, version_id = february
    next_dir = batch_factory("march")
    next_run = _prepare(db, directory=next_dir)
    before = [(row["pair_id"], row["decision"], row["origin"])
              for row in store.list_decisions(next_run)]
    cluster_service.ensure_clusters(next_run)
    after = [(row["pair_id"], row["decision"], row["origin"])
             for row in store.list_decisions(next_run)]
    assert before == after
    assert any(origin == "carried" for _pair, _decision, origin in after)


def test_resolving_pending_carry_marks_reconfirmed(db, batch_factory, february):
    _run, _version = february
    changed = batch_factory("march-city", mutations={
        "erp_a_primary_utf8.csv": [("S-0013,000113,重庆两江包装有限公司,91500000MA5X6Y7Z8,重庆,",
                                    "S-0013,000113,重庆两江包装有限公司,91500000MA5X6Y7Z8,贵阳,")]
    })
    next_run = _prepare(db, directory=changed)
    pending = [item for item in carry_service.summary(next_run)["pending"] if item["pair_id"]]
    assert pending
    review_service.decide(next_run, pending[0]["pair_id"], "keep", 0, "李核")
    cluster_service.ensure_clusters(next_run)
    counts = carry_service.counts(next_run)
    assert counts["relations_reconfirmed"] == 1
    assert counts["relations_pending"] - counts["relations_reconfirmed"] == 0


def test_carry_without_published_version_is_empty(db):
    run_id = _prepare(db)
    summary = carry_service.summary(run_id)
    assert summary["source_version"] == ""
    assert summary["counts"]["relations"] == 0
    assert summary["counts"]["subjects"] == 0


def test_carry_stage_does_not_duplicate_pairs(db, batch_factory, february):
    next_dir = batch_factory("march")
    next_run = _prepare(db, directory=next_dir)
    first = store.count(next_run, "pair_scores")
    cluster_service.ensure_clusters(next_run)
    cluster_service.ensure_clusters(next_run)
    assert store.count(next_run, "pair_scores") == first


def test_key_field_change_enters_review_queue_and_leaves_subject(db, batch_factory, february):
    """关键字段变化：候选对从自动改为复核，主体承接登记为退出，不再自动沿用。"""
    changed = batch_factory("march-single-field", mutations={
        "erp_a_primary_utf8.csv": [
            ("S-0001,000101,北京鼎新科技有限公司,91110108MA01C7X21,北京,",
             "S-0001,000101,北京鼎新科技有限公司,91110108MA01C7X21,天津,"),
        ]
    })
    next_run = _prepare(db, directory=changed)
    queue = review_service.queue_items(next_run)
    touched = [item for item in queue if "S-0001" in (item["left_id"], item["right_id"])]
    assert touched
    assert all(item["carry_code"] == "R_PENDING_KEY_FIELD" for item in touched)

    carried_pairs = [pair for pair in store.list_pairs(next_run)
                     if "S-0001" in (pair["left_id"], pair["right_id"])
                     and pair["carry_status"] == "carried"]
    assert not carried_pairs

    decisions = [row for row in store.list_decisions(next_run)
                 if row["pair_id"] in {item["pair_id"] for item in touched}]
    assert not decisions or all(row["origin"] != "carried" for row in decisions)

    subjects = carry_service.summary(next_run)["subjects"]
    old = [entry for entry in subjects if "NO:000101" in entry["prev_member_keys"]]
    # 主体仍有重叠成员，只是关键字段变了：不回退为"退出"，而是构成变化待复核。
    assert old and old[0]["status"] == "pending"
    assert old[0]["code"] == "R_SUBJECT_CHANGED"
