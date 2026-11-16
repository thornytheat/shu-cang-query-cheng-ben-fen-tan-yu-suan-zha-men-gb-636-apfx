"""M1 编排：读文件 → 采信 → 质量 → 计费 → 分摊 → 闸门 → 导出清单，一次跑完。

批次身份来自「来源 + 预算 + 评估日」，列契约在首次接入时绑定到批次上：

- 内容（输入摘要）一致且已完成 → 复用原批次，不新增行、不改合计；
- 内容一致但上次中断/失败 → 恢复原批次，沿用原批次号与校验位，重试不新增账单；
- 身份相同但契约不同（升级前接了 v1、这次是 v2 表头）→ 拒绝（`Q_CONTRACT_MISMATCH`）；
- 身份相同但内容变了 → 冲突（`Q_BATCH_CONTENT_CONFLICT`），绝不把新内容塞进旧批次；
- 表头混用两套列 → 拒绝（`Q_CONTRACT_MIXED`），不猜「按哪套读」。
"""
from __future__ import annotations

import datetime as dt
import time

from app import config, errors, manifest, reasons
from app.modules.m2_billing import contract
from app.modules.m3_quality import service as quality_service
from app.modules.m2_billing import service as billing_service
from app.modules.m4_alloc import invariant, service as alloc_service
from app.modules.m5_gate import batchkey, service as gate_service, state as gate_state

from . import decode, reader, sniff

ALLOWED_SUFFIXES = (".csv", ".txt")


def resolve_sample(settings, name: str):
    samples = settings.samples
    if not name or not isinstance(name, str):
        raise errors.BadRequest("缺少 source", code="E_BAD_REQUEST")
    clean = name.strip()
    if "/" in clean or "\\" in clean or clean.startswith("."):
        raise errors.BadRequest("来源只能是 sample_data 下的文件名，不能带路径",
                                code="E_BAD_REQUEST")
    path = (samples / clean).resolve()
    if samples not in path.parents and path.parent != samples:
        raise errors.BadRequest("来源路径越界", code="E_BAD_REQUEST")
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        raise errors.BadRequest(f"只支持 {'/'.join(ALLOWED_SUFFIXES)}",
                                code="E_BAD_REQUEST")
    if not path.is_file():
        raise errors.NotFound(f"样例不存在：{clean}", code="E_NOT_FOUND")
    return path


def list_samples(settings) -> list[dict]:
    samples = settings.samples
    if not samples.is_dir():
        return []
    rows = []
    for path in sorted(samples.glob("*.csv")):
        rows.append({"name": path.name, "bytes": path.stat().st_size})
    return rows


def _now() -> str:
    return dt.datetime.now(config.UTC).isoformat(timespec="seconds")


def _issue(code: str, *, row_ref=None, field=None, raw_value=None,
           detail: str = "") -> dict:
    return reasons.evidence(code, row_ref=row_ref, field=field, raw_value=raw_value,
                            detail=detail)


def empty_state(counts: dict, blank_lines: int) -> str:
    """空态三态分开：仅表头 / 全空行 / 正常。"""
    if counts["rows_data"] == 0:
        return "ONLY_HEADER" if blank_lines == 0 else "NO_ROWS"
    if counts["rows_valid"] == 0 and counts["rows_empty"] == counts["rows_data"]:
        return "ALL_EMPTY"
    return ""


def _batch_state(batch: dict, mode: str) -> dict:
    """批次来源标记：新接入 / 复用 / 续跑，以及绑定的契约与尝试次数。"""
    version = batch.get("contract_version") or config.CONTRACT_LEGACY
    if version not in config.CONTRACT_LABELS:
        # 解码阶段就失败、还没认出契约的批次：展示上退回旧契约，不抛 KeyError。
        version = config.CONTRACT_LEGACY
    return {
        "reused": mode == "reused",
        "resumed": mode == "resumed",
        "access_mode": mode,
        "access_label": {"new": "新接入", "reused": "复用", "resumed": "续跑"}[mode],
        "contract_version": version,
        "contract_label": config.CONTRACT_LABELS[version],
        "measure_basis": contract.measure_basis(version),
        "attempts": int(batch.get("attempts", 0)),
    }


def _resume_batch(existing: dict, *, budget_name: str,
                  budget_digest: str, attempts: int) -> dict:
    """中断后续跑：沿用原批次号、校验位与身份，只把内容重新跑一遍。

    批次版本回到 1 之前要先把旧版本明细清干净——`save_run` 本来就是先删后插，
    所以这里不额外造行；重试因此不会新增账单。
    """
    return {
        "batch_id": existing["batch_id"], "batch_no": existing["batch_no"],
        "check_digit": existing["check_digit"],
        "batch_key": existing["batch_key"],
        "identity_key": existing.get("identity_key") or existing["batch_key"],
        "run_id": manifest.new_run_id(),
        "source": existing["source"], "budget_source": budget_name,
        "status": "done", "version": 1, "created_at": existing["created_at"],
        "finished_at": None, "input_digest": existing["input_digest"],
        "budget_digest": budget_digest, "total_cents": 0, "row_count": 0,
        "note": "", "contract_version": existing.get("contract_version")
        or config.CONTRACT_LEGACY, "attempts": attempts,
    }


def _save_failed_attempt(store, settings, *, source: str, budget_name: str,
                          budget_digest: str, input_digest: str, started_at: str,
                          contract_version: str, stages: list[dict], note: str,
                          batch: dict | None = None) -> None:
    """失败尝试：只写批次与阶段。

    首次接入失败会落一个 failed 批次并把身份记下，下次同内容重试就能续跑它；
    已有批次（续跑失败）直接复用原批次号，不新增第二个失败批次。
    """
    if batch is None:
        identity = batchkey.identity_key(
            source=source, budget_digest=budget_digest,
            eval_date=settings.eval_date.isoformat())
        existing = store.find_by_identity(identity)
        if existing is None:
            existing = store.find_legacy_failed(
                source=source, input_digest=input_digest,
                budget_digest=budget_digest)
            if existing is not None:
                store.rebind_batch(existing["batch_id"], identity, identity)
        if existing is not None and existing["input_digest"] != input_digest:
            # 同一身份下已有别的批次（通常是已完成的），这次失败不能改写它：
            # 不落第二批次，身份仍归原来那一条。
            return
        if existing is not None:
            batch = {**existing, "status": "failed",
                     "batch_key": existing["batch_key"],
                     "identity_key": identity,
                     "attempts": int(existing.get("attempts", 0)) + 1}
        else:
            content_key = batchkey.batch_key(
                source=source, input_digest=input_digest,
                budget_digest=budget_digest,
                rule_version=settings.rule_version,
                eval_date=settings.eval_date.isoformat(),
                contract_version=contract_version or config.CONTRACT_LEGACY)
            ids = gate_state.next_batch(store, settings.eval_date)
            batch = {
                "batch_id": ids["batch_id"], "batch_no": ids["batch_no"],
                "check_digit": ids["check_digit"], "batch_key": content_key,
                "identity_key": identity,
                "run_id": manifest.new_run_id(), "source": source,
                "budget_source": budget_name, "created_at": started_at,
                "input_digest": input_digest, "budget_digest": budget_digest,
                # 解码阶段失败时还认不出契约，先记 auto；续跑时表头认出的
                # 契约会接管绑定。
                "contract_version": contract_version or "auto",
                "attempts": 0,
            }
    store.save_attempt(batch={**batch, "status": "failed"}, stages=stages,
                       note=note)


def run_import(store, settings, *, source: str, budget_source: str | None = None,
               charset: str | None = None,
               contract_version: str | None = None,
               resume: bool | None = None) -> dict:
    """导入一份账单。

    ``contract_version``：只在「首次接入」时用来声明契约；续跑旧批次时必须与
    批次绑定的契约一致，否则拒绝。
    ``resume``：显式要求续跑（True）或新接入（False/None 自动判断）。
    """
    started_at = _now()
    clock = time.perf_counter()
    path = resolve_sample(settings, source)
    data = path.read_bytes()
    if not data:
        raise errors.Unprocessable("样例是空文件（0 字节）", code="E_EMPTY_FILE")
    budget_name = budget_source or settings.default_budget_source
    budget = gate_service.load_budgets(settings, budget_name)

    input_digest = manifest.digest_bytes(data)
    # 先认表头契约：不看清列就谈批次身份，等于让毫秒和秒共用同一把键。
    try:
        decoded = decode.decode_bytes(data, charset)
    except decode.DecodeFailure as exc:
        _save_failed_attempt(store, settings, source=path.name,
                             budget_name=budget_name, budget_digest=budget["digest"],
                             input_digest=input_digest, started_at=started_at,
                             contract_version=contract_version or "auto",
                             stages=[{"stage": "ingest", "in_count": 0,
                                      "out_count": 0, "quarantine_count": 1,
                                      "status": "failed", "elapsed_ms": 0,
                                      "note": exc.message}], note=exc.message)
        raise errors.Unprocessable(exc.message, code="Q_DECODE_FAILED",
                                   detail={"line_no": exc.line_no})
    parsed = reader.read_records(decoded["text"])
    try:
        probed = sniff.probe(parsed)
    except contract.ContractError as exc:
        raise errors.Conflict(exc.message, code=exc.code, detail=exc.detail)
    except ValueError as exc:
        _save_failed_attempt(store, settings, source=path.name,
                             budget_name=budget_name, budget_digest=budget["digest"],
                             input_digest=input_digest, started_at=started_at,
                             contract_version=contract_version or "auto",
                             stages=[{"stage": "ingest", "in_count": 0,
                                      "out_count": 0, "quarantine_count": 1,
                                      "status": "failed", "elapsed_ms": 0,
                                      "note": str(exc)}], note=str(exc))
        raise errors.Unprocessable(str(exc), code="E_NO_HEADER")
    detected = probed["contract_version"]

    identity = batchkey.identity_key(
        source=path.name, budget_digest=budget["digest"],
        eval_date=settings.eval_date.isoformat())
    if contract_version and contract.normalize_contract(contract_version) != detected:
        raise errors.Conflict(
            f"指定契约 {contract_version} 与表头认出的 {detected} 不一致",
            code="Q_CONTRACT_MISMATCH",
            detail={"requested": contract_version, "detected": detected})

    # 同一身份（来源 + 预算 + 评估日）只允许一个批次：
    # 内容相同 → 复用/续跑；契约不同 → 拒绝混用；内容不同 → 冲突，
    # 绝不把新内容或另一套列塞进旧批次。
    same_identity = store.find_by_identity(identity)
    if same_identity is None:
        # 升级前落下的批次按旧算法算键（含规则版本与输入摘要），新的身份键找不到
        # 它；按「来源 + 输入摘要」找回并沿用原批次身份，避免升级后重复计费。
        legacy = store.find_legacy_failed(
            source=path.name, input_digest=input_digest,
            budget_digest=budget["digest"])
        if legacy is not None:
            store.rebind_batch(legacy["batch_id"], identity, identity)
            same_identity = {**legacy, "identity_key": identity}
    if same_identity is not None:
        bound = same_identity.get("contract_version") or config.CONTRACT_LEGACY
        if bound == "auto":
            # 上次在解码阶段就失败，还没绑定契约；这次由表头认出的契约接管。
            bound = detected
        if bound != detected:
            raise errors.Conflict(
                f"批次 {same_identity['batch_no']} 已绑定 {bound} 契约，"
                f"本次表头是 {detected} 契约；同一来源不能中途换契约",
                code="Q_CONTRACT_MISMATCH",
                detail={"batch_id": same_identity["batch_id"],
                        "batch_no": same_identity["batch_no"],
                        "bound": bound, "detected": detected})
        if same_identity["input_digest"] != input_digest:
            raise errors.Conflict(
                f"批次 {same_identity['batch_no']} 已用同一来源与契约接入，"
                "但本文件内容已变化；续跑必须沿用原内容，新内容请另开来源",
                code="Q_BATCH_CONTENT_CONFLICT",
                detail={"batch_id": same_identity["batch_id"],
                        "batch_no": same_identity["batch_no"],
                        "bound_digest": same_identity["input_digest"],
                        "incoming_digest": input_digest,
                        "contract_version": detected})
        if same_identity["status"] == "done":
            summary = gate_state.summary(
                same_identity, store.load_totals(same_identity["batch_id"]),
                store.count_rows(same_identity["batch_id"]))
            summary.update(_batch_state(same_identity, "reused"))
            return summary
        if resume is False:
            raise errors.Conflict(
                f"批次 {same_identity['batch_no']} 已中断，需要续跑而不是新接入",
                code="Q_BATCH_RESUME_REQUIRED",
                detail={"batch_id": same_identity["batch_id"],
                        "batch_no": same_identity["batch_no"]})
        batch = _resume_batch(same_identity,
                              budget_name=budget_name, budget_digest=budget["digest"],
                              attempts=store.bump_attempt(same_identity["batch_id"]))
        batch["contract_version"] = detected
        resuming = True
    else:
        content_key = batchkey.batch_key(
            source=path.name, input_digest=input_digest,
            budget_digest=budget["digest"],
            rule_version=settings.rule_version,
            eval_date=settings.eval_date.isoformat(),
            contract_version=detected)
        ids = gate_state.next_batch(store, settings.eval_date)
        batch = {
            "batch_id": ids["batch_id"], "batch_no": ids["batch_no"],
            "check_digit": ids["check_digit"], "batch_key": content_key,
            "identity_key": identity,
            "run_id": manifest.new_run_id(), "source": path.name,
            "budget_source": budget_name, "status": "done", "version": 1,
            "created_at": started_at, "finished_at": None,
            "input_digest": input_digest, "budget_digest": budget["digest"],
            "total_cents": 0, "row_count": 0, "note": "",
            "contract_version": detected, "attempts": 0,
        }
        resuming = False

    # 先占位成 running：进程中途被打断时，下次同内容导入能凭这个身份续跑，
    # 而不是另开一批。成功由 save_run 改成 done，失败由 save_attempt 改成 failed。
    store.begin_attempt(batch)

    # 1 采信（解码已在上面做过，这里只留证据与阶段耗时）
    t0 = time.perf_counter()
    issues: list[dict] = []
    if resuming:
        issues.append(_issue("Q_BATCH_RESUMED", field="batch_no",
                             raw_value=batch["batch_no"],
                             detail=f"沿用原批次身份续跑，第 {batch['attempts']} 次尝试，"
                                    f"契约 {detected}"))
    if decoded["fallback"]:
        issues.append(_issue("Q_CHARSET_FALLBACK", field="encoding",
                             raw_value=decoded["declared"],
                             detail=f"按 {decoded['encoding']} 解码"))
    if decoded["bom_stripped"]:
        issues.append(_issue("Q_BOM_STRIPPED", field="encoding",
                             raw_value="UTF-8 BOM"))
    if probed["missing_columns"]:
        detail = "缺少列：" + "、".join(probed["missing_columns"])
        _save_failed_attempt(store, settings, source=path.name,
                             budget_name=budget_name, budget_digest=budget["digest"],
                             input_digest=input_digest, started_at=started_at,
                             contract_version=detected,
                             stages=[{"stage": "ingest",
                                      "in_count": probed["data_count"],
                                      "out_count": 0,
                                      "quarantine_count": probed["data_count"],
                                      "status": "failed", "elapsed_ms": 0,
                                      "note": detail}], note=detail,
                             batch=batch)
        raise errors.Unprocessable(detail, code="Q_MISSING_FIELD",
                                   detail={"missing": probed["missing_columns"],
                                           "contract_version": detected})
    if probed["duplicate_headers"]:
        issues.append(_issue("Q_DUP_HEADER", field="header",
                             raw_value=str(probed["duplicate_headers"]),
                             detail=f"共 {probed['segments']} 段"))
    ingest_ms = int((time.perf_counter() - t0) * 1000)

    # 2 质量 + 3 计费
    t1 = time.perf_counter()
    quality = quality_service.run(probed, contract_version=detected)
    issues.extend(quality["issues"])
    counters = quality["counters"]
    quality_ms = int((time.perf_counter() - t1) * 1000)
    t2 = time.perf_counter()
    priced = billing_service.price_rows(quality["rows"])
    for row in priced:
        if row["status"] == "ZERO_SCAN":
            row["flags"] = list(row["flags"])
    billing_ms = int((time.perf_counter() - t2) * 1000)

    # 4 分摊
    t3 = time.perf_counter()
    alloc = alloc_service.allocate(priced)
    issues.extend(alloc["issues"])
    dept_scan = billing_service.dept_scan_totals(priced)
    alloc_ms = int((time.perf_counter() - t3) * 1000)

    # 5 闸门
    t4 = time.perf_counter()
    issues.extend(budget["issues"])
    gate_result = gate_service.run_gate(alloc["shares"], priced, budget["budgets"])
    issues.extend(gate_result["issues"])
    depts = alloc_service.dept_rows(alloc["dept_totals"], dept_scan,
                                    budget["budgets"])
    depts = alloc_service.apply_gate(depts, gate_result["depts"])
    gate_ms = int((time.perf_counter() - t4) * 1000)

    kept_cents = sum(item["kept_cents"] for item in depts)
    blocked_cents = sum(item["blocked_cents"] for item in depts)
    totals = {
        "rows_data": counters["rows_data"], "rows_valid": counters["rows_valid"],
        "rows_quarantine": counters["rows_quarantine"],
        "rows_dedup": counters["rows_dedup"], "rows_empty": counters["rows_empty"],
        "blank_lines": counters["blank_lines"],
        "amount_total": alloc["total_cents"],
        "amount_allocated": alloc["allocated_cents"],
        "amount_unallocated": alloc["unallocated_cents"],
        "amount_kept": kept_cents, "amount_blocked": blocked_cents,
        "shared_query_count": alloc["shared_query_count"],
        "dept_count": len(depts), "segment_count": probed["segments"],
        "duplicate_headers": probed["duplicate_headers"],
        "residual_diff": 0,
    }
    accounts = invariant.all_accounts(
        counters=counters, total_cents=alloc["total_cents"],
        allocated_cents=alloc["allocated_cents"],
        unallocated_cents=alloc["unallocated_cents"],
        kept_cents=kept_cents, blocked_cents=blocked_cents,
        query_costs=alloc["query_costs"], shares_by_query=alloc["shares_by_query"])
    totals["residual_diff"] = accounts[3]["right"]

    finished_at = _now()
    elapsed_ms = int((time.perf_counter() - clock) * 1000)
    stages = [
        {"stage": "ingest", "in_count": len(probed["records"]),
         "out_count": probed["data_count"], "quarantine_count": 0,
         "status": "ok", "elapsed_ms": ingest_ms,
         "note": f"{decoded['encoding']} / 分隔符 {probed['delimiter']!r} / "
                 f"{probed['segments']} 段"},
        {"stage": "quality", "in_count": counters["rows_data"],
         "out_count": counters["rows_valid"],
         "quarantine_count": counters["rows_quarantine"], "status": "ok",
         "elapsed_ms": quality_ms, "note": f"{len(quality['distribution'])} 种原因码"},
        {"stage": "billing", "in_count": counters["rows_valid"],
         "out_count": len(priced), "quarantine_count": 0, "status": "ok",
         "elapsed_ms": billing_ms, "note": "按小时桶 + 时段单价"},
        {"stage": "alloc", "in_count": len(priced), "out_count": len(depts),
         "quarantine_count": 1 if alloc["unallocated_cents"] else 0,
         "status": "ok", "elapsed_ms": alloc_ms,
         "note": f"共享查询 {alloc['shared_query_count']} 条"},
        {"stage": "gate", "in_count": alloc["allocated_cents"],
         "out_count": kept_cents, "quarantine_count": blocked_cents,
         "status": "ok", "elapsed_ms": gate_ms, "note": "金额单位：分"},
        {"stage": "export", "in_count": len(issues), "out_count": len(issues),
         "quarantine_count": 0, "status": "ok", "elapsed_ms": 0,
         "note": "证据行可导出"},
    ]
    batch["finished_at"] = finished_at
    batch["total_cents"] = alloc["total_cents"]
    batch["row_count"] = counters["rows_valid"]
    batch["note"] = empty_state(counters, counters["blank_lines"])

    run_manifest = manifest.build(
        run_id=batch["run_id"], batch_id_value=batch["batch_id"],
        batch_no_value=batch["batch_no"], check=batch["check_digit"],
        batch_key=batch["batch_key"], source=path.name, budget_source=budget_name,
        input_digest=input_digest, budget_digest=budget["digest"],
        settings=settings, params={"charset": charset or "auto",
                                   "source_bytes": len(data),
                                   "contract_version": detected,
                                   "attempts": int(batch.get("attempts", 0)),
                                   "resumed": resuming},
        started_at=batch.get("created_at", started_at),
        finished_at=finished_at, elapsed_ms=elapsed_ms,
        stages=stages,
        counters={k: totals[k] for k in ("rows_data", "rows_valid",
                                         "rows_quarantine", "rows_dedup",
                                         "amount_total")},
        budget_rows=budget["rows"])

    store.save_run(batch=batch, bills=priced, quarantine=issues,
                   shares=alloc["shares"], depts=depts, gates=gate_result["gates"],
                   stages=stages, totals=totals, manifest=run_manifest)
    store.capture_snapshot(batch["batch_id"], "import")
    summary = gate_state.summary(batch, totals, store.count_rows(batch["batch_id"]))
    summary.update(_batch_state(batch, "resumed" if resuming else "new"))
    summary["empty_state"] = batch["note"]
    summary["accounts"] = accounts
    summary["elapsed_ms"] = elapsed_ms
    summary["measure_basis"] = contract.measure_basis(detected)
    return summary


def pending_resumes(store) -> list[dict]:
    """待续跑的批次：失败/中断、尚未完成的最近批次，页面用来区分「新接入 / 续跑」。"""
    rows = []
    for batch in store.list_batches(50):
        if batch["status"] == "done":
            continue
        version = batch.get("contract_version") or config.CONTRACT_LEGACY
        rows.append({
            "batch_id": batch["batch_id"], "batch_no": batch["batch_no"],
            "source": batch["source"], "status": batch["status"],
            "contract_version": version,
            "contract_label": contract.display_label(version),
            "attempts": int(batch.get("attempts", 0)),
            "note": batch.get("note", ""),
            "created_at": batch.get("created_at", ""),
        })
    return rows


def load_page_context(store, settings, batch: dict | None) -> dict:
    if batch is None:
        return {"totals": {}, "accounts": [], "stages": [], "samples": [],
                "contract": None}
    totals = store.load_totals(batch["batch_id"])
    counters = {
        "rows_data": int(totals.get("rows_data", 0)),
        "rows_valid": int(totals.get("rows_valid", 0)),
        "rows_quarantine": int(totals.get("rows_quarantine", 0)),
        "rows_dedup": int(totals.get("rows_dedup", 0)),
        "rows_empty": int(totals.get("rows_empty", 0)),
        "blank_lines": int(totals.get("blank_lines", 0)),
    }
    accounts = invariant.all_accounts(
        counters=counters,
        total_cents=int(totals.get("amount_total", 0)),
        allocated_cents=int(totals.get("amount_allocated", 0)),
        unallocated_cents=int(totals.get("amount_unallocated", 0)),
        kept_cents=int(totals.get("amount_kept", 0)),
        blocked_cents=int(totals.get("amount_blocked", 0)),
        query_costs={}, shares_by_query={})
    accounts[3] = {"key": "尾差账", "left": 0,
                   "right": int(totals.get("residual_diff", 0)),
                   "ok": int(totals.get("residual_diff", 0)) == 0,
                   "note": "每条共享查询的份额合计 = 该查询成本"}
    return {"totals": totals, "accounts": accounts, "counters": counters}
