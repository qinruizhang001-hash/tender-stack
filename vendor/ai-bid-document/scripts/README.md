# 脚本说明

两个脚本都是**纯标准库**实现，无需安装第三方依赖（仅读取 `.docx` 时需要 `python-docx`，可选）。

环境要求：Python 3.8+

```bash
python --version   # 确认版本
```

---

## 1. extract_requirements.py — 要求抽取

把招标文件变成可追溯的条目台账 `requirements.csv`。

### 用法

```bash
# 基础：从 txt/md 抽取
python scripts/extract_requirements.py --input 招标文件.md

# 指定输出
python scripts/extract_requirements.py --input 招标文件.txt --output requirements.csv

# 读取 Word（需先 pip install python-docx）
python scripts/extract_requirements.py --input 招标文件.docx

# 过滤过短句子（默认 8 字，长文本可调大以减少噪声）
python scripts/extract_requirements.py --input 招标文件.md --min-len 12
```

### 输出字段

| 字段 | 说明 |
|---|---|
| `req_id` | `R-001` 起，全流程唯一标识 |
| `section` | 来源章节（如「第二章 技术要求 2.1」） |
| `requirement` | 要求正文（去编号） |
| `type` | `资格`/`格式`/`技术`/`商务`/`服务`/`评分` |
| `mandatory` | `Y` = 不满足即废标/否决 |
| `source_quote` | 原文摘录，供复核 |
| `response_type` | `响应`/`偏离`/`证明`/`承诺` |
| `need_clarify` | `Y` = 表述模糊，需发澄清函 |
| `owner` / `status` / `note` | 留空，人工填写 |

### 识别能力与边界

**能识别**
- 章节结构：`第X章`、`1.1`、`2.3.1`、`（一）`、markdown 标题
- 强制词：必须 / 须 / 应 / 不得 / 严禁 / 不低于 / 不少于 / 最多 / 至少 等
- 单字「应」「须」有防误伤处理（不会误判 `响应`、`适应`、`投标人须知`）
- 类型判定优先看章节名，其次看句内关键词

**抓不到（必须人工补）**
- 陈述句形式的强制条款，如「合同签订后 60 个日历日内交货」——没有情态动词
- 表格内的要求（仅在 `.docx` 模式下提取单元格文本，不做表格结构还原）
- 图片、扫描件中的文字（需先 OCR）
- 条款之间的交叉引用（如「见 3.2 条要求」）

> **所以抽取结果必须人工过一遍。** 脚本的目标是"把 80% 的机械工作做掉"，
> 不是"替代判断"。判断 `mandatory` 是投标成败的关键，必须由人确认。

---

## 2. validate_bid.py — 交付前校验

检查投标稿是否达到可交付状态。

### 用法

```bash
# 完整校验
python scripts/validate_bid.py --bid ./bid --req requirements.csv

# 带章节映射检查（推荐）
python scripts/validate_bid.py --bid ./bid --req requirements.csv --map outline-map.csv

# 只列出待确认项（写标书过程中随时用）
python scripts/validate_bid.py --bid ./bid --list-todo

# 严格模式：把 WARN 也视为失败（适合正式送印前）
python scripts/validate_bid.py --bid ./bid --req requirements.csv --strict
```

`--bid` 可以是单个文件，也可以是目录（递归读取 `.md`/`.txt`/`.docx`）。

### 检查项

| 级别 | 检查 | 说明 |
|---|---|---|
| **BLOCKER** | 占位符残留 | `[待确认`、`TODO`、`TBD`、`XXX`、`待补充`、`____`、`？？` |
| **BLOCKER** | 必答覆盖率 | `mandatory=Y` 但全文没有任何章节引用其 `req_id` |
| **BLOCKER** | 灰色响应表述 | 「基本响应」「部分响应」「原则上满足」——会被评委判为不响应 |
| **BLOCKER** | 章节映射缺失 | 传了 `--map` 且必答条目未映射章节 |
| **WARN** | 一级章节跳号/重号/倒退 | `# 第3章` 直接跳到 `# 第5章` |
| **WARN** | 绝对化表述 | `100%`、`零故障`、`终身免费`、`卓越`、`全方位` 等 |
| **WARN** | 注水套话 | 「随着……」「众所周知」「势在必行」 |

### 退出码

| 码 | 含义 |
|---|---|
| `0` | 通过（可能仍有 WARN） |
| `1` | 存在 BLOCKER，或 `--strict` 下有 WARN |
| `2` | 输入错误（文件不存在、CSV 缺列等） |

可直接用于 CI 或提交前钩子：

```bash
python scripts/validate_bid.py --bid ./bid --req requirements.csv || echo "校验未通过，禁止交付"
```

### 脚本查不出来的（必须人工）

| 类型 | 为什么查不出 |
|---|---|
| **答非所问** | 语义匹配超出正则能力。招标问"是否支持国密"，答了一段"安全理念"也能通过 |
| **数值方向错误** | 写「≥1000 路」而招标要求「≥2000 路」，字符串层面无法判定 |
| **参数来源不可信** | 数字看起来合理但无出处，脚本无法验证真伪 |
| **正本副本/电子版不一致** | 脚本只看文本内容，不比对文件 |
| **盖章与签字** | 物理动作 |
| **承诺超出授权** | 需要业务判断 |

> 这正是 `references/compliance-checklist.md` 存在的原因：**脚本管形式，清单管实质。**

---

## 3. 一次完整调用示例

```bash
# 1) 抽取要求
python scripts/extract_requirements.py --input 招标文件.docx --output requirements.csv

# 2) 人工复核 requirements.csv（改 mandatory、补 owner，这一步不能省）

# 3) 撰写过程中随时查待确认项
python scripts/validate_bid.py --bid ./bid --list-todo

# 4) 交付前完整校验
python scripts/validate_bid.py --bid ./bid --req requirements.csv --map outline-map.csv

# 5) 清零 BLOCKER 后复跑，直到 exit 0
```

---

## 4. 常见问题

**Q：脚本报「无法识别文件编码」**
A：招标文件多为 GBK/GB18030 编码，脚本已按 `utf-8-sig → utf-8 → gb18030 → gbk` 顺序尝试。仍失败说明是二进制或加密文档，请用 Word 另存为 `.docx` 或 `.txt`（UTF-8）。

**Q：抽取出来的条目太多 / 太碎**
A：调大 `--min-len`（如 12–15）过滤短句。仍太多时，说明招标文件本身条款密度高，属正常。

**Q：覆盖率显示 100%，但我感觉有漏项**
A：脚本只检查「`req_id` 是否被引用」，不检查「引用后是否真的答了」。所以要在章节开头写覆盖声明（格式见 SKILL.md），并**真的在该章节里作答**。这也是为什么 `--map` 检查很重要——它检查的是"有没有安排位置"，比"有没有提到"更严格。

**Q：`requirements.csv` 用 Excel 打开是乱码**
A：输出为 UTF-8-BOM，Excel 应能正常识别。若仍乱码，用「数据 → 从文本/CSV」导入并手动选 UTF-8。

**Q：可以只用脚本不用方法论文档吗**
A：不建议。脚本只能防"形式错误"，防不了"内容错"。投标失败的大部分原因是答非所问和承诺失控，这两类只有靠 `references/` 里的规则来防。

---

# v2.0 变更

## 新增参数

| 参数 | 作用 | 备注 |
|---|---|---|
| `--score scoring-matrix.csv` | 检查评分点是否有承载章节与证据 | 缺失即 `SCORE-01/02` |
| `--project "项目名"` | 复用污染比对：出现其他项目名称 | `POLL-02`，WARN |
| `--project-no "编号"` | 复用污染比对：出现其他项目编号 | `POLL-01`，BLOCKER |
| `--report validate.json` | 输出机读结果（含 issue id / rule / location） | 契约见 `references/delivery-contract.md` |
| `--fail-on`（等价 `--strict`） | WARN 也视为失败 | CI 里建议开 |
| `extract --ver "补遗-01"` | 标注来源版本，写进 `source_ver` | 多版本/补遗场景必填 |
| `extract --no-dedupe` | 关闭重复条款合并 | 需要保留全部原文时用 |

## 新增/修复的检查项

| 规则 | 变化 | 说明 |
|---|---|---|
| `COV-02` | **新增** | 仅出现在覆盖声明行、正文无实质展开 —— 修补 v1 最大漏洞 |
| `COV-03` | 修复 | 原 `\b` 正则在中文语境下失配，导致引用检查形同虚设 |
| `NUM-01` | 修复 | 支持 `第X章` / `# N.` / `N、` 三种体例；v1 只认 Markdown `#` |
| `POLL-01/02/03` | **新增** | 复用污染：旧项目编号、旧项目名称、内部用语 |
| `SCORE-00..06` | **新增** | 表读不出/无章节/无证据/分值异常/把握度非法/缺原文或拆分过粗/可达分偏低 |
| `IO-01` | 修复 | docx 缺依赖时明确报错退出 2；v1 静默返回 `[跳过]` 并参与扫描 |
| `MAP-02` | 修复 | 映射表 `chapter` 为空时判 BLOCKER（原来只报缺失映射） |
| 退出码 | 修复 | 输入错误统一 `2`；v1 的 `sys.exit("字符串")` 实际返回 1 |

## 抽取脚本的增强

- 类型判定改为「句内优先、章节兜底」，并让「评标办法 / 合同条款」成为强信号
- 新增 `strength`（禁止/必须/应/宜/可）与 `line_no`、`source_ver`、`dup_of` 列
- 新增去重：完全重复 / 子集包含 / 相似度 ≥0.90，被合并项写 `dup_of`
- 统计口径改为「原始条目 / 去重后主条目 / 必答主条目」三个数字

## 兼容性

- 旧版 `requirements.csv`（11 列）仍可被校验脚本读取；
- 但新脚本产出的 15 列台账不支持旧版脚本反向读取（缺列会报 `KeyError`），请配套升级；
- 建议升级后重跑一次 `extract_requirements.py`，拿到 `line_no` 与去重结果。

## 回归测试

```bash
python -m unittest discover -s tests -v      # 26 项，零依赖
```

测试用例与 bug 一一对应（`tests/test_scripts.py` 顶部的 T01–T10 注释）。
