#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_absorb.py — 解析/评估结束后，把 miss 与 quality=low 写成 inbox 案例卡。

用法：
  python -m scripts.kb_absorb --export out/req.jsonl --gold contract/gold/x.json \
      --doc-type service --pdf data/tenders/x.pdf --out kb/inbox
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "adapter"))

from kb import absorb  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", required=True, help="requirement JSONL")
    parser.add_argument("--gold", required=True, help="金标 JSON")
    parser.add_argument("--out", default=str(ROOT / "kb" / "inbox"))
    parser.add_argument("--doc-type", default="")
    parser.add_argument("--pdf", default="")
    parser.add_argument("--source-hash", default="")
    args = parser.parse_args(argv)

    result = absorb(
        args.export,
        args.gold,
        args.out,
        doc_type=args.doc_type,
        source_pdf=args.pdf or None,
        source_hash=args.source_hash,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
