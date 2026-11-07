"""标书工作台 —— 把五个 CLI 步骤串成可运行、可观察、可人工复核的工作流。

设计约束（沿用整个项目的前提）：
- 只通过**子进程 + 文件**调用 adapter/*.py，不 import 任何业务模块；
- 工作台自己不实现解析、提取、门禁，只做编排、状态、日志、人工回写。

启动：
    .venv/Scripts/python.exe -m uvicorn workbench.server:app --host 127.0.0.1 --port 8330
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from workbench.report import build_report

ROOT = Path(__file__).resolve().parent.parent
WORKBENCH = Path(__file__).resolve().parent
OUT = ROOT / "out"
DATA = ROOT / "data"
TENDERS = DATA / "tenders"
SAMPLES = ROOT / "vendor/rag-tender/samples"
PY = sys.executable

API_PORT = 8000
STUB_PORT = 8099

def keeper(stop: threading.Event) -> None:
    """守护线程：依赖进程随父进程死，这里负责把它们拉回来。

    只在没有任务运行时自愈，避免把正在提取的 worker 掐掉。
    """
    while not stop.is_set():
        try:
            running = any(job.get("status") == "running" for job in list(JOBS.values()))
            if not running:
                if not port_open(STUB_PORT):
                    start_stub()
                if not port_open(API_PORT):
                    start_api()
                    wait_api()
                worker = SERVICES.get("worker")
                if worker is None or worker.poll() is not None:
                    start_worker("", "stub")
        except Exception:  # noqa: BLE001
            pass
        stop.wait(20)


@asynccontextmanager
async def lifespan(_: FastAPI):
    stop = threading.Event()
    if os.environ.get("WORKBENCH_AUTOSTART", "1") != "0":
        threading.Thread(target=keeper, args=(stop,), daemon=True).start()
    yield
    stop.set()


app = FastAPI(title="标书工作台", lifespan=lifespan)

# ------------------------------------------------------------------ helpers


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.35)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def load_env_file() -> dict[str, str]:
    path = ROOT / ".env.local"
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def base_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT)
    env.pop("TENDER_PRESCAN_JSON", None)
    return env


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ------------------------------------------------------------------ services

SERVICES: dict[str, subprocess.Popen] = {}
SERVICE_LOG = ROOT / "workbench" / "logs"
SERVICE_LOG.mkdir(parents=True, exist_ok=True)


def _spawn(name: str, cmd: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen:
    log_path = SERVICE_LOG / f"{name}.log"
    handle = log_path.open("a", encoding="utf-8")
    handle.write(f"\n=== {now()} start: {' '.join(cmd)}\n")
    handle.flush()
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), env=env, stdout=handle, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    SERVICES[name] = proc
    return proc


def start_stub() -> str:
    if port_open(STUB_PORT):
        return "已在运行"
    env = base_env()
    env["STUB_PORT"] = str(STUB_PORT)
    _spawn("stub", [PY, "adapter/fake_model_server.py"], ROOT, env)
    return "启动中"


def start_api() -> str:
    if port_open(API_PORT):
        return "已在运行"
    env = base_env()
    env["TENDER_DATA_DIR"] = str(DATA / "rt_bench")
    # 没有这个开关，rag-tender 只会把文档标成 waiting_api，不入库、不提取
    env["TENDER_LIVE_MODEL_CALLS"] = "true"
    backend = ROOT / "vendor/rag-tender/backend"
    env["PYTHONPATH"] = str(backend)
    _spawn(
        "api",
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(API_PORT)],
        backend,
        env,
    )
    return "启动中"


def wait_api(timeout: float = 45.0) -> bool:
    """等 API 真正可用（建表完成），否则 worker 会和它抢着建表。"""
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/v2/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0)
    return False


def _kill_processes(pattern: str) -> int:
    """按命令行特征清理残留进程（Windows）。

    工作台每跑一次就 spawn 一个 worker，旧进程没人回收；残留进程会一直握着
    旧代码和数据库连接，导致「改了代码不生效」「数据库被锁」这类假故障。
    """
    if os.name != "nt":
        return 0
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        f"Where-Object {{ $_.CommandLine -match '{pattern}' }} | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:  # noqa: BLE001
        return 0
    killed = 0
    for line in (result.stdout or "").split():
        if line.strip().isdigit():
            subprocess.run(["taskkill", "/PID", line.strip(), "/F"], capture_output=True)
            killed += 1
    return killed


def _worker_pids() -> list[int]:
    """当前存活的 app.worker 进程。"""
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -match 'app\\.worker' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:  # noqa: BLE001
        return []
    return [int(line) for line in (result.stdout or "").split() if line.strip().isdigit()]


def start_worker(prescan: str = "", mode: str = "stub") -> str:
    if "worker" in SERVICES and SERVICES["worker"].poll() is None:
        SERVICES["worker"].terminate()
        try:
            SERVICES["worker"].wait(timeout=8)
        except subprocess.TimeoutExpired:
            SERVICES["worker"].kill()
        time.sleep(0.6)
    stale = _kill_processes("app\\.worker")
    if stale:
        time.sleep(1.0)

    wait_api()
    env = base_env()
    env["TENDER_DATA_DIR"] = str(DATA / "rt_bench")
    env["TENDER_EXTRACT_MAX_TOKENS"] = "16384"
    env["TENDER_LIVE_MODEL_CALLS"] = "true"
    backend = ROOT / "vendor/rag-tender/backend"
    env["PYTHONPATH"] = str(backend)
    if prescan:
        env["TENDER_PRESCAN_JSON"] = prescan

    # 只 spawn 一次：Windows 上 python.exe 可能是快速退出的包装进程，
    # 用 poll() 判断会误以为失败而重复拉起，最后两个 worker 抢同一个库。
    _spawn("worker", [PY, "-m", "app.worker"], backend, env)
    time.sleep(4.0)
    return "运行中" if _worker_pids() else "启动失败"


def service_state() -> dict:
    return {
        "api": port_open(API_PORT),
        "model": port_open(STUB_PORT),
        "worker": bool(SERVICES.get("worker") and SERVICES["worker"].poll() is None),
    }


# ------------------------------------------------------------------ arms


def arm_dir(name: str) -> Path:
    path = (OUT / name).resolve()
    if OUT.resolve() not in path.parents:
        raise HTTPException(400, "非法路径")
    return path


def ledger_path(name: str) -> Path | None:
    for candidate in (OUT / name / "gated/requirements.csv", OUT / name / "ledger/requirements.csv"):
        if candidate.exists():
            return candidate
    return None


def read_provenance(folder: Path) -> dict[str, dict]:
    """台账旁挂的 provenance.jsonl：页码 / 块号 / 提取方式 / 置信度。

    这些字段不进 15 列契约，只在这里 join 给界面用。
    """
    path = folder / "requirements.provenance.jsonl"
    if not path.exists():
        return {}
    result: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("req_id"):
            result[item["req_id"]] = item
    return result


def read_rows(name: str) -> list[dict]:
    path = ledger_path(name)
    if not path:
        return []
    with path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    overlay = read_json(OUT / name / "review.json") or {}
    for row in rows:
        patch = overlay.get(row["req_id"])
        if patch:
            row["_review"] = patch
            row["status"] = patch.get("status") or row["status"]
            if patch.get("owner"):
                row["owner"] = patch["owner"]
    provenance = read_provenance(path.parent)
    for row in rows:
        item = provenance.get(row["req_id"]) or {}
        page = item.get("page_index")
        row["page"] = page + 1 if isinstance(page, int) else ""
        row["block_id"] = item.get("block_id") or ""
        row["via"] = item.get("extraction_method") or ""
        confidence = item.get("confidence")
        row["confidence"] = "" if confidence is None else round(float(confidence), 2)
    return rows


def gate_summary(name: str) -> dict:
    report = read_json(OUT / name / "gated/gate_report.json")
    if not report:
        return {}
    stages = []
    for stage in report.get("stages", []):
        checks = stage.get("checks") or []
        failed = [item for item in checks if not item.get("pass")]
        stages.append(
            {
                "gate": stage.get("gate"),
                "passed": stage.get("passed"),
                "blockers": sum(1 for item in failed if item.get("severity") == "BLOCKER"),
                "warns": sum(1 for item in failed if item.get("severity") == "WARN"),
                "failed": [
                    {"rule": item.get("rule"), "severity": item.get("severity"), "detail": item.get("detail")}
                    for item in failed
                ],
            }
        )
    return {
        "overall": report.get("overall_passed"),
        "records": report.get("records"),
        "stages": stages,
    }


def validate_summary(name: str) -> dict:
    report = read_json(OUT / name / "validate.json")
    if not report:
        return {}
    return {
        "passed": report.get("passed"),
        "total": report.get("total_records"),
        "failed": (report.get("failed") or [])[:8],
        "failed_count": len(report.get("failed") or []),
        "checks": report.get("checks") or report.get("columns") or [],
    }


def arm_meta(name: str) -> dict:
    folder = OUT / name
    meta = read_json(folder / "meta.json") or {}
    job = read_json(folder / "job.json") or {}
    rows = read_rows(name)
    gate = gate_summary(name)
    validate = validate_summary(name)
    label = meta.get("label") or job.get("label") or {
        "armA_real": "真实标书 · 替身（仅模型）",
        "armB_real": "真实标书 · 替身 + 扫描",
        "armC_real": "真实标书 · DeepSeek",
        "armA_sample": "样本 · 替身（仅模型）",
        "armB_sample": "样本 · 替身 + 扫描",
        "armC_sample": "样本 · DeepSeek（仅模型）",
        "armD_sample": "样本 · DeepSeek + 扫描",
        "armD2_sample": "样本 · DeepSeek + 扫描（复跑）",
    }.get(name, name)
    gate_records = gate.get("records") or {}
    return {
        "name": name,
        "label": label,
        "file": meta.get("document_filename") or meta.get("input_file") or "",
        "pages": meta.get("page_count"),
        "run_id": (meta.get("run_id") or "")[:8],
        "requirements": len(rows) or meta.get("n_requirements") or 0,
        "gate_overall": gate.get("overall"),
        "blockers": gate_records.get("blocker", sum(stage["blockers"] for stage in gate.get("stages", []))),
        "warns": gate_records.get("warn", sum(stage["warns"] for stage in gate.get("stages", []))),
        "rule_warns": sum(stage["warns"] for stage in gate.get("stages", [])),
        "validate_passed": validate.get("passed"),
        "has_gate": bool(gate),
        "mtime": folder.stat().st_mtime if folder.exists() else 0,
        "bench": bool(read_json(folder / "job.json")),
    }


def list_arms() -> list[dict]:
    arms = []
    for folder in sorted(OUT.iterdir() if OUT.exists() else []):
        if not folder.is_dir() or not folder.name.startswith(("arm", "bench")):
            continue
        if not (folder / "meta.json").exists() and not (folder / "gated").exists() and not (folder / "job.json").exists():
            continue
        arms.append(arm_meta(folder.name))
    arms.sort(key=lambda item: item["mtime"], reverse=True)
    return arms


# ------------------------------------------------------------------ documents


def list_documents() -> list[dict]:
    docs = []
    for folder, tag in ((TENDERS, "真实标书"), (TENDERS / "imported", "导入的标书"), (SAMPLES, "自带样本")):
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.pdf")):
            docs.append(
                {
                    "path": str(path),
                    "name": path.name,
                    "size_mb": round(path.stat().st_size / 1048576, 2),
                    "group": tag,
                }
            )
    return docs


# ------------------------------------------------------------------ jobs

STAGE_DEFS = [
    ("extract", "模型提取", "rag-tender /api/v2 → requirements.raw.json + blocks.json"),
    ("scan", "确定性扫描", "tender-review-kit → hits.json / lines.txt → known_issues.json"),
    ("audit", "审计补漏", "逐页问模型「还缺哪些条款」→ audit_candidates.json（第三通道）"),
    ("inject", "注入重提取", "带 known_issues 再提一次（可选）"),
    ("map", "台账化", "15 列 requirements.csv + provenance.jsonl"),
    ("kb", "知识沉淀", "requirement 导出 → kb/inbox（失败不阻断审查）"),
    ("gate", "六阶段门禁", "G0–G3 分级 + 回写 status"),
    ("validate", "逐列校验", "列结构 / 枚举 / quote 逐字回查"),
]

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
# 同时最多跑 2 条流水线：再多只会互相抢 OCR 与模型配额
PIPELINE_SLOTS = threading.Semaphore(2)


def stage_list(inject: bool, audit: bool = False) -> list[dict]:
    stages = []
    for key, name, desc in STAGE_DEFS:
        if key == "inject" and not inject:
            continue
        if key == "audit" and not audit:
            continue
        stages.append(
            {"key": key, "name": name, "desc": desc, "state": "pending", "lines": [], "started": None, "ended": None, "progress": ""}
        )
    return stages


def push_line(job: dict, stage: dict, line: str) -> None:
    line = line.rstrip("\r\n")
    if not line:
        return
    stage["lines"].append(line)
    if len(stage["lines"]) > 400:
        del stage["lines"][:100]
    job["log"].append(f"[{stage['key']}] {line}")
    if len(job["log"]) > 800:
        del job["log"][:200]


def run_command(job: dict, stage: dict, cmd: list[str], env: dict[str, str], cwd: Path | None = None) -> int:
    push_line(job, stage, f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd or ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        push_line(job, stage, line)
    return proc.wait()


def extract_progress(job: dict, stage: dict, stop: threading.Event) -> None:
    """提取期间从 rag-tender 的 SQLite 读批次进度。"""
    db = DATA / "rt_bench/workbench.db"
    while not stop.is_set():
        try:
            import sqlite3

            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            row = conn.execute(
                "SELECT metadata_json FROM documents ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            conn.close()
            if row and row[0]:
                meta = json.loads(row[0])
                batches = len(meta.get("requirement_batches") or [])
                entries = len(meta.get("extracted_requirements") or [])
                stage["progress"] = f"{batches} 批 / {entries} 条"
        except Exception:
            pass
        stop.wait(4.0)


def execute(job: dict) -> None:
    """排队 → 执行。并发上限由 PIPELINE_SLOTS 控制，其余任务保持 queued。"""
    job["status"] = "queued"
    with PIPELINE_SLOTS:
        job["status"] = "running"
        _execute(job)


def _execute(job: dict) -> None:
    name = job["arm"]
    folder = OUT / name
    folder.mkdir(parents=True, exist_ok=True)
    file_path = job["file"]
    env = base_env()

    if job["mode"] == "real":
        secrets = load_env_file()
        key = secrets.get("DEEPSEEK_KEY") or secrets.get("DEEPSEEK_API_KEY") or ""
        env["DEEPSEEK_API_KEY"] = key
        model_base, model_name = "https://api.deepseek.com/v1", "deepseek-flash"
    else:
        model_base, model_name = f"http://127.0.0.1:{STUB_PORT}/v1", "stub-extractor-v1"

    prescan_path = str(folder / "scan/known_issues.json")

    def extract_cmd() -> list[str]:
        return [
            PY, "adapter/rt_client.py", "run",
            "--file", file_path,
            "--out", str(folder),
            "--project", job["label"],
            "--base", f"http://127.0.0.1:{API_PORT}",
            "--model-base", model_base,
            "--model", model_name,
            "--embed-base", f"http://127.0.0.1:{STUB_PORT}/v1",
            "--key-env", "DEEPSEEK_API_KEY" if job["mode"] == "real" else "",
        ]

    for stage in job["stages"]:
        key = stage["key"]
        stage["state"] = "running"
        stage["started"] = now()
        job["current"] = key
        code = 0
        stop = threading.Event()
        watcher = None

        try:
            if key == "extract":
                if not port_open(API_PORT):
                    push_line(job, stage, "! rag-tender API 未启动，请先在顶部点「启动服务」")
                    code = 1
                else:
                    status = start_worker("", job["mode"])
                    push_line(job, stage, f"· worker {status}")
                    if status == "启动失败":
                        code = 1
                    else:
                        watcher = threading.Thread(target=extract_progress, args=(job, stage, stop), daemon=True)
                        watcher.start()
                        code = run_command(job, stage, extract_cmd(), env)

            elif key == "scan":
                code = run_command(
                    job,
                    stage,
                    [PY, "adapter/scan_bridge.py", "--file", file_path, "--run", str(folder), "--out", str(folder)],
                    env,
                )

            elif key == "audit":
                code = run_command(
                    job,
                    stage,
                    [PY, "adapter/audit_pass.py", "--run", str(folder), "--workers", "6"],
                    env,
                )

            elif key == "inject":
                status = start_worker(prescan_path, job["mode"])
                push_line(job, stage, f"· worker {status}（TENDER_PRESCAN_JSON 已注入）")
                if status == "启动失败":
                    code = 1
                else:
                    watcher = threading.Thread(target=extract_progress, args=(job, stage, stop), daemon=True)
                    watcher.start()
                    code = run_command(job, stage, extract_cmd(), env)

            elif key == "map":
                code = run_command(
                    job,
                    stage,
                    [
                        PY, "adapter/map_to_ledger.py",
                        "--run", str(folder),
                        "--scan", str(folder / "scan"),
                        "--prescan", prescan_path if job["inject"] else "",
                        "--audit", str(folder / "audit/audit_candidates.json") if job.get("audit") else "",
                        "--out", str(folder / "ledger"),
                    ],
                    env,
                )

            elif key == "kb":
                kb_result = run_command(
                    job,
                    stage,
                    [PY, "scripts/kb_stage.py", "--pdf", file_path, "--out", str(folder / "kb")],
                    env,
                )
                # 知识沉淀失败只留痕，不把整单审查判失败
                code = 0
                push_line(job, stage, f"· kb stage exit={kb_result}")

            elif key == "gate":
                code = run_command(
                    job,
                    stage,
                    [
                        PY, "adapter/gate_engine.py",
                        "--ledger", str(folder / "ledger"),
                        "--run", str(folder),
                        "--scan", str(folder / "scan"),
                        "--out", str(folder / "gated"),
                    ],
                    env,
                )

            elif key == "validate":
                code = run_command(
                    job,
                    stage,
                    [
                        PY, "adapter/validate_ledger.py",
                        "--ledger", str(folder / "gated"),
                        "--run", str(folder),
                        "--out", str(folder / "validate.json"),
                    ],
                    env,
                )
        except Exception as error:  # noqa: BLE001
            push_line(job, stage, f"! {type(error).__name__}: {error}")
            code = 1
        finally:
            stop.set()

        stage["ended"] = now()
        stage["state"] = "done" if code == 0 else "failed"
        if code != 0:
            job["status"] = "failed"
            job["error"] = f"{stage['name']} 退出码 {code}"
            (folder / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            return

    job["status"] = "done"
    job["current"] = ""
    job["finished"] = now()
    (folder / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


def start_job(payload: dict) -> dict:
    file_path = payload.get("file") or ""
    if not Path(file_path).exists():
        raise HTTPException(400, "文件不存在")
    mode = payload.get("mode") or "stub"
    inject = bool(payload.get("inject"))
    audit = bool(payload.get("audit"))
    label = payload.get("label") or Path(file_path).stem
    name = payload.get("arm") or f"bench_{datetime.now().strftime('%m%d_%H%M%S')}"

    job = {
        "id": uuid.uuid4().hex[:12],
        "arm": name,
        "label": label,
        "file": file_path,
        "mode": mode,
        "inject": inject,
        "audit": audit,
        "status": "running",
        "created": now(),
        "finished": "",
        "current": "",
        "error": "",
        "log": [],
        "stages": stage_list(inject, audit),
    }
    with JOBS_LOCK:
        JOBS[job["id"]] = job
    threading.Thread(target=execute, args=(job,), daemon=True).start()
    return {"id": job["id"], "arm": name}


# ------------------------------------------------------------------ api


def file_report(path: Path) -> dict:
    """调用文件物理属性校验（暗标泄露 / 修订记录）。"""
    import sys as _sys

    adapter = str(ROOT / "adapter")
    if adapter not in _sys.path:
        _sys.path.insert(0, adapter)
    try:
        from file_meta_check import check_file
    except ImportError:  # noqa: BLE001
        return {"status": "需人工复核", "details": ["校验模块不可用"], "leaks": []}
    return check_file(path).as_dict()


def _allowed_doc(path: Path) -> bool:
    """只允许检查 data/tenders 与自带样本目录内的文件，避免任意路径读取。"""
    target = path.resolve()
    for root in (TENDERS.resolve(), SAMPLES.resolve()):
        if target == root or root in target.parents:
            return True
    return False


@app.get("/api/filecheck")
def api_filecheck(path: str = ""):
    target = Path(path)
    if not path or not _allowed_doc(target):
        raise HTTPException(400, "只允许检查 data/tenders 或自带样本目录内的文件")
    if not target.exists():
        raise HTTPException(404, "文件不存在")
    return file_report(target)


@app.post("/api/upload")
async def api_upload(request: Request, name: str = ""):
    """手动导入招标文件：请求体是 PDF 原始字节，文件名走 query（支持中文）。"""
    raw = await request.body()
    filename = Path(name or "").name
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "只支持 PDF 文件")
    if not raw:
        raise HTTPException(400, "空文件")
    TENDERS.mkdir(parents=True, exist_ok=True)
    imported = TENDERS / "imported"
    imported.mkdir(parents=True, exist_ok=True)
    target = imported / filename
    index = 1
    while target.exists():
        target = imported / f"{Path(filename).stem}-{index}.pdf"
        index += 1
    target.write_bytes(raw)
    return {
        "path": str(target),
        "name": target.name,
        "size_mb": round(len(raw) / 1048576, 2),
        "group": "导入的标书",
        "meta_check": file_report(target),
    }


BIDS = DATA / "bids"
# arm → slug → {status, bid, error}；同一份招标要求可以挂多份投标文件
MATCH_STATE: dict[str, dict[str, dict]] = {}
# 同时最多跑 2 份：模型接口有速率限制，开太多只会一起变慢
MATCH_SLOTS = threading.Semaphore(2)


def bid_slug(filename: str) -> str:
    """稳定、可读、无冲突的批内标识：文件名 + 短哈希。"""
    stem = Path(filename).stem.strip() or "bid"
    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", stem)[:36].strip("-") or "bid"
    digest = hashlib.sha1(filename.encode("utf-8")).hexdigest()[:6]
    return f"{safe}-{digest}"


def match_dirs(arm: str) -> list[dict]:
    """列出一个运行下所有比对批次（含旧格式的单份比对目录）。"""
    base = OUT / arm / "match"
    items: list[dict] = []
    if base.exists() and ((base / "coverage.csv").exists() or (base / "summary.json").exists()):
        items.append({"slug": "default", "dir": base, "name": _bid_name(base) or "默认比对"})
    if base.exists():
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            if not (
                (child / "coverage.csv").exists()
                or (child / "summary.json").exists()
                or (child / "match.log").exists()
            ):
                continue
            items.append({"slug": child.name, "dir": child, "name": _bid_name(child) or child.name})
    return items


def _bid_name(folder: Path) -> str:
    summary = read_json(folder / "summary.json")
    if isinstance(summary, dict) and summary.get("bid"):
        return Path(summary["bid"]).name
    return ""


def _run_match(arm: str, bid_path: Path, out_dir: Path, slug: str) -> None:
    states = MATCH_STATE.setdefault(arm, {})
    states[slug] = {"status": "queued", "bid": bid_path.name}
    try:
        with MATCH_SLOTS:
            states[slug] = {"status": "running", "bid": bid_path.name}
            proc = subprocess.run(
                [
                    PY, "adapter/match_bid.py",
                    "--ledger", str(OUT / arm / "gated"),
                    "--bid", str(bid_path),
                    "--out", str(out_dir),
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "match.log").write_text((proc.stdout or "") + "\n" + (proc.stderr or ""), encoding="utf-8")

        states[slug] = {
            "status": "done" if proc.returncode == 0 else "failed",
            "bid": bid_path.name,
            "error": "" if proc.returncode == 0 else (proc.stderr or "")[-300:],
        }
    except Exception as error:  # noqa: BLE001
        states[slug] = {"status": "failed", "bid": bid_path.name, "error": f"{type(error).__name__}: {error}"}


@app.post("/api/arms/{name}/match")
async def api_match(name: str, request: Request, filename: str = "", slug: str = ""):
    """上传一份投标文件，与要求清单做覆盖比对；同一运行可挂多份。"""
    folder = arm_dir(name)
    if not (folder / "gated" / "requirements.csv").exists():
        raise HTTPException(400, "这份运行还没有 gated 台账，先跑完流水线")
    raw = await request.body()
    filename = Path(filename or "").name
    if not filename.lower().endswith(".pdf") or not raw:
        raise HTTPException(400, "只支持 PDF，且不能为空")
    slug = slug or bid_slug(filename)
    bid_dir = BIDS / name
    bid_dir.mkdir(parents=True, exist_ok=True)
    target = bid_dir / filename
    target.write_bytes(raw)
    out_dir = folder / "match" / slug
    threading.Thread(target=_run_match, args=(name, target, out_dir, slug), daemon=True).start()
    return {"arm": name, "bid": str(target), "slug": slug, "status": "queued"}


@app.get("/api/arms/{name}/bids")
def api_bids(name: str):
    """这一份招标要求下，已经比对过（或正在比对）的所有投标文件。"""
    arm_dir(name)
    states = MATCH_STATE.get(name, {})
    items = []
    for entry in match_dirs(name):
        summary = read_json(entry["dir"] / "summary.json")
        summary = summary if isinstance(summary, dict) else {}
        counts = summary.get("coverage") or {}
        state = states.get(entry["slug"]) or {}
        items.append(
            {
                "slug": entry["slug"],
                "name": entry["name"],
                "status": state.get("status") or ("done" if summary else "unknown"),
                "has_result": bool(summary),
                "coverage": counts,
                "missing": summary.get("missing", 0),
                "pending_confirm": summary.get("pending_confirm", 0),
            }
        )
    return {"bids": items}


def kb_context(rows: list[dict] | None = None) -> dict:
    """只读检索：失败一律返回空列表，不影响主流程。"""
    try:
        sys.path.insert(0, str(ROOT / "adapter"))
        from kb import context_for

        return context_for(rows or [])
    except Exception as error:  # noqa: BLE001
        return {"items": [], "fallback": f"{type(error).__name__}: {error}"}


@app.get("/api/arms/{name}/match")
def api_match_result(name: str, slug: str = ""):
    folder = arm_dir(name)
    dirs = match_dirs(name)
    if not dirs:
        return {"state": {}, "summary": None, "rows": [], "has_result": False, "bid": "", "slug": "", "bids": [],
                "kb_context": {"items": [], "fallback": ""}}
    entry = next((item for item in dirs if item["slug"] == slug), dirs[0]) if slug else dirs[0]
    out_dir = entry["dir"]
    summary = read_json(out_dir / "summary.json")
    rows: list[dict] = []
    csv_path = out_dir / "coverage.csv"
    if csv_path.exists():
        with csv_path.open(encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))

    # 人工确认覆盖：审查员点过的项以 review.json 为准，并重算统计
    overlay = read_json(out_dir / "review.json") or {}
    overlay = {k: v for k, v in overlay.items() if not k.startswith("_")}  # 跳过 _invalidated 元数据
    if overlay and rows:
        counts: dict[str, int] = {}
        stale = 0
        for row in rows:
            patch = overlay.get(row.get("req_id"))
            if patch:
                row["coverage"] = patch.get("status", row.get("coverage"))
                row["reviewed"] = "Y"
                row["need_confirm"] = ""
                if patch.get("sig") and row.get("sig") and patch["sig"] != row["sig"]:
                    row["review_stale"] = "Y"
                    stale += 1
            counts[row["coverage"]] = counts.get(row["coverage"], 0) + 1
        summary = {**(summary or {}), "coverage": counts}
        summary["missing"] = (
            counts.get("未响应", 0) + counts.get("未找到", 0) + counts.get("负偏离", 0)
        )
        summary["pending_confirm"] = counts.get("人工介入", 0)
        summary["reviewed"] = len(overlay)
        summary["review_stale"] = stale

    state = (MATCH_STATE.get(name) or {}).get(entry["slug"]) or {}
    return {
        "state": state,
        "summary": summary,
        "rows": rows,
        "has_result": bool(summary),
        "bid": state.get("bid") or (summary or {}).get("bid", ""),
        "slug": entry["slug"],
        "bids": [{"slug": item["slug"], "name": item["name"]} for item in dirs],
        "kb_context": kb_context(rows),
    }


@app.get("/api/kb/context")
def api_kb_context(q: str = "", doc_type: str = "", k: int = 5):
    """只读检索已确认案例（补偿层）。"""
    try:
        sys.path.insert(0, str(ROOT / "adapter"))
        from kb import retrieve

        keywords = [word for word in q.replace(",", " ").split() if word]
        return retrieve(doc_type, keywords, k)
    except Exception as error:  # noqa: BLE001
        return {"items": [], "fallback": f"{type(error).__name__}: {error}"}


@app.get("/api/arms/{name}/matrix")
def api_matrix(name: str):
    """多份投标文件的横向对比：每个要求一行，每份标书一列。"""
    folder = arm_dir(name)
    dirs = match_dirs(name)
    if not dirs:
        return {"bids": [], "rows": []}

    ledger = {row["req_id"]: row for row in read_rows(name)}
    per_bid: dict[str, dict[str, dict]] = {}
    bids = []
    for entry in dirs:
        csv_path = entry["dir"] / "coverage.csv"
        rows: dict[str, dict] = {}
        if csv_path.exists():
            with csv_path.open(encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    rows[row.get("req_id", "")] = row
        per_bid[entry["slug"]] = rows
        summary = read_json(entry["dir"] / "summary.json") or {}
        bids.append({
            "slug": entry["slug"],
            "name": entry["name"],
            "coverage": (summary or {}).get("coverage") or {},
            "missing": (summary or {}).get("missing", 0),
        })

    order: list[str] = []
    for rows in per_bid.values():
        for req_id in rows:
            if req_id not in order:
                order.append(req_id)
    for req_id in ledger:
        if req_id not in order:
            order.append(req_id)

    matrix = []
    for req_id in order:
        cells = {}
        for entry in dirs:
            row = per_bid[entry["slug"]].get(req_id)
            cells[entry["slug"]] = (
                {"coverage": row.get("coverage", ""), "page": row.get("bid_page", "")} if row else None
            )
        matrix.append({
            "req_id": req_id,
            "requirement": (ledger.get(req_id) or {}).get("requirement", ""),
            "section": (ledger.get(req_id) or {}).get("section", ""),
            "cells": cells,
        })
    return {"bids": bids, "rows": matrix}

@app.get("/api/state")
def api_state():
    return {
        "services": service_state(),
        "documents": list_documents(),
        "arms": list_arms(),
        "jobs": [
            {
                "id": job["id"],
                "arm": job["arm"],
                "label": job["label"],
                "status": job["status"],
                "created": job["created"],
                "current": job.get("current", ""),
                "stages_done": sum(1 for stage in job["stages"] if stage["state"] == "done"),
                "stages_total": len(job["stages"]),
                "error": job.get("error", ""),
            }
            for job in sorted(JOBS.values(), key=lambda item: item["created"], reverse=True)
        ],
        "env": {
            "has_key": bool(load_env_file().get("DEEPSEEK_KEY")),
            "data_dir": str(DATA / "rt_bench"),
        },
    }


@app.post("/api/services/start")
def api_services_start(payload: dict = Body(default={})):
    mode = (payload or {}).get("mode", "stub")
    result = {"stub": start_stub(), "api": start_api()}
    time.sleep(1.0)
    start_worker("", mode)
    for _ in range(40):
        if port_open(API_PORT):
            break
        time.sleep(0.5)
    return {**result, "services": service_state()}


@app.get("/api/arms")
def api_arms():
    return list_arms()


@app.get("/api/arms/{name}")
def api_arm(name: str):
    folder = arm_dir(name)
    if not folder.exists():
        raise HTTPException(404, "运行不存在")
    return {
        "meta": arm_meta(name),
        "gate": gate_summary(name),
        "validate": validate_summary(name),
        "job": read_json(folder / "job.json"),
    }


@app.get("/api/arms/{name}/ledger")
def api_ledger(name: str):
    rows = read_rows(name)
    stats = {
        "total": len(rows),
        "by_type": {},
        "by_status": {},
        "mandatory": sum(1 for row in rows if row["mandatory"] == "Y"),
        "no_line": sum(1 for row in rows if row["line_no"] == "-1"),
        "reviewed": sum(1 for row in rows if row.get("_review")),
    }
    for row in rows:
        stats["by_type"][row["type"]] = stats["by_type"].get(row["type"], 0) + 1
        stats["by_status"][row["status"]] = stats["by_status"].get(row["status"], 0) + 1
    return {"rows": rows, "stats": stats}


@app.post("/api/arms/{name}/review")
def api_review(name: str, payload: dict = Body(...)):
    folder = arm_dir(name)
    if not folder.exists():
        raise HTTPException(404, "运行不存在")
    path = folder / "review.json"
    overlay = read_json(path) or {}
    req_id = payload.get("req_id")
    if not req_id:
        raise HTTPException(400, "缺少 req_id")
    entry = overlay.get(req_id) or {}
    for key in ("status", "owner", "note"):
        if key in payload:
            entry[key] = payload[key]
    entry["at"] = now()
    overlay[req_id] = entry
    path.write_text(json.dumps(overlay, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "reviewed": len(overlay)}


MATCH_REVIEW_STATUSES = {"已响应", "部分响应", "未响应", "负偏离"}


@app.post("/api/arms/{name}/match/review")
def api_match_review(name: str, payload: dict = Body(...)):
    """人工介入项的确认：把审查员的选择落到 match/review.json。"""
    folder = arm_dir(name)
    if not folder.exists():
        raise HTTPException(404, "运行不存在")
    req_id = payload.get("req_id")
    status = payload.get("status")
    if not req_id:
        raise HTTPException(400, "缺少 req_id")
    if status not in MATCH_REVIEW_STATUSES:
        raise HTTPException(400, f"status 只能是 {sorted(MATCH_REVIEW_STATUSES)}")
    out_dir = folder / "match"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "review.json"
    overlay = read_json(path) or {}

    # 记下确认时的证据指纹，重跑后证据变了才能提示"建议复核"
    sig = ""
    csv_path = out_dir / "coverage.csv"
    if csv_path.exists():
        with csv_path.open(encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                if row.get("req_id") == req_id:
                    sig = row.get("sig", "")
                    break

    overlay[req_id] = {"status": status, "note": payload.get("note", ""), "sig": sig, "at": now()}
    path.write_text(json.dumps(overlay, ensure_ascii=False, indent=2), encoding="utf-8")
    active = sum(1 for key in overlay if not key.startswith("_"))
    return {"ok": True, "reviewed": active, "sig": sig}


@app.get("/api/arms/{name}/export.csv")
def api_export(name: str):
    rows = read_rows(name)
    if not rows:
        raise HTTPException(404, "没有台账")
    columns = [
        "req_id", "section", "requirement", "type", "mandatory", "strength",
        "source_quote", "source_ver", "line_no", "page", "response_type", "need_clarify",
        "dup_of", "owner", "status", "note",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}-requirements.csv"'},
    )




@app.get("/api/arms/{name}/export.pdf")
def api_export_pdf(name: str):
    """把一次运行渲染成 PDF 报告（台账 + 比对结论 + 规则健康度）。"""
    folder = arm_dir(name)
    if not folder.exists():
        raise HTTPException(404, "运行不存在")

    rows = read_rows(name)
    dirs = match_dirs(name)
    bids: list[dict] = []
    for entry in dirs:
        bid_summary = read_json(entry["dir"] / "summary.json")
        bid_summary = bid_summary if isinstance(bid_summary, dict) else {}
        bids.append(
            {
                "name": entry["name"],
                "coverage": bid_summary.get("coverage") or {},
                "missing": bid_summary.get("missing", 0),
            }
        )

    coverage: list[dict] = []
    summary = None
    primary = next((item for item in dirs if item["slug"] == "default"), dirs[0] if dirs else None)
    if primary:
        coverage_path = primary["dir"] / "coverage.csv"
        if coverage_path.exists():
            with coverage_path.open(encoding="utf-8-sig") as handle:
                coverage = list(csv.DictReader(handle))
        summary = read_json(primary["dir"] / "summary.json")

    if not rows and not coverage:
        raise HTTPException(404, "这个运行还没有台账或比对结果")

    meta = read_json(folder / "meta.json") or {}
    content = build_report(
        name=name,
        meta=meta if isinstance(meta, dict) else {},
        rows=rows,
        coverage=coverage,
        summary=summary if isinstance(summary, dict) else None,
        bids=bids,
    )
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}-report.pdf"'},
    )


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job


@app.post("/api/jobs")
def api_job_create(payload: dict = Body(...)):
    return start_job(payload)


@app.post("/api/jobs/batch")
def api_jobs_batch(payload: dict = Body(...)):
    """一次提交多份招标文件：每份一个运行，排队执行（最多同时 2 条）。"""
    files = [str(item) for item in (payload.get("files") or []) if Path(str(item)).exists()]
    if not files:
        raise HTTPException(400, "没有可用的招标文件")
    stamp = datetime.now().strftime("%m%d_%H%M%S")
    jobs = []
    for index, file_path in enumerate(files, 1):
        arm = payload.get("arm_prefix") or f"bench_{stamp}_{index:02d}"
        jobs.append(start_job({**payload, "file": file_path, "arm": arm}))
    return {"jobs": jobs, "count": len(jobs)}


@app.get("/", response_class=HTMLResponse)
def index():
    # 工作台界面经常改，别让浏览器拿缓存，否则改完看不到效果
    return HTMLResponse(
        (WORKBENCH / "static/index.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


app.mount("/static", StaticFiles(directory=WORKBENCH / "static"), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8330)