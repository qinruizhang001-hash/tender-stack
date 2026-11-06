#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""map_to_ledger.py — 把 rag-tender 的要求记录映射成 ai-bid-document 的 15 列台账

这一层只做一件事：**格式适配**。输入是 rag-tender 导出的 JSON（HTTP 接口产物），
输出是 requirements.csv（15 列）+ provenance.jsonl（溯源旁挂）。不 import 任何
一方代码，任何一方升级都不影响本层的输入输出契约。

用法：
  python adapter/map_to_ledger.py --run out/armA_real --scan out/scan_real \
      --contract contract --out out/armA_real/ledger
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
from collections import Counter
from pathlib import Path

NUMBERING = re.compile(r"^\s*(?:[（(]?\d+(?:\.\d+)*[）)]?[、.．]?|[一二三四五六七八九十]+[、.．])\s*")
HEADING = re.compile(
    r"^(第[一二三四五六七八九十百]+[章部分节]|[一二三四五六七八九十]+、|\d+(?:\.\d+){0,2}\s*[^\d\s])"
)
# 章节标题行：章/节/一、/（一）/1.1 开头，短、且不是完整句子
HEADING_LINE = re.compile(
    r"^\s*(第[一二三四五六七八九十百]+[章节部分]|[一二三四五六七八九十]+[、.]|[（(][一二三四五六七八九十]+[）)]|\d+(?:\.\d+){0,3}[、.\s])"
)
MAX_BLOCK_LOOKBACK = 12

# 审计通道的噪声模式：定义、平台流程、联系方式、交叉引用、纯政策表述
AUDIT_NOISE = re.compile(
    r"(登录|注册|下载|客户端|用户指南|操作指南|通讯地址|联系电话|邮政编码|传真|邮箱"
    r"|是指|指两个以上的|定义为|见第[一二三四五六七八九十\d]+章|详见|参见)"
)
CLARIFY_WORDS = [
    "原则上", "或同等", "视情况", "另行", "酌情", "参照执行", "按有关规定",
    "其他情形", "等类似", "不少于两家", "视同", "另行约定",
]
SERVICE_WORDS = ["售后", "培训", "驻场", "备件", "响应时间", "运维", "巡检", "质保服务"]


def norm(value: str) -> str:
    return re.sub(r"[\s\u3000，。、；：（）()【】\[\]“”\"'·,.;:!?\-—_/]", "", value or "")


def load_lines(path: Path) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if "\t" not in raw:
            continue
        head, _, text = raw.partition("\t")
        try:
            rows.append((int(head), text.strip()))
        except ValueError:
            continue
    return rows


def locate_line(quote: str, lines: list[tuple[int, str]]) -> int:
    """在带行号文本里反查 quote 所在行；找不到返回 -1。"""
    target = norm(quote)
    if not target:
        return -1
    for number, text in lines:
        if target in norm(text):
            return number
    # 退化：取最长公共片段所在行
    best, best_ratio = -1, 0.0
    for number, text in lines:
        ratio = difflib.SequenceMatcher(None, target, norm(text)).ratio()
        if ratio > best_ratio:
            best, best_ratio = number, ratio
    return best if best_ratio >= 0.6 else -1


def infer_section(line_no: int, lines: list[tuple[int, str]]) -> str:
    if line_no < 0:
        return ""
    index = next((i for i, (number, _) in enumerate(lines) if number == line_no), None)
    if index is None:
        return ""
    for back in range(index, max(-1, index - 300), -1):
        text = lines[back][1]
        if not text or len(text) > 40:
            continue
        if HEADING.match(text):
            return text.strip()
    return ""


def strength_of(record: dict) -> str:
    text = record.get("statement") or ""
    if any(word in text for word in ["不得", "严禁", "禁止", "不允许", "不接受"]):
        return "禁止"
    if any(word in text for word in ["必须", "须", "不低于", "不少于", "至少", "不得超过"]):
        return "必须"
    if any(word in text for word in ["应", "需提供", "应提供", "须提交"]):
        return "应"
    if any(word in text for word in ["宜", "建议", "优先", "鼓励"]):
        return "宜"
    return "可"


SECTION_TYPE_RULES = [
    ("资格", ["资格审查", "资格要求", "投标人资格", "资质要求"]),
    ("评分", ["评标办法", "评分标准", "评标程序", "评审因素", "打分"]),
    ("格式", ["投标文件格式", "格式要求", "装订", "密封", "份数"]),
    ("商务", ["合同条款", "合同主要条款", "商务条款", "付款", "采购需求"]),
    ("技术", ["技术需求", "技术规格", "技术要求", "技术标准", "服务要求", "采购需求"]),
    ("服务", ["售后服务", "服务方案", "运维"]),
]


def type_of(record: dict, category_map: dict, section: str = "") -> str:
    """parsing.md §十：句内优先、章节兜底；评标办法/合同条款两章为强信号。"""
    category = record.get("category") or "other"
    base = category_map.get(category, "其他")
    text = record.get("statement") or ""
    if base == "其他" and any(word in text for word in SERVICE_WORDS):
        return "服务"
    if record.get("evaluation_type") == "score":
        return "评分"
    if base != "其他":
        return base
    # 章节兜底：句内无线索时按所属章节标题判断
    for item_type, keywords in SECTION_TYPE_RULES:
        if section and any(word in section for word in keywords):
            return item_type
    return "其他"


KIND_TO_TYPE = {"资格": "资格", "格式": "格式", "商务": "商务", "技术": "技术", "评分": "评分"}


def audit_type(kind: str, quote: str) -> str:
    """审计通道自带 kind；缺失时退回关键词判断。"""
    base = KIND_TO_TYPE.get(kind or "")
    if base:
        return base
    for item_type, keywords in SECTION_TYPE_RULES:
        if any(word in quote for word in keywords):
            return item_type
    if any(word in quote for word in ["盖章", "签字", "密封", "装订", "正本", "副本", "份数", "格式", "签章"]):
        return "格式"
    return "其他"


def response_of(handling: str) -> str:
    return {
        "technical": "响应",
        "evidence": "证明",
        "submission": "证明",
        "fill": "证明",
        "commercial": "承诺",
        "todo": "待办",
        "manual": "人工",
    }.get(handling or "", "待办")


def mark_duplicates(rows: list[dict]) -> None:
    """parsing.md §八：归一化相同 / 相似度≥0.90 且 type 相同 / 子集关系。"""
    for i, row in enumerate(rows):
        if row["dup_of"]:
            continue
        base = norm(row["requirement"])
        if len(base) < 8:
            continue
        for j in range(i + 1, len(rows)):
            other = rows[j]
            if other["dup_of"] or other["type"] != row["type"]:
                continue
            target = norm(other["requirement"])
            if len(target) < 8:
                continue
            if target == base:
                other["dup_of"] = row["req_id"]
                continue
            if target in base or base in target:
                other["dup_of"] = row["req_id"]
                continue
            ratio = difflib.SequenceMatcher(None, base, target).ratio()
            if ratio >= 0.90:
                other["dup_of"] = row["req_id"]


def find_verbatim(quote: str, block_text: str, word: str = "") -> str:
    """在块原文里找回逐字片段；扫描层的行文本可能与块文本有空白差异。"""
    if quote and quote in block_text:
        return quote
    target = norm(word or quote)
    if not target:
        return ""
    for sentence in re.split(r"(?<=[。；;])", block_text):
        if target in norm(sentence):
            return sentence.strip(" \n")
    return ""


def expand_quote(quote: str, block_text: str) -> str:
    """引文若只是半句/片段，扩到所在整句，保证「原文」能独立支撑结论。

    注意：不能走 find_verbatim 的“原文已包含就直接返回”分支——
    片段本来就逐字存在于块里，那样永远扩不开。
    """
    quote = (quote or "").strip()
    if not quote or quote[-1] in "。；;！？!?":
        return quote
    target = norm(quote)
    if not target:
        return quote
    limit = max(len(quote) * 4, 120)
    for sentence in re.split(r"(?<=[。；;])", block_text):
        if target not in norm(sentence):
            continue
        sentence = sentence.strip(" \n")
        # 表格块常常没有句末标点，整块会被当成一句；这种“扩张”宁可不要
        if len(sentence) <= len(quote) or len(sentence) > limit or "\n" in sentence:
            continue
        return sentence
    return quote


def verbatim_span(quote: str, text: str) -> str:
    """把清洗过的引文映射回块原文的连续片段（保留原有换行/空格）。

    门禁的 g3.quote_verbatim 要求 source_quote 逐字出现在所属 block 里；
    审计通道给的引文经过空白归一，直接落库会整批 FAIL。
    """
    if not quote:
        return ""
    if quote in text:
        return quote
    target = norm(quote)
    if not target:
        return ""
    chars: list[str] = []
    index: list[int] = []
    for position, char in enumerate(text):
        piece = norm(char)
        if piece:
            chars.append(piece)
            index.append(position)
    start = "".join(chars).find(target)
    if start < 0 or start + len(target) > len(index):
        return ""
    return text[index[start]: index[start + len(target) - 1] + 1]


def is_heading(line: str) -> bool:
    """标题行判定：短、带编号、不是句子、不是条款正文、不是表格行。"""
    if not line or len(line) > 32:
        return False
    if any(char in line for char in "。；;：:|"):
        return False
    if re.match(r"^\s*\d{2,}\s", line):  # 「01 标段…」「27 序号 名称」这类表格行
        return False
    if re.search(r"\d+\.\d{2,}", line):  # 金额 / 编号，如 366.2853
        return False
    if re.search(r"\s{2,}\d", line):  # 表格列拼出来的行
        return False
    if not HEADING_LINE.match(line):
        return False
    core = NUMBERING.sub("", line).strip()
    if len(core) < 2:
        return False
    if any(word in core for word in ("投标人", "供应商", "采购人", "应当", "不得", "必须")):
        return False
    if any(word in core for word in ("序号", "名称", "规格", "数量", "单价", "合计", "备注")):
        return False
    return True


def block_section(block: dict, quote: str, ordered: list[dict], index: dict[str, int]) -> str:
    """从块文本里找最近的标题行；找不到就沿阅读序往前翻块。

    比 infer_section（在扫描行里按行号回溯）稳定：块文本是解析器给的原文，
    不会因为扫描行错位而把整句要求当成章节。
    """
    def heading_of(text: str, limit: int | None = None) -> str:
        body = text[:limit] if limit and limit > 0 else text
        for line in reversed(body.splitlines()):
            line = line.strip()
            if is_heading(line):
                return line
        return ""

    text = block.get("text") or ""
    if not text:
        return ""
    pos = text.find(quote) if quote else -1
    found = heading_of(text, pos if pos > 0 else None)
    if found:
        return found

    at = index.get(block.get("block_id") or "")
    if at is None:
        return ""
    for prev in reversed(ordered[max(0, at - MAX_BLOCK_LOOKBACK):at]):
        found = heading_of(prev.get("text") or "")
        if found:
            return found
    return ""


def build(run_dir: Path, scan_dir: Path, contract_dir: Path, prescan: Path | None = None, audit: Path | None = None) -> dict:
    schema = json.loads((contract_dir / "ledger_schema.json").read_text(encoding="utf-8"))
    category_map = schema["category_map"]
    owner_map = schema["owner_map"]

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    requirements = json.loads((run_dir / "requirements.raw.json").read_text(encoding="utf-8"))
    decisions = {
        item["requirement_id"]: item
        for item in json.loads((run_dir / "decisions.raw.json").read_text(encoding="utf-8"))
    }
    blocks = {
        item["block_id"]: item
        for item in json.loads((run_dir / "blocks.json").read_text(encoding="utf-8"))
    }
    ordered = sorted(
        blocks.values(), key=lambda item: (item.get("page_index", 9999), item.get("reading_order", 9999))
    )
    block_index = {item.get("block_id"): position for position, item in enumerate(ordered)}

    lines_file = next(iter(sorted(scan_dir.glob("*.lines.txt"))), None)
    lines = load_lines(lines_file) if lines_file else []

    # 稳定排序：页序 → 块阅读序 → 块内字符偏移
    def sort_key(record: dict):
        span = record.get("source_span") or {}
        block = blocks.get(span.get("block_id") or "", {})
        return (
            block.get("page_index", 9999),
            block.get("reading_order", 9999),
            span.get("quote", ""),
        )

    pending: list[dict] = []
    for record in sorted(requirements, key=sort_key):
        span = record.get("source_span") or {}
        quote = span.get("quote") or ""
        block_id = span.get("block_id") or ""
        block = blocks.get(block_id, {})
        line_no = locate_line(quote, lines) if lines else -1
        section = block_section(block, quote, ordered, block_index)
        if not section and lines:
            section = infer_section(line_no, lines)
        if not section:
            section = f"第{block.get('page_index', 0) + 1}页"
        quote = expand_quote(quote, block.get("text") or "")

        item_type = type_of(record, category_map, section)
        decision = decisions.get(record["id"], {})
        status = "需复核" if decision.get("status") == "needs_review" else "待响应"

        clarify = "Y" if any(word in quote for word in CLARIFY_WORDS) else "N"
        if record.get("confidence") is not None and record["confidence"] < 0.6:
            clarify = "Y"

        pending.append(
            {
                "_page": block.get("page_index", 9999),
                "_line": line_no,
                "_prov": {
                    "block_id": block_id,
                    "page_id": span.get("page_id"),
                    "page_index": span.get("page_index", block.get("page_index")),
                    "quote_hash": record.get("fingerprint"),
                    "confidence": record.get("confidence"),
                    "extraction_method": record.get("extraction_method"),
                    "rag_tender_requirement_id": record.get("id"),
                    "source_span_id": span.get("id"),
                    "decision_status": decision.get("status"),
                    "decision_summary": decision.get("summary"),
                },
                "req_id": "",
                "section": section,
                "requirement": record.get("statement") or "",
                "type": item_type,
                "mandatory": "Y" if record.get("obligation") == "mandatory" else "N",
                "strength": strength_of(record),
                "source_quote": quote,
                "source_ver": "v1",
                "line_no": line_no,
                "response_type": response_of(record.get("handling_mode")),
                "need_clarify": clarify,
                "dup_of": "",
                "owner": owner_map.get(item_type, "待定"),
                "status": status,
                "note": (
                    f"block_id={block_id}; via={record.get('extraction_method', 'llm')}; "
                    f"conf={record.get('confidence')}; stage={record.get('evaluation_stage')}"
                ),
            }
        )

    # 合并扫描层确认的条目（仅第二轮：--prescan 提供时）
    merged_scan = 0
    if prescan and prescan.exists():
        known = json.loads(prescan.read_text(encoding="utf-8"))
        for hit in known.get("by_quote") or []:
            quote = hit.get("quote") or ""
            if not quote:
                continue
            # 以文本定位所属块（block_id 每次解析会变，不能作对接键）
            block = next((b for b in blocks.values() if quote in (b.get("text") or "")), None)
            if block is None:
                continue
            quote_norm = norm(quote)
            # 与已有条目（模型条目）比对：引文或要求正文任一处被覆盖即视为重复
            if any(
                quote_norm
                and (
                    quote_norm in norm(row["source_quote"])
                    or norm(row["source_quote"]) in quote_norm
                    or quote_norm in norm(row["requirement"])
                    or norm(row["requirement"]) in quote_norm
                )
                for row in pending
            ):
                continue
            line_no = hit.get("line") or (locate_line(quote, lines) if lines else -1)
            section = infer_section(line_no, lines) if lines else ""
            if not section:
                section = f"第{block.get('page_index', 0) + 1}页"
            fake = {"statement": quote, "category": "other", "obligation": "mandatory",
                    "handling_mode": "submission"}
            item_type = type_of(fake, category_map, section)
            if item_type == "其他":
                item_type = "格式" if any(w in quote for w in ["密封", "签字", "盖章", "正本", "副本", "装订"]) else "其他"
            pending.append(
                {
                    "_page": block.get("page_index", 9999),
                    "_line": line_no,
                    "_prov": {
                        "block_id": block.get("block_id"),
                        "page_id": block.get("page_id"),
                        "page_index": block.get("page_index"),
                        "quote_hash": None,
                        "confidence": None,
                        "extraction_method": "scan",
                        "rag_tender_requirement_id": None,
                        "source_span_id": None,
                        "decision_status": "deterministic",
                        "decision_summary": hit.get("group"),
                    },
                    "req_id": "",
                    "section": section,
                    "requirement": quote,
                    "type": item_type,
                    "mandatory": "Y",
                    "strength": strength_of(fake),
                    "source_quote": quote,
                    "source_ver": "v1",
                    "line_no": line_no,
                    "response_type": "证明",
                    "need_clarify": "N",
                    "dup_of": "",
                    "owner": owner_map.get(item_type, "待定"),
                    "status": "待响应",
                    "note": (
                        f"block_id={block.get('block_id')}; via=scan; group={hit.get('group')}; "
                        f"word={hit.get('word')}; line={line_no}"
                    ),
                }
            )
            merged_scan += 1

    # 合并审计通道（第三通道：只捞"漏掉的"，逐字引文）
    merged_audit = 0
    if audit and audit.exists():
        payload = json.loads(audit.read_text(encoding="utf-8"))
        for hit in payload.get("items") or []:
            quote = (hit.get("quote") or "").strip()
            if not quote:
                continue
            quote_norm = norm(quote)
            if len(quote_norm) < 12:
                continue
            kind = hit.get("kind") or ""
            # 审计通道偏召回，40% 是定义/流程/采购人义务；先在入口挡掉最明显的噪声
            if AUDIT_NOISE.search(quote):
                continue
            if quote.rstrip().endswith(("：", ":")):
                continue
            if kind == "其他" and not any(
                cue in quote for cue in ("须", "应", "不得", "必须", "否则", "承诺", "应当", "不予")
            ):
                continue
            block = next(
                (item for item in blocks.values() if quote_norm in norm(item.get("text") or "")),
                None,
            )
            if block is None:
                continue
            if any(
                quote_norm in norm(row["requirement"])
                or norm(row["requirement"]) in quote_norm
                or quote_norm in norm(row["source_quote"])
                or norm(row["source_quote"]) in quote_norm
                for row in pending
            ):
                continue
            item_type = audit_type(hit.get("kind") or "", quote)
            line_no = locate_line(quote, lines) if lines else -1
            section = block_section(block, quote, ordered, block_index)
            if not section and lines:
                section = infer_section(line_no, lines)
            if not section:
                section = f"第{block.get('page_index', 0) + 1}页"
            pending.append(
                {
                    "_page": block.get("page_index", 9999),
                    "_line": line_no,
                    "_prov": {
                        "block_id": block.get("block_id"),
                        "page_id": block.get("page_id"),
                        "page_index": block.get("page_index"),
                        "quote_hash": None,
                        "confidence": None,
                        "extraction_method": "audit",
                        "rag_tender_requirement_id": None,
                        "source_span_id": None,
                        "decision_status": "audit_candidate",
                        "decision_summary": hit.get("why"),
                    },
                    "req_id": "",
                    "section": section,
                    "requirement": quote,
                    "type": item_type,
                    "mandatory": "Y",
                    "strength": strength_of({"statement": quote}),
                    "source_quote": verbatim_span(quote, block.get("text") or "") or quote,
                    "source_ver": "v1",
                    "line_no": line_no,
                    "response_type": "证明" if item_type in ("资格", "格式") else "响应",
                    "need_clarify": "N",
                    "dup_of": "",
                    "owner": owner_map.get(item_type, "待定"),
                    "status": "需复核",
                    "note": f"block_id={block.get('block_id')}; via=audit; why={hit.get('why', '')}",
                }
            )
            merged_audit += 1

    # 统一编号：页序 → 行号 → 引文
    pending.sort(key=lambda row: (row["_page"], row["_line"] if row["_line"] > 0 else 99999, row["source_quote"]))
    rows: list[dict] = []
    provenance: list[dict] = []
    for index, row in enumerate(pending, start=1):
        row["req_id"] = f"R-{index:03d}"
        prov = row.pop("_prov")
        prov["req_id"] = row["req_id"]
        provenance.append(prov)
        rows.append(row)

    mark_duplicates(rows)
    for row in rows:
        if row["dup_of"]:
            row["status"] = "重复合并"
    return {
        "rows": rows,
        "provenance": provenance,
        "meta": meta,
        "schema": schema,
        "merged_scan": merged_scan,
        "merged_audit": merged_audit,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument("--scan", required=True)
    parser.add_argument("--contract", default="contract")
    parser.add_argument("--out", required=True)
    parser.add_argument("--prescan", default="", help="known_issues.json（第二轮合并扫描层条目）")
    parser.add_argument("--audit", default="", help="audit_candidates.json（第三通道补漏结果）")
    args = parser.parse_args()

    run_dir, scan_dir, contract_dir, out_dir = (
        Path(args.run), Path(args.scan), Path(args.contract), Path(args.out)
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    prescan = Path(args.prescan) if args.prescan else None
    audit = Path(args.audit) if args.audit else None
    result = build(run_dir, scan_dir, contract_dir, prescan, audit)
    rows = result["rows"]
    columns = [column["name"] for column in result["schema"]["columns"]]

    with (out_dir / "requirements.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in columns})

    with (out_dir / "requirements.provenance.jsonl").open("w", encoding="utf-8") as handle:
        for item in result["provenance"]:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    report = {
        "total": len(rows),
        "columns": columns,
        "by_type": dict(Counter(row["type"] for row in rows)),
        "by_mandatory": dict(Counter(row["mandatory"] for row in rows)),
        "by_strength": dict(Counter(row["strength"] for row in rows)),
        "by_owner": dict(Counter(row["owner"] for row in rows)),
        "dup_merged": sum(1 for row in rows if row["dup_of"]),
        "need_clarify": sum(1 for row in rows if row["need_clarify"] == "Y"),
        "line_located": sum(1 for row in rows if row["line_no"] > 0),
        "line_missing": sum(1 for row in rows if row["line_no"] <= 0),
        "section_inferred": sum(1 for row in rows if not row["section"].startswith("第")),
        "with_block_id": sum(1 for row in rows if "block_id=" in row["note"]),
        "with_quote": sum(1 for row in rows if row["source_quote"].strip()),
        "by_via": dict(
            Counter(
                "scan" if "via=scan" in row["note"]
                else ("audit" if "via=audit" in row["note"] else "llm")
                for row in rows
            )
        ),
        "merged_audit": result.get("merged_audit", 0),
        "pages": result["meta"].get("page_count"),
        "items_per_10_pages": round(len(rows) / max(result["meta"].get("page_count") or 1, 1) * 10, 1),
    }
    (out_dir / "mapping_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
