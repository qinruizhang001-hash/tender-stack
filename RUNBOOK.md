# 复现手册

全部命令在 `tender-stack/` 目录下执行。Windows Git Bash + 项目内 venv。

> **只想跑一遍看结果**：不用手工起三个进程，直接用工作台 →
> `.venv/Scripts/python.exe -m uvicorn workbench.server:app --host 127.0.0.1 --port 8330`，
> 打开 http://127.0.0.1:8330 ，点右上「启动服务」→「＋ 新建运行」。
> 下面是等价的纯命令行路径，用于排查和脚本化。

## 0. 环境

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -i https://mirrors.aliyun.com/pypi/simple/ \
  --trusted-host mirrors.aliyun.com \
  "python-docx>=1.0" "pypdf>=4.0" "openpyxl>=3.1" tzdata \
  "aiosqlite>=0.20" "alembic>=1.15" "cryptography>=45" "fastapi>=0.115" \
  "httpx>=0.28" "pillow>=11" "pdfplumber>=0.11.10" "pydantic>=2.11" \
  "pydantic-settings>=2.8" "pypdfium2>=5" "python-multipart>=0.0.20" \
  "sqlalchemy[asyncio]>=2.0.38" "uvicorn>=0.34" "opencv-python-headless>=4.10"
```

> 两个必须补的依赖：`tzdata`（Windows 无 IANA 时区库，rag-tender 用 `ZoneInfo("Asia/Shanghai")` 会崩）、
> `opencv-python-headless`（扫描表格定位）。
> pypi.org 在本机不可达，必须用阿里云/清华镜像。

## 1. 取代码

`git clone` 在本机 TLS 握手失败，用 codeload：

```bash
for r in HunterLzap/rag-tender Jackon-Li/ai-bid-document matongAI-lab/tender-review-kit; do
  n=$(basename $r)
  curl -sL -o /tmp/$n.tgz "https://codeload.github.com/$r/tar.gz/refs/heads/main"
  mkdir -p vendor/$n && tar -xzf /tmp/$n.tgz -C vendor/$n --strip-components=1
done
```

## 2. 起服务（三进程）

```bash
# ① 模型替身（OpenAI 兼容）
STUB_PORT=8099 STUB_LOG="$PWD/out/stub_A.jsonl" .venv/Scripts/python.exe adapter/fake_model_server.py

# ② rag-tender API
cd vendor/rag-tender/backend
TENDER_LIVE_MODEL_CALLS=true \
TENDER_DATA_DIR="<abs>/tender-stack/data/rt_A" \
../../..//.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# ③ rag-tender worker（真正干活的是它）
TENDER_LIVE_MODEL_CALLS=true TENDER_DATA_DIR="<abs>/tender-stack/data/rt_A" \
../../..//.venv/Scripts/python.exe -m app.worker
```

B 臂额外加一个变量：

```bash
TENDER_PRESCAN_JSON="<abs>/tender-stack/out/prescan_real/known_issues.json"
```

> 环境变量是进程级、且被 `prescan_payload()` 缓存，**换文档必须重启 worker**。

## 3. 跑一份标书（A 臂）

```bash
.venv/Scripts/python.exe adapter/rt_client.py run \
  --file "data/tenders/北京怀柔-公园绿地养护-公开招标文件.pdf" \
  --out out/armA_real --project "北京怀柔-绿地养护"
```

产出：`requirements.raw.json`（含 block_id + quote）、`decisions.raw.json`、`blocks.json`、`meta.json`。

## 4. 台账化 + 门禁 + 校验

```bash
# 扫描层（A 臂也要跑，line_no 靠它反查；用 --reuse 复用已有扫描）
.venv/Scripts/python.exe adapter/scan_bridge.py \
  --file "data/tenders/北京怀柔-公园绿地养护-公开招标文件.pdf" \
  --run out/armA_real --out out/prescan_real

# JSON → 15 列台账（B 臂加 --prescan）
.venv/Scripts/python.exe adapter/map_to_ledger.py \
  --run out/armA_real --scan out/prescan_real/scan --out out/armA_real/ledger

# 六阶段门禁
.venv/Scripts/python.exe adapter/gate_engine.py \
  --ledger out/armA_real/ledger --run out/armA_real \
  --scan out/prescan_real/scan --out out/armA_real/gated

# 逐列校验
.venv/Scripts/python.exe adapter/validate_ledger.py \
  --ledger out/armA_real/gated --run out/armA_real --out out/armA_real/validate.json
```

## 5. 打补丁（B 臂前置）

```bash
# 应用
patch -p0 vendor/rag-tender/backend/app/services/extraction.py \
  < adapter/patches/rag-tender-prescan.patch

# 回滚
cp adapter/patches/extraction.py.orig vendor/rag-tender/backend/app/services/extraction.py
```

补丁不设置 `TENDER_PRESCAN_JSON` 时行为与上游一致，可安全保留。

## 6. 度量与对比

```bash
.venv/Scripts/python.exe adapter/measure.py --db data/rt_A/workbench.db --out out/measure_A.json
.venv/Scripts/python.exe adapter/measure.py --db data/rt_B/workbench.db --out out/measure_B.json

.venv/Scripts/python.exe adapter/compare.py \
  --a out/armA_real/gated --b out/armB_real/gated \
  --prescan out/prescan_real/known_issues.json \
  --measure-a out/measure_A.json --measure-b out/measure_B.json \
  --run-a <runA_id> --run-b <runB_id> \
  --out out/compare_real.json
```

`--run-a/--run-b` 取自各臂 `meta.json` 的 `run_id` 前 8 位。**不传会按条目数猜，容易错**（B 臂条目数与 run 内 requirement 数不一致）。

漏报率有两个口径，`compare.py` 同时输出：

- **主口径（三级判定）**：逐字引文 → 行号 → 最长公共子串(≥12 字)，任一级命中即算覆盖。真实模型会改写引文跨度，必须用这个。
- **strict**：纯逐字引文口径。仅用于说明"口径差异有多大"（同一份真实标书：32.6% vs 20.5%）。

## 6.1 真实模型（DeepSeek）专用步骤

换模型**只改 model-config**，管线不动：

```bash
set -a; . ./.env.local; set +a     # 里面是 DEEPSEEK_KEY=sk-...
DEEPSEEK_API_KEY="$DEEPSEEK_KEY" .venv/Scripts/python.exe adapter/rt_client.py run \
  --file "data/tenders/北京怀柔-公园绿地养护-公开招标文件.pdf" \
  --out out/armC_real --project "怀柔-真实模型" \
  --base http://127.0.0.1:8000 \
  --model-base https://api.deepseek.com/v1 --model deepseek-flash \
  --embed-base http://127.0.0.1:8099/v1 --key-env DEEPSEEK_API_KEY
```

要点：

- **密钥只走环境变量**，`--key-env` 传变量名，避免出现在命令行/日志。
- 长文档会触发 `ModelOutputTruncated`：`TENDER_EXTRACT_MAX_TOKENS=16384` 必须在**起 worker 时**设好。
- 83 页 / 84 批，实测约 55 分钟（≈40 秒/批）。

### 提取已完成、但队列任务失败过时

`create_run` 会被守卫拦下（`409 请等待项目招标文件和模板完成 AI 入库`），因为 `ai_status='failed'`。
提取结果在 `metadata_json` 里是全的，重排一次即可（命中缓存，零模型调用）：

```bash
cd vendor/rag-tender/backend && PYTHONPATH="$PWD" \
  TENDER_LIVE_MODEL_CALLS=true TENDER_DATA_DIR="<abs>/data/rt_C" \
  ../../../.venv/Scripts/python.exe ../../../adapter/requeue_document.py
```

然后对**已有文档**补一次 run 并导出（不重新上传）：

```bash
.venv/Scripts/python.exe adapter/rt_client.py export \
  --project-id <pid> --document-id <did> --out out/armC_real \
  --base http://127.0.0.1:8000 \
  --model-base https://api.deepseek.com/v1 --model deepseek-flash \
  --embed-base http://127.0.0.1:8099/v1 --key-env DEEPSEEK_API_KEY
```

### 单臂对黄金集

A/B 需要两臂；单臂也要一个绝对口径：

```bash
cd adapter && ../.venv/Scripts/python.exe golden_check.py \
  --ledger ../out/armC_real/gated \
  --prescan ../out/prescan_real/known_issues.json \
  --out ../out/golden_armC_real.json
```

### 直调提取的调用量归集

`requeue_document.py` / `dbg_extract.py` 这类直调产生的 `model_calls.job_id` 为空，
`measure.py` 无法通过 `jobs.payload` 归集，需要显式指定时间窗：

```bash
.venv/Scripts/python.exe adapter/measure.py --db data/rt_C/workbench.db \
  --out out/measure_C_real.json \
  --fallback-run <完整 run_id> --fallback-since "2026-11-02 14:51"
```

`--fallback-run` 必须是**完整 32 位 run_id**，传前 8 位不会报错但会静默失效。

## 7. 已知坑

| 现象 | 原因 | 处理 |
|---|---|---|
| `409 Conflict` 创建分析 | 文档 `ai_status` 还没到 ready | `rt_client.wait_document` 已同时等 parse + ai_status |
| `409 请等待项目招标文件和模板完成 AI 入库` | 队列任务失败过，`ai_status='failed'`，但提取其实已完成 | 跑 `adapter/requeue_document.py` 重排（命中缓存，零调用） |
| 解析失败 `numpy.int32` | 上游表格解析缺陷 | 换文档；或改上传 PDF 前先去表格 |
| `ZoneInfoNotFoundError` | Windows 缺 tzdata | 装 `tzdata` |
| 注入完全无效 | `block_id` 每次解析重生成 | 用 `by_quote`/`skip_texts` 文本键（已实现） |
| httpx 响应错位 | uvicorn + keep-alive | 客户端已设 `Connection: close` |
| docx 解析失败 | 走 LibreOffice 渲染，本机未装 | 用 PDF 版 |
| `ModelOutputTruncated` | 真实模型单批产出远超 8192 | `TENDER_EXTRACT_MAX_TOKENS=16384`（起 worker 时设） |
| `table organizations already exists` | API 与 worker 同时首启，`create_all` 竞争 | 重启 API 即可（表已建好） |
| 客户端 `wait_document` 超时但任务还在跑 | 单 worker 串行，前一个任务占着 | 别重启客户端，等 `ai_status` 落 `ready` 后走 `export` |
| `measure.py --fallback-run` 静默失效 | 传了前 8 位而不是完整 run_id | 用完整 32 位 id |
| 跨臂比对"漏报"虚高 | 真实模型改写引文跨度 | 用 `golden_check.py` / `compare.py` 的三级判定口径 |
| 比对全是"未找到"，切块只有 1~2 段 | 旧 `match_bid.py` 按 350 字机械硬切 + Jaccard 阈值 0.10，几乎必然低于阈值 | 已换 `adapter/bid_chunker.py`（PyMuPDF 段落块 + 句级打包）并把预筛改成 IDF 加权词覆盖率，阈值 0.16 |

## 8. 投标文件切块与比对（match 步骤）

切块在 `adapter/bid_chunker.py`，与 rag-tender 的解析解耦——它只吃 PDF，产出带页码/章节的语义段落。

```bash
# 自检：看后端、页数、段数、被清掉的页眉页脚行数
python adapter/bid_chunker.py 投标文件.pdf
# 只看预筛分数分布，不调模型（省钱）
python adapter/match_bid.py --ledger out/<arm>/gated --bid 投标文件.pdf --out out/<arm>/match --dry-run
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--chunk-target` | 900 | 单段目标字数；块不跨页，所以段数下限≈页数 |
| `--chunk-max` | 1400 | 单段硬上限，超长单句才在这里硬切 |
| `--top-k` | 3 | 每条要求取最相近的几段送模型 |
| `--min-score` | 0.16 | IDF 加权词覆盖率阈值，低于此值直接判"未找到"（不调模型） |
| `--backend` | 自动 | 有 PyMuPDF 用 `pymupdf`，否则退 `pdfplumber` |

实测（83 页真实招标文件）：PyMuPDF 后端 131 段、平均 415 字、清掉 254 行页眉页脚；
`pdfplumber` 兜底 141 段。扫描件页面标 `needs_ocr`，装了 `pytesseract` + `tesseract`
（`chi_sim`）会自动 OCR，没装则在 `chunk_stats.ocr_missing_pages` 里显式报出，
工作台匹配页顶部会有黄色告警，不再静默返回空结果。

### 8.1 确定性规则层（`adapter/verify_rules.py`）

模型只负责读懂语义，**是否达标由规则层拍板**。规则在模型给出结论之后运行，命中即改判：

| 规则 | 触发 | 结果 |
|---|---|---|
| 数值比较 | 要求与证据都出现同类数值（金额/尺寸/数量/期限/百分比） | 证据劣于要求 → **负偏离**（强制，模型无权保留"已响应"） |
| 资质一致性 | 要求 ISO13485、证据只有 ISO9001；要求《A证》证据是《B证》 | **未响应** |
| 无法判定 | 方向词缺失、同类数值多于一个、单位不可比 | **人工介入**，等审查员点确认 |

判定细节：
- 单位先归一化再比较（万元→元、英寸→25.4mm、个月→30天）
- 主体词取数值所在**小句**，且必须出现在数字**前面**——「副本3份」认副本，
  「少了一份副本」不认
- 日期按"前/后"判截止，`2026年11月6日` 晚于 `2026年11月5日17:00` → 负偏离
- 否定语境不算具备：「未提供 ISO13485」里的 ISO13485 不计入已有资质

状态口径（coverage.csv 的 `coverage` 列）：

| 状态 | 含义 | 进不进"需处理" |
|---|---|---|
| 已响应 | 有对应内容且规则核对通过 | 否 |
| 部分响应 | 提到了但不完整 | 否（但要改） |
| 负偏离 | 有回应但数值/资质不达标 | **是** |
| 未响应 | 资质不符或明确没做 | **是** |
| 未找到 | 预筛分低于阈值，没送模型 | **是** |
| 人工介入 | 规则判不了，等审查员确认 | 单独计 `pending_confirm` |

工作台匹配页每行带「判定依据」小字和「人工确认」下拉，选完即写 `match/review.json`，
刷新后统计自动重算（`/api/arms/{name}/match/review`）。

回归用例：`python tools/check_rules.py`（8 条，覆盖金额/尺寸/份数/日期/ISO/证照/无规则命中）。

### 8.2 人工确认的持久化

`match/review.json` 保存审查员的选择，**重跑不丢**：

- 每行有一个证据指纹 `sig` = `sha1(候选段落原文[:400] + 规则依据)`，只取确定性内容，
  不含模型自由发挥的引文——否则模型换个措辞就会误报"证据有变"。
- 重跑时按 `req_id` 套回：指纹一致 → 静默沿用（`reviewed=Y`）；
  指纹不一致 → **保留人工结论**并标 `review_stale=Y`，界面显示"已确认·证据有变"，提示复核。
- 台账里已删除的 `req_id` 不套用，计入 `review_dropped`。

```bash
# 重置某次比对的全部人工确认
rm out/<arm>/match/review.json

# 证据变更时自动作废旧确认、退回待确认（重新走人工）
python adapter/match_bid.py --ledger out/<arm>/gated --bid 投标文件.pdf --out out/<arm>/match --recheck-stale
```

`--recheck-stale` 的行为：指纹变了就**不沿用**旧结论，把该条改回「人工介入」重新排队，
旧确认记录移到 `review.json` 的 `_invalidated`（保留最近 200 条，带 `invalidated_at`），
summary 里记 `review_rechecked`。不加这个开关时，旧结论保留，只标 `review_stale`。

实测：确认后重跑 → `reviewed=1 / stale=0 / pending_confirm=0`；
改切块粒度（`--chunk-target 200`）重跑 → `review_stale=1`，状态仍是人工给的「已响应」；
同样改动再加 `--recheck-stale` → `review_rechecked=1 / pending_confirm=1`，
`review.json` 里 `R-002` 消失、`_invalidated` 多一条。

## 8. 判定策略（2026-11 起）

一条要求进模型前后，会经过三道闸；任何一道能给出确定结论，就不让模型"和稀泥"。

```
词面预筛（IDF 覆盖率 <0.16 直接判未找到）
   ↓
数值预判（确定性）
   · 只在「门槛式表述 + 有单位 + 数值前名词对上」时才判
   · 判负偏离 → 直接出结论，不调模型
   ↓
时间预判（确定性）
   · 要求与证据都要有时间（时长或日期），且 ≥2 个汉字二字组重合
   · 日期必须在同一语义组：「有效期至」不能拿「开标时间」来比
   · 判「时间不足 / 已逾期」→ 直接出结论，不调模型
   ↓
模型判定 ×2（双跑投票）
   · 两票一致 → 采信，votes 记 "已响应×2"
   · 两票不一致 → 降级「人工介入」，votes 记 "未响应|已响应"
   · 规则已强制改判的条目只跑一票（规则是确定性的，不需要投票）
   ↓
规则层复核（数值 / 时间 / 资质一致性，命中即强制改判）
```

关键参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--votes` | 2 | 1=单跑省钱；2=双跑投票（生产建议）；3=争议标段复核 |
| `--no-numeric-precheck` | 关 | 关闭规则预判（数值/时间/实体），用于 A/B 对比 |
| `--no-cache` | 关 | 禁用判定缓存 |
| `--cache-path` | `.cache/judge_cache.json` | 缓存位置 |
| `--precheck-max-rate` | 0.75 | 规则短路率上限，超过则告警（规则可能过松） |
| `--precheck-min-sample` | 50 | 低于这个条数不做零命中/高短路率告警 |
| `--fail-on-precheck-warn` | 关 | 告警时以退出码 3 结束，用于 CI 卡口 |

`summary.json` 里的 `precheck` 段落记录三道规则的命中数与健康度：

```json
"precheck": {
  "status": "warn", "total": 120, "short_circuit": 0, "rate": 0.0,
  "numeric": 0, "time": 0, "entity_hit": 0, "entity_missing": 0,
  "entity_candidates": 0,
  "warnings": ["120 条要求里三道规则一次都没命中，先确认解析与实体抽取是否失效"]
}
```

### 缓存为什么能保证"重跑一致"

缓存键 = `模型 + PROMPT + 要求 + 候选段 + 第几票`。

- 加**第几票**：否则第二次投票会命中第一次的缓存，永远"一致"，投票就失效了；
- 加 **PROMPT**：改提示词后旧结论自动失效，不会拿旧规则判新条款。

实测：同一份材料跑第二次，`model_calls=0 / cache_hits=6`，两次 `coverage.csv` 逐字段相同。

### 自检与测试

```bash
# 一键：语法 → 单元测试 → 数值自测
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check.ps1

# 只跑语法（秒级，适合 pre-commit）
powershell ... -File scripts\check.ps1 -SkipTests

# 单独跑测试（pytest 或纯 python 均可）
python -m pytest tests -q
python tests/test_match_votes.py
```

测试覆盖的行为（`tests/test_match_votes.py`，11 条）：

- 两票一致采信、证据取更长者
- 两票不一致 → `人工介入`，保留两票原文
- 规则已改判 → 只跑一次
- 第二次调用失败 → 不推翻第一次
- `--votes 1` 只调一次
- 数值预判：命中负偏离/已响应；分母不同、量词、不同主体、维度不同 → 一律不判

## 9. 规则引擎（rag-tender 后端拦截）

后端在 `app/services/rule_engine.py` 里调用同一套脚本，把确定性判定插在模型结论前后：

```
召回证据 → 规则引擎预判 ──fail──► 直接判未响应（不问模型）
                        │
                     pass/grey
                        ↓
                   模型判定
                        ↓
            grey + 模型给「满足」 → 降级「灰区」（NEEDS_REVIEW）
```

| 规则结论 | 后端动作 |
|---|---|
| `fail`（数值不足 / 时间逾期 / 资质漏项） | `DecisionStatus.NOT_MET`，summary 前缀 `【规则引擎命中：数值/时间/漏项】` |
| `grey`（方向不明、单位跨族、多处数值） | 模型若给「满足」则降级 `NEEDS_REVIEW`，summary 前缀 `【灰区】` |
| `pass` | 不影响模型结论 |

脚本定位：优先读环境变量 `BID_RULES_PATH`，否则用仓库内 `adapter/`（相对 `app/services/rule_engine.py` 上溯 5 层）。
加载不到时**不拦**，但返回 `available=false`，调用方应记日志——静默失效比误判更危险。

文档级检查（暗标泄露 / 修订记录）在**上传时**执行，结果写进 `document.metadata_json.meta_check`，不阻断上传。

已知边界：

- 只有"要求里出现**带单位的**数值"才做数值判定——`ISO13485` 里的 13485 是证号，不是数量。
- 只有"要求里出现时间"才做时间判定，否则"没有时间"会被误当成"判不了"，把正常条款全打成灰区。
- 规则命中 `fail` 时直接返回，`condition_results` 为空：这是刻意的，规则已判定不满足，不必再让模型解释。

### 规则不可用时不许静默降级

脚本目录不可达（部署漏打 `adapter/`、`BID_RULES_PATH` 写错）时，规则引擎不会拦任何东西。
如果不做标记，线上就会悄悄退化成纯模型判定，而报告上看不出异常。所以：

| 位置 | 表现 |
|---|---|
| 健康检查 | `GET /api/v2/health` 返回 `rules_loaded: false` |
| 后端日志 | 每个进程告警一次：`规则引擎未加载：… 比对将退化为纯模型判定` |
| 决策结论 | summary 追加 `（规则未加载：本结论仅来自模型）`，并写入 `rule_trace` |

排查顺序：先看 `/api/v2/health` 的 `rules_loaded`，再看 `BID_RULES_PATH` 是否指向含 `numeric_check.py` 的目录。





## 10. 同一份要求，同时审多份标书

「标书比对」不再绑定单份文件：一次可以上传多份，最多同时跑 2 份（模型限速），
结果按 `out/{arm}/match/{slug}/` 分目录存放，互不覆盖。

- 目录：`match/{文件名}-{短哈希}/coverage.csv | summary.json | review.json`
- 旧数据：`match/coverage.csv` 视为 `default` 批次，继续可读
- 接口：`GET /api/arms/{name}/bids`（批次列表）、`GET /api/arms/{name}/matrix`（横向对比）
- 页面：`多标对比` 标签页 —— 每条要求一行、每份标书一列，带页码与结论色块
- PDF 报告：多份时自动加一节「投标文件对比」表格

并发上限在 `MATCH_SLOTS = threading.Semaphore(2)`；调大只会让每份都变慢，
除非确认模型配额足够。
## 11. 一次提交多份招标文件

「新建运行」的文件选择支持多选：选中 N 份 PDF，工作台会逐份上传并各建一个运行，
最多同时跑 2 条（`PIPELINE_SLOTS = threading.Semaphore(2)`），其余显示为「排队中」。

- 接口：`POST /api/jobs/batch`，body `{files: [...], mode, inject, audit}`
- 命名：`bench_MMDD_HHMMSS_01`、`_02`…，同一批次不会撞名
- 队列条：顶部实时显示每份的当前阶段与进度（`3/5`），点击可切到那个运行
- 轮询：有任务在跑时每 3 秒刷新一次，全部结束后自动停

注意：扫描阶段依赖 rag-tender 与模型服务，服务没起会立刻失败（状态 `failed`，原因写在任务里）。
## 12. 冒烟：确认页面是真的活着

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke.ps1
powershell ... -File scripts\smoke.ps1 -SkipBrowser   # 只跑 HTTP
```

流程：起一个临时服务（`WORKBENCH_AUTOSTART=0`，不拉 rag-tender）→ 跑 `tests/test_smoke.py`
→ 用 ego-browser 打开页面，断言运行列表渲染出节点 → 关掉服务。

三层检查的分工：

| 层 | 抓什么 | 在哪 |
|---|---|---|
| 语法 | 脚本能不能解析 | `check_frontend.py` / `check.ps1` 第 2 步 |
| HTTP | 服务能不能起、接口字段在不在 | `tests/test_smoke.py` |
| 浏览器 | 脚本跑起来后页面有没有内容 | `scripts/smoke_ui.mjs` |

少了第三层，"id 改名 / 事件绑到 null"这类错误能一路通过前两层。
## 13. Git 钩子

```powershell
powershell -File scripts\install_hooks.ps1          # 安装
powershell -File scripts\install_hooks.ps1 -Force   # 覆盖
```

| 钩子 | 时机 | 内容 | 耗时 |
|---|---|---|---|
| `pre-commit` | 每次提交 | 语法 → 单测 → 数值/时间自测；改了 `workbench/` 再加跑 HTTP 冒烟 | 约 25 秒 |
| `pre-push` | 每次推送 | 完整冒烟（HTTP + 浏览器断言） | 约 20 秒 |

- 纯文档改动（`.md/.csv/.png` 等）自动跳过
- 自定义 hook 不会被覆盖，除非 `-Force`
- 应急绕过：`git commit --no-verify` / `git push --no-verify`
- hook 文件必须是 **LF + 无 BOM**，否则 Git for Windows 的 sh 找不到解释器（安装脚本已处理）

## 14. 扫描版 PDF 的 OCR

没有文字层的 PDF 页（扫描件、图片型附件）在切块前先补 OCR：

| 项 | 值 |
|---|---|
| 引擎 | RapidOCR（onnxruntime，离线） |
| 触发 | `page.needs_ocr`（页面文字少于阈值且有图） |
| 缓存 | `.cache/pdf_ocr/{页面指纹}.txt`，同一页重跑不再识别 |
| 上限 | `chunk_pdf(max_ocr_pages=60)`，超出计入 `ocr_limited_pages` |
| 统计 | `stats.ocr_done_pages` / `ocr_missing_pages` / `ocr_limited_pages` |

一页 A4 约 4 秒（首次还要加载模型，约 7 秒）；缓存命中后基本为 0。

文本层的表格仍走 pdfplumber/pymupdf 的结构化提取，OCR 只补"整页没有字"的情况——
OCR 出来的表格不带行列结构，硬当成表格用会比不用更糟。
## 15. 复杂表格解析

| 问题 | 处理 |
|---|---|
| 合并单元格 | PyMuPDF 会把合并区拆成「有值列 + 空列」，先删整列为空的列再拼行 |
| 无框线表格 | `find_tables()` 无结果时换 `strategy="text"`（按文字对齐）再试一次 |
| 跨页续表 | 相邻页、表头相同、列数相同 → 合成一块，保留 `pages: [1, 2]` |
| 重复文本 | 文字块中心落在表格 bbox 内则丢弃，表格块统一表达，避免同一内容进两次 |
| 空行噪音 | `text` 策略会插空行，连续重复行与全空行在归一化时去掉 |

对应实现：`_normalize_rows` / `merge_cross_page_tables` / `_inside_tables`（`adapter/bid_chunker.py`），
测试见 `tests/test_tables.py`。
表格块除了文本，还带一份结构化数据：

```json
{"header": ["序号", "项目", "金额"], "rows": [["1", "社保缴纳证明", "3万元"]]}
```

数值规则据此按列取值，而不是拿整行拼接去匹配：

```
要求：投标报价金额不低于5万元
表格：金额 | 3万元
→ 负偏离，证据「金额：3万元」
```

列头就是标签，所以表格取值跳过「名词上下文重合」那道门控——否则「金额」一个词
撞不满两个二字组，表格里的数永远判不出来。
表头带单位时（`金额（万元）`），裸数字单元格会自动补上单位再比较：

```
要求：报价不低于 50000 元
表格：金额（万元） → 3
→ 3 万元 = 30000 元 → 负偏离
```

单位换算由 `numeric_check` 负责（万元 → 元），表格这层只负责「把列头单位补到值上」。
表头还做了一层语义归一——同一类数的不同叫法归到一族：

| 语义族 | 表头写法 |
|---|---|
| 金额 | 金额 / 报价 / 投标价 / 总价 / 价格 / 价款 / 合同价 / 费用 / 单价 / 合价 / 限价 |
| 数量 | 数量 / 件数 / 个数 / 台数 / 套数 |
| 日期 | 日期 / 时间 / 期限 / 工期 / 交货期 / 交付期 / 服务期 |
| 比例 | 比例 / 比率 / 费率 / 百分比 / 折扣 |

要求写「报价」、表头写「投标总价」→ 同族命中，正常比较；
要求写「注册资本」、表头写「投标总价」→ 不同族、也不重合，直接放弃（不误比）。

## 16. Word（.docx）解析

比对入口接受 `.docx`，与 PDF 走同一套切块与规则：

| 项 | 做法 |
|---|---|
| 顺序 | 按 `document.body` 的子元素顺序读，段落与表格不会错位 |
| 表格 | 行列 → `header`/`rows` 结构，合并单元格的重复文字去重 |
| 页码 | Word 没有固定页，按约 2600 字/页折算，仅用于证据定位 |
| 后端标识 | `stats.backend == "docx"` |

`.doc`（旧二进制格式）不支持，会走 PDF 路径并报解析失败——需要先另存为 `.docx`。

## 17. 按标题层级切块 + 表格转 Markdown

`chunk_pdf` 默认走 `layout_parse`（`mode="sections"`），不再按固定字数切：

| 规则 | 说明 |
|---|---|
| 标题判定 | 编号模式（第X章 / 一、/（一）/1.1）+ 字号 ≥ 正文 1.12 倍 + 长度 ≤ 60 |
| 切块单位 | 一个章节（含其子条）一块；同级或更高级标题才开新块 |
| 超长处理 | 只在段落边界续块，续块沿用原章节路径，不切断句子 |
| 行合并 | PDF 把一句话拆成多行时先接回段落，再判断块边界 |
| 表格 | 一律转 GitHub 风格 Markdown；跨页表先合并再输出 |
| 结构保留 | chunk 同时带 `table`（header/rows），数值规则仍可按列取值 |
| 兜底 | 文字层太薄（扫描件）自动退回逐页解析 + OCR |

导出入库用 Markdown：

```bash
python adapter/layout_parse.py 招标文件.pdf 招标文件.md
```

实测（怀柔 83 页）：39 块、平均 1524 字、23 张表格、11 个章节。

## 18. OCR 后端与向量库导出

OCR 后端按可用性自动降级，也可用 `TENDER_OCR` 强制指定：

| 后端 | 位置 | 说明 |
|---|---|---|
| `paddle` | `.venv-paddle`（3.11 侧车） | PaddleOCR，行级检测 + 方向分类；主环境是 3.13，用子进程隔离 |
| `mineru` | 系统 CLI | 装了才有；无逐页接口时退回 paddle |
| `rapidocr` | 主环境 | 内置兜底，离线、轻量 |

侧车安装：

```powershell
uv pip install --python .venv-paddle\Scripts\python.exe paddlepaddle paddleocr
```

导出向量库入库文件：

```bash
python adapter/vector_export.py 招标文件.pdf out/chunks.jsonl
```

每行一条：`id / text / section / pages / kind / has_table / source / chars`，
`text` 里表格已是 Markdown，可直接送 embedding。

### 17.1 解析层硬化（试点版）

| 问题 | 处理 | 观测量 |
|---|---|---|
| 目录页混入正文 | 点线 ≥3 或「目录」标题+点线 → 整页跳过 | `stats.toc_pages` |
| 封面/版本行成为章节 | 只有带编号的标题才进章节栈；字号启发式仅标记「强调行」 | — |
| 页眉页脚 | bbox 上下各 5% 剔除 | — |
| 跨页表 | 结构化表头 NFKC 归一比较；次页无表头视为续表 | `pages` |
| 单元格标点错位 | 按 `(y, x)` 重排单元格文字；连续标点压缩 | `stats.table_suspect`、`quality=low` |
| OCR 降级 | 不静默：`logging.warning` + `stats.ocr_fallback[{from,to,reason,page}]`；`TENDER_OCR_STRICT=1` 直接抛错 | `stats.ocr_fallback` |

条款级导出（台账/检索主键）：

```bash
python adapter/vector_export.py 招标文件.pdf out/req.jsonl      # requirement 级
python adapter/vector_export.py 招标文件.pdf out/req.jsonl --both  # 同时出 sections
```

评估钩子：

```bash
python scripts/eval_chunks.py gold.json out/req.jsonl
```

### 17.2 条款级金标（20 条）

`contract/gold_clauses_20.json`：从怀柔标书原文逐条摘录 20 条完整条款（含页号），
用于回归「条款是否被切碎/丢失」。

```bash
python adapter/vector_export.py 招标文件.pdf out/req.jsonl
python scripts/eval_chunks.py contract/gold_clauses_20.json out/req.jsonl
```

口径：

| 指标 | 含义 |
|---|---|
| `exact` | 整条条款完整落在单条 requirement 里 |
| `covered` | 条款 4-gram 覆盖率 ≥ 0.8（允许被切到两条、标点差异） |
| `recall` | covered / total，回归门禁看这个（当前 0.95） |

`exact` 偏低是正常的：条款按编号拆条后，编号与正文分开存放，整句不会原样落在一条里。

门禁已接入 pre-push（`scripts/pre_push.ps1`），也可单独跑：

```bash
python scripts/gold_gate.py                 # recall ≥ 0.90 才通过，退出码 1 拦截
python scripts/gold_gate.py --threshold 0.85
```

## 19. 第四轮：试点商用缺口

### 19.1 开关矩阵（默认全关）

| 开关 | 默认 | 作用 | 依赖缺失时 |
|---|---|---|---|
| `TENDER_LAYOUT` | `0` | 版面检测（toc/table 框） | `stats.layout_fallback="not_installed"`，走规则 |
| `TENDER_TABLE_MODEL` | `0` | 表结构校验列对齐 | 返回 None，保持规则合并结论 |
| `TENDER_VLM_OCR` | `0` | 难页 VLM OCR | 不启用，RapidOCR 兜底 |

### 19.2 表行进 requirement

条款型表行（编号 / ★ / 必须 / 否决 / 废标 / 勾选框 □■☑）独立成条：

- `kind="table_row"`，带 `table_header`、`row_index`、`table_header`；
- 纯数字矩阵（报价表）不拆，整表留作 section 证据；
- `display_text = "{number}. {text}"`（无编号时等于 `text`）。

### 19.3 多文档金标门禁

```bash
python scripts/gold_gate.py          # contract/gold/index.json：两份标书
```

| 阈值 | 默认 |
|---|---|
| 总 recall（加权） | 0.90 |
| 最差单文档 recall | 0.85 |
| 风险条款漏检（废标/否决/必须/★） | 直接失败 |

### 19.4 健康检查

```bash
python scripts/healthcheck.py        # OCR 预热失败 → 退出码 1
```

## 20. 案例知识沉淀与受控迭代

分层职责见 `docs/LAYERS.md`；卡片 schema 见 `kb/schema.json`。

### 20.1 吸收（自动只写 inbox）

```bash
python -m scripts.kb_absorb \
  --export out/req.jsonl --gold contract/gold/huairou_service.json \
  --doc-type service --pdf data/tenders/xxx.pdf --out kb/inbox
```

输出（含 `kb_absorbed_count`）：

```json
{"absorbed": 11, "kb_absorbed_count": 11, "candidates": 11, "duplicates": 0, "sealed": false,
 "source_doc_hash": "1a4e42cf..."}
```

来源：金标 miss（covered<0.8）+ `quality=low` 的 requirement。卡片状态一律 `draft`。

### 20.2 检索（补偿层只读）

```bash
python -m scripts.kb_query --doc-type service --keywords 保证金
```

开关 `TENDER_KB_RETRIEVE`（默认 1）；索引缺失/损坏时返回 `items: []` 并给 `fallback`，不影响主流程。

### 20.3 受控晋升（唯一进化入口）

```bash
python -m scripts.kb_promote --card <case_id> --dry-run     # 生成 kb/proposed/
python -m scripts.kb_promote --card <case_id> --approve --by 张三
python -m scripts.kb_promote --card <case_id> --reject --reason "误报"
```

硬性规则：

- 必须有 `linked_test`，且该测试函数在 `tests/` 里存在，否则拒绝；
- 默认跑 `gold_gate`（`--no-gate` 仅调试用）；
- 金标类批准后写 `contract/gold/absorbed/`，**不自动加入 index.json**，需人工决定是否参与门禁；
- 规则类只生成 `kb/proposed/*.json` 草稿，**不改 layout_parse**。

### 20.4 sealed

`kb/sealed/index.json` 里的 `doc_hashes` 禁止被自动吸收或晋升改写。

### 20.5 sealed 基线与回归

`kb/sealed/index.json` 的 `gold_files` 指向 sealed 金标子集（当前两份各 10 条）：

```bash
python -m scripts.kb_sealed --init    # 写基线 kb/sealed/baseline.json
python -m scripts.kb_sealed --check   # 与基线比较，下降退出码 1
```

`kb_promote --approve` 会先算 sealed recall，低于基线直接拒绝（`stage=sealed_regression`）。
