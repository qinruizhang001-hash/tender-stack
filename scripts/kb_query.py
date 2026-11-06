#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_query.py — 只读检索已确认案例（补偿层）。

用法：python -m scripts.kb_query --doc-type service --keywords 社保 保证金
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "adapter"))

from kb import retrieve  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc-type", default="")
    parser.add_argument("--keywords", nargs="*", default=[])
    parser.add_argument("--k", type=int, default=3)
    args = parser.parse_args(argv)
    print(json.dumps(retrieve(args.doc_type, args.keywords, args.k), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
