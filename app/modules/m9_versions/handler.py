"""M9 路由：发布版本、版本清单、前后差异与发布前预览。"""
from __future__ import annotations

from fastapi import APIRouter, Response

from ... import store
from ...errors import NotFound
from ...reasons import V_UNKNOWN_VERSION
from ..m1_intake import service as intake_service
from . import service

CSV_TYPE = "text/csv; charset=utf-8"

router = APIRouter(prefix="/api", tags=["m9-versions"])


@router.post("/versions/publish")
def publish_version():
    run = intake_service.ensure_run()
    version = service.publish(run["run_id"])
    return {"run_id": run["run_id"], "version": version}


@router.get("/versions")
def list_versions(limit: int = 20):
    items = [service.summary_of(row) for row in store.list_versions("", limit)]
    return {"latest": service.latest_published(), "items": items}


@router.get("/versions/latest")
def latest_version():
    latest = service.latest_published()
    if not latest:
        raise NotFound(V_UNKNOWN_VERSION, "尚无已发布版本")
    return latest


@router.get("/versions/preview")
def preview_version():
    run = intake_service.ensure_run()
    return service.preview(run["run_id"])


@router.get("/versions/{version_id}/master.csv")
def version_master(version_id: str):
    service.get(version_id)
    return Response(
        content="\ufeff" + service.version_master_csv(version_id),
        media_type=CSV_TYPE,
        headers={"Content-Disposition": f'attachment; filename="master-{version_id}.csv"'},
    )


@router.get("/versions/{version_id}/decisions.csv")
def version_decisions(version_id: str):
    service.get(version_id)
    return Response(
        content="\ufeff" + service.version_decisions_csv(version_id),
        media_type=CSV_TYPE,
        headers={"Content-Disposition":
                 f'attachment; filename="decisions-{version_id}.csv"'},
    )


@router.get("/versions/diff")
def diff_versions(before: str, after: str = ""):
    """before/after 都给版本号时对比任意两版；after 为空时对比选定版本与它之前一版。"""
    if not after:
        selected = service.get(before)
        parent = selected.get("parent_version")
        if not parent:
            raise NotFound(V_UNKNOWN_VERSION, "该版本没有父版本")
        return service.compare(parent, before)
    return service.compare(before, after)


@router.get("/versions/{version_id}")
def version_detail(version_id: str):
    version = service.get(version_id)
    return {
        **version,
        "clusters": store.version_clusters(version_id),
        "decisions": service.decisions_of(version_id),
    }
