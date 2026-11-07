#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# LAYER: load-bearing (旁路结构化索引；不参与判定)
"""decision_graph.py — 轻量条款决策图（确定性构建，只读旁路）。

原则：
- 节点必须带锚点：doc_hash + page/pages + requirement_id/chunk_id + 原文切片；
- 边只由确定性规则生成，不接受 LLM 独创边；
- 「沉默 ≠ 接受」：没有 accepted 澄清边的要求，状态只能是 pending/unknown；
- 图缺失/构建失败不影响审查主路径。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAPH_DIR = ROOT / "kb" / "graph"
SCHEMA_VERSION = 1

NODE_KINDS = {"requirement", "table_row", "evidence_chunk", "clarification", "gate_result"}
EDGE_KINDS = {"continues", "cites", "evidences", "supersedes", "reviewed_as"}
CLARIFICATION_STATUS = {"accepted", "pending", "rejected"}


def _hash(*parts: str) -> str:
    return hashlib.sha1("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:16]


def _node(kind: str, node_id: str, **anchors) -> dict:
    node = {"id": node_id, "kind": kind}
    node.update(anchors)
    return node


def _edge(kind: str, source: str, target: str, **meta) -> dict:
    edge = {"kind": kind, "from": source, "to": target}
    edge.update(meta)
    return edge


def _text_of(record: dict) -> str:
    return (record.get("display_text") or record.get("text") or "").strip()


def build_from_export(
    rows: list[dict],
    sections: list[dict] | None = None,
    gate_rows: list[dict] | None = None,
    clarifications: list[dict] | None = None,
    supersedes: list[dict] | None = None,
    doc_hash: str = "",
) -> dict:
    """从导出的 requirement / section / gate 行确定性地建图。"""
    doc_hash = doc_hash or next((row.get("source_sha256") for row in rows if row.get("source_sha256")), "")
    nodes: list[dict] = []
    edges: list[dict] = []

    for row in rows:
        kind = "table_row" if row.get("kind") == "table_row" else "requirement"
        node_id = f"req:{row.get('id')}"
        nodes.append(
            _node(
                kind,
                node_id,
                doc_hash=doc_hash,
                page=row.get("page"),
                pages=row.get("pages") or [row.get("page")],
                requirement_id=row.get("id", ""),
                section=row.get("section", ""),
                number=row.get("number", ""),
                text=_text_of(row)[:200],
                quality=row.get("quality", "ok"),
            )
        )

    chunk_nodes: dict[str, dict] = {}
    for section in sections or []:
        node_id = f"chunk:{section.get('id')}"
        chunk = _node(
            "evidence_chunk",
            node_id,
            doc_hash=doc_hash,
            page=(section.get("pages") or [None])[0],
            pages=section.get("pages") or [],
            chunk_id=section.get("id", ""),
            section=section.get("section", ""),
            text=(section.get("text") or "")[:200],
        )
        chunk_nodes[node_id] = chunk
        nodes.append(chunk)

    # evidences：requirement → 同 section 且页码相交的 chunk（每要求最多 2 条）
    for row in rows:
        req_node = f"req:{row.get('id')}"
        pages = set(row.get("pages") or [row.get("page")])
        candidates = [
            (chunk_id, chunk)
            for chunk_id, chunk in chunk_nodes.items()
            if chunk["section"] == row.get("section") and pages & set(chunk["pages"])
        ]
        for chunk_id, _ in candidates[:2]:
            edges.append(_edge("evidences", req_node, chunk_id))

    # continues：跨页 requirement → 后续页的 chunk
    for row in rows:
        page_list = row.get("pages") or []
        if len(page_list) < 2:
            continue
        req_node = f"req:{row.get('id')}"
        for page in page_list[1:]:
            for chunk_id, chunk in chunk_nodes.items():
                if page in chunk["pages"]:
                    edges.append(_edge("continues", req_node, chunk_id, page=page))
                    break

    # cites：table_row → 同 section、页码相邻的 requirement（每行最多 1 条）
    plain = [row for row in rows if row.get("kind") != "table_row"]
    for row in rows:
        if row.get("kind") != "table_row":
            continue
        best = None
        for candidate in plain:
            if candidate.get("section") != row.get("section"):
                continue
            distance = abs(int(candidate.get("page") or 0) - int(row.get("page") or 0))
            if distance > 1:
                continue
            if best is None or distance < best[0]:
                best = (distance, candidate)
        if best:
            edges.append(_edge("cites", f"req:{row.get('id')}", f"req:{best[1].get('id')}"))

    # reviewed_as：gate 行 → gate_result 节点
    if gate_rows:
        for gate in gate_rows:
            status = (gate.get("status") or "unknown").lower()
            if status not in {"pass", "fail", "unknown"}:
                status = "unknown"
            gate_node = f"gate:{status}"
            if not any(node["id"] == gate_node for node in nodes):
                nodes.append(_node("gate_result", gate_node, doc_hash=doc_hash, status=status))
            req_id = gate.get("req_id") or gate.get("requirement_id")
            if req_id and any(node["id"] == f"req:{req_id}" for node in nodes):
                # 只连图上真实存在的 requirement，避免悬空边
                edges.append(_edge("reviewed_as", f"req:{req_id}", gate_node, status=status))

    # clarification：只能由人工/夹具写入，不自动生成
    for item in clarifications or []:
        status = (item.get("status") or "pending").lower()
        if status not in CLARIFICATION_STATUS:
            status = "pending"
        node_id = f"clar:{_hash(doc_hash, item.get('requirement_id', ''), item.get('note', ''))}"
        nodes.append(
            _node(
                "clarification",
                node_id,
                doc_hash=doc_hash,
                page=item.get("page"),
                requirement_id=item.get("requirement_id", ""),
                status=status,
                note=(item.get("note") or "")[:200],
            )
        )
        if item.get("requirement_id"):
            edges.append(_edge("reviewed_as", f"req:{item['requirement_id']}", node_id, status=status))

    for item in supersedes or []:
        old = f"req:{item.get('old')}"
        new = f"req:{item.get('new')}"
        edges.append(_edge("supersedes", new, old, note=item.get("note", "")))

    return {
        "schema_version": SCHEMA_VERSION,
        "doc_hash": doc_hash,
        "nodes": nodes,
        "edges": edges,
        "counts": {"nodes": len(nodes), "edges": len(edges)},
        "built_at": _now(),
    }


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def graph_path(doc_hash: str, root: str | Path | None = None) -> Path:
    return Path(root or GRAPH_DIR) / f"{doc_hash}.json"


def save(graph: dict, root: str | Path | None = None) -> Path | None:
    if not (graph.get("doc_hash") or "").strip():
        return None  # 空 hash 拒写，避免 kb/graph/.json
    path = graph_path(graph["doc_hash"], root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load(doc_hash: str, root: str | Path | None = None) -> dict | None:
    path = graph_path(doc_hash, root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def neighbours(graph: dict, node_id: str) -> dict:
    """返回节点自身 + 一跳邻居 + 相关边；节点不存在时返回空结构。"""
    node = next((item for item in graph.get("nodes", []) if item["id"] == node_id), None)
    if node is None:
        return {"node": None, "neighbours": [], "edges": [], "source": "none"}
    related = [edge for edge in graph.get("edges", []) if node_id in (edge["from"], edge["to"])]
    ids = {edge["from"] for edge in related} | {edge["to"] for edge in related}
    ids.discard(node_id)
    neighbours = [item for item in graph.get("nodes", []) if item["id"] in ids]
    return {"node": node, "neighbours": neighbours, "edges": related, "source": "graph"}


def find_node(graph: dict, requirement_id: str = "", keywords: list[str] | None = None) -> dict | None:
    if requirement_id:
        for node in graph.get("nodes", []):
            if node.get("requirement_id") == requirement_id:
                return node
    for word in keywords or []:
        for node in graph.get("nodes", []):
            if word and word in (node.get("text") or ""):
                return node
    return None


def decision_status(graph: dict, requirement_id: str) -> str:
    """沉默 ≠ 接受：没有 accepted 澄清就只能 unknown/pending。"""
    statuses = [
        edge.get("status")
        for edge in graph.get("edges", [])
        if edge["kind"] == "reviewed_as" and edge["from"] == f"req:{requirement_id}"
    ]
    if "accepted" in statuses:
        return "accepted"
    if "pending" in statuses:
        return "pending"
    return "unknown"


def summary(graph: dict) -> dict:
    requirements = [node for node in graph.get("nodes", []) if node["kind"] in {"requirement", "table_row"}]
    counts = {"accepted": 0, "pending": 0, "unknown": 0}
    for node in requirements:
        counts[decision_status(graph, node.get("requirement_id", ""))] += 1
    all_accepted = bool(requirements) and counts["accepted"] == len(requirements)
    return {
        "total": len(requirements),
        "counts": counts,
        "claim": "全部满足" if all_accepted else "存在未澄清项（沉默不等于接受）",
    }

def query_wiki_first(
    requirement_id: str = "",
    keywords: list[str] | None = None,
    doc_hash: str = "",
    k: int = 3,
    graph_root: str | Path | None = None,
) -> dict:
    """查询顺序：图邻居 → 已确认案例 → 空（允许 LLM，但本函数不调用）。"""
    graph = load(doc_hash, graph_root) if doc_hash else None
    if graph:
        node = find_node(graph, requirement_id, keywords)
        if node is not None:
            payload = neighbours(graph, node["id"])
            return {
                "source": "graph",
                "node": payload["node"],
                "items": [
                    {"node_id": item["id"], "kind": item["kind"], "text": item.get("text", ""),
                     "page": item.get("page"), "section": item.get("section", "")}
                    for item in payload["neighbours"]
                ],
                "graph_neighbors": payload["neighbours"],
                "allow_llm": False,
                "no_graph_evidence": False,
            }

    from kb import retrieve

    fallback = retrieve(keywords=keywords or [], k=k)
    if fallback.get("items"):
        return {
            "source": "kb",
            "items": fallback["items"],
            "graph_neighbors": [],
            "allow_llm": False,
            "no_graph_evidence": True,
        }
    return {
        "source": "none",
        "items": [],
        "graph_neighbors": [],
        "allow_llm": True,
        "no_graph_evidence": True,
    }

def apply_gate(graph: dict, pairs: list[tuple[str, str]]) -> dict:
    """把 gate 结果补进已建好的图（只连图上存在的 requirement）。"""
    existing = {node["id"] for node in graph.get("nodes", [])}
    for requirement_id, status in pairs:
        node_id = f"req:{requirement_id}"
        if node_id not in existing:
            continue
        status = status if status in {"pass", "fail", "unknown"} else "unknown"
        gate_node = f"gate:{status}"
        if gate_node not in existing:
            graph["nodes"].append(_node("gate_result", gate_node, doc_hash=graph.get("doc_hash", ""), status=status))
            existing.add(gate_node)
        edge = _edge("reviewed_as", node_id, gate_node, status=status)
        if edge not in graph["edges"]:
            graph["edges"].append(edge)
    graph["counts"] = {"nodes": len(graph["nodes"]), "edges": len(graph["edges"])}
    return graph

def preserve_gate_edges(old_graph: dict | None, new_graph: dict) -> dict:
    """重导出时保留旧的 gate_result 节点与 reviewed_as 边（防止图被重跑冲掉）。"""
    if not old_graph:
        return new_graph
    existing_nodes = {node["id"] for node in new_graph["nodes"]}
    for node in old_graph.get("nodes", []):
        if node["kind"] == "gate_result" and node["id"] not in existing_nodes:
            new_graph["nodes"].append(node)
            existing_nodes.add(node["id"])
    existing_edges = {(edge["kind"], edge["from"], edge["to"]) for edge in new_graph["edges"]}
    for edge in old_graph.get("edges", []):
        if edge["kind"] != "reviewed_as":
            continue
        if edge["from"] not in {node["id"] for node in new_graph["nodes"]}:
            continue  # 节点已不存在的悬空边丢弃
        key = (edge["kind"], edge["from"], edge["to"])
        if key not in existing_edges:
            new_graph["edges"].append(edge)
            existing_edges.add(key)
    new_graph["counts"] = {"nodes": len(new_graph["nodes"]), "edges": len(new_graph["edges"])}
    return new_graph
