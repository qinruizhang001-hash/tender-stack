#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scan_bridge.py — 在模型调用之前插入确定性扫描层（方案 B 的桥）

职责边界（严格数据流对接）：
  1. 以**子进程**方式调用 tender-review-kit 的 run_pipeline.py prep，只吃它的
     文件产物（lines.txt / hits.json），不 import 它的任何模块。
  2. 把命中的"行"映射到 rag-tender 的"块"（纯文本匹配，不碰对方数据库）。
  3. 产出 known_issues.json：
       - by_block   : 每个 block 已确认的确定性条款（供注入提示词，叫模型别重复判断）
       - skip_blocks: 整块已被扫描完全覆盖的 block（可整批跳过模型调用）
       - stats      : 扫描层覆盖率统计

用法：
  python adapter/scan_bridge.py --file data/tenders/xxx.pdf --run out/armA_real \
      --out out/prescan_real
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

TRK = Path("vendor/tender-review-kit/run_pipeline.py")

STRONG = [
    "不得", "严禁", "禁止", "不允许", "不接受", "必须", "须", "应当",
    "不低于", "不少于", "不高于", "不得超过", "至少",
    "需提供", "应提供", "须提交", "应提交",
    "予以否决", "按无效投标处理", "作为废标处理", "拒收", "否决",
    "视为放弃", "视为无效", "实质性偏离", "取消资格", "无效", "废标",
]
HIT_GROUPS = ["primary", "secondary", "customization", "certifications", "emphasis_marks", "contract"]


def norm(value: str) -> str:
    return re.sub(r"[\s\u3000，。、；：（）()【】\[\]“”\"'·,.;:!?\-—_/]", "", value or "")


def run_prep(file: Path, outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(TRK), "prep", str(file), "--outdir", str(outdir)]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if result.returncode != 0:
        raise RuntimeError(f"tender-review-kit prep 失败:\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}")


def load_hits(scan_dir: Path) -> tuple[dict, list[tuple[int, str]]]:
    hits_file = next(iter(sorted(scan_dir.glob("*.hits.json"))), None)
    lines_file = next(iter(sorted(scan_dir.glob("*.lines.txt"))), None)
    if hits_file is None or lines_file is None:
        raise RuntimeError(f"{scan_dir} 缺少 hits.json 或 lines.txt")
    hits = json.loads(hits_file.read_text(encoding="utf-8"))
    lines: list[tuple[int, str]] = []
    for raw in lines_file.read_text(encoding="utf-8").splitlines():
        if "\t" not in raw:
            continue
        head, _, text = raw.partition("\t")
        try:
            lines.append((int(head), text.strip()))
        except ValueError:
            continue
    return hits, lines


def flatten_hits(hits: dict) -> list[dict]:
    items: list[dict] = []
    for group in HIT_GROUPS:
        for hit in (hits.get("hits") or {}).get(group, []) or []:
            items.append(
                {
                    "group": group,
                    "line": hit.get("line"),
                    "word": hit.get("word") or hit.get("pattern") or hit.get("mark") or "",
                    "text": hit.get("text") or "",
                    "scope": hit.get("scope") or [],
                }
            )
    return items


def map_to_blocks(items: list[dict], blocks: list[dict]) -> dict[str, list[dict]]:
    """把扫描命中的行文本，映射到 rag-tender 的块。纯字符串匹配，不依赖任何一方内部结构。"""
    normalized_blocks = [(block["block_id"], norm(block.get("text", ""))) for block in blocks]
    by_block: dict[str, list[dict]] = {}
    for item in items:
        target = norm(item["text"])
        if len(target) < 4:
            continue
        matched = None
        for block_id, text in normalized_blocks:
            if target and target in text:
                matched = block_id
                break
        if matched is None:
            # 退化：用命中词定位（词短、噪声稍高，但保证不丢）
            word = norm(item["word"])
            if len(word) >= 2:
                for block_id, text in normalized_blocks:
                    if word in text:
                        matched = block_id
                        break
        if matched:
            item = dict(item)
            item["block_id"] = matched
            by_block.setdefault(matched, []).append(item)
    return by_block


def sentences(text: str) -> list[str]:
    return [part for part in re.split(r"[。；;\n]", text) if part.strip()]


def find_verbatim(quote: str, block_text: str, word: str = "") -> str:
    """扫描层的行文本与块文本可能有空白差异，这里回取块内逐字片段。"""
    if quote and quote in block_text:
        return quote
    target = norm(word or quote)
    if not target:
        return ""
    for sentence in re.split(r"(?<=[。；;])", block_text):
        if target in norm(sentence):
            return sentence.strip(" \n")
    return ""


def is_covered(block_text: str, hits: list[dict], max_chars: int = 220) -> bool:
    """整块是否已被扫描完全覆盖：短块 + 每个含强信号的分句都有命中。"""
    if len(block_text) > max_chars or not hits:
        return False
    words = [norm(hit["word"]) for hit in hits if len(norm(hit["word"])) >= 2]
    if not words:
        return False
    for sentence in sentences(block_text):
        target = norm(sentence)
        if not any(word in target for word in STRONG):
            continue
        if not any(word and word in target for word in words):
            return False
    return True


def build(file: Path, run_dir: Path, out_dir: Path, reuse: bool) -> dict:
    scan_dir = out_dir / "scan"
    if not reuse or not scan_dir.exists() or not any(scan_dir.glob("*.hits.json")):
        run_prep(file, scan_dir)

    hits, lines = load_hits(scan_dir)
    items = flatten_hits(hits)
    blocks = json.loads((run_dir / "blocks.json").read_text(encoding="utf-8"))
    by_block = map_to_blocks(items, blocks)

    block_text = {block["block_id"]: block.get("text", "") for block in blocks}
    skip_blocks = [
        block_id
        for block_id, block_hits in by_block.items()
        if is_covered(block_text.get(block_id, ""), block_hits)
    ]

    # 稳定键：block_id 每次解析都会重新生成，不能跨运行使用；
    # 块文本对同一份文件是稳定的，故以「原文片段」作为对接键。
    by_quote: list[dict] = []
    seen_quotes: set[str] = set()
    for block_id, block_hits in by_block.items():
        text = block_text.get(block_id, "")
        for hit in block_hits:
            quote = find_verbatim(hit.get("text") or "", text, hit.get("word") or "")
            if not quote or quote in seen_quotes:
                continue
            seen_quotes.add(quote)
            by_quote.append(
                {
                    "quote": quote,
                    "word": hit.get("word") or "",
                    "group": hit.get("group") or "",
                    "line": hit.get("line"),
                }
            )
    skip_texts = [block_text[block_id] for block_id in skip_blocks]

    known = {
        "source_file": str(file),
        "generated_by": "vendor/tender-review-kit/run_pipeline.py prep",
        "by_quote": by_quote,
        "skip_texts": skip_texts,
        "by_block": {
            block_id: [
                {"quote": hit["text"], "word": hit["word"], "group": hit["group"], "line": hit["line"]}
                for hit in block_hits
            ]
            for block_id, block_hits in by_block.items()
        },
        "skip_blocks": skip_blocks,
        "stats": {
            "hit_items": len(items),
            "hit_blocks": len(by_block),
            "total_blocks": len(blocks),
            "block_coverage": round(len(by_block) / max(len(blocks), 1) * 100, 1),
            "skip_blocks": len(skip_blocks),
            "skip_block_ratio": round(len(skip_blocks) / max(len(blocks), 1) * 100, 1),
            "stable_quotes": len(by_quote),
            "by_group": {
                group: len((hits.get("hits") or {}).get(group, []) or []) for group in HIT_GROUPS
            },
            "detected_emphasis_marks": hits.get("detected_emphasis_marks"),
            "total_lines": hits.get("total_lines"),
        },
    }
    (out_dir / "known_issues.json").write_text(
        json.dumps(known, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return known


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--reuse", action="store_true")
    args = parser.parse_args()

    known = build(Path(args.file), Path(args.run), Path(args.out), args.reuse)
    print(json.dumps(known["stats"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
