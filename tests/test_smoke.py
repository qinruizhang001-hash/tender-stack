#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冒烟测试：真的把服务器起起来，访问首页与接口。

单元测试用假数据，跑得再快也证明不了"服务能启动、页面能返回、接口字段还在"。
这里起一个真实进程（关掉自动拉起 rag-tender），跑完立刻关掉。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# pre-commit 阶段由 smoke.ps1 单独跑真服务，这里跳过，免得起两次
pytestmark = pytest.mark.skipif(
    os.environ.get("DSH_SKIP_SMOKE") == "1",
    reason="pre-commit 阶段跳过冒烟（由 scripts/smoke.ps1 覆盖）",
)

import check_frontend as cf  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", "replace")


@pytest.fixture(scope="module")
def server() -> str:
    port = _free_port()
    env = {
        **os.environ,
        # 冒烟只验证工作台本身，不要让 keeper 去拉起 rag-tender / worker
        "WORKBENCH_AUTOSTART": "0",
        "PYTHONIOENCODING": "utf-8",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "workbench.server:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 40
    while time.time() < deadline:
        if process.poll() is not None:
            pytest.fail("服务进程提前退出")
        try:
            _get(base + "/api/state", timeout=2)
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.4)
    else:
        process.terminate()
        pytest.fail("服务在 40 秒内没有启动")

    yield base

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def test_首页可以打开(server):
    status, html = _get(server + "/")
    assert status == 200
    assert "标书工作台" in html


def test_页面挂载点齐全(server):
    _, html = _get(server + "/")
    for marker in ('id="arms"', 'id="queue"', 'id="tab-matrix"', 'id="btnPdf"', "<script>"):
        assert marker in html, f"页面缺少 {marker}"


def test_返回的脚本语法正确(server):
    _, html = _get(server + "/")
    blocks = cf.extract_scripts(html)
    assert blocks, "页面没有内联脚本"
    for block in blocks:
        ok, message = cf.check_source(block)
        assert ok, message


def test_状态接口结构完整(server):
    status, body = _get(server + "/api/state")
    assert status == 200
    data = json.loads(body)
    for key in ("services", "documents", "arms", "jobs", "env"):
        assert key in data, f"/api/state 缺少 {key}"
    assert isinstance(data["arms"], list)
    assert isinstance(data["jobs"], list)
    assert set(data["services"]) >= {"api", "model", "worker"}


def test_运行列表字段可用于渲染(server):
    _, body = _get(server + "/api/state")
    arms = json.loads(body)["arms"]
    if not arms:
        pytest.skip("没有运行目录")
    for arm in arms[:5]:
        for key in ("name", "requirements"):
            assert key in arm, f"运行 {arm.get('name')} 缺少 {key}"


def test_多标对比接口可用(server):
    _, body = _get(server + "/api/state")
    arms = [arm["name"] for arm in json.loads(body)["arms"]]
    if "arm_demo_multi" not in arms:
        pytest.skip("没有 arm_demo_multi 演示运行")
    status, payload = _get(server + "/api/arms/arm_demo_multi/matrix")
    assert status == 200
    matrix = json.loads(payload)
    assert len(matrix["bids"]) >= 2
    assert len(matrix["rows"]) >= 1
    assert "cells" in matrix["rows"][0]


def test_不存在的运行返回_404(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(server + "/api/arms/not-exist/export.pdf")
    assert excinfo.value.code == 404
