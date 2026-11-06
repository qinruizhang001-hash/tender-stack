#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_promote.py — 受控晋升（进化唯一入口）。

  python -m scripts.kb_promote --card <case_id> --dry-run
  python -m scripts.kb_promote --card <case_id> --approve --by 张三
  python -m scripts.kb_promote --card <case_id> --reject --reason "误报"

规则：schema 校验 → linked_test 必须存在 → gold_gate（可 --no-gate 跳过）→
写 kb/proposed/；--approve 才写 kb/approved/ 与 contract/gold/absorbed/。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "adapter"))

from kb import INBOX, load_jsonl, promote  # noqa: E402


def find_card(case_id: str) -> dict | None:
    for path in sorted(Path(INBOX).glob("*.jsonl")):
        for card in load_jsonl(path):
            if card.get("case_id") == case_id:
                return card
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--card", required=True, help="case_id 或 JSON 文件")
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--reject", action="store_true")
    parser.add_argument("--reason", default="")
    parser.add_argument("--by", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-gate", action="store_true")
    args = parser.parse_args(argv)

    card_path = Path(args.card)
    card = json.loads(card_path.read_text(encoding="utf-8-sig")) if card_path.suffix == ".json" and card_path.exists() else find_card(args.card)
    if card is None:
        print(json.dumps({"ok": False, "stage": "lookup", "errors": ["case_id 未找到"]}, ensure_ascii=False))
        return 1

    result = promote(
        card,
        approve=args.approve and not args.dry_run,
        reject=args.reject,
        reason=args.reason,
        approved_by=args.by,
        run_gate=not args.no_gate,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
