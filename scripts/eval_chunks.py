#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eval_chunks.py — 条款级金标召回评估（不接大模型）。

金标格式（任选其一）：
  ["条款原文", ...]
  [{"id": "G01", "page": 3, "text": "条款原文"}, ...]

判定口径：
  exact    —— 整条条款完整落在某一条 requirement 里（归一化后包含）
  covered  —— 条款的 4-gram 覆盖率 ≥ 0.8（允许被切到两条里、或标点/空白差异）
  recall   —— covered / total

用法：
  python scripts/eval_chunks.py gold.json out/req.jsonl
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

COVERAGE_THRESHOLD = 0.8
GRAM = 4


def normalise(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"[\s\u3000]+", "", value)


def load_gold(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("items") or data.get("gold") or []

    items: list[dict] = []
    for index, item in enumerate(data, start=1):
        if isinstance(item, str):
            items.append({"id": f"G{index:02d}", "text": item, "page": None})
        elif isinstance(item, dict):
            value = item.get("text") or item.get("snippet") or ""
            if value:
                items.append(
                    {
                        "id": item.get("id") or f"G{index:02d}",
                        "text": value,
                        "page": item.get("page"),
                        "section": item.get("section", ""),
                    }
                )
    return [item for item in items if normalise(item["text"])]


def load_texts(path: Path) -> list[str]:
    """requirement 文本 + 「编号+正文」两种形态，编号本身也是条款的一部分。"""
    texts: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        body = normalise(record.get("text", ""))
        if body:
            texts.append(body)
        number = normalise(record.get("number", ""))
        if number and body:
            texts.append(number + body)
    return texts


def coverage(needle: str, texts: list[str]) -> float:
    """needle 的 4-gram 有多少被 requirements 覆盖（允许跨条）。"""
    if len(needle) < GRAM:
        return 1.0 if any(needle in text for text in texts) else 0.0
    grams = [needle[index : index + GRAM] for index in range(len(needle) - GRAM + 1)]
    hit = sum(1 for gram in grams if any(gram in text for text in texts))
    return hit / len(grams)


def evaluate(gold_path: str | Path, chunks_path: str | Path) -> dict:
    gold = load_gold(Path(gold_path))
    texts = load_texts(Path(chunks_path))
    if not gold:
        return {"total": 0, "exact": 0, "covered": 0, "recall": None, "miss": [], "note": "金标为空，跳过"}

    exact = covered = 0
    miss: list[dict] = []
    details: list[dict] = []
    for item in gold:
        needle = normalise(item["text"])
        is_exact = any(needle in text for text in texts)
        ratio = 1.0 if is_exact else coverage(needle, texts)
        is_covered = ratio >= COVERAGE_THRESHOLD
        exact += int(is_exact)
        covered += int(is_covered)
        details.append(
            {"id": item["id"], "page": item.get("page"), "exact": is_exact, "coverage": round(ratio, 3)}
        )
        if not is_covered:
            miss.append(
                {"id": item["id"], "page": item.get("page"), "coverage": round(ratio, 3), "text": item["text"][:60]}
            )

    return {
        "total": len(gold),
        "exact": exact,
        "covered": covered,
        "exact_rate": round(exact / len(gold), 3),
        "recall": round(covered / len(gold), 3),
        "threshold": COVERAGE_THRESHOLD,
        "miss": miss,
        "details": details,
        "chunks": len(texts),
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python scripts/eval_chunks.py gold.json chunks.jsonl")
        raise SystemExit(2)
    print(json.dumps(evaluate(sys.argv[1], sys.argv[2]), ensure_ascii=False, indent=2))
