<div align="center">

# ai-bid-document v2.0.1

**把"我们声称能查"变成"真的会查"**

[![Version](https://img.shields.io/badge/version-2.0.1-22d3ee?style=flat-square)](https://github.com/Jackon-Li/ai-bid-document/blob/main/CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-3.8%2B-a78bfa?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-0-34d399?style=flat-square)](https://github.com/Jackon-Li/ai-bid-document)
[![Tests](https://img.shields.io/badge/tests-26%20passed-34d399?style=flat-square)](https://github.com/Jackon-Li/ai-bid-document/blob/main/tests/test_scripts.py)
[![Rules](https://img.shields.io/badge/rules-21-22d3ee?style=flat-square)](https://github.com/Jackon-Li/ai-bid-document#readme)
[![License](https://img.shields.io/badge/license-MIT-9aa6bf?style=flat-square)](https://github.com/Jackon-Li/ai-bid-document/blob/main/LICENSE)

**[可视化发布页](https://jackon-li.github.io/ai-bid-document/)** · **[完整变更记录](https://github.com/Jackon-Li/ai-bid-document/blob/main/CHANGELOG.md)** · **[README](https://github.com/Jackon-Li/ai-bid-document#readme)**

</div>

---

> v2.0.0 批评 v1 的问题是「**看起来通过、其实没做**」。
> 发布后自查发现，v2.0.0 自己也犯了同一类错：**有两条规则写进了规范文档，却没有一行判定代码。**
> 这个补丁把它们补齐，并把几处对外数字改回真实值。

## 本次发布速览

| | 内容 |
|---|---|
| **性质** | 补丁版。流程、命令行参数、CSV 字段、`validate.json` schema 全部未变 |
| **核心修复** | `SCORE-05`、`FMT-01` 两条规则从"文档承诺"变为"真的会报" |
| **规则总数** | 12（文档口径）→ **21 条 / 9 组**（代码实际，现已逐条对齐） |
| **测试** | 23 → **26** 项，新增 3 个 `SCORE-05` 用例 |
| **行为变化** | 每次校验会多一条 `FMT-01` INFO；评分表缺原文或拆得过粗会多 `SCORE-05` WARN |

## 一、补上的两条规则

### `SCORE-05`：评分点拆分过粗 / 缺原文

`references/scoring-matrix.md` 早就写了这条规则和等级，但 `validate_bid.py` 里从来没有对应代码。
现在有两个判定：

| 触发条件 | 为什么算问题 |
|---|---|
| `score_rule`（原文摘录）为空 | 没有原文就没法核对这一条是否拆全，等于放弃了核对 |
| 评分点 < 15 条 **且** 平均每条 > 8 分 | 一条吃下 8 分以上，通常意味着几个得分点被压成了一行 |

阈值用"**平均分值**"而不是只看条数：小项目评分项本来就少，只看条数会大面积误报。
用自带模板验证过——10 条 / 62 分 = 6.2 分每条，不触发；一条 10 分的粗拆表才会报。

### `FMT-01`：篇幅统计

README 声称有"篇幅统计"检查，实际上只有 `validate.json` 里一个 `chars` 字段。
现在每次校验都输出一篇：总数、平均数，以及单篇不足 500 字的点名。

> 用途不是卡字数，而是抓"**整篇只有标题和占位符**"的稿子——这类稿子跑覆盖率会全绿，
> 因为章节标题里确实写了 `R-0XX`。

## 二、改回真实的数字

| 位置 | 原写法 | 实际 | 说明 |
|---|---|---|---|
| README 校验项数 | "检查 12 项" | **21 条规则 / 9 组** | 并补了一张能与 `validate.json` 逐条对上的规则表 |
| README 模板数量 | "7 份" | **8 份** | `templates/` 实际 8 个文件 |
| 测试数量 | 23 项 | **26 项** | 徽章、正文、目录树三处同步 |
| README 目录 | 缺"贡献" | 补齐 | 全文 17 个二级标题，目录只列了 16 个 |

新增的规则表长这样，右边一列是脚本真实的等级，不是"应该有"的等级：

| 组 | 规则 | 等级 |
|---|---|---|
| 占位 | `TODO-01` | BLOCKER |
| 覆盖 | `COV-01` / `COV-02` | BLOCKER |
| | `COV-03` | WARN |
| 表述 | `GRAY-01` | BLOCKER |
| | `RISK-01` / `RISK-02` | WARN |
| 结构 | `NUM-01` | WARN |
| 映射 | `MAP-01` / `MAP-02` | BLOCKER |
| 评分 | `SCORE-00` / `SCORE-01` / `SCORE-02` | BLOCKER |
| | `SCORE-03` / `SCORE-04` / `SCORE-05` | WARN |
| | `SCORE-06` | INFO |
| 污染 | `POLL-01` / `POLL-03` | BLOCKER |
| | `POLL-02` | WARN |
| 篇幅 | `FMT-01` | INFO |

## 三、一处错误陈述

README 原写「GitHub 不支持中文仓库名与中文 topics」。

**这是错的**——GitHub 支持中文 topics，只是不利于检索排序。已改为准确表述，
免得读者据此放弃给仓库加中文标签。

## 四、发布页

`docs/index.html` 之前只有 `<title>` 和 `description`，分享到群或社交平台是一张空白卡片。
这次补上：

- `og:type` / `og:title` / `og:description` / `og:url` / `og:locale`
- `twitter:card` / `twitter:title` / `twitter:description`
- `theme-color`（移动端浏览器地址栏配色）

> 仍然**没有 `og:image`**。图片得是位图（PNG/JPG），仓库里没有，我也没有编一张。
> 现在的效果是"文字卡片"，比空白强，但不如带图。要做的话需要一张 1200×630 的品牌图。

## 五、升级方式

```bash
# 替换 SKILL.md / references / templates / scripts / tests 即可，无需改任何既有台账
python -m unittest discover -s tests -v     # 26 项应全部 OK
python scripts/validate_bid.py --bid ./bid --req requirements.csv --score scoring-matrix.csv
```

**唯一需要注意的兼容点**：`validate.json` 的 `findings` 会多出 `SCORE-05`、`FMT-01` 两类。
如果你的下游脚本按 `rule` 白名单过滤，记得把 `FMT-01` 加进去——它是 INFO，不阻断交付。

## 六、验证记录

```
Ran 26 tests in 1.892s
OK
```

- 模板自检：`check_score(templates/scoring-matrix.csv)` → 规则命中 0 条，可达率 72.6%
- 规则统计：脚本实现中出现的规则编号共 21 个，`SCORE-00..06` 连续无断档

## 七、这次没做什么

保持诚实比凑齐功能表重要：

- **没加新功能**。这是补丁，不是 2.1。
- **没改任何既有规则的判定语义**。`COV`/`MAP`/`POLL`/`RISK`/`NUM`/`GRAY`/`TODO` 全部原样。
- **没动 `mandatory` 人工复核的设计**。脚本仍然只做初判，强制条款判定必须由人确认。
- **没修 `og:image`**。缺图就是缺图。

---

<div align="center">

<sub>如果这个补丁让你少信一次"跑过了"，它就值得了。</sub>

</div>
