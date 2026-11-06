#!/usr/bin/env python3
# LAYER: compensation
# -*- coding: utf-8 -*-
"""kb.py — 案例知识沉淀（inbox / approved / proposed）+ 只读检索。

边界（见 docs/LAYERS.md）：
- 自动写入只进 kb/inbox/，状态 draft；
- 晋升到 approved 或 gold 必须人工 approve + linked_test；
- 检索只返回只读上下文，不参与解析主键。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB = ROOT / "kb"
INBOX = KB / "inbox"
APPROVED = KB / "approved"
PROPOSED = KB / "proposed"
SEALED_INDEX = KB / "sealed" / "index.json"
GOLD_DIR = ROOT / "contract" / "gold"

KINDS = {"gold_miss", "false_positive", "table_row", "layout", "ocr", "rule_gap"}
REQUIRED = ("case_id", "source_doc_hash", "doc_type", "kind", "evidence", "lesson", "created_at")
COVERAGE_THRESHOLD = 0.8


def _sys_path() -> None:
    for path in (ROOT / "adapter", ROOT / "scripts"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def file_hash(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def case_id(source_hash: str, kind: str, page, text: str) -> str:
    material = f"{source_hash}|{kind}|{page}|{text[:120]}"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def validate(card: dict) -> list[str]:
    errors = [f"缺少字段 {name}" for name in REQUIRED if not card.get(name)]
    if card.get("kind") and card["kind"] not in KINDS:
        errors.append(f"kind 非法：{card['kind']}")
    patch = card.get("patch_candidate")
    if patch and patch.get("status") not in {"draft", "approved", "rejected"}:
        errors.append("patch_candidate.status 必须是 draft/approved/rejected")
    return errors


def load_jsonl(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def append_jsonl(path: str | Path, rows: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def sealed_hashes(index_path: str | Path = SEALED_INDEX) -> set[str]:
    path = Path(index_path)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return set(data.get("doc_hashes") or [])


def _coverage(needle: str, texts: list[str]) -> float:
    _sys_path()
    from eval_chunks import coverage, normalise

    return coverage(normalise(needle), [normalise(text) for text in texts])


def absorb(
    chunks_path: str | Path,
    gold_path: str | Path,
    out_dir: str | Path = INBOX,
    *,
    doc_type: str = "",
    source_pdf: str | Path | None = None,
    source_hash: str = "",
    sealed_index: str | Path = SEALED_INDEX,
) -> dict:
    """从一次导出的 requirement + 金标生成 inbox 卡片。"""
    chunks_path, gold_path, out_dir = Path(chunks_path), Path(gold_path), Path(out_dir)
    rows = load_jsonl(chunks_path)
    gold = json.loads(gold_path.read_text(encoding="utf-8")) if gold_path.exists() else []
    if isinstance(gold, dict):
        gold = gold.get("items") or gold.get("gold") or []

    doc_hash = source_hash or (file_hash(source_pdf) if source_pdf else "")
    if not doc_hash:
        doc_hash = hashlib.sha256(chunks_path.read_bytes()).hexdigest()

    if doc_hash in sealed_hashes(sealed_index):
        return {"absorbed": 0, "sealed": True, "reason": "sealed_doc", "source_doc_hash": doc_hash}

    texts = [row.get("display_text") or row.get("text", "") for row in rows]
    cards: list[dict] = []

    for item in gold:
        text = item.get("text") or item.get("snippet") or ""
        if not text:
            continue
        if _coverage(text, texts) >= COVERAGE_THRESHOLD:
            continue
        cards.append(
            {
                "case_id": case_id(doc_hash, "gold_miss", item.get("page"), text),
                "source_doc_hash": doc_hash,
                "doc_type": doc_type,
                "page": item.get("page"),
                "section": item.get("section", ""),
                "requirement_id": "",
                "kind": "gold_miss",
                "evidence": text[:300],
                "lesson": "金标条款未覆盖：检查编号跨行 / 表格行 / 分页边界",
                "patch_candidate": {"type": "gold", "status": "draft", "content": text},
                "created_at": now(),
                "approved_by": "",
                "linked_test": "",
            }
        )

    for row in rows:
        if row.get("quality") != "low":
            continue
        text = row.get("text", "")
        cards.append(
            {
                "case_id": case_id(doc_hash, "rule_gap", row.get("page"), text),
                "source_doc_hash": doc_hash,
                "doc_type": doc_type,
                "page": row.get("page"),
                "section": row.get("section", ""),
                "requirement_id": row.get("id", ""),
                "kind": "rule_gap",
                "evidence": text[:300],
                "lesson": row.get("quality_reason") or "quality=low：需人工确认切条或表结构",
                "patch_candidate": {"type": "rule", "status": "draft", "content": ""},
                "created_at": now(),
                "approved_by": "",
                "linked_test": "",
            }
        )

    known = {card["case_id"] for card in load_jsonl(out_dir / "cases.jsonl")}
    known |= {card["case_id"] for path in out_dir.glob("*.jsonl") for card in load_jsonl(path)}
    fresh = [card for card in cards if card["case_id"] not in known]

    if fresh:
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"{doc_hash[:8]}-{datetime.now().strftime('%Y%m%d%H%M%S')}.jsonl"
        target.write_text("\n".join(json.dumps(card, ensure_ascii=False) for card in fresh) + "\n", encoding="utf-8")
    return {
        "absorbed": len(fresh),
        "kb_absorbed_count": len(fresh),
        "candidates": len(cards),
        "duplicates": len(cards) - len(fresh),
        "sealed": False,
        "source_doc_hash": doc_hash,
    }


def retrieve(doc_type: str = "", keywords: list[str] | None = None, k: int = 3, kb_dir: str | Path = APPROVED) -> dict:
    """只读检索：返回相似案例上下文；任何异常都降级为空。"""
    if (os.environ.get("TENDER_KB_RETRIEVE") or "1").strip() in {"0", "false", "no", "off"}:
        return {"items": [], "fallback": "disabled"}
    try:
        cards = load_jsonl(Path(kb_dir) / "cases.jsonl")
    except Exception as error:  # noqa: BLE001
        return {"items": [], "fallback": f"{type(error).__name__}: {error}"}

    words = [word for word in (keywords or []) if word]

    def score(card: dict) -> float:
        value = 0.0
        if doc_type and card.get("doc_type") == doc_type:
            value += 1.0
        haystack = f"{card.get('lesson', '')}{card.get('evidence', '')}"
        value += sum(0.3 for word in words if word in haystack)
        return value

    ranked = sorted(cards, key=score, reverse=True)
    picked = [card for card in ranked if score(card) > 0][:k]
    return {
        "items": [
            {
                "case_id": card["case_id"],
                "kind": card.get("kind"),
                "lesson": card.get("lesson"),
                "evidence": card.get("evidence", "")[:200],
                "page": card.get("page"),
                "doc_type": card.get("doc_type"),
            }
            for card in picked
        ],
        "fallback": "",
    }


def test_exists(name: str, tests_dir: str | Path = ROOT / "tests") -> bool:
    if not name:
        return False
    pattern = re.compile(rf"def\s+{re.escape(name)}\s*\(")
    for path in Path(tests_dir).glob("test_*.py"):
        if pattern.search(path.read_text(encoding="utf-8")):
            return True
    return False


def _run_pytest(linked_test: str, timeout: int = 600) -> tuple[bool, str]:
    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-k", linked_test],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output[-400:]


def _sealed_recall() -> float | None:
    """sealed 金标子集上的 recall；未配置 sealed 金标时返回 None。"""
    import tempfile

    index = Path(SEALED_INDEX)
    if not index.exists():
        return None
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    gold_files = data.get("gold_files") or []
    if not gold_files:
        return None

    _sys_path()
    from eval_chunks import evaluate
    from vector_export import export_chunks

    total = covered = 0
    with tempfile.TemporaryDirectory() as folder:
        for entry in gold_files:
            pdf = ROOT / entry["pdf"]
            gold = ROOT / entry["gold"]
            if not pdf.exists() or not gold.exists():
                continue
            target = Path(folder) / f"{pdf.stem}.jsonl"
            export_chunks(pdf, target, absorb=False)
            result = evaluate(gold, target)
            total += result["total"]
            covered += result["covered"]
    return round(covered / total, 3) if total else None


def promote(
    card: dict,
    *,
    approve: bool = False,
    reject: bool = False,
    reason: str = "",
    approved_by: str = "",
    run_gate: bool = True,
    proposed_dir: str | Path = PROPOSED,
    approved_dir: str | Path = APPROVED,
    gold_dir: str | Path = GOLD_DIR,
    tests_dir: str | Path = ROOT / "tests",
) -> dict:
    """受控晋升：schema → linked_test 存在且真跑 → gold_gate → sealed → 写 proposed。"""
    import os
    import tempfile

    errors = validate(card)
    if errors:
        return {"ok": False, "stage": "schema", "errors": errors}
    if card.get("source_doc_hash") in sealed_hashes():
        return {"ok": False, "stage": "sealed", "errors": ["sealed 文档禁止自动吸收/晋升"]}

    if reject:
        append_jsonl(Path(approved_dir) / "rejected.jsonl", [{**card, "rejected_at": now(), "reason": reason}])
        return {"ok": True, "stage": "rejected"}

    if approve and (not approved_by or approved_by.strip().lower() in {"", "unknown", "n/a"}):
        return {"ok": False, "stage": "approved_by", "errors": ["--by 必填且不能是 unknown"]}

    if not card.get("linked_test") or not test_exists(card["linked_test"], tests_dir):
        return {"ok": False, "stage": "linked_test", "errors": [f"linked_test 不存在：{card.get('linked_test') or '(空)'}"]}

    if approve:
        ok, output = _run_pytest(card["linked_test"])
        if not ok:
            return {"ok": False, "stage": "linked_test_run", "errors": [output]}

    allow_skip = os.environ.get("TENDER_KB_ALLOW_NO_GATE") == "1" and not os.environ.get("CI")
    if not run_gate and not allow_skip:
        return {
            "ok": False,
            "stage": "gate_required",
            "errors": ["生产路径禁止跳过 gold_gate（仅 TENDER_KB_ALLOW_NO_GATE=1 且非 CI 可用）"],
        }
    if run_gate:
        import subprocess

        gate = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "gold_gate.py")],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if gate.returncode != 0:
            return {"ok": False, "stage": "gate", "errors": [((gate.stdout or "") + (gate.stderr or ""))[-400:]]}

    if approve:
        try:
            recall = _sealed_recall()
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "stage": "sealed_eval", "errors": [f"{type(error).__name__}: {error}"]}
        baseline_path = KB / "sealed" / "baseline.json"
        if recall is not None:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else {}
            previous = baseline.get("sealed_recall")
            if previous is not None and recall < previous:
                return {"ok": False, "stage": "sealed_regression", "errors": [f"sealed recall {recall} < {previous}"]}
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(json.dumps({"sealed_recall": recall, "updated_at": now()}, ensure_ascii=False, indent=2), encoding="utf-8")

    patch = card.get("patch_candidate") or {}
    target = Path(proposed_dir) / f"{card['case_id']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({**card, "promoted_at": now()}, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {"ok": True, "stage": "approved" if approve else "proposed", "patch": str(target)}

    if approve:
        stored = {**card, "approved_by": approved_by, "approved_at": now()}
        append_jsonl(Path(approved_dir) / "cases.jsonl", [stored])
        result["note"] = "只写 kb/approved 与 kb/proposed；gold 需人工合并进 contract/gold 并更新 index.json"
    return result
