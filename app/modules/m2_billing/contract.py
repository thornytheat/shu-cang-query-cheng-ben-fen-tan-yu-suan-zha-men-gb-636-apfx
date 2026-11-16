"""列契约与规则契约：两套账单列、按批次绑定、规则优先级与单价表。

列契约按接入批次绑定，一经绑定不随系统升级漂移：

- ``v2``（当前契约）：时长 ``duration_ms`` 毫秒、扫描量 ``scan_bytes`` 字节，
  不再提供扫描单位列；入库时折回「秒 / GB」再计费。
- ``v1``（旧契约）：时长 ``duration_s`` 秒、扫描量由 ``scan_size + scan_unit``
  折算；只在旧批次续跑时使用。

``detect_contract()`` 只认出唯一一套契约；两套列同时出现（例如既有
``duration_ms`` 又有 ``duration_s``，或 v2 字段配 ``scan_unit``）一律按
「混用两套列」拒绝，不做「先到先得」的猜测。

``ordered_rules()`` 负责把规则表排成与注册顺序无关的确定顺序，这是
「同一查询命中多条规则」时结果可复现的前提。
"""
from __future__ import annotations

from app import config

# ---------- v1：秒 + 扫描单位列 ----------
COLUMNS_V1 = (
    {"name": "query_id", "label": "查询编号", "type": "text", "unit": "",
     "required": True, "nullable": False, "note": "字符串保形，前导零不改写",
     "aliases": ("query_id", "queryid", "查询编号", "查询id")},
    {"name": "dept", "label": "部门", "type": "text", "unit": "",
     "required": True, "nullable": False, "note": "计费归集主体",
     "aliases": ("dept", "department", "部门", "使用部门")},
    {"name": "project", "label": "项目", "type": "text", "unit": "",
     "required": True, "nullable": True, "note": "业务项目名",
     "aliases": ("project", "项目", "作业")},
    {"name": "started_at", "label": "开始时间", "type": "instant", "unit": "时区偏移",
     "required": True, "nullable": False, "note": "入库统一 UTC",
     "aliases": ("started_at", "start_time", "开始时间", "起始时间")},
    {"name": "duration_s", "label": "时长", "type": "decimal", "unit": "秒",
     "required": True, "nullable": False, "note": "换算小时后按时段单价计费",
     "aliases": ("duration_s", "duration", "时长", "运行时长")},
    {"name": "scan_size", "label": "扫描量", "type": "decimal", "unit": "见扫描单位",
     "required": True, "nullable": False, "note": "支持千分位、全角、科学计数法",
     "aliases": ("scan_size", "scan", "扫描量", "扫描")},
    {"name": "scan_unit", "label": "扫描单位", "type": "unit", "unit": "B/KB/MB/GB/TB/PB",
     "required": True, "nullable": True, "note": "归一 GB 后再计费",
     "aliases": ("scan_unit", "unit", "扫描单位", "单位")},
    {"name": "concurrency", "label": "并发", "type": "int", "unit": "个",
     "required": False, "nullable": True, "note": "超过 8 的部分按小时加价",
     "aliases": ("concurrency", "并发", "并发数")},
    {"name": "value_score", "label": "收益分", "type": "decimal", "unit": "分",
     "required": False, "nullable": True, "note": "闸门排序用，0-100",
     "aliases": ("value_score", "value", "收益分", "业务价值")},
    {"name": "shared_with", "label": "共享部门", "type": "shared", "unit": "部门:小时:GB",
     "required": False, "nullable": True, "note": "空表示独占",
     "aliases": ("shared_with", "shared", "共享部门", "共享")},
)

# ---------- v2：毫秒 + 字节，没有单位列 ----------
COLUMNS_V2 = (
    {"name": "query_id", "label": "查询编号", "type": "text", "unit": "",
     "required": True, "nullable": False, "note": "字符串保形，前导零不改写",
     "aliases": ("query_id", "queryid", "查询编号", "查询id")},
    {"name": "dept", "label": "部门", "type": "text", "unit": "",
     "required": True, "nullable": False, "note": "计费归集主体",
     "aliases": ("dept", "department", "部门", "使用部门")},
    {"name": "project", "label": "项目", "type": "text", "unit": "",
     "required": True, "nullable": True, "note": "业务项目名",
     "aliases": ("project", "项目", "作业")},
    {"name": "started_at", "label": "开始时间", "type": "instant", "unit": "时区偏移",
     "required": True, "nullable": False, "note": "入库统一 UTC",
     "aliases": ("started_at", "start_time", "开始时间", "起始时间")},
    {"name": "duration_ms", "label": "时长（毫秒）", "type": "decimal", "unit": "毫秒",
     "required": True, "nullable": False, "note": "入库折回秒再按时段单价计费，不可当秒",
     "aliases": ("duration_ms", "duration_millis", "durationms", "时长毫秒",
                 "毫秒时长")},
    {"name": "scan_bytes", "label": "扫描量（字节）", "type": "decimal", "unit": "字节",
     "required": True, "nullable": False, "note": "1024 进制折算 GB，无单位列",
     "aliases": ("scan_bytes", "scanbytes", "scan_byte", "扫描字节", "字节数")},
    {"name": "concurrency", "label": "并发", "type": "int", "unit": "个",
     "required": False, "nullable": True, "note": "超过 8 的部分按小时加价",
     "aliases": ("concurrency", "并发", "并发数")},
    {"name": "value_score", "label": "收益分", "type": "decimal", "unit": "分",
     "required": False, "nullable": True, "note": "闸门排序用，0-100",
     "aliases": ("value_score", "value", "收益分", "业务价值")},
    {"name": "shared_with", "label": "共享部门", "type": "shared", "unit": "部门:小时:GB",
     "required": False, "nullable": True, "note": "空表示独占",
     "aliases": ("shared_with", "shared", "共享部门", "共享")},
)

COLUMNS_BY_CONTRACT = {
    config.CONTRACT_LEGACY: COLUMNS_V1,
    config.CONTRACT_CURRENT: COLUMNS_V2,
}

# 计量相关列：只拿这几列判断契约，query_id/dept 等两套契约共用的列不参与。
MEASURE_FIELDS = {
    config.CONTRACT_LEGACY: ("duration_s", "scan_size", "scan_unit"),
    config.CONTRACT_CURRENT: ("duration_ms", "scan_bytes"),
}

# 兼容旧调用：默认是 v1（旧样例与旧批次续跑都按 v1 解析）。
COLUMNS = COLUMNS_V1
COLUMN_NAMES = tuple(col["name"] for col in COLUMNS_V1)
REQUIRED = tuple(col["name"] for col in COLUMNS_V1 if col["required"])


class ContractError(ValueError):
    """列契约不成立：混用两套列或认不出计量列。"""

    def __init__(self, code: str, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def normalize_contract(version: str | None) -> str:
    text = str(version or "").strip().lower()
    if text in config.CONTRACTS:
        return text
    raise ContractError("Q_CONTRACT_MIXED", f"未知的列契约版本：{version}",
                        detail={"contracts": list(config.CONTRACTS)})


def columns_for(version: str | None = None) -> tuple[dict, ...]:
    return COLUMNS_BY_CONTRACT[normalize_contract(version)
                               if version else config.CONTRACT_LEGACY]


def column_names(version: str | None = None) -> tuple[str, ...]:
    return tuple(col["name"] for col in columns_for(version))


def required(version: str | None = None) -> tuple[str, ...]:
    return tuple(col["name"] for col in columns_for(version) if col["required"])


def column_by_name(name: str, version: str | None = None) -> dict | None:
    for col in columns_for(version):
        if col["name"] == name:
            return col
    return None


def _key(text: str) -> str:
    return "".join(str(text or "").replace("﻿", "").split()).lower()


def alias_map(version: str | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    for col in columns_for(version):
        for alias in col["aliases"]:
            out[_key(alias)] = col["name"]
    return out


def measure_alias_keys(version: str) -> dict[str, str]:
    """只取计量列的别名 → 列名，用于识别契约、不误伤共用列。"""
    wanted = set(MEASURE_FIELDS[version])
    out: dict[str, str] = {}
    for col in COLUMNS_BY_CONTRACT[version]:
        if col["name"] in wanted:
            for alias in col["aliases"]:
                out[_key(alias)] = col["name"]
    return out


def detect_contract(header: list[str]) -> str:
    """按表头认出唯一一套契约；两套列同时出现或计量列缺失都抛 ContractError。"""
    keys = [_key(cell) for cell in header]
    seen: dict[str, list[str]] = {}
    for version in config.CONTRACTS:
        aliases = measure_alias_keys(version)
        hits = sorted({aliases[key] for key in keys if key in aliases})
        seen[version] = hits
    v1_hits = seen[config.CONTRACT_LEGACY]
    v2_hits = seen[config.CONTRACT_CURRENT]
    if v1_hits and v2_hits:
        raise ContractError(
            "Q_CONTRACT_MIXED",
            "表头同时出现两套列契约的列：" + "、".join(v1_hits + v2_hits),
            detail={"v1": v1_hits, "v2": v2_hits})
    if v2_hits:
        # v2 不再提供单位列；带着 scan_unit 就是旧列混进新契约。
        if "scan_unit" in [_key(cell) for cell in header]:
            raise ContractError(
                "Q_CONTRACT_MIXED", "v2 契约不再提供扫描单位列，表头里却有 scan_unit",
                detail={"v1": ["scan_unit"], "v2": v2_hits})
        return config.CONTRACT_CURRENT
    if v1_hits:
        return config.CONTRACT_LEGACY
    raise ContractError(
        "Q_CONTRACT_MIXED", "表头里既没有 v2 的 duration_ms/scan_bytes，"
        "也没有 v1 的 duration_s/scan_size",
        detail={"v1": v1_hits, "v2": v2_hits})


def map_header(header: list[str],
               version: str | None = None) -> tuple[dict[str, int], list[str]]:
    """把表头映射成 列名 → 下标；返回 (映射, 未识别列名)。"""
    aliases = alias_map(version)
    mapping: dict[str, int] = {}
    unknown: list[str] = []
    for index, raw in enumerate(header):
        name = aliases.get(_key(raw))
        if name and name not in mapping:
            mapping[name] = index
        elif name is None:
            unknown.append(str(raw))
    return mapping, unknown


def detect_from_fields(fields: dict) -> str:
    """试算入参只有字段名、没有表头：按出现的计量字段认出契约。

    两套计量字段同时出现（例如既有 ``duration_ms`` 又有 ``duration_s``）一律
    按混用拒绝；只给共用字段时按当前 v2 契约读。
    """
    keys = {_key(name) for name in fields if fields.get(name) not in (None, "")}
    present = {}
    for version in config.CONTRACTS:
        aliases = measure_alias_keys(version)
        present[version] = sorted({aliases[key] for key in keys if key in aliases})
    if present[config.CONTRACT_LEGACY] and present[config.CONTRACT_CURRENT]:
        raise ContractError(
            "Q_CONTRACT_MIXED",
            "试算字段同时用了两套计量列：" + "、".join(
                present[config.CONTRACT_LEGACY] + present[config.CONTRACT_CURRENT]),
            detail={"v1": present[config.CONTRACT_LEGACY],
                    "v2": present[config.CONTRACT_CURRENT]})
    if present[config.CONTRACT_LEGACY]:
        return config.CONTRACT_LEGACY
    return config.CONTRACT_CURRENT


def rule_table() -> list[dict]:
    return [dict(rule) for rule in config.RULES]


def ordered_rules(rules=None) -> list[dict]:
    rows = [dict(r) for r in (rules if rules is not None else config.RULES)]
    rows.sort(key=lambda r: (int(r["priority"]), str(r["id"])))
    return rows


def price_table() -> list[dict]:
    return [
        {"id": "P_SCAN", "label": "扫描单价", "value": str(config.PRICE_PER_GB),
         "unit": "元/GB", "when": "按折算后 GB 线性"},
        {"id": "P_DAY", "label": "白天时长单价", "value": str(config.DAY_RATE_PER_HOUR),
         "unit": "元/小时", "when": "本地 06:00-22:00 的小时桶"},
        {"id": "P_NIGHT", "label": "夜间时长单价", "value": str(config.NIGHT_RATE_PER_HOUR),
         "unit": "元/小时", "when": "本地 22:00-06:00 的小时桶"},
        {"id": "P_CONC", "label": "并发加价", "value": str(config.CONC_SURCHARGE_PER_HOUR),
         "unit": "元/并发·小时", "when": f"并发数 > {config.FREE_CONCURRENCY}"},
        {"id": "P_MIN", "label": "最低计费", "value": str(config.MIN_CHARGE),
         "unit": "元/查询", "when": "成本低于最低值时"},
        {"id": "P_VOL", "label": "大客户折扣系数", "value": str(config.VOLUME_FACTOR),
         "unit": "倍", "when": f"部门本批扫描量 ≥ {config.VOLUME_THRESHOLD_GB} GB"},
    ]


def measure_basis(version: str | None = None) -> dict:
    contract = version if version in config.MEASURE_BASIS else config.CONTRACT_CURRENT
    basis = dict(config.MEASURE_BASIS[contract])
    basis.update({"contract_version": contract,
                  "contract_label": config.CONTRACT_LABELS[contract]})
    return basis


def display_label(version: str | None) -> str:
    """展示用标签：认不出的版本（如解码阶段失败的 auto）显示成「未绑定」。"""
    if version in config.CONTRACT_LABELS:
        return config.CONTRACT_LABELS[version]
    return "未绑定（解码阶段失败）"


def contract_payload(version: str | None = None) -> dict:
    current = version or config.CONTRACT_CURRENT
    payload = {
        "rule_version": config.RULE_VERSION,
        "money_unit": config.MONEY_UNIT,
        "contract_version": current,
        "current_contract": config.CONTRACT_CURRENT,
        "contracts": [
            {"version": name, "label": config.CONTRACT_LABELS[name],
             "measure_basis": measure_basis(name),
             "columns": [{k: (list(v) if isinstance(v, tuple) else v)
                          for k, v in col.items()}
                         for col in COLUMNS_BY_CONTRACT[name]],
             "required": list(required(name))}
            for name in config.CONTRACTS
        ],
        # 顶层默认给当前契约；页面要展示两套列时读 contracts。
        "columns": [{k: (list(v) if isinstance(v, tuple) else v)
                     for k, v in col.items()} for col in columns_for(current)],
        "required": list(required(current)),
        "rules": ordered_rules(),
        "prices": price_table(),
        "scan_units": list(config.UNIT_ORDER),
        "sentinels": sorted(config.SENTINELS),
        "night_window": f"{config.NIGHT_START_HOUR:02d}:00-{config.NIGHT_END_HOUR:02d}:00",
        "min_charge": str(config.MIN_CHARGE),
    }
    return payload
