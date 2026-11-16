"""M2 计费规则入口：列契约、单价与规则表、单条试算。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app import errors

from . import contract, coerce, service

router = APIRouter(tags=["billing"])


@router.get("/api/contract")
def get_contract(contract_version: str | None = None) -> dict:
    try:
        version = (contract.normalize_contract(contract_version)
                   if contract_version else None)
    except contract.ContractError as exc:
        raise errors.BadRequest(exc.message, code=exc.code,
                                detail=exc.detail)
    return contract.contract_payload(version)


@router.post("/api/billing/preview")
async def post_preview(request: Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise errors.BadRequest("请求体必须是 JSON 对象", code="E_BAD_REQUEST")
    fields = body.get("row") if isinstance(body.get("row"), dict) else body
    if not isinstance(fields, dict) or not fields:
        raise errors.BadRequest("缺少字段 row", code="E_BAD_REQUEST")
    version = body.get("contract_version")
    try:
        priced = service.preview(fields, contract_version=version)
    except coerce.CoerceError as exc:
        raise errors.BadRequest(exc.message, code=exc.code,
                                detail={"field": exc.field, "raw": exc.raw})
    return {
        "query_id": priced["query_id"],
        "dept": priced["dept"],
        "contract_version": priced["contract_version"],
        "scan_gb": str(priced["scan_gb"]),
        "scan_unit": priced["scan_unit"],
        "duration_s": str(priced["duration_s"]),
        "cost_cents": priced["cost_cents"],
        "scan_cost_cents": priced["scan_cost_cents"],
        "hour_cost_cents": priced["hour_cost_cents"],
        "addon_cents": priced["addon_cents"],
        "rules": priced["rules"],
        "buckets": priced["buckets"],
    }

