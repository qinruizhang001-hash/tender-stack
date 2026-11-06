<div align="center">

# ai-bid-document v2.0.0

**把标书当账本写**

[![Version](https://img.shields.io/badge/version-2.0.0-22d3ee?style=flat-square)](https://github.com/Jackon-Li/ai-bid-document/blob/main/CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-3.8%2B-a78bfa?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-0-34d399?style=flat-square)](https://github.com/Jackon-Li/ai-bid-document)
[![Tests](https://img.shields.io/badge/tests-23%20passed-34d399?style=flat-square)](https://github.com/Jackon-Li/ai-bid-document/blob/main/tests/test_scripts.py)
[![License](https://img.shields.io/badge/license-MIT-9aa6bf?style=flat-square)](https://github.com/Jackon-Li/ai-bid-document/blob/main/LICENSE)

**[可视化发布页](https://jackon-li.github.io/ai-bid-document/)** · **[完整变更记录](https://github.com/Jackon-Li/ai-bid-document/blob/main/CHANGELOG.md)** · **[README](https://github.com/Jackon-Li/ai-bid-document#readme)**

</div>

---

> 投标失败通常不是因为写得不好，而是因为 **漏了一条要求**、**编了一个参数**、**承诺了一句不该承诺的话**。
> v2 把这三种事故变成三门可被机器拦住的闸机：**条目化台账 · 零发明标记 · 双轨核查**。

## 本次发布速览

| | 内容 |
|---|---|
| **定位变化** | 从「一套方法论 + 两个脚本」升级为「可审计、可复用、可接入 CI 的投标作业系统」 |
| **核心用途** | 不变。依据招标文件逐条应答，不编造、不漏项、结构对齐、承诺可控 |
| **新增能力** | 四模式路由 · 资格符合性自检 · 评分作战表 · 复用污染扫描 · 机器可读报告 · 交付契约 |
| **修复缺陷** | 9 个可复现的"假通过"问题（每一个都有对应回归测试） |
| **检查项** | 7 → **21** 条确定性规则（9 组） |
| **测试** | 0 → **23** 项，零依赖，`python -m unittest discover -s tests` |
| **兼容性** | 旧台账仍可被新校验脚本读取；建议重跑抽取以拿到行号与去重结果 |

---

## 一、为什么要有 v2

v1 能用，但它会在三个地方让你误以为"已经做完了"。这三个问题都不是功能不够，而是**通过得太容易**。

### 1. 声明式覆盖

章节头写了 `> 覆盖条目：R-012`，正文一个字没写。v1 的覆盖率算 100%，报告全绿。
真实评审里，这条就是"没响应"。

### 2. 正则失配的假通过

覆盖率用 `\bR-\d{3,}\b`。Python 的 `\w` 包含中文，`条目R-001` 里 `R` 前面没有单词边界，
**匹配不到**。覆盖率可能恒为 0，也可能因为别处偶然命中而恒为 100 —— 两种都不可信。

### 3. 静默降级

没装 `python-docx` 时返回字符串 `[跳过] file.docx`，它被当作正文参与扫描。
报告看起来正常，数据全错。

> v2 的原则很简单：**宁可失败，不给假绿。**

---

## 二、新增的四模式路由

v1 不管问什么都跑六阶段，一句话的请求也要走全流程。v2 先判断你要哪一种活：

| 模式 | 你的话 | 走哪些阶段 | 拿到什么 |
|---|---|---|---|
| **M1 读标** | "这份招标文件讲了什么、红线在哪" | S0 → S1 → 解读 | 台账 + 废标红线清单 + 评分点表 |
| **M2 编标** | "帮我写技术标" | S0 → S5 | 八件套 |
| **M3 体检** | "这份标会不会废标" | S0 轻量 → S4 | 核查报告 + 整改清单 |
| **M4 定点** | "这句能不能写、算不算负偏离" | 单点判断 | 结论 + 原文依据 |

**路由口诀**：有现成稿 → M3；只有招标文件 → M1；要成稿 → M2；问一句 → M4。
模式可以升级，但不默认升级。

---

## 三、六阶段与四道闸机

```
S0 建档      I1–I8 输入清单 + 资格符合性自检（只列缺口，不判"能不能投"）
             └─ G0：无招标文件 / 纯扫描件未 OCR ──▶ 停止，说明缺什么

S1 解析      条目化（R-001 起，15 列）+ 去重 + 溯源行号
             └─ G1：条目数明显低于经验值（每 10 页 15–30 条）──▶ 补抽

S1.5 人工修订  修 mandatory / strength，补 owner，整理澄清函（不能省）

S2 映射      每条要求有主应答章节；评分点有落点与证据
             └─ G2 硬门禁：必答未映射 / 评分点无章节 ──▶ 禁止撰写

S3 撰写      覆盖声明 + 首段逐字回应 + 承诺三档 + 复用污染自检

S4 双轨核查   轨一：12 项确定性检查    轨二：A–G 七维语义复核
             └─ G3：BLOCKER 必须清零

S5 交付      八件套 + 待确认清单 + 机器可读报告
```

门禁不过时的正确动作是**停下来说明缺什么**，不是继续往下写。

---

## 四、修复清单

| # | v1 的行为 | 真实后果 | v2 |
|---|---|---|---|
| 1 | 覆盖声明行也算"已引用" | 章节头写编号即通过，正文空着也报 100% | `COV-02` 仅声明未展开 → 阻断 |
| 2 | `\bR-\d{3,}\b` 匹配条目编号 | 中文语境失配，覆盖率可能恒为 0 | lookaround 改写 + 回归用例 `T01` |
| 3 | docx 缺依赖返回 `[跳过]` | 占位字符串被当正文扫描，报告全错 | 显式 `InputError`，退出码 2 |
| 4 | 章节编号只认 Markdown `#` | docx 转文本没有 `#`，检查恒为空 | 支持 `第X章` / `# N.` / `N、` 三种体例 |
| 5 | 类型判定"章节优先" | 资格、格式条款被批量误判，owner 派错人 | 句内优先、章节兜底 + 强信号章节 |
| 6 | 无去重 | 同一要求写三遍算 3 条必答，分母虚高 | `dup_of`：完全重复 / 子集 / 相似度 ≥0.90 |
| 7 | `source_quote` 与正文同源 | 号称溯源，其实无法定位原文位置 | 新增 `line_no` + `source_ver` |
| 8 | 输入错误退出码为 1 | 与文档不符，CI 把"参数用错"判成"不合格" | 统一 `0 / 1 / 2` 语义 |
| 9 | 映射表 `chapter` 为空不报错 | "有安排没落点"，仍然算通过 | `MAP-02` 阻断 |

<details>
<summary><b>为什么这些 bug 值得单独发一个版本</b></summary>

因为它们不报错。一个会报错的工具，用户会去修；一个悄悄给出"通过"的工具，
用户会拿它当依据去递交标书——这才是真正的风险。

v2 的所有修复都遵循同一条原则：**不确定就失败，不猜、不降级、不静默**。
配套的 23 条测试（`tests/test_scripts.py` 的 T01–T10）就是把这些行为锁死，
防止下一次改动再退回去。

</details>

---

## 五、六块新能力

### ▍评分作战表 `scoring-matrix.csv`

把评分办法拆成「得分点 → 分值 → 证据 → 章节 → 把握度」，一张表回答三个问题：
哪些分确定拿不到、哪些分还没落点、离满分差多远。

- `SCORE-01` 评分点无承载章节 → 阻断（白丢分）
- `SCORE-02` 证据为空 → 阻断（有分无证据）
- `SCORE-06` 可达分 / 理论满分 < 70% → 竞争力提示

### ▍复用污染扫描 `POLL-01..03`

比"凭空编造"更危险的编造变体：旧项目名、旧编号、内部用语没删干净。
读起来完全通顺，直到资格性审查因为项目名不一致被废标。

### ▍双层覆盖校验

区分「覆盖声明里提到」与「正文真的写了」。只有声明 → 阻断；两处都有 → 通过。

### ▍机器可读报告 `validate.json`

每条发现带 `id / level / rule / location / evidence / suggestion`，
可直接被 OA、缺陷平台或周报脚本消费：

```json
{
  "id": "F-001",
  "level": "BLOCKER",
  "rule": "COV-02",
  "message": "必答条目 R-008 仅出现在覆盖声明行，正文无实质展开",
  "location": { "file": "03-技术标.md", "line": 3 },
  "evidence": "> 覆盖条目：…（仅声明）",
  "suggestion": "在声明所指章节补写 ≥80 字响应段"
}
```

### ▍强制强度五级 + 溯源三件套

`strength`（禁止/必须/应/宜/可）替代 Y/N 二分，决定整改顺序；
`source_quote + line_no + source_ver` 让"100% 溯源"可被第三方验证——
随机抽三条，30 秒内找不到原文，就是溯源不合格。

### ▍交付契约与异常边界

固定目录（`00-输入` 到 `50-交付`）、命名规则、Issue ID（`F-001`）、十节核查报告结构；
外加 14 类真实异常的处理口径：扫描件、加密、超长标书、多版本补遗、联合体、分包、
电子标 CA、涉密项目…… 每一类都明确"怎么降级、怎么标注、什么情况下必须停"。

---

## 六、升级指南

```bash
git pull
python -m unittest discover -s tests -v     # 期望：Ran 23 tests ... OK
```

已有项目的迁移三步：

| 步骤 | 命令 | 说明 |
|---|---|---|
| 1. 重跑抽取 | `python scripts/extract_requirements.py --input 招标文件.md` | 拿到 `line_no`、`strength`、去重结果 |
| 2. 人工修订 | 打开 `requirements.csv` | 确认 `mandatory` 与 `strength`，补 `source_ver` |
| 3. 重跑校验 | `python scripts/validate_bid.py ... --report validate.json` | 关注新增的 `COV-02` / `SCORE` / `POLL` |

> 旧台账（11 列）仍可被新校验脚本读取，但新脚本产出的 15 列台账**不支持旧版脚本反向读取**，
> 请配套升级。

---

## 七、验证记录

```
$ python -m unittest discover -s tests -v
test_match_after_chinese (T01_IDRegex) ................. ok
test_only_declared_is_blocker (T02_Coverage) ........... ok
test_docx_never_silently_skipped (T03_DocxDependency) .. ok
test_chinese_chapter_detected (T04_Numbering) .......... ok
test_sentence_beats_section (T05_TypeDetection) ........ ok
test_identical_merged (T06_Dedupe) ..................... ok
test_other_project_no (T07_Pollution) .................. ok
test_missing_chapter_and_evidence (T08_Score) .......... ok
test_blocker_is_1_and_json_written (T09_ExitCodes) ..... ok
test_extract_and_count (T10_ExtractSmoke) .............. ok
...
Ran 23 tests in 1.70s
OK
```

端到端演示（样例招标文件 → 台账 → 有问题的标书 → 校验）：

```
【统计】
  · 必答条目：11 条
  · 必答覆盖（实质）：0/11（0.0%）

【阻断项 BLOCKER】共 14 项
  ▼ [COV-02] 必答条目 R-008 仅出现在覆盖声明行，正文无实质展开
     位置 03-技术标.md:3
  ▼ [TODO-01] 命中「待确认标记」        位置 03-技术标.md:15
  ▼ [GRAY-01] 命中「基本响应」          位置 03-技术标.md:16
  ▼ [POLL-01] 出现非本项目编号 KM-2026-0901   位置 03-技术标.md:7
```

补齐响应段后：

```
  · 必答覆盖（实质）：11/11（100.0%）
结论：通过 ✓
  脚本只查形式。仍需按 compliance-checklist.md 做语义复核。
```

---

## 八、仍然不做的事

明确边界，避免误用：

- 不判断"这个项目该不该投"——只输出符合性缺口，决策留给经营人员；
- 不测算报价、成本、利润率——给报价约束清单，不给数字；
- 不判定"是否废标"——列事实与风险，最终判断在评审与法务；
- 不处理扫描件——先 OCR，否则一律标"待人工确认"；
- 不替代签章、授权与法律意见——投标文件的法律后果由投标人及其签署人承担。

---

<div align="center">

**如果这个版本帮你少漏了一条要求，它就值得。**

[![Star](https://img.shields.io/badge/star-支持一下-fbbf24?style=flat-square)](https://github.com/Jackon-Li/ai-bid-document)
[![Issues](https://img.shields.io/badge/issues-反馈问题-fb7185?style=flat-square)](https://github.com/Jackon-Li/ai-bid-document/issues)

<sub>MIT License · 本技能提供写作方法与检查工具，不构成法律、商务或合规建议</sub>

</div>
