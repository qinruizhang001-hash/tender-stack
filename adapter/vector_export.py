#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vector_export.py — 把切块结果导出成向量库入库用的 JSONL。

默认导出 **requirement 级**（单条要求条款），章节块另存 `sections.jsonl`
作为父上下文；id 用「章节 + 条款号 + 原文」稳定哈希，重跑不变。
降级信息随 stats 一起返回，不静默。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def export_chunks(source: str | Path, target: str | Path, level: str = "requirement") -> dict:
    from bid_chunker import chunk_pdf
    from layout_parse import extract_requirements

    source, target = Path(source), Path(target)
    chunks, stats = chunk_pdf(source)

    sections = []
    for index, chunk in enumerate(chunks):
        text = (chunk.get("text") or "").strip()
        if not text:
            continue
        sections.append(
            {
                "id": hashlib.sha1(f"{source.name}|{chunk.get('section', '')}|{index}".encode()).hexdigest()[:16],
                "text": text,
                "section": chunk.get("section", ""),
                "pages": chunk.get("pages") or [chunk.get("page")],
                "kind": chunk.get("kind", "section"),
                "has_table": bool(chunk.get("table")),
                "quality": chunk.get("quality", "ok"),
                "quality_reason": chunk.get("quality_reason", ""),
                "source": source.name,
                "chars": len(text),
            }
        )

    requirements = []
    for item in extract_requirements(chunks):
        requirements.append(
            {
                "id": item["id"],
                "text": item["text"],
                "number": item["number"],
                "section": item["section"],
                "page": item["page"],
                "pages": item["pages"],
                "quality": item.get("quality", "ok"),
                "quality_reason": item.get("quality_reason", ""),
                "source": source.name,
                "chars": len(item["text"]),
            }
        )

    sections_path = target.with_name(target.stem + ".sections.jsonl")
    rows: list[dict] = []
    if level in {"requirement", "both"}:
        _write(target, requirements)
        rows = requirements
    if level in {"section", "both"}:
        _write(sections_path if level == "both" else target, sections)
        if level == "section":
            rows = sections
    else:
        _write(sections_path, sections)

    fallbacks = stats.get("ocr_fallback") or []
    return {
        "level": level,
        "requirements": len(requirements),
        "sections": len(sections),
        "table_sections": sum(1 for row in sections if row["has_table"]),
        "low_quality": sum(1 for row in rows if row.get("quality") == "low"),
        "chars": sum(row["chars"] for row in rows),
        "backend": stats.get("backend") or stats.get("mode"),
        "toc_pages": stats.get("toc_pages") or [],
        "table_suspect": stats.get("table_suspect", 0),
        "ocr_fallback": fallbacks[:5],
        "ocr_fallback_count": len(fallbacks),
        "target": str(target),
        "sections_target": str(sections_path),
    }


if __name__ == "__main__":
    level = "requirement"
    args = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    if "--sections" in sys.argv:
        level = "section"
    if "--both" in sys.argv:
        level = "both"
    print(json.dumps(export_chunks(args[0], args[1], level=level), ensure_ascii=False))
