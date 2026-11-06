# 交付契约（Delivery Contract）

> 交付不是"把文件发过去"，而是**任何同事拿到这个目录，不用问你就能接着干**。
> 本文定义目录、命名、文件格式与机器可读契约。全部为强制约定，不遵守即视为未交付。

---

## 一、工作目录结构

```
bid-<项目编号>-<YYYYMMDD>/
├── 00-输入/                  # 只读。给什么放什么，不修改原件
│   ├── 招标文件.pdf
│   ├── 补遗-01-<日期>.pdf
│   ├── 评分办法.pdf
│   └── 公司素材/             # 资质、业绩、人员、原厂资料
├── 10-台账/
│   ├── requirements.csv      # 要求台账（唯一事实底账）
│   ├── outline-map.csv       # 章节映射
│   ├── scoring-matrix.csv    # 评分作战表（有评分办法时必需）
│   ├── qualification-check.csv # 资格符合性自检（Go/No-Go 缺口）
│   └── clarification-log.csv # 澄清/补遗台账
├── 20-正文/
│   ├── 00-封面.md
│   ├── 01-投标函.md
│   ├── 02-商务标.md
│   ├── 03-技术标.md
│   └── ...
├── 30-应答/
│   ├── response-matrix.md
│   └── deviation-table.md
├── 40-核查/
│   ├── validate.json         # 脚本机读结果
│   ├── 核查报告.md           # 人读版（含 AI 语义复核结论）
│   └── 待确认清单.md
└── 50-交付/
    ├── delivery-checklist.md
    └── 最终成稿.docx
```

**命名规则**：章节文件用两位序号前缀，保证目录顺序 = 投标顺序；
版本用后缀 `_v2`，不覆盖旧版；日期统一 `YYYYMMDD`。

---

## 二、台账字段契约

### `requirements.csv`（12 列，顺序固定）

| 列 | 必填 | 取值 | 说明 |
|---|---|---|---|
| `req_id` | ✔ | `R-001` 起三位递增 | 全流程唯一，正文引用此号 |
| `section` | ✔ | 原文章节号+标题 | 便于翻回原文 |
| `requirement` | ✔ | 归一化后的一句话 | 一条只放一个可判定点 |
| `type` | ✔ | 资格/格式/技术/商务/服务/评分 | 六类，决定 owner 分工 |
| `mandatory` | ✔ | `Y`/`N` | Y = 不满足即废标/否决 |
| `strength` | ✔ | 禁止/必须/应/宜/可 | 强制强度五级（见 requirement-parsing） |
| `source_quote` | ✔ | 原文照抄 | 含错别字，不得改写 |
| `source_ver` | ✔ | `v1`/`补遗-01` | 出自哪一版文件 |
| `line_no` | ✔ | 整数 | 原文行号（docx 为段落序号） |
| `response_type` | ✔ | 响应/偏离/证明/承诺 | 决定在标书里的呈现形式 |
| `need_clarify` | ✔ | `Y`/`N` | 是否发澄清函 |
| `dup_of` | ✔ | `R-00X` 或空 | 与哪条重复（去重后指向主条） |
| `owner` / `status` / `note` | ✔ | 人名 / 待响应… / 自由文本 | 协作与追溯 |

> 兼容性：v1 的 11 列仍可被脚本读取，缺列按默认值补齐（`strength`=按词表推断、`line_no`=0、`source_ver`=`v1`、`dup_of`=空）。
> 新增列不会破坏旧台账，但新台账必须写全。

### `scoring-matrix.csv`（8 列）

`score_id, score_item, score_max, score_rule, evidence, chapter, capability, note`

`capability` ∈ `高/中/低/无`；`evidence` 为空视为"该项预计不得分"，必须显式写 `无`。
`chapter` 为空 → 视为无承载章节，进入 BLOCKER（评分点漏答）。

### `qualification-check.csv`（6 列）

`item, requirement, our_status, evidence, gap, action`

`our_status` ∈ `满足/不满足/待确认`。**本表不写"能否投标"的结论。**

---

## 三、`validate.json` 契约（机读结果）

```json
{
  "schema_version": "2.0",
  "generated_at": "2026-10-18T10:00:00",
  "tool": {"name": "validate_bid.py", "version": "2.0.1"},
  "input": {
    "bid": "./20-正文",
    "requirements": "10-台账/requirements.csv",
    "map": "10-台账/outline-map.csv",
    "score": "10-台账/scoring-matrix.csv"
  },
  "stats": {
    "files": 6, "lines": 3200, "chars": 86000,
    "requirements_total": 128, "mandatory": 61,
    "coverage": {"declared": 61, "substantive": 59, "percent": 96.7}
  },
  "findings": [
    {
      "id": "F-001",
      "level": "BLOCKER",
      "rule": "COV-01",
      "message": "R-019 仅出现在覆盖声明行，正文无实质展开",
      "location": {"file": "03-技术标.md", "line": 214},
      "evidence": "> 覆盖条目：R-019",
      "suggestion": "在 3.4 节补充 R-019 的具体响应段落（≥80 字，含量化指标）",
      "reviewer": "技术负责人"
    }
  ],
  "summary": {"blocker": 1, "warn": 3, "info": 5, "conclusion": "不通过"}
}
```

**规则编号命名**：`COV-*` 覆盖、`TODO-*` 占位、`GRAY-*` 灰色表述、
`NUM-*` 章节编号、`RISK-*` 绝对化、`MAP-*` 映射、`SCORE-*` 评分、
`POLL-*` 复用污染、`FMT-*` 格式、`IO-*` 输入异常。

**发现项必须齐六要素**：`level / rule / message / location / evidence / suggestion`（+ `reviewer`）。
缺 `location` 或 `evidence` 的发现，视为"不合格发现"，必须补齐后再进报告——
否则整改方无法定位，等于没提。

---

## 四、核查报告人读版结构（固定十节）

| 节 | 内容 | 责任侧 |
|---|---|---|
| 一 | 结论摘要（BLOCKER 数 / 覆盖率 / 是否可交付） | 程序 |
| 二 | 关键字段与项目标识一致性 | 程序 |
| 三 | 必答覆盖（含仅声明未展开） | 程序 |
| 四 | 格式与章节结构 | 程序 |
| 五 | 评分点证据完整性 | 程序 |
| 六 | 复用污染（旧项目/旧客户/内部用语） | 程序 |
| 七 | 占位符与灰色表述 | 程序 |
| 八 | 语义复核：废标条款比对、答非所问、前后矛盾 | AI + 人工 |
| 九 | 整改清单（按 P0/P1/P2，含责任人） | AI |
| 十 | 放行结论与未决事项 | 人工 |

**前七节由脚本生成，第八节起由 AI 判断后追加，两者不得互相覆盖。**
AI 结论与脚本结论冲突时，两个都保留，并标注"以人工复核为准"。

---

## 五、归档与版本

| 动作 | 时点 | 产物 |
|---|---|---|
| 台账冻结 | 投标截止前 1 天 | `requirements.csv` 只读快照 |
| 成稿归档 | 递交后 | `50-交付/` 整目录 + 递交凭证（回执/截图） |
| 复盘沉淀 | 开标后 | 失分点、被废原因、可复用段落 → 回写公司素材库 |
| 查重复用 | 每次复用历史标书 | 替换项目名/编号/客户名后跑 `validate_bid.py --project` |

**禁止**：把本项目台账原样用于下一个项目而不改 `req_id` 来源与 `source_ver`——
这是复用污染与"答非所问"的头号成因。
