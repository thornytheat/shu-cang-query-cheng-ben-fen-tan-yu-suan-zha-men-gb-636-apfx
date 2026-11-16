"""结构探测：分隔符、表头、列契约、列映射、列数、分段与末行完整性。

只做「结构」判断，不解释字段值；字段级归一在 m2 计费模块完成。
列契约在这一步认出并返回，由 M1 编排把它绑到批次上，后续质量与计费都按同一契约读。
"""
from __future__ import annotations

from app.modules.m2_billing import contract

CANDIDATE_DELIMITERS = (",", ";", "\t", "|")


def detect_delimiter(text: str) -> str:
    for line in text.splitlines()[:5]:
        if not line.strip():
            continue
        counts = {sep: line.count(sep) for sep in CANDIDATE_DELIMITERS}
        best = max(counts, key=lambda sep: counts[sep])
        return best if counts[best] > 0 else ","
    return ","


def header_key(fields: list[str]) -> tuple[str, ...]:
    return tuple(str(cell).strip().replace("﻿", "") for cell in fields)


def probe(bundle: dict, *, contract_version: str | None = None,
          expected_contract: str | None = None) -> dict:
    """把 reader 的结构结果补上列契约、列映射与列数契约。

    ``expected_contract`` 是批次已绑定的契约（续跑时传入）：表头认出的契约与它
    不一致就拒绝，绝不能把两套列混进同一批次。
    """
    records = bundle["records"]
    header = bundle["header"]
    if not header:
        raise ValueError("文件没有表头")
    detected = contract.detect_contract(header)
    if expected_contract and detected != expected_contract:
        raise contract.ContractError(
            "Q_CONTRACT_MISMATCH",
            f"本批次绑定 {expected_contract} 契约，但表头是 {detected} 契约",
            detail={"bound": expected_contract, "detected": detected})
    version = expected_contract or detected
    if contract_version and contract_version != version:
        raise contract.ContractError(
            "Q_CONTRACT_MISMATCH",
            f"指定的契约 {contract_version} 与表头认出的 {detected} 不一致",
            detail={"requested": contract_version, "detected": detected})
    mapping, unknown = contract.map_header(header, version)
    missing = [name for name in contract.required(version) if name not in mapping]
    data_records = [r for r in records if r["kind"] == "data"]
    return {
        **bundle,
        "contract_version": version,
        "mapping": mapping,
        "unknown_columns": unknown,
        "missing_columns": missing,
        "expected_columns": len(header),
        "data_records": data_records,
        "data_count": len(data_records),
    }
