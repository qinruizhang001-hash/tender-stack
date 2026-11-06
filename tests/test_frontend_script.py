#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""前端脚本语法检查器自身的测试。"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_frontend as cf  # noqa: E402

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="未安装 node")


def test_只抽内联脚本():
    html = '<script src="app.js"></script><script>let a = 1;</script>'
    assert cf.extract_scripts(html) == ["let a = 1;"]


def test_跳过空脚本():
    assert cf.extract_scripts("<script>   </script>") == []


def test_多段脚本都抽出来():
    html = "<script>var a=1;</script><script>var b=2;</script>"
    assert len(cf.extract_scripts(html)) == 2


@needs_node
def test_能发现语法错误():
    ok, message = cf.check_source("function broken( {")
    assert not ok
    assert message


@needs_node
def test_正确脚本通过():
    ok, _ = cf.check_source("const a = 1; async function f(){ await a; }")
    assert ok


@needs_node
def test_真实页面脚本全部通过():
    blocks = cf.extract_scripts(cf.INDEX.read_text(encoding="utf-8"))
    assert blocks, "index.html 应当包含内联脚本"
    for block in blocks:
        ok, message = cf.check_source(block)
        assert ok, message
