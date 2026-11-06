#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_pass.py — 第三通道：审计补漏

输入一份已抽取的运行目录，逐页问模型同一句话：
    「这一页里，还有哪些对投标人有约束、但你刚才没抽到的条款？」

和主提取的区别：
  - 只看「漏没漏」，不重复已经抽到的；
  - 输出必须是**逐字引文**（不接受改写），便于回查；
  - 逐页调用，上下文是整页，能捞到跨块、表格、脚注里的条款。

用法：
  python adapter/audit_pass.py --run out/armC_v3 --limit 1          # 试一页
  python adapter/audit_pass.py --run out/armC_v3 --workers 6        # 全量
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL = os.environ.get("AUDIT_MODEL", "deepseek-chat")
BASE = os.environ.get("AUDIT_BASE", "https://api.deepseek.com/v1")
MAX_PAGE_CHARS = 6500
MAX_ITEMS = 10

PROMPT = """你是投标合规审查专家。下面是招标文件第 {page} 页的原文。

已经抽取到的要求（**不要重复这些**）：
{existing}

任务：找出这一页里**被遗漏的、对投标人（供应商/乙方）有约束力**的条款。

三条全部满足才输出：
1. 约束对象是投标人/供应商/乙方——不是采购人/甲方/招标人；
2. 是一条完整的要求，含义务表述（须/应/不得/必须/否则）或明确的数量、时间、格式规定；
3. 原文中有连续、可逐字引用的片段（不少于 10 个字），不改写。

**不要输出**（这些上一轮被大量误收）：
- 采购人/甲方的义务，例如「采购人应足额支付…」「甲方应执行国家劳动标准…」
- 名词定义，例如「联合体：指两个以上…」
- 平台操作指引，例如「登录…下载客户端」「进行注册绑定」
- 交叉引用，例如「见第四章」「详见投标人须知资料表」
- 半截话、表格碎片、目录项、通讯地址、联系方式
- 已经在上方列出的内容

每条输出四个字段：
{{"quote":"逐字原文片段","kind":"资格|格式|商务|技术|评分","target":"投标人|供应商|乙方","why":"一句话说明约束了什么"}}

只输出 JSON：{{"items":[...]}}，最多 {max_items} 条。

原文：
{text}
"""


def load_key() -> str:
    env = ROOT / ".env.local"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DEEPSEEK_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get("DEEPSEEK_API_KEY", "")


def norm(text: str) -> str:
    return re.sub(r"[\s，。；：、（）()【】\[\]“”\"'‘’]", "", text or "")


def call_model(key: str, prompt: str, timeout: int = 150) -> str:
    payload = json.dumps(
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 2048,
            "response_format": {"type": "json_object"},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def parse_items(raw: str) -> list[dict]:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    items = data.get("items") if isinstance(data, dict) else data
    return items if isinstance(items, list) else []


def build_pages(blocks: list[dict], requirements: list[dict]) -> dict[int, dict]:
    pages: dict[int, dict] = {}
    for block in blocks:
        page = block.get("page_index", 0)
        entry = pages.setdefault(page, {"text": [], "blocks": [], "existing": []})
        entry["text"].append(block.get("text") or "")
        entry["blocks"].append(block)
    for page, entry in pages.items():
        entry["text"] = "\n".join(part for part in entry["text"] if part).strip()
    for record in requirements:
        span = record.get("source_span") or {}
        page = span.get("page_index")
        statement = (record.get("statement") or "").strip()
        if page in pages and statement:
            pages[page]["existing"].append(statement[:70])
    return pages


def audit_page(key: str, page: int, entry: dict) -> dict:
    text = entry["text"][:MAX_PAGE_CHARS]
    existing = "\n".join(f"- {item}" for item in entry["existing"][:40]) or "（无）"
    prompt = PROMPT.format(page=page + 1, existing=existing, max_items=MAX_ITEMS, text=text)
    started = time.time()
    try:
        raw = call_model(key, prompt)
    except Exception as error:  # noqa: BLE001
        return {"page": page, "error": f"{type(error).__name__}: {error}", "items": []}

    page_norm = norm(entry["text"])
    kept, dropped = [], []
    for item in parse_items(raw):
        quote = (item.get("quote") or "").strip()
        if len(norm(quote)) < 10:
            dropped.append({"quote": quote, "reason": "too_short"})
            continue
        if norm(quote) not in page_norm:
            dropped.append({"quote": quote, "reason": "not_verbatim"})
            continue
        target = (item.get("target") or "").strip()
        if any(word in target for word in ("采购人", "甲方", "招标人")):
            dropped.append({"quote": quote, "reason": f"wrong_target:{target}"})
            continue
        kind = item.get("kind") or ""
        if kind not in ("资格", "格式", "商务", "技术", "评分"):
            dropped.append({"quote": quote, "reason": f"bad_kind:{kind}"})
            continue
        kept.append({"quote": quote, "kind": kind, "target": target, "why": item.get("why") or ""})

    block_id = entry["blocks"][0].get("block_id") if entry["blocks"] else ""
    return {
        "page": page,
        "block_id": block_id,
        "items": kept,
        "dropped": dropped,
        "raw_chars": len(raw or ""),
        "seconds": round(time.time() - started, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 页，用于验证")
    args = parser.parse_args()

    run_dir = Path(args.run)
    blocks = json.loads((run_dir / "blocks.json").read_text(encoding="utf-8"))
    requirements = json.loads((run_dir / "requirements.raw.json").read_text(encoding="utf-8"))
    pages = build_pages(blocks, requirements)

    out_dir = run_dir / "audit"
    (out_dir / "pages").mkdir(parents=True, exist_ok=True)

    key = load_key()
    if not key:
        sys.exit("缺少 DEEPSEEK_KEY")

    targets = sorted(pages.items())
    if args.limit:
        targets = targets[: args.limit]

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(audit_page, key, page, entry): page for page, entry in targets}
        for future in as_completed(futures):
            page = futures[future]
            result = future.result()
            results.append(result)
            (out_dir / "pages" / f"{page:03d}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            flag = "ERR" if result.get("error") else f"{len(result['items'])} 条"
            print(f"  p{page + 1:03d} {flag} {result.get('seconds', '')}s", flush=True)

    results.sort(key=lambda item: item["page"])
    errors = [item for item in results if item.get("error")]
    total_items = sum(len(item["items"]) for item in results)
    summary = {
        "run": str(run_dir),
        "model": MODEL,
        "pages": len(results),
        "errors": len(errors),
        "candidates": total_items,
        "dropped_verbatim": sum(len(item.get("dropped") or []) for item in results),
        "avg_seconds": round(sum(item.get("seconds", 0) for item in results) / max(len(results), 1), 1),
        "items": [
            {"page": item["page"], "block_id": item.get("block_id"), **candidate}
            for item in results
            for candidate in item["items"]
        ],
        "error_samples": [item["error"] for item in errors[:3]],
    }
    (out_dir / "audit_candidates.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in summary.items() if k != "items"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
