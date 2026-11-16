"""批次键与陈旧提交校验。

批次键 = sha256(来源 + 输入摘要 + 预算摘要 + 列契约 + 规则版本 + 评估日)。
列契约进键：升级后的 v2 账单与旧的 v1 账单即使内容摘要巧合相同，也不会
被当成同一个批次复用（否则会把毫秒当秒、或把有单位列的行塞进无单位列的批次）。

同键重跑直接复用原批次，不新增行、不改合计；键相同但内容摘要不同属于
「同一批次换内容」，由 M1 判成冲突。
"""
from __future__ import annotations

from app import errors, manifest


def batch_key(*, source: str, input_digest: str, budget_digest: str,
              rule_version: str, eval_date: str,
              contract_version: str = "v1") -> str:
    return manifest.digest_parts([source, input_digest, budget_digest,
                                  contract_version, rule_version,
                                  eval_date])[:32]


def identity_key(*, source: str, budget_digest: str, eval_date: str) -> str:
    """批次身份键：同一来源 + 同一预算 + 同一评估日。

    身份里刻意不放输入摘要（用它判「复用 / 续跑 / 换内容」）也不放规则版本
    （升级不另开一批重复计费）。列契约不进键，而是作为批次上的绑定单独校验：
    同一来源换了一套列属于「同一批次换内容」，报冲突而不是悄悄开新批次。
    """
    return manifest.digest_parts([source, budget_digest, eval_date])[:32]


def require_version(current: int, expected: int | None) -> None:
    if expected is None:
        return
    if int(expected) != int(current):
        raise errors.StaleVersion(
            f"提交版本 {expected} 已落后于当前版本 {current}，请刷新后重试",
            detail={"current_version": int(current), "expected_version": int(expected)})


def verify_check(batch_no_value: str, digit: str) -> bool:
    return manifest.verify(batch_no_value, digit)
