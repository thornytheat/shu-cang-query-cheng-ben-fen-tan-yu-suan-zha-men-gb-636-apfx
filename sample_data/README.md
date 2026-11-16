# 样例说明

| 文件 | 编码 | 埋了什么 |
|---|---|---|
| `bills_2026_11_dirty.csv` | UTF-8 + BOM，CRLF | 千分位、全角数字、MB/GB/TB 混写、小写 gb、`N/A` 哨兵、重复 query_id 不同扫描量、完全相同重复行、空行、字段全空行、分段重复表头、坏时间、坏单位、多一列、末行截断、跨零点夜间查询、共享查询（含权重全零） |
| `bills_2026_11_v2.csv` | UTF-8 | **v2 列契约**：`duration_ms` 毫秒 + `scan_bytes` 字节，没有单位列；含共享查询与零扫描行，用于升级后新接入 |
| `bills_2026_11_v2_mixed.csv` | UTF-8 | v2 与 v1 计量列同时出现（`duration_ms` + `duration_s` + `scan_bytes`），用于验证「混用两套列」被拒（`Q_CONTRACT_MIXED`） |
| `bills_2026_11_full.csv` | UTF-8 + BOM | 28 行干净数据，4 个部门，用于端到端与守恒断言 |
| `bills_2026_11_gb18030.csv` | GB18030 | 中文部门名与项目名，用于编码回退（声明 utf-8 也能读） |
| `bills_bignum.csv` | UTF-8 | 超过 2^53 的字节数、科学计数法扫描量，用于大整数保形 |
| `bills_empty_header.csv` | UTF-8 | 只有表头（空态一） |
| `bills_all_empty.csv` | UTF-8 | 表头 + 两行字段全空（空态二） |
| `bills_blank.csv` | UTF-8 | 只有空行，没有表头（空态三，会被拒绝） |
| `budgets.csv` | UTF-8 | 全角预算、千分位预算、空预算行 |

导入用 `POST /api/import {"source": "文件名"}`，只接受本目录下的 csv，不能带路径。

列契约按表头自动识别并绑定到批次：v1 读 `duration_s` + `scan_size`/`scan_unit`，
v2 读 `duration_ms` + `scan_bytes`。同一来源不能中途换契约；`bills_2026_11_v2_mixed.csv`
会因此被拒。续跑旧批次时传 `"resume": true` 可显式沿用原批次身份。
