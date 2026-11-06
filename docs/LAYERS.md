# 分层与边界（LAYERS）

本文件钉死系统的分层职责。改动只有一种合法方式：**在对应层内加能力，不跨界**。

## 1. 承载层（deterministic core）

| 模块 | 职责 | 不允许 |
|---|---|---|
| `adapter/layout_parse.py` | PDF → 章节块 / 表 Markdown / 条款；标题、目录、跨页表规则 | 调模型、读 KB 改判定 |
| `adapter/bid_chunker.py` | 页面解析、OCR 调度、降级留痕 | 静默换引擎 |
| `adapter/vector_export.py` | 导出 requirement / section JSONL（主键在这里） | 改 requirement 主键定义 |
| `scripts/gold_gate.py` | 条款级金标门禁（总/最差/风险条款） | 自动调阈值 |

**规则拦在模型前面**：数值、日期、资质缺失由代码定案；解析层只产出可追溯证据（页码、section、原文/表 Markdown、quality）。

## 2. 补偿层（assistive，只读）

- 检索增强、相似案例提示、路由建议。
- 允许：`TENDER_KB_RETRIEVE=1` 时把 `kb/approved` 的案例作为**只读上下文**注入报告/面板。
- **禁止**：直接改硬规则、改 gold、改 requirement 主键、改 gate 阈值；禁止把补偿结果写回判定主键。

## 3. 知识库与受控迭代（本轮新增）

### 3.1 知识库是承载层的数据资产

`kb/` 只增厚，不覆盖历史。每条知识是一张**案例卡**（schema 见 `kb/schema.json`），必须带 provenance：

- `source_doc_hash`（源 PDF 的 sha256）
- `page` / `section` / `requirement_id`（可空）
- `kind`：`gold_miss` | `false_positive` | `table_row` | `layout` | `ocr` | `rule_gap`
- `evidence`：原文摘录或表 Markdown 片段
- `lesson`：一句话人话结论（必填）
- `patch_candidate`：可选（正则草稿 / 规则 ID / 金标 JSON 片段），状态 `draft|approved|rejected`
- `created_at`、`approved_by`、`linked_test`

### 3.2 写入只进暂存

- 自动吸收只写 `kb/inbox/`，状态 `draft`。
- 晋升到 `kb/approved/`、`contract/gold/`，或生成代码补丁，**必须过人（HITL）+ 门禁**。
- 任何自动流程不得直接改 `layout_parse.py`、不得覆写 gold、不得改门禁阈值。

### 3.3 迭代闭环（固定 6 步）

1. 选案例父集（按 doc_type / 品类）
2. 跑解析与审查
3. 分析 miss / 误报 / quality=low
4. 生成**候选**补丁（规则草稿或金标条目），落 `kb/proposed/`
5. `gold_gate` + 针对 `linked_test` 的回归测
6. 人工 Absorb 或丢弃（写审计）

### 3.4 train / gate / sealed

| 集合 | 来源 | 用途 |
|---|---|---|
| train | 本次审查的文档 | 生成 inbox 卡片 |
| gate | `contract/gold/index.json` | 晋升前跑 `gold_gate` |
| sealed | `kb/sealed/index.json` 列出的文档哈希 | **禁止被自动吸收改写**；晋升时 sealed recall 不得下降 |

## 4. 变更纪律

- 不能自动热加载规则；规则合并走人工 PR。
- 不能用大模型直接覆写 gold。
- 不引入外部 RSI 运行时；只借「可变表面外的评测 + 人工 Gate」的思想。
- 任何新增能力都要有最小回归测，否则 `kb_promote` 拒绝晋升。

### 3.5 侧车归属

| 侧车 | 归属层 | 开关 | 失败行为 |
|---|---|---|---|
| 版面检测（DocLayout-YOLO / PP-DocLayout） | 补偿层 | `TENDER_LAYOUT=1` | 回退规则，`stats.layout_fallback` 留痕 |
| 表结构模型（Table Transformer / PP-Structure） | 补偿层 | `TENDER_TABLE_MODEL=1` | 返回 None，保持规则合并结论 |
| VLM OCR（PaddleOCR-VL / 本地 VLM） | 补偿层 | `TENDER_VLM_OCR=1` | 不启用，走 RapidOCR |
| KB 检索 | 补偿层 | `TENDER_KB_RETRIEVE=1`（默认开） | 索引缺失返回空列表，不影响主流程 |
| KB 吸收 | 承载层数据资产 | `TENDER_KB_ABSORB=1`（默认开） | 写 inbox 失败只记 `kb_fallback` |

侧车永远不能成为唯一解析路径；任何侧车失败都必须回退到规则路径并留下 stats。
