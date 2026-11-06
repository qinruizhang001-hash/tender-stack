# 标书工作台（workbench）

把 `adapter/` 下五个 CLI 步骤串成一条可运行、可观察、可人工复核的工作流。

**它不实现任何业务逻辑**：解析/提取交给 rag-tender（HTTP），扫描交给 tender-review-kit（子进程），
台账/门禁/校验交给 `adapter/*.py`（子进程 + 文件）。工作台只负责编排、状态、日志、人工回写。

## 启动

```bash
cd tender-stack
.venv/Scripts/python.exe -m uvicorn workbench.server:app --host 127.0.0.1 --port 8330
```

打开 http://127.0.0.1:8330 。右上角「启动服务」会自动拉起三个依赖进程
（rag-tender API:8000、确定性模型替身:8099、worker）。

## 界面

| 区域 | 作用 |
|---|---|
| 左栏 | 所有运行记录（`out/arm*` 与 `out/bench_*`），带条目数、页数、门禁记录级统计 |
| 步进条 | 提取 → 扫描 →（注入重提）→ 台账化 → 门禁 → 校验，实时状态与批次数 |
| 台账 | 474/35 条逐行查看，**状态下拉可直接改**（需复核 / 已确认 / 已驳回 / 待补证据），改动写回 `out/<arm>/review.json` |
| 门禁 | G0–G3 逐阶段，失败规则与说明 |
| 校验 | 15 列逐列 PASS/FAIL |
| 日志 | 当前任务的实时输出 |

深链：`?arm=armC_real#gate`（运行 + 标签页），`?new=1`（直接打开新建运行）。

## 新建一次运行

弹窗里选文件、选模型（确定性替身 / DeepSeek-flash）、勾选是否含「注入重提取」。

- 替身：11 页样本约 2 分钟，83 页标书约 3 分钟
- DeepSeek-flash：11 页约 8 分钟，83 页约 55 分钟（84 个批次，约 40 秒/批）

产物落在 `out/bench_<月日_时分秒>/`，结构与手工跑的 arm 完全一致：
`requirements.raw.json` / `blocks.json` / `scan/known_issues.json` / `ledger/` / `gated/` / `validate.json` / `job.json`。

## 依赖与数据

- 数据目录：`data/rt_bench/`（工作台专用，不污染 `rt_A`–`rt_D` 的历史实验）
- 服务日志：`workbench/logs/{api,worker,stub}.log`
- DeepSeek Key：读 `tender-stack/.env.local` 的 `DEEPSEEK_KEY`，不出现在命令行

## 踩过的坑（已内置处理）

| 现象 | 原因 | 处理 |
|---|---|---|
| worker 启动即崩 `table organizations already exists` | API 与 worker 抢着建表 | `start_worker` 先 `wait_api()` 等健康检查通过，失败重试 3 次 |
| 文档卡在 `waiting_api`，永不提取 | 没设 `TENDER_LIVE_MODEL_CALLS=true`，rag-tender 只标记不入库 | API 与 worker 都强制设为 true |
| 提取进度一直是 0 批 | 提取调用挂在 `process_document` 任务上 | 每 4 秒读一次 `workbench.db` 的 `metadata_json` 汇报批次 |
