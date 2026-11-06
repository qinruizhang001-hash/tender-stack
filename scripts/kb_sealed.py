#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_sealed.py — sealed 金标 recall 基线。

  python -m scripts.kb_sealed --init     # 计算并写入 kb/sealed/baseline.json
  python -m scripts.kb_sealed --check    # 与基线比较，下降则退出码 1
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "adapter"))

import kb  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    recall = kb._sealed_recall()
    if recall is None:
        print(json.dumps({"sealed_recall": None, "note": "未配置 gold_files 或文件缺失"}, ensure_ascii=False))
        return 0 if args.init else 1

    baseline_path = ROOT / "kb" / "sealed" / "baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else {}

    if args.init or not baseline:
        baseline_path.write_text(
            json.dumps({"sealed_recall": recall, "updated_at": kb.now()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps({"sealed_recall": recall, "baseline": "written"}, ensure_ascii=False))
        return 0

    previous = baseline.get("sealed_recall")
    ok = previous is None or recall >= previous
    print(json.dumps({"sealed_recall": recall, "baseline": previous, "ok": ok}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
