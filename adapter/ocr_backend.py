#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ocr_backend.py — OCR 后端选择：PaddleOCR（侧车）> RapidOCR（内置）。

- 只提供真正可用的后端：侧车存在但依赖缺失时，Paddle 一律判为不可用；
- 探测走 `--ping`（导入检查），不是「目录存在就算能用」；
- 侧车路径同时支持 Windows（Scripts/python.exe）与 POSIX（bin/python）。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIDECAR = ROOT / ".venv-paddle"
PADDLE_CANDIDATES = (
    SIDECAR / "Scripts" / "python.exe",
    SIDECAR / "bin" / "python",
    SIDECAR / "bin" / "python3",
)
PADDLE_WORKER = ROOT / "scripts" / "paddle_ocr_worker.py"

_PROBE_CACHE: dict[str, bool] | None = None
_STDERR_LOG = ROOT / "logs" / "paddle_ocr.err.log"
_PADDLE_PROC: subprocess.Popen | None = None
_PADDLE_LOCK = threading.Lock()


def paddle_python() -> Path | None:
    for candidate in PADDLE_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def _probe_paddle() -> bool:
    """轻量导入探测：侧车里有 paddleocr 且能 import 才算可用。"""
    python = paddle_python()
    if python is None or not PADDLE_WORKER.exists():
        return False
    try:
        result = subprocess.run(
            [str(python), str(PADDLE_WORKER), "--ping"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
    except Exception:  # noqa: BLE001
        return False
    return result.returncode == 0


def _has(module: str) -> bool:
    try:
        return __import__(module) is not None
    except Exception:  # noqa: BLE001
        return False


def available(refresh: bool = False) -> dict[str, bool]:
    global _PROBE_CACHE
    if _PROBE_CACHE is None or refresh:
        _PROBE_CACHE = {"paddle": _probe_paddle(), "rapidocr": _has("rapidocr_onnxruntime")}
    return dict(_PROBE_CACHE)


def backend() -> str:
    """TENDER_OCR 可强制指定；不可用时退回可用后端，不假装成功。"""
    flags = available()
    choice = (os.environ.get("TENDER_OCR") or "").strip().lower()
    if choice and flags.get(choice):
        return choice
    for name in ("paddle", "rapidocr"):
        if flags.get(name):
            return name
    return ""


def describe() -> dict:
    flags = available()
    return {"selected": backend(), **flags}


def ocr_image_file(path: str | Path) -> tuple[str, str]:
    """对一张图 OCR，返回 (text, backend)。"""
    name = backend()
    if not name:
        raise RuntimeError("没有可用的 OCR 后端")
    if name == "paddle":
        return _paddle(str(path)), "paddle"
    return _rapid(str(path)), "rapidocr"


def _paddle_proc() -> subprocess.Popen:
    global _PADDLE_PROC
    if _PADDLE_PROC is None or _PADDLE_PROC.poll() is not None:
        python = paddle_python()
        if python is None:
            raise RuntimeError("找不到 Paddle 侧车解释器")
        _STDERR_LOG.parent.mkdir(parents=True, exist_ok=True)
        stderr_handle = _STDERR_LOG.open("a", encoding="utf-8")
        _PADDLE_PROC = subprocess.Popen(
            [str(python), str(PADDLE_WORKER), "--serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_handle,  # 侧车日志落盘，不再 DEVNULL
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
    return _PADDLE_PROC


def _paddle(image_path: str) -> str:
    with _PADDLE_LOCK:
        process = _paddle_proc()
        process.stdin.write(json.dumps({"path": image_path}) + "\n")
        process.stdin.flush()
        line = process.stdout.readline()
        if not line:
            raise RuntimeError("paddle 侧车无响应（进程可能崩溃）")
        payload = json.loads(line)
    if not payload.get("ok", True):
        raise RuntimeError(f"paddle OCR 失败：{payload.get('error')}")
    return "\n".join(payload.get("lines") or [])


def warmup() -> bool:
    """部署时预热一次：把小图跑通，确认侧车/模型可用（不做 import 时自动执行）。"""
    import tempfile

    try:
        from PIL import Image

        handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        handle.close()
        Image.new("RGB", (64, 32), "white").save(handle.name)
        text, _ = ocr_image_file(handle.name)
        Path(handle.name).unlink(missing_ok=True)
        return text is not None
    except Exception:  # noqa: BLE001
        return False


def _rapid(image_path: str) -> str:
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    result, _ = engine(image_path)
    return "\n".join(str(item[1]) for item in (result or []))


if __name__ == "__main__":
    print(json.dumps(describe(), ensure_ascii=False))
