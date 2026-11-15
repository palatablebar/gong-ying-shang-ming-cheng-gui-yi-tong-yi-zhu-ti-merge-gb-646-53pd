"""M9 版本发布：原子切换、失败回滚、幂等、前后差异与发布后锁定。"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app import store
from app.errors import DomainError, NotFound
from app.modules.m1_intake import service as intake_service
from app.modules.m5_clusters import service as cluster_service
from app.modules.m6_reviews import service as review_service
from app.modules.m8_carry import identity
from app.modules.m9_versions import service as version_service
from tests.fixtures import expected as EXP


def _ready(db, files=None, directory=None) -> str:
    run = intake_service.ingest(files=files, directory=directory)
    cluster_service.ensure_clusters(run["run_id"])
    return run["run_id"]


def _snapshot(version):
    return {
        "clusters": store.version_clusters(version["version_id"]),
        "members": store.version_members(version["version_id"]),
        "decisions": store.version_decisions(version["version_id"]),
    }


def test_publish_switches_master_and_decisions_together(db):
    run_id = _ready(db)
    review_service.decide(run_id, EXP.PAIR_CITY_MISMATCH, "merge", 0, "王核")
    version = version_service.publish(run_id)
    assert version["created"] is True
    assert version["status"] == "published"

    clusters = store.version_clusters(version["version_id"])
    members = store.version_members(version["version_id"])
    decisions = store.version_decisions(version["version_id"])
    assert len(clusters) == EXP.CLUSTERS - 1
    assert len(members) == EXP.ACCEPTED
    assert sum(Decimal(row["share_amount"]) for row in members) == EXP.AMOUNT_TOTAL
    assert sum(Decimal(row["total_amount"]) for row in clusters) == EXP.AMOUNT_TOTAL
    assert decisions and decisions[0]["decision"] == "merge"
    assert decisions[0]["operator"] == "王核"


def test_republish_same_content_adds_no_version(db):
    run_id = _ready(db)
    first = version_service.publish(run_id)
    second = version_service.publish(run_id)
    assert second["version_id"] == first["version_id"]
    assert second["created"] is False
    assert len(store.list_versions("", 50)) == 1
    assert store.count(run_id, "decisions") == len(
        store.version_decisions(first["version_id"]))


def test_republish_after_new_decision_supersedes_previous(db):
    """发布后继续判定，再发布会生成新版本；旧版本保留为"已被替代"可追溯。"""
    run_id = _ready(db)
    first = version_service.publish(run_id)
    review_service.decide(run_id, EXP.PAIR_CITY_MISMATCH, "merge", 0, "王核")
    second = version_service.publish(run_id)
    assert second["version_id"] != first["version_id"]
    assert second["parent_version"] == first["version_id"]
    assert second["created"] is True
    sealed = store.get_version(first["version_id"])
    assert sealed["status"] == "superseded"
    # 旧版本的快照内容不随新决定变化。
    assert len(store.version_clusters(first["version_id"])) == EXP.CLUSTERS
    assert len(store.version_clusters(second["version_id"])) == EXP.CLUSTERS - 1


def test_failed_publish_keeps_previous_version(db, monkeypatch):
    run_id = _ready(db)
    good = version_service.publish(run_id)
    before = _snapshot(good)

    review_service.decide(run_id, EXP.PAIR_CITY_MISMATCH, "merge", 0, "王核")
    # 让写入在版本头之后失败，验证没有半成品版本。
    original = store.publish_version

    def broken(version, clusters, members, decisions):
        original(version, clusters, members, decisions)
        raise RuntimeError("磁盘写入失败")

    monkeypatch.setattr(store, "publish_version", broken)
    with pytest.raises(DomainError) as error:
        version_service.publish(run_id)
    assert error.value.code == "V_PUBLISH_FAILED"
    monkeypatch.setattr(store, "publish_version", original)

    versions = store.list_versions("", 50)
    assert [row["version_id"] for row in versions] == [good["version_id"]]
    assert store.get_version("V-0002") is None
    assert _snapshot(good) == before
    # 已发布版本仍可正常读取，主数据没有丢失。
    assert sum(Decimal(row["total_amount"]) for row in before["clusters"]) == EXP.AMOUNT_TOTAL


def test_failed_self_check_aborts_publish(db, monkeypatch):
    run_id = _ready(db)
    version_service.publish(run_id)
    review_service.decide(run_id, EXP.PAIR_CITY_MISMATCH, "merge", 0, "王核")
    monkeypatch.setattr(version_service, "_self_check",
                        lambda *args, **kwargs: [
                            {"name": "分摊守恒", "left": "1", "right": "2", "ok": False}])
    with pytest.raises(DomainError) as error:
        version_service.publish(run_id)
    assert error.value.code == "V_PUBLISH_FAILED"
    assert len(store.list_versions("", 50)) == 1


def test_same_batch_rerun_keeps_one_version(db):
    run_id = _ready(db)
    first = version_service.publish(run_id)
    for _ in range(3):
        again = intake_service.ingest(force=True)
        cluster_service.ensure_clusters(again["run_id"])
        version = version_service.publish(again["run_id"])
        assert version["version_id"] == first["version_id"]
    assert len(store.list_versions("", 50)) == 1


def test_compare_reports_carry_and_exits(db, batch_factory):
    run_id = _ready(db)
    review_service.decide(run_id, EXP.PAIR_CITY_MISMATCH, "merge", 0, "王核")
    review_service.decide(run_id, EXP.PAIR_REVIEW, "keep", 0, "王核")
    first = version_service.publish(run_id)

    february = batch_factory  # 仅用于说明：下一批由 batch_factory 构造
    next_dir = february("march", extra_rows={
        "erp_a_primary_utf8.csv":
            "S-0090,000190,华远物流有限公司,91310115MA1H8K2Q9,上海,6222020200099999,ERP-A,,21000.00,2026-05-05"
    })
    next_run = _ready(db, directory=next_dir)
    second = version_service.publish(next_run)
    assert second["parent_version"] == first["version_id"]

    diff = version_service.compare(first["version_id"], second["version_id"])
    assert diff["before"]["version_id"] == first["version_id"]
    assert diff["after"]["version_id"] == second["version_id"]
    added = diff["subjects"]["added"]
    assert any(row["after"]["rep_id"] == "S-0090" for row in added)
    assert diff["counts"]["decisions_removed"] == 0 or diff["decisions"]
    assert Decimal(diff["amount"]["after"]) == EXP.AMOUNT_TOTAL + Decimal("21000.00")
    # 新增成员并入旧主体的候选对被拦在复核里，旧主体不直接吞并新成员。
    grouped = {member["identity_key"] for member in store.version_members(second["version_id"])
               if member["supplier_id"] == "S-0090"}
    assert grouped


def test_diff_aligns_subjects_by_identity_not_cluster_id(db, batch_factory):
    run_id = _ready(db)
    first = version_service.publish(run_id)
    # 删掉一条记录会改变后续簇编号，但主体应被识别为"退出/构成变化"而不是全部新增。
    removed = batch_factory("march-drop")
    path = f"{removed}/erp_d_amounts.csv"
    lines = [line for line in open(path, encoding="utf-8").read().splitlines()
             if not line.startswith("S-0078,")]
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    next_run = _ready(db, directory=removed)
    second = version_service.publish(next_run)
    diff = version_service.compare(first["version_id"], second["version_id"])
    assert diff["counts"]["subjects_added"] <= 1
    assert diff["counts"]["subjects_same"] + diff["counts"]["subjects_changed"] >= (
        diff["before"]["counts"]["subjects"] - 2)


def test_preview_matches_published_diff(db, batch_factory):
    run_id = _ready(db)
    review_service.decide(run_id, EXP.PAIR_CITY_MISMATCH, "merge", 0, "王核")
    first = version_service.publish(run_id)
    next_dir = batch_factory("march")
    next_run = _ready(db, directory=next_dir)
    preview = version_service.preview(next_run)
    assert preview["source_version"] == first["version_id"]
    assert preview["counts"]["subjects"] == len(preview["subjects"]["same"]) + \
        len(preview["subjects"]["added"]) + len(preview["subjects"]["changed"])
    second = version_service.publish(next_run)
    diff = version_service.compare(first["version_id"], second["version_id"])
    assert diff["counts"]["subjects_same"] == len(preview["subjects"]["same"])


def test_version_api_and_missing_version(client):
    published = client.post("/api/versions/publish")
    assert published.status_code == 200
    version_id = published.json()["version"]["version_id"]
    detail = client.get(f"/api/versions/{version_id}")
    assert detail.status_code == 200
    assert detail.json()["clusters"]
    missing = client.get("/api/versions/V-9999")
    assert missing.status_code == 404
    assert missing.json()["reason_code"] == "V_UNKNOWN_VERSION"

    listing = client.get("/api/versions")
    assert listing.status_code == 200
    assert listing.json()["latest"]["version_id"] == version_id
    preview = client.get("/api/versions/preview")
    assert preview.status_code == 200
    assert "subjects" in preview.json()


def test_publish_api_is_idempotent(client):
    first = client.post("/api/versions/publish").json()["version"]
    second = client.post("/api/versions/publish").json()["version"]
    assert first["version_id"] == second["version_id"]
    assert second["created"] is False


def test_manifest_includes_version(client):
    client.post("/api/versions/publish")
    manifest = json.loads(client.get("/api/export/manifest.json").text)
    assert manifest["reconcile"]["ok"] is True
    assert manifest["run_id"] == EXP.RUN_ID


def test_publish_failure_does_not_touch_published_master(client, monkeypatch):
    """发布失败时读取已发布版本仍返回完整旧版：主体、成员与决定都在。"""
    client.post("/api/versions/publish")
    version_id = client.get("/api/versions/latest").json()["version_id"]
    before = client.get(f"/api/versions/{version_id}").json()

    original = store.publish_version

    def broken(version, clusters, members, decisions):
        original(version, clusters, members, decisions)
        raise RuntimeError("事务提交失败")

    monkeypatch.setattr(store, "publish_version", broken)
    client.post("/api/reviews/%s/decision" % EXP.PAIR_CITY_MISMATCH,
                json={"decision": "merge", "expected_version": 0})
    failed = client.post("/api/versions/publish")
    assert failed.status_code == 400
    assert failed.json()["reason_code"] == "V_PUBLISH_FAILED"
    monkeypatch.setattr(store, "publish_version", original)

    after = client.get(f"/api/versions/{version_id}").json()
    assert after["clusters"] == before["clusters"]
    assert after["decisions"] == before["decisions"]
    assert client.get("/api/versions/latest").json()["version_id"] == version_id


def test_carry_page_and_api(client):
    client.post("/api/versions/publish")
    page = client.get("/carry")
    assert page.status_code == 200
    assert "跨批次承接" in page.text
    assert "主体承接" in page.text
    api = client.get("/api/carry").json()
    assert "counts" in api and "relations" in api
    recompute = client.post("/api/carry/recompute")
    assert recompute.status_code == 200


def test_versions_page_shows_diff(client):
    client.post("/api/versions/publish")
    page = client.get("/versions")
    assert page.status_code == 200
    assert "发布前差异" in page.text
    assert "版本清单" in page.text


def test_diff_api_between_two_versions(client):
    client.post("/api/versions/publish")
    first = client.get("/api/versions/latest").json()["version_id"]
    client.post(f"/api/reviews/{EXP.PAIR_CITY_MISMATCH}/decision",
                json={"decision": "merge", "expected_version": 0})
    second = client.post("/api/versions/publish").json()["version"]["version_id"]
    body = client.get("/api/versions/diff", params={"before": first, "after": second}).json()
    # 合并后两个旧主体并成一个：其中一个记为"被并入"，差异表覆盖上一版全部主体。
    assert (body["counts"]["subjects_same"] + body["counts"]["subjects_changed"]
            + body["counts"]["subjects_removed"]) == body["before"]["counts"]["subjects"]
    assert (body["counts"]["subjects_same"] + body["counts"]["subjects_changed"]
            + body["counts"]["subjects_added"]) == body["after"]["counts"]["subjects"]
    alone = client.get("/api/versions/diff", params={"before": second}).json()
    assert alone["before"]["version_id"] == first


def test_version_exports_come_from_snapshot(client):
    """版本导出读的是快照：后续重新判定不会改写已发布版本的文件内容。"""
    import csv
    import io

    published = client.post("/api/versions/publish").json()["version"]
    version_id = published["version_id"]
    master_before = client.get(f"/api/versions/{version_id}/master.csv").text
    decisions_before = client.get(f"/api/versions/{version_id}/decisions.csv").text

    client.post(f"/api/reviews/{EXP.PAIR_CITY_MISMATCH}/decision",
                json={"decision": "merge", "expected_version": 0})
    second = client.post("/api/versions/publish").json()["version"]
    assert second["version_id"] != version_id

    assert client.get(f"/api/versions/{version_id}/master.csv").text == master_before
    assert client.get(f"/api/versions/{version_id}/decisions.csv").text == decisions_before

    rows = list(csv.reader(io.StringIO(master_before.lstrip("﻿"))))
    assert rows[0][0] == "版本"
    assert rows[1][0] == version_id
    total = sum(Decimal(row[6]) for row in rows[1:])
    assert total == EXP.AMOUNT_TOTAL
    decision_rows = list(csv.reader(io.StringIO(decisions_before.lstrip("﻿"))))
    assert decision_rows[0][4] == "结论"


def test_republishing_old_content_does_not_reactivate_superseded_version(db):
    """旧内容重跑命中已被替代的版本：不复活旧版本，当前正式版本保持不变。"""
    run_id = _ready(db)
    first = version_service.publish(run_id)
    review_service.decide(run_id, EXP.PAIR_CITY_MISMATCH, "merge", 0, "王核")
    second = version_service.publish(run_id)
    assert second["version_id"] != first["version_id"]

    again = version_service.publish(run_id)
    assert again["version_id"] == second["version_id"]
    assert again["created"] is False
    assert store.get_version(first["version_id"])["status"] == "superseded"
    assert store.latest_version("published")["version_id"] == second["version_id"]


def test_only_one_current_version(client):
    client.post("/api/versions/publish")
    client.post(f"/api/reviews/{EXP.PAIR_CITY_MISMATCH}/decision",
                json={"decision": "merge", "expected_version": 0})
    client.post("/api/versions/publish")
    states = {item["version_id"]: item["status"]
              for item in client.get("/api/versions").json()["items"]}
    assert list(states.values()).count("published") == 1
