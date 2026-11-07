#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_stage.py — 工作台流水线里的「知识沉淀」阶段。

调用 vector_export 导出 requirement 并按 TENDER_KB_ABSORB 吸收到 kb/inbox。
任何失败只返回 fallback，不抛错（不能炸整单审查）。

用法：python scripts/kb_stage.py --pdf 标书.pdf --out out/arm/kb [--doc-type service]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "adapter"))


def run(pdf: str | Path, out_dir: str | Path, doc_type: str = "") -> dict:
    from vector_export import export_chunks

    pdf, out_dir = Path(pdf), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "requirements.jsonl"
    try:
        info = export_chunks(pdf, target)
        return {"ok": True, "stats": info, "requirements": str(target)}
    except Exception as error:  # noqa: BLE001 - 知识沉淀失败不影响审查
        return {"ok": False, "fallback": f"{type(error).__name__}: {error}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--doc-type", default="")
    args = parser.parse_args(argv)
    result = run(args.pdf, args.out, args.doc_type)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
