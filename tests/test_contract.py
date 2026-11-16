"""列契约升级与批次续跑：v2 毫秒/字节、按批次绑契约、续跑与冲突。

覆盖点：
- v2 账单只认 duration_ms / scan_bytes，毫秒折回秒、字节折回 GB，不能把毫秒当秒；
- 两套列同时出现（或 v2 带 scan_unit）按混用拒绝，不猜「按哪套读」；
- 契约绑定在批次上：同一来源中途换契约、换内容都报冲突；
- 中断后恢复原批次、重试不新增账单、批次号与校验位不变；
- 升级前落下的旧批次（旧 batch_key 算法）重试也沿用原批次身份；
- 接入页能区分新接入 / 续跑，显示计量依据、隔离原因与行数/金额对账。
"""
from __future__ import annotations

import datetime as dt
import pathlib

import pytest

from app import config, errors, manifest
from app.modules.m1_ingest import service as ingest_service
from app.modules.m2_billing import contract, service as billing_service
from app.modules.m5_gate import batchkey

EVAL = dt.date(2026, 11, 15)
V2_HEADER = ("query_id,dept,project,started_at,duration_ms,scan_bytes,"
             "concurrency,value_score,shared_with")


def _v2_env(sample_env):
    """在自定义样例目录里造一份 v2 账单，返回 (路径, 环境)。"""
    sample_env["write"]("v2.csv", [
        "v2001,数据平台,日报,2026-11-03T09:15:00+08:00,3600000,107374182400,4,72,",
        "v2002,风控,夜间,2026-11-03T23:00:00+08:00,3600000,1073741824,2,88,",
    ], header=V2_HEADER)
    return sample_env


def test_v2_duration_ms_is_converted_to_seconds(sample_env):
    """3600000 毫秒必须是 3600 秒，而不是 3600000 秒。"""
    env = _v2_env(sample_env)
    summary = env["run"]("v2.csv")
    bills = {bill["query_id"]: bill for bill in
             env["store"].load_bills(summary["batch_id"])}
    assert bills["v2001"]["duration_s"] == 3600
    assert bills["v2001"]["scan_gb"] == 100
    assert bills["v2001"]["scan_unit"] == "B"
    # 100 GB × 0.028 + 1h × 0.42 = 322 分；若把毫秒当秒会多出 3600 倍时长费。
    assert bills["v2001"]["cost_cents"] == 322
    assert summary["totals"]["amount_total"] == 322 + 24


def test_v2_scan_bytes_are_converted_to_gb(sample_env):
    env = _v2_env(sample_env)
    summary = env["run"]("v2.csv")
    bills = {bill["query_id"]: bill for bill in
             env["store"].load_bills(summary["batch_id"])}
    assert bills["v2002"]["scan_gb"] == 1
    assert bills["v2002"]["duration_s"] == 3600


def test_v2_preview_uses_bytes_and_milliseconds():
    priced = billing_service.preview({
        "query_id": "x", "dept": "风控", "project": "p",
        "started_at": "2026-11-05T10:00:00+08:00",
        "duration_ms": "3600000", "scan_bytes": "107374182400",
        "concurrency": "4", "value_score": "80", "shared_with": ""})
    assert priced["contract_version"] == "v2"
    assert str(priced["duration_s"]) == "3600.000000"
    assert str(priced["scan_gb"]) == "100.000000"
    assert priced["cost_cents"] == 322


def test_v1_preview_still_reads_seconds_and_unit_column():
    priced = billing_service.preview({
        "query_id": "x", "dept": "风控", "project": "p",
        "started_at": "2026-11-05T10:00:00+08:00",
        "duration_s": "3600", "scan_size": "100", "scan_unit": "GB",
        "concurrency": "4", "value_score": "80", "shared_with": ""})
    assert priced["contract_version"] == "v1"
    assert priced["cost_cents"] == 322


def test_preview_mixing_both_measure_columns_is_rejected():
    with pytest.raises(errors.BadRequest) as excinfo:
        billing_service.preview({
            "query_id": "x", "dept": "风控", "project": "p",
            "started_at": "2026-11-05T10:00:00+08:00",
            "duration_ms": "3600000", "duration_s": "3600", "scan_bytes": "1"})
    assert excinfo.value.code == "Q_CONTRACT_MIXED"


def test_header_mixing_both_contracts_is_rejected_before_any_batch(sample_env):
    sample_env["write"]("mixed.csv", [
        "m1,数据平台,日报,2026-11-03T09:15:00+08:00,3600000,3600,107374182400,4,72,",
    ], header=("query_id,dept,project,started_at,duration_ms,duration_s,"
               "scan_bytes,concurrency,value_score,shared_with"))
    with pytest.raises(errors.Conflict) as excinfo:
        sample_env["run"]("mixed.csv")
    assert excinfo.value.code == "Q_CONTRACT_MIXED"
    # 混用被拒时不应留下任何批次，更不能有账单行。
    assert sample_env["store"].list_batches(10) == []


def test_v2_with_legacy_unit_column_is_rejected(sample_env):
    sample_env["write"]("v2_with_unit.csv", [
        "m1,数据平台,日报,2026-11-03T09:15:00+08:00,3600000,107374182400,GB,4,72,",
    ], header=("query_id,dept,project,started_at,duration_ms,scan_bytes,"
               "scan_unit,concurrency,value_score,shared_with"))
    with pytest.raises(errors.Conflict) as excinfo:
        sample_env["run"]("v2_with_unit.csv")
    assert excinfo.value.code == "Q_CONTRACT_MIXED"


def test_contract_is_detected_per_header():
    assert contract.detect_contract(
        ["query_id", "dept", "duration_ms", "scan_bytes"]) == "v2"
    assert contract.detect_contract(
        ["query_id", "dept", "duration_s", "scan_size", "scan_unit"]) == "v1"
    with pytest.raises(contract.ContractError) as excinfo:
        contract.detect_contract(["query_id", "dept", "foo"])
    assert excinfo.value.code == "Q_CONTRACT_MIXED"


def test_batch_binds_contract_and_refuses_switch(sample_env):
    """同一来源先按 v1 接入，再换成 v2 表头 → 冲突，不能混进同一批次。"""
    env = sample_env
    env["write"]("switch.csv", [
        "s1,数据平台,日报,2026-11-03T09:15:00+08:00,3600,10,GB,2,30,",
    ])
    first = env["run"]("switch.csv")
    assert first["contract_version"] == "v1"
    env["write"]("switch.csv", [
        "s1,数据平台,日报,2026-11-03T09:15:00+08:00,3600000,10737418240,2,30,",
    ], header=V2_HEADER)
    with pytest.raises(errors.Conflict) as excinfo:
        env["run"]("switch.csv")
    assert excinfo.value.code == "Q_CONTRACT_MISMATCH"
    assert excinfo.value.detail["bound"] == "v1"
    assert excinfo.value.detail["detected"] == "v2"
    assert len(env["store"].list_batches(10)) == 1


def test_declared_contract_must_match_header(sample_env):
    env = _v2_env(sample_env)
    with pytest.raises(errors.Conflict) as excinfo:
        env["run"]("v2.csv", contract_version="v1")
    assert excinfo.value.code == "Q_CONTRACT_MISMATCH"


def test_same_batch_with_new_content_is_a_conflict(sample_env):
    env = sample_env
    env["write"]("same.csv", [
        "s1,数据平台,日报,2026-11-03T09:15:00+08:00,3600,10,GB,2,30,"])
    first = env["run"]("same.csv")
    env["write"]("same.csv", [
        "s1,数据平台,日报,2026-11-03T09:15:00+08:00,3600,20,GB,2,30,"])
    with pytest.raises(errors.Conflict) as excinfo:
        env["run"]("same.csv")
    assert excinfo.value.code == "Q_BATCH_CONTENT_CONFLICT"
    assert excinfo.value.detail["batch_no"] == first["batch_no"]
    assert len(env["store"].list_batches(10)) == 1


def test_interrupted_batch_resumes_with_same_identity(sample_env, monkeypatch):
    """跑到一半进程没了 → 再导入同一份内容，恢复原批次，不新增账单。"""
    env = _v2_env(sample_env)

    def boom(rows):
        raise RuntimeError("进程被打断")

    monkeypatch.setattr(ingest_service.billing_service, "price_rows", boom)
    with pytest.raises(RuntimeError):
        env["run"]("v2.csv")
    interrupted = env["store"].list_batches(10)
    assert len(interrupted) == 1
    assert interrupted[0]["status"] == "running"
    first_no = interrupted[0]["batch_no"]
    first_id = interrupted[0]["batch_id"]

    monkeypatch.undo()
    resumed = env["run"]("v2.csv")
    assert resumed["resumed"] is True
    assert resumed["access_mode"] == "resumed"
    assert resumed["batch_no"] == first_no
    assert resumed["batch_id"] == first_id
    assert resumed["attempts"] == 1
    # 重试不新增批次，账单行数恰等于有效行数。
    assert len(env["store"].list_batches(10)) == 1
    rows = env["store"].load_bills(first_id)
    assert len(rows) == resumed["totals"]["rows_valid"] == 2
    codes = [row["code"] for row in env["store"].load_quarantine(first_id)]
    assert "Q_BATCH_RESUMED" in codes


def test_failed_batch_retry_reuses_batch_number(sample_env):
    env = sample_env
    (env["dir"] / "bad.csv").write_bytes(b"\r\n\r\n")
    with pytest.raises(errors.Unprocessable):
        env["run"]("bad.csv")
    failed = env["store"].list_batches(10)
    assert [row["status"] for row in failed] == ["failed"]
    with pytest.raises(errors.Unprocessable):
        env["run"]("bad.csv")
    still = env["store"].list_batches(10)
    assert len(still) == 1
    assert still[0]["batch_no"] == failed[0]["batch_no"]
    assert still[0]["attempts"] == 1


def test_explicit_new_access_to_interrupted_batch_is_rejected(sample_env, monkeypatch):
    env = _v2_env(sample_env)

    def boom(rows):
        raise RuntimeError("中断")

    monkeypatch.setattr(ingest_service.billing_service, "price_rows", boom)
    with pytest.raises(RuntimeError):
        env["run"]("v2.csv")
    monkeypatch.undo()
    with pytest.raises(errors.Conflict) as excinfo:
        env["run"]("v2.csv", resume=False)
    assert excinfo.value.code == "Q_BATCH_RESUME_REQUIRED"


def _fake_legacy_batch(store, *, source: str, data: bytes,
                       budget_digest: str) -> dict:
    """造一条升级前落下的失败批次：批次键用旧算法（含规则版本与输入摘要）。"""
    ids = {"batch_id": "CS-20261115-0009-0", "batch_no": "CS-20261115-0009",
           "check_digit": "0"}
    digest = manifest.digest_bytes(data)
    old_key = manifest.digest_parts(
        [source, digest, budget_digest, "costsplit/1.3.0", EVAL.isoformat()])[:32]
    store.begin_attempt({
        "batch_id": ids["batch_id"], "batch_no": ids["batch_no"],
        "check_digit": ids["check_digit"], "batch_key": old_key,
        "run_id": "CSR-OLD", "source": source, "budget_source": "budgets.csv",
        "status": "running", "version": 1,
        "created_at": "2026-11-15T00:00:00+00:00",
        "input_digest": digest, "budget_digest": budget_digest,
        "contract_version": "v1", "attempts": 1,
    })
    return ids


def test_legacy_batch_retry_keeps_original_identity(sample_env):
    """升级前失败的老批次：重试沿用原批次号与 v1 契约，不另开一批重复计费。"""
    from conftest import load_budgets

    env = sample_env
    env["write"]("legacy.csv", [
        "l1,数据平台,日报,2026-11-03T09:15:00+08:00,3600,10,GB,2,30,"])
    path = env["dir"] / "legacy.csv"
    budget_digest = load_budgets(env["settings"])["digest"]
    ids = _fake_legacy_batch(env["store"], source="legacy.csv",
                             data=path.read_bytes(),
                             budget_digest=budget_digest)
    summary = env["run"]("legacy.csv")
    assert summary["batch_id"] == ids["batch_id"]
    assert summary["batch_no"] == ids["batch_no"]
    assert summary["contract_version"] == "v1"
    assert summary["resumed"] is True
    assert len(env["store"].list_batches(10)) == 1


def test_legacy_batch_key_differs_from_new_identity():
    old = manifest.digest_parts(["s.csv", "d", "b", "costsplit/1.3.0",
                                 EVAL.isoformat()])[:32]
    new = batchkey.identity_key(source="s.csv", budget_digest="b",
                                eval_date=EVAL.isoformat())
    assert old != new


def test_index_page_distinguishes_new_and_resume(client):
    first = client.post("/api/import", json={"source": "bills_2026_11_dirty.csv"})
    assert first.status_code == 200
    page = client.get("/").text
    assert "新接入" in page
    assert "计量依据" in page
    assert "秒" in page and "duration_s" in page
    assert "行账" in page and "金额账" in page
    # 页面能区分两套契约的列。
    assert "duration_ms" in page and "scan_bytes" in page
    assert "v2" in page and "v1" in page


def test_index_page_shows_pending_resume(sample_env):
    from fastapi.testclient import TestClient
    from app.main import create_app

    (sample_env["dir"] / "bad.csv").write_bytes(b"\r\n\r\n")
    with pytest.raises(errors.Unprocessable):
        sample_env["run"]("bad.csv")
    app = create_app(sample_env["settings"])
    page = TestClient(app).get("/").text
    assert "待续跑的旧批次" in page
    assert "CS-20261115-" in page


def test_import_api_reports_contract_and_access_mode(client):
    body = client.post("/api/import", json={"source": "bills_2026_11_dirty.csv"}).json()
    assert body["contract_version"] == "v1"
    assert body["access_label"] == "新接入"
    assert body["measure_basis"]["duration_unit"] == "秒"
    again = client.post("/api/import",
                        json={"source": "bills_2026_11_dirty.csv"}).json()
    assert again["access_mode"] == "reused"
    assert again["batch_id"] == body["batch_id"]


def test_contract_catalog_endpoint_lists_both_contracts(client):
    payload = client.get("/api/ingest/contracts").json()
    assert payload["current"] == "v2"
    versions = {item["version"] for item in payload["contracts"]}
    assert versions == {"v1", "v2"}
    basis = {item["version"]: item["measure_basis"]
             for item in payload["contracts"]}
    assert basis["v1"]["duration_unit"] == "秒"
    assert basis["v2"]["duration_unit"] == "毫秒"
    assert basis["v2"]["scan_unit"] == "字节"


def test_v2_batch_declares_measure_basis_on_receipt_and_manifest(sample_env, settings):
    env = _v2_env(sample_env)
    summary = env["run"]("v2.csv")
    batch = env["store"].get_batch(summary["batch_id"])
    manifest_payload = env["store"].load_manifest(summary["batch_id"])
    assert manifest_payload["contract_version"] == "v2"
    assert manifest_payload["measure_basis"]["duration_unit"] == "毫秒"
    assert batch["contract_version"] == "v2"
