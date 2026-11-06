#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本仓库打成可上传到 SkillHub / Skills 广场的技能包（纯文本、根目录直放 SKILL.md）。

为什么需要这个脚本
------------------
技能市场上的包和 GitHub 仓库**不是同一份东西**。仓库要 LICENSE、VERSION、
.nojekyll、.github/ 才能正常工作；上传包只要"模型运行时会读到的纯文本"。
把仓库原样 zip 上传，常见的报错就是"包含禁止上传的文件"——挡的通常是：

    .nojekyll     无扩展名的隐藏文件（GitHub Pages 专用开关）
    LICENSE       无扩展名，且许可声明已在 README/About 里
    VERSION       无扩展名，发版流程自用
    .gitattributes / .gitignore / .github/   版本控制与 CI 元数据
    docs/*.html   发布页，不是技能内容

本脚本按白名单取文件、逐项自检，并保证：
    1. SKILL.md 在 zip 根目录（不套外层文件夹）
    2. 只有纯文本（扩展名白名单 + 二进制魔数嗅探）
    3. 路径一律正斜杠，层级不超过两级
    4. 单文件 ≤ 2MB、整包 ≤ 10MB、文件数 ≤ 300
    5. SKILL.md 里引用的相对路径在包内真实存在

用法
----
    python tools/pack_skillhub.py                    # 输出 dist/ai-bid-document-skillhub.zip
    python tools/pack_skillhub.py --out ~/Desktop/x.zip
    python tools/pack_skillhub.py --wrap             # 平台要求"根目录/技能名/文件"时用
    python tools/pack_skillhub.py --check 已有.zip    # 只体检，不打包

退出码：0 通过 / 1 自检不通过 / 2 输入错误。
零第三方依赖，Python 3.8+。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import zipfile

# ---------------------------------------------------------------- 白名单

# 根目录允许的散文件。CHANGELOG/VERSION/LICENSE 不进包：
# 前两者对运行时无用，后者无扩展名会被多数平台直接拒收。
ROOT_FILES = ("SKILL.md", "README.md")

# 允许整目录带入的内容目录（深度 1）
CONTENT_DIRS = ("references", "templates", "scripts", "examples", "tests")

# 纯文本扩展名白名单
ALLOW_EXT = {".md", ".py", ".csv", ".txt", ".json", ".yaml", ".yml"}

# 隐藏/缓存/临时
DENY_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", "__pycache__", ".git", ".github", "node_modules"}
DENY_EXT = {".pyc", ".pyo", ".log", ".tmp", ".bak", ".swp", ".zip", ".tar", ".gz", ".7z",
            ".exe", ".dll", ".so", ".dylib", ".png", ".jpg", ".jpeg", ".gif", ".svg",
            ".mp4", ".pdf", ".docx", ".xlsx", ".pptx", ".ico"}

# 二进制魔数（文本扩展名却实为二进制时兜底）
MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"%PDF", b"PK\x03\x04", b"MZ", b"\x7fELF",
         b"SQLite format 3", b"RIFF", b"\x1f\x8b")

LIMIT_FILE = 2 * 1024 * 1024          # 单文件 2MB
LIMIT_TOTAL = 10 * 1024 * 1024        # 整包 10MB
LIMIT_COUNT = 300                     # 文件数 300
LIMIT_DEPTH = 2                       # 相对技能根目录的最大层级

REL_REF = re.compile(
    r"(?:references|templates|scripts|examples|tests)/[A-Za-z0-9_./\-]+\.(?:md|py|csv|json|txt|ya?ml)"
)


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_binary(head: bytes) -> bool:
    if b"\x00" in head:
        return True
    return any(head.startswith(m) for m in MAGIC)


def collect(src: str) -> tuple[list[str], list[str]]:
    """返回 (待打包的相对路径列表, 被跳过的文件及原因)。"""
    keep: list[str] = []
    skipped: list[str] = []

    for name in ROOT_FILES:
        p = os.path.join(src, name)
        if os.path.isfile(p):
            keep.append(name)
        else:
            skipped.append(f"{name}（缺失）")

    for d in CONTENT_DIRS:
        base = os.path.join(src, d)
        if not os.path.isdir(base):
            skipped.append(f"{d}/（目录不存在）")
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in DENY_NAMES)
            for fn in sorted(files):
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, src).replace("\\", "/")
                ext = os.path.splitext(fn)[1].lower()
                if fn.startswith(".") or fn in DENY_NAMES:
                    skipped.append(f"{rel}（隐藏文件/系统文件）")
                elif ext in DENY_EXT:
                    skipped.append(f"{rel}（二进制或归档）")
                elif ext not in ALLOW_EXT:
                    skipped.append(f"{rel}（扩展名 {ext or '无'} 不在纯文本白名单）")
                elif not os.path.isfile(full):
                    skipped.append(f"{rel}（非普通文件）")
                else:
                    keep.append(rel)

    return sorted(keep), skipped


def verify(entries: list[tuple[str, bytes]]) -> list[str]:
    """对 (相对路径, 内容) 列表做平台硬性校验，返回问题清单（空 = 通过）。"""
    problems: list[str] = []
    names = [n for n, _ in entries]

    # 1. SKILL.md 位置与唯一性
    root_skill = [n for n in names if n.lower() == "skill.md"]
    nested_skill = [n for n in names if n.lower().endswith("/skill.md")]
    if nested_skill:
        problems.append("SKILL.md 被套在外层目录里：" + ", ".join(nested_skill))
    if not root_skill:
        problems.append("zip 根目录没有 SKILL.md（多数平台硬性要求根目录直放）")
    if len(root_skill) + len(nested_skill) > 1:
        problems.append(f"包内有 {len(root_skill) + len(nested_skill)} 个 SKILL.md，应为 1 个")

    # 2. 单文件 / 总量 / 数量
    total = 0
    for n, data in entries:
        size = len(data)
        total += size
        if size > LIMIT_FILE:
            problems.append(f"{n} 超过 2MB（{size / 1048576:.2f}MB）")
        ext = os.path.splitext(n)[1].lower()
        if ext and ext not in ALLOW_EXT:
            problems.append(f"{n} 扩展名 {ext} 不在纯文本白名单")
        if not ext:
            problems.append(f"{n} 无扩展名，平台通常判为非法文件")
        if n.startswith(".") or "/." in n:
            problems.append(f"{n} 是隐藏文件，平台通常拒收")
        if is_binary(data[:512]):
            problems.append(f"{n} 实为二进制内容（魔数嗅探）")
        depth = n.count("/")
        if depth > LIMIT_DEPTH:
            problems.append(f"{n} 层级过深（相对技能根 {depth} 级，上限 {LIMIT_DEPTH}）")
    if total > LIMIT_TOTAL:
        problems.append(f"解压后总大小 {total / 1048576:.2f}MB 超过 10MB")
    if len(entries) > LIMIT_COUNT:
        problems.append(f"文件数 {len(entries)} 超过 300")

    # 3. 反斜杠路径（Windows 打包常见坑）
    bad_sep = [n for n in names if "\\" in n]
    if bad_sep:
        problems.append("条目路径含反斜杠：" + ", ".join(bad_sep[:3]))

    # 4. SKILL.md 引用的相对路径必须存在
    skill = next((d for n, d in entries if n.lower() == "skill.md"), None)
    if skill:
        text = skill.decode("utf-8", "replace")
        refs = sorted(set(REL_REF.findall(text)))
        present = set(names)
        missing = [r for r in refs if r not in present]
        if missing:
            problems.append("SKILL.md 引用了包内不存在的文件：" + ", ".join(missing))

    return problems


def write_zip(entries: list[tuple[str, bytes]], out: str, wrap: str = "") -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            arc = f"{wrap}/{name}" if wrap else name
            info = zipfile.ZipInfo(arc, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    return out


def read_zip(path: str) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(path) as zf:
        return [(i.filename, zf.read(i.filename)) for i in zf.infolist() if not i.is_dir()]


def main(argv: list[str]) -> int:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description="打包可上传到 SkillHub / Skills 广场的技能包")
    ap.add_argument("--src", default=here, help="仓库根目录（默认脚本所在仓库）")
    ap.add_argument("--out", default=os.path.join(here, "dist", "ai-bid-document-skillhub.zip"))
    ap.add_argument("--wrap", nargs="?", const="ai-bid-document", default="",
                    help="加一层目录（部分平台要求 根目录/技能名/文件），可指定目录名")
    ap.add_argument("--check", metavar="ZIP", help="只体检已有 zip，不重新打包")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.check:
        if not os.path.isfile(args.check):
            print(f"[错误] 找不到 {args.check}", file=sys.stderr)
            return 2
        entries = read_zip(args.check)
        problems = verify(entries)
        print(f"体检：{args.check}")
        print(f"  条目 {len(entries)} 个，压缩包 {os.path.getsize(args.check) / 1024:.1f} KB")
        if problems:
            print(f"  不通过 {len(problems)} 项：")
            for p in problems:
                print(f"    ✗ {p}")
            return 1
        print("  ✓ 全部通过")
        return 0

    if not os.path.isdir(args.src):
        print(f"[错误] 源目录不存在：{args.src}", file=sys.stderr)
        return 2

    keep, skipped = collect(args.src)
    entries = []
    for rel in keep:
        with open(os.path.join(args.src, rel.replace("/", os.sep)), "rb") as f:
            entries.append((rel, f.read()))

    problems = verify(entries)
    if problems:
        print("[失败] 自检未通过，未生成 zip：", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        return 1

    out = write_zip(entries, args.out, args.wrap)
    size = os.path.getsize(out)
    digest = sha256_of(out)

    if not args.quiet:
        print(f"已生成：{out}")
        print(f"  大小 {size / 1024:.1f} KB · 文件 {len(entries)} 个 · sha256 {digest[:16]}…")
        if args.wrap:
            print(f"  根目录：{args.wrap}/ （外层包裹模式）")
        else:
            print("  根目录：SKILL.md 直放（推荐）")
        if skipped:
            print("  未纳入：")
            for s in skipped:
                print(f"    - {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
