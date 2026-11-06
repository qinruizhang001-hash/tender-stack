# 标书工作台（tender-stack）

把一份招标文件变成**可复核的要求台账**，再拿它逐条对照我方投标文件，输出覆盖矩阵。
卖的不是"AI 写标书"，是**提交前的漏项检查**。

## 它做什么

```
招标文件 PDF ──► 提取要求 ──► 15 列台账 ──► 门禁/校验 ──► 工作台逐条复核
                                     │
我方投标文件 PDF ────────────────────┴──► 覆盖矩阵（已响应 / 部分响应 / 负偏离 / 未响应 / 未找到）
```

- **要求台账**：每条带强度、响应方式、章节、页码、逐字原文，可回查。
- **判定**：数值预判（确定性）→ 双跑投票（模型）→ 规则复核，分歧一律降级人工。
- **可复现**：同一份材料重跑，结论逐字段一致（判定缓存，键含模型 + 提示词 + 第几票）。

## 目录

| 路径 | 作用 |
|---|---|
| `adapter/` | 提取、扫描、台账化、门禁、校验、标书比对、数值/时间/文件属性/漏项判定的脚本 |
| `workbench/` | FastAPI 编排 + 单页工作台（`static/index.html`） |
| `contract/` | 15 列台账契约、门禁规则、人工黄金集 |
| `tests/` | 投票与数值判定的回归测试 |
| `scripts/check.ps1` | 一键自检：Python 语法 → 前端脚本语法 → 单测 → 数值/时间自测 |
| `scripts/smoke.ps1` | 端到端冒烟：起真实服务 → HTTP 检查 → 浏览器确认渲染 |
| `scripts/install_hooks.ps1` | 安装 git 钩子：提交跑自检，推送跑完整冒烟 |
| `vendor/rag-tender/` | 上游提取服务（含本地修复，见 `adapter/patches/`） |

## 快速开始

```powershell
# 1. 起服务（工作台 8330 / 提取 API 8000 / 模型替身 8099）
.venv\Scripts\python.exe -m uvicorn workbench.server:app --host 127.0.0.1 --port 8330

# 2. 打开 http://127.0.0.1:8330 ，点「新建运行」

# 3. 自检
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check.ps1
```

详细流程与参数见 [RUNBOOK.md](RUNBOOK.md)；项目现状与质量数据见 [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md)；
三仓库组合验证见 [REPORT.md](REPORT.md)。

## 测试

```bash
powershell -File scripts\check.ps1   # Python 语法 + 前端语法 + 测试 + 两个自测
python -m pytest tests -q          # 165 条：投票、数值、时间、文件属性、漏项、实体、门禁、规则引擎、PDF、多标对比、批量任务、前端、冒烟
python tests/test_match_votes.py   # 无 pytest 环境亦可
python adapter/numeric_check.py    # 17 条数值比对自测
python adapter/time_check.py       # 18 条时间/日期比对自测
python adapter/file_meta_check.py 投标文件.pdf --json   # 物理属性校验（暗标泄露/修订记录）
```

CI：`.github/workflows/tests.yml`，push / PR 时跑同样的三步。

## 注意

- `.env.local`（模型密钥）与 `data/`、`out/` 不入库；克隆后需要自备密钥与标书文件。
- 判定结论是**辅助**，不构成法律意见；最终责任在投标人。
