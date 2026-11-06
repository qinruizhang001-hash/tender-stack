#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gate_engine.py — 六阶段门禁引擎（替换 rag-tender 默认的 needs_review 阈值）

默认行为（rag-tender 原生）：硬性要求找不到证据 → 整条 needs_review。
问题：本机无企业资料库时 206/206 全部 needs_review，等于零信息量，无法排序整改。

本层改成 ai-bid-document 的门禁口径：
  G0 输入可读 → G1 解析口径 → G2 映射闭合（硬）→ G3 核查清零（硬）
每条记录按命中的规则分级 BLOCKER / WARN，BLOCKER 阻断交付。
rag-tender 的 needs_review 状态**保留不改**，作为一条输入信号参与判定。

用法：
  python adapter/gate_engine.py --ledger out/armA_real/ledger --scan out/scan_real \
      --contract contract --run out/armA_real
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

# 只认"我方未填"的占位符；不认 XX——招标文件本身常用 XX 做地址/名称脱敏
PLACEHOLDER = re.compile(r"\[待确认|\[待商务确认|待补充|TBD|待填写")
GREY = ["基本响应", "部分响应", "大致满足", "原则上满足", "基本符合"]
TOC_LINE = re.compile(r"(\.{5,}|…{3,}|·{5,})")
HEADING_ONLY = re.compile(r"^(第[一二三四五六七八九十百]+[章部分节]|[一二三四五六七八九十]+、|\d+(?:\.\d+){0,2}\s*[^\d\s])")


def unverifiable(row: dict) -> str:
    """判定该条能否独立回答"满足/不满足"（parsing.md §一）。返回原因，空串表示可判定。"""
    text = (row["requirement"] or "").strip()
    compact = re.sub(r"\s+", "", text)
    if TOC_LINE.search(text):
        return "目录行"
    if compact and compact == re.sub(r"\s+", "", row["section"] or ""):
        return "章节标题"
    if HEADING_ONLY.match(compact) and len(compact) <= 30:
        return "标题残句"
    if len(compact) < 8:
        return "过短"
    if text[:1] in {"，", "、", "。", "；", "）", ")"}:
        return "跨行残句"
    return ""


def read_csv(path: Path) -> tuple[list[dict], list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader), list(reader.fieldnames or [])


def check_g0(meta: dict, blocks: list[dict]) -> list[dict]:
    pages = meta.get("page_count") or 0
    with_text = len({block["page_index"] for block in blocks if block.get("text", "").strip()})
    results = []

    def add(rule_id, ok, detail, severity):
        results.append({"rule": rule_id, "severity": severity, "pass": ok, "detail": detail})

    add("g0.pages", pages >= 1, f"页数={pages}", "BLOCKER")
    add("g0.blocks", with_text >= 1, f"有文本的页数={with_text}", "BLOCKER")
    scanned_ratio = (pages - with_text) / pages if pages else 1.0
    add(
        "g0.scanned_ratio",
        scanned_ratio <= 0.2,
        f"无文本页占比={scanned_ratio:.1%}",
        "WARN",
    )
    return results


def check_g1(rows: list[dict], pages: int) -> list[dict]:
    density = len(rows) / max(pages, 1) * 10
    without_quote = sum(1 for row in rows if not row["source_quote"].strip())
    bad = [row for row in rows if unverifiable(row)]
    dup_unmerged = sum(
        1
        for index, row in enumerate(rows)
        if not row["dup_of"]
        and any(
            other["dup_of"] == row["req_id"] for other in rows
        )
        and row["status"] != "重复合并"
    )
    return [
        {
            "rule": "g1.density_low",
            "severity": "BLOCKER",
            "pass": density >= 15,
            "detail": f"每10页条目数={density:.1f}（阈值≥15）",
        },
        {
            "rule": "g1.density_high",
            "severity": "WARN",
            "pass": density <= 30,
            "detail": f"每10页条目数={density:.1f}（阈值≤30）",
        },
        {
            "rule": "g1.dup_merged",
            "severity": "WARN",
            "pass": dup_unmerged == 0,
            "detail": f"已合并重复={sum(1 for r in rows if r['dup_of'])}，未合并={dup_unmerged}",
        },
        {
            "rule": "g1.quote_trace",
            "severity": "BLOCKER",
            "pass": without_quote == 0,
            "detail": f"无 source_quote 条目={without_quote}",
        },
        {
            "rule": "g1.verifiable",
            "severity": "WARN",
            "pass": len(bad) == 0,
            "detail": f"不可独立判定（目录/标题/残句）={len(bad)}",
            "records": [row["req_id"] for row in bad][:50],
        },
    ]


def check_g2(rows: list[dict]) -> list[dict]:
    mandatory = [row for row in rows if row["mandatory"] == "Y"]
    unmapped = [row for row in mandatory if not row["section"].strip()]
    pending_owner = [row for row in rows if row["owner"] == "待定"]
    no_line = [row for row in rows if int(row["line_no"] or -1) <= 0]
    return [
        {
            "rule": "g2.mandatory_mapped",
            "severity": "BLOCKER",
            "pass": len(unmapped) == 0,
            "detail": f"必答条目={len(mandatory)}，未映射章节={len(unmapped)}",
            "records": [row["req_id"] for row in unmapped][:50],
        },
        {
            "rule": "g2.owner_assigned",
            "severity": "WARN",
            "pass": len(pending_owner) == 0,
            "detail": f"owner=待定 条目={len(pending_owner)}",
            "records": [row["req_id"] for row in pending_owner][:50],
        },
        {
            "rule": "g2.line_located",
            "severity": "WARN",
            "pass": len(no_line) == 0,
            "detail": f"无法回查行号条目={len(no_line)}",
            "records": [row["req_id"] for row in no_line][:50],
        },
    ]


def check_g3(rows: list[dict], blocks: list[dict], contract: dict) -> list[dict]:
    block_text = {block["block_id"]: block.get("text", "") for block in blocks}
    not_verbatim, no_block, placeholder, grey = [], [], [], []
    for row in rows:
        quote = row["source_quote"]
        match = re.search(r"block_id=([0-9a-zA-Z]+)", row.get("note") or "")
        block_id = match.group(1) if match else ""
        if not block_id:
            no_block.append(row["req_id"])
        elif quote and quote not in block_text.get(block_id, ""):
            not_verbatim.append(row["req_id"])
        if PLACEHOLDER.search(quote) or PLACEHOLDER.search(row["requirement"]):
            placeholder.append(row["req_id"])
        if any(word in quote for word in GREY):
            grey.append(row["req_id"])
    return [
        {
            "rule": "g3.quote_verbatim",
            "severity": "BLOCKER",
            "pass": len(not_verbatim) == 0,
            "detail": f"quote 非原文逐字={len(not_verbatim)}",
            "records": not_verbatim[:50],
        },
        {
            "rule": "g3.block_id",
            "severity": "BLOCKER",
            "pass": len(no_block) == 0,
            "detail": f"无 block_id 条目={len(no_block)}",
            "records": no_block[:50],
        },
        {
            "rule": "g3.placeholder",
            "severity": "WARN",
            "pass": len(placeholder) == 0,
            "detail": f"占位符残留={len(placeholder)}",
            "records": placeholder[:50],
        },
        {
            "rule": "g3.grey_wording",
            "severity": "WARN",
            "pass": len(grey) == 0,
            "detail": f"灰色表述={len(grey)}",
            "records": grey[:50],
        },
    ]


def apply_review_policy(rows: list[dict], gate_results: dict, policy: dict) -> dict:
    """用门禁结果替换 rag-tender 的默认 needs_review 判定，产出分级。"""
    min_conf = 0.75
    for trigger in policy.get("triggers", []):
        if trigger["id"] == "nr.low_confidence":
            min_conf = trigger.get("threshold", 0.75)

    blocker_rules = {
        result["rule"]
        for gate, results in gate_results.items()
        for result in results
        if result["severity"] == "BLOCKER" and not result["pass"]
    }
    warn_rules = {
        result["rule"]
        for gate, results in gate_results.items()
        for result in results
        if result["severity"] == "WARN" and not result["pass"]
    }

    def records_of(rules: set[str]) -> set[str]:
        ids: set[str] = set()
        for gate, results in gate_results.items():
            for result in results:
                if result["rule"] in rules:
                    ids.update(result.get("records") or [])
        return ids

    blocker_records = records_of(blocker_rules)
    warn_records = records_of(warn_rules)

    graded = []
    for row in rows:
        req_id = row["req_id"]
        hits: list[str] = []
        severity = "OK"

        # 记录级 BLOCKER：quote 非逐字 / 无 block_id / 必答未映射
        if req_id in blocker_records:
            severity = "BLOCKER"
            hits.extend(sorted(blocker_rules))

        confidence = None
        match = re.search(r"conf=([0-9.]+|None)", row.get("note") or "")
        if match and match.group(1) != "None":
            confidence = float(match.group(1))
        if confidence is not None and confidence < min_conf:
            hits.append("nr.low_confidence")
            severity = "BLOCKER" if severity == "BLOCKER" else "WARN"
        if row["need_clarify"] == "Y":
            hits.append("nr.clarify")
            severity = "BLOCKER" if severity == "BLOCKER" else "WARN"
        if int(row["line_no"] or -1) <= 0:
            hits.append("nr.no_line")
            severity = "BLOCKER" if severity == "BLOCKER" else "WARN"
        if row["status"] == "需复核" and severity == "OK":
            # rag-tender 原生 needs_review 作为一条信号保留（降级为 WARN）
            hits.append("rt.needs_review")
            severity = "WARN"

        # 台账 status 列：BLOCKER/WARN 一律落 需复核，便于人工按优先级过
        if severity in {"BLOCKER", "WARN"} and row["status"] != "重复合并":
            row["status"] = "需复核"
        row["_severity"] = severity
        row["_gate_hits"] = hits
        graded.append(row)

    return {
        "records": graded,
        "blocker_count": sum(1 for row in graded if row["_severity"] == "BLOCKER"),
        "warn_count": sum(1 for row in graded if row["_severity"] == "WARN"),
        "ok_count": sum(1 for row in graded if row["_severity"] == "OK"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--scan", required=True)
    parser.add_argument("--contract", default="contract")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    ledger_dir = Path(args.ledger)
    run_dir = Path(args.run)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    contract = json.loads((Path(args.contract) / "gates.json").read_text(encoding="utf-8"))
    rows, columns = read_csv(ledger_dir / "requirements.csv")
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    blocks = json.loads((run_dir / "blocks.json").read_text(encoding="utf-8"))

    gate_results = {
        "G0": check_g0(meta, blocks),
        "G1": check_g1(rows, meta.get("page_count") or 0),
        "G2": check_g2(rows),
        "G3": check_g3(rows, blocks, contract),
    }
    graded = apply_review_policy(rows, gate_results, contract["needs_review_policy"])

    # 回写台账 status（只改 status 列，列结构不动）
    with (out_dir / "requirements.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in graded["records"]:
            writer.writerow({key: row.get(key, "") for key in columns})

    # 溯源旁挂与映射报告一并带过来，保证交付目录自包含
    for name in ("requirements.provenance.jsonl", "mapping_report.json"):
        source = ledger_dir / name
        if source.exists():
            (out_dir / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    with (out_dir / "requirements.graded.jsonl").open("w", encoding="utf-8") as handle:
        for row in graded["records"]:
            handle.write(
                json.dumps(
                    {
                        "req_id": row["req_id"],
                        "severity": row["_severity"],
                        "gate_hits": row["_gate_hits"],
                        "status": row["status"],
                        "type": row["type"],
                        "mandatory": row["mandatory"],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    # 阶段级判定：任一 BLOCKER 规则未过 → 该阶段不通过
    stages = []
    for gate_id, results in gate_results.items():
        failed_blocker = [item["rule"] for item in results if item["severity"] == "BLOCKER" and not item["pass"]]
        failed_warn = [item["rule"] for item in results if item["severity"] == "WARN" and not item["pass"]]
        stages.append(
            {
                "gate": gate_id,
                "passed": not failed_blocker,
                "failed_blockers": failed_blocker,
                "failed_warns": failed_warn,
                "checks": results,
            }
        )

    report = {
        "contract_version": contract.get("version"),
        "gate_order": ["G0", "G1", "G2", "G3"],
        "stages": stages,
        "overall_passed": all(stage["passed"] for stage in stages),
        "records": {
            "total": len(graded["records"]),
            "blocker": graded["blocker_count"],
            "warn": graded["warn_count"],
            "ok": graded["ok_count"],
        },
        "replaces": contract["needs_review_policy"]["replaces_default"],
        "note": "rag-tender 原生 needs_review 状态保留，降级为 rt.needs_review 信号参与分级，不再单独决定放行。",
    }
    (out_dir / "gate_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "overall_passed": report["overall_passed"],
                "stages": [
                    {
                        "gate": stage["gate"],
                        "passed": stage["passed"],
                        "blockers": stage["failed_blockers"],
                        "warns": stage["failed_warns"],
                    }
                    for stage in stages
                ],
                "records": report["records"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
