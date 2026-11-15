"""M8 路由：跨批次承接台账、待复核承接关系、主体对照。"""
from __future__ import annotations

from fastapi import APIRouter

from ... import store
from ..m1_intake import service as intake_service
from ..m5_clusters import service as cluster_service
from . import service

router = APIRouter(prefix="/api", tags=["m8-carry"])


@router.get("/carry")
def carry_summary():
    run = intake_service.ensure_run()
    run_id = run["run_id"]
    cluster_service.ensure_clusters(run_id)
    service.compute(run_id)
    payload = service.summary(run_id)
    payload["stats"] = store.get_carry_run(run_id)["counts"]
    return payload


@router.post("/carry/recompute")
def carry_recompute():
    run = intake_service.ensure_run()
    run_id = run["run_id"]
    cluster_service.ensure_clusters(run_id)
    return service.compute(run_id) | {"stats": store.get_carry_run(run_id)["counts"]}


@router.get("/carry/pending")
def carry_pending():
    run = intake_service.ensure_run()
    return {"run_id": run["run_id"], "items": service.pending_entries(run["run_id"])}


@router.get("/carry/subjects")
def carry_subjects():
    run = intake_service.ensure_run()
    return {"run_id": run["run_id"], "items": service.subjects_for_display(run["run_id"])}
