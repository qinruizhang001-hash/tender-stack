#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""前端脚本语法检查。

页面脚本一处语法错误，整个工作台就白屏——比后端抛异常更难发现，
因为浏览器不会把错误报到任何日志里。这里把 index.html 里的内联
`<script>` 抽出来交给 `node --check`，在提交前拦住这类事故。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "workbench" / "static" / "index.html"

SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)


def extract_scripts(html: str) -> list[str]:
    """取出所有内联脚本；带 src 的外链跳过。"""
    blocks: list[str] = []
    for attrs, body in SCRIPT_RE.findall(html):
        if "src=" in attrs.lower():
            continue
        if body.strip():
            blocks.append(body)
    return blocks


def check_source(code: str, node: str | None = None) -> tuple[bool, str]:
    """语法检查单段脚本，返回 (是否通过, 错误信息)。"""
    node = node or shutil.which("node")
    if not node:
        return True, "未找到 node，跳过"
    handle = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    try:
        handle.write(code)
        handle.close()
        proc = subprocess.run(
            [node, "--check", handle.name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return proc.returncode == 0, (proc.stderr or "")[:900]
    finally:
        Path(handle.name).unlink(missing_ok=True)


def main() -> int:
    if not INDEX.exists():
        print(f"[FAIL] 找不到页面：{INDEX}")
        return 1
    node = shutil.which("node")
    if not node:
        print("[skip] 没有 node，跳过前端语法检查（CI 请装 Node）")
        return 0

    blocks = extract_scripts(INDEX.read_text(encoding="utf-8"))
    if not blocks:
        print("[FAIL] 页面里没有可检查的内联脚本")
        return 1

    for index, code in enumerate(blocks, 1):
        ok, message = check_source(code, node)
        if not ok:
            print(f"[FAIL] 第 {index} 段脚本语法错误：")
            print(message)
            return 1

    print(f"[ok] 前端脚本 {len(blocks)} 段，语法通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
