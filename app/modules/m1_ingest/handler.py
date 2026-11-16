"""M1 账单接入入口：导入样例、看逐行诊断、看本次批次结论。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app import config, errors, web
from app.modules.m2_billing import contract
from app.modules.m3_quality import service as quality_service

from . import service

router = APIRouter(tags=["ingest"])


@router.post("/api/import")
async def post_import(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - 请求体不是 JSON
        raise errors.BadRequest("请求体必须是 JSON 对象", code="E_BAD_REQUEST")
    if not isinstance(body, dict):
        raise errors.BadRequest("请求体必须是 JSON 对象", code="E_BAD_REQUEST")
    source = body.get("source")
    if not source:
        raise errors.BadRequest("缺少 source", code="E_BAD_REQUEST")
    budget_source = body.get("budget_source")
    charset = body.get("charset")
    contract_version = body.get("contract_version")
    resume_raw = body.get("resume")
    if resume_raw is not None and not isinstance(resume_raw, bool):
        raise errors.BadRequest("resume 必须是布尔值", code="E_BAD_REQUEST")
    summary = service.run_import(
        request.app.state.store, request.app.state.settings,
        source=str(source), budget_source=budget_source,
        charset=str(charset) if charset else None,
        contract_version=str(contract_version) if contract_version else None,
        resume=resume_raw)
    return summary


@router.get("/api/batches/{batch_id}/contract")
def get_batch_contract(request: Request, batch_id: str) -> dict:
    """批次绑定的列契约与计量依据，供接入页核对。"""
    store = request.app.state.store
    batch = store.get_batch(batch_id)
    if batch is None:
        raise errors.NotFound(f"批次不存在：{batch_id}", code="Q_NO_BATCH")
    version = batch.get("contract_version") or "v1"
    return {
        "batch_id": batch["batch_id"], "batch_no": batch["batch_no"],
        "status": batch["status"],
        "contract_version": version,
        "contract_label": contract.display_label(version),
        "measure_basis": contract.measure_basis(version),
        "attempts": int(batch.get("attempts", 0)),
    }


@router.get("/api/ingest/contracts")
def get_contract_catalog() -> dict:
    """两套可接入的契约：页面据此区分「新接入」与「旧批次续跑」。"""
    return {
        "current": config.CONTRACT_CURRENT,
        "contracts": [
            {"version": name, "label": config.CONTRACT_LABELS[name],
             "default": name == config.CONTRACT_CURRENT,
             "measure_basis": contract.measure_basis(name)}
            for name in config.CONTRACTS
        ],
    }


@router.get("/")
def index_page(request: Request, batch_id: str | None = None):
    store = request.app.state.store
    settings = request.app.state.settings
    batch = store.get_batch(batch_id) if batch_id else store.latest_batch()
    context = service.load_page_context(store, settings, batch)
    quality = (quality_service.load(store, batch["batch_id"]) if batch else
               {"issues": [], "distribution": [], "summary": {}, "row_account": {}})
    depts = store.load_depts(batch["batch_id"]) if batch else []
    full_distribution = quality["distribution"]
    bar_max = max([row["count"] for row in full_distribution] or [1])
    contract_version = (batch or {}).get("contract_version") or None
    if contract_version not in config.CONTRACTS:
        # 解码阶段就失败的批次还没绑定契约：展示按当前契约，页面另标「未绑定」。
        contract_version = None
    # 页面上「新接入 / 旧批次续跑」按尝试次数判定：首次接入 attempts=0。
    batch_state = (service._batch_state(
        batch, "resumed" if int(batch.get("attempts", 0)) else "new")
        if batch else None)
    pending = service.pending_resumes(store)
    # 隔离原因（按原因码聚合），页面直接显示「为什么隔离」。
    isolation = quality.get("distribution", [])
    return web.render(
        request, "index.html", page_key="import", batch=batch,
        samples=service.list_samples(settings),
        default_source=settings.default_source,
        contract=contract.contract_payload(contract_version),
        contract_catalog=contract.contract_payload(),
        measure_basis=contract.measure_basis(
            contract_version or config.CONTRACT_CURRENT),
        contracts=config.CONTRACTS,
        contract_labels=config.CONTRACT_LABELS,
        current_contract=config.CONTRACT_CURRENT,
        batch_state=batch_state,
        pending_resumes=pending,
        isolation=isolation,
        totals=context["totals"], accounts=context["accounts"],
        counters=context.get("counters", {}),
        distribution=full_distribution[:6], bar_max=bar_max,
        quality_summary=quality["summary"],
        depts=depts[:8],
        empty_state=(batch or {}).get("note", ""),
        row_account=quality.get("row_account", {}))
