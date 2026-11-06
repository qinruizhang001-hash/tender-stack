#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fake_model_server.py — OpenAI 兼容的确定性模型替身

为什么需要它：
  rag-tender 的提取层只认 OpenAI-compatible /chat/completions（见
  vendor/rag-tender/backend/app/services/model_gateway.py）。本机没有可用的
  真实模型 Key，仓库本身也明令开发阶段禁止真实调用。为了把"数据流对接"
  真正跑通，这里起一个本地 HTTP 服务，让 rag-tender 通过**公开接口**接进来——
  不改它一行代码，也不 import 它的任何内部函数。

它模拟的是什么：
  一个"称职但会漏"的提取器。刻意保留两个已知盲点，用来让 A/B 对比有意义：
    盲点 A：无模态动词的陈述式硬约束（如"投标保证金 5 万元。""投标有效期 90 日。"）
    盲点 B：每批最多提取 STUB_MAX_PER_BATCH 条，超出即丢（模拟注意力上限）
  这两类正是确定性扫描层（tender-review-kit）能吃掉的，所以"插入扫描层"
  才有可测量的漏报率下降。

  两臂（before/after）跑的是同一个替身、同一套规则，差异只来自扫描层注入。

输出严格符合 rag-tender 的 ExtractedRequirements schema：
  {requirements:[...], reading_items:[...], coverage_notes:[]}
其中 quote 必须是 block.text 的逐字子串，否则会被 rag-tender 的原文校验丢掉。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG_PATH = os.environ.get("STUB_LOG", "")
MAX_PER_BATCH = int(os.environ.get("STUB_MAX_PER_BATCH", "6"))
SKIP_DECLARATIVE = os.environ.get("STUB_SKIP_DECLARATIVE", "1") == "1"
MODEL_NAME = os.environ.get("STUB_MODEL_NAME", "stub-extractor-v1")

_LOCK = threading.Lock()
_COUNTER = {"calls": 0}

# ---------- 规则表 ----------

# 强模态词（parsing.md §二/§七）
STRONG = [
    "不得", "严禁", "禁止", "不允许", "不接受", "必须", "须", "应当",
    "不低于", "不少于", "不高于", "不得超过", "至少", "最多",
    "需提供", "应提供", "须提交", "应提交", "需提交",
    "予以否决", "按无效投标处理", "作为废标处理", "拒收", "否决",
    "视为放弃", "视为无效", "实质性偏离", "取消资格", "资格审查不合格",
    "不予受理", "不予退还",
]
# "应" 单独判，需排除 响应/对应/相应/供应/应急/应用/适应
_YING = re.compile(r"(?<![响对相供适反])应(?![答用急用])")

WEAK = ["宜", "建议", "优先", "鼓励", "可予", "可以"]

REFER = re.compile(r"^(详见|见第|参照|按照第|依据第)")
NUMBERING = re.compile(r"^\s*(?:[（(]?\d+(?:\.\d+)*[）)]?[、.．]?|[一二三四五六七八九十]+[、.．])\s*")

CATEGORY_RULES = [
    ("submission", ["密封", "签字", "盖章", "骑缝章", "正本", "副本", "份数", "装订", "封套", "递交", "送达", "格式"]),
    ("qualification", ["营业执照", "资质", "注册资本", "财务报告", "审计", "业绩", "社保", "纳税", "信用", "联合体", "证书", "认证", "ISO", "法人"]),
    ("financial", ["财务报表", "资产负债", "净资产", "营业收入"]),
    ("commercial", ["付款", "质保", "交货", "工期", "保证金", "违约", "验收", "合同价", "报价", "限价", "税费", "发票"]),
    ("technical", ["技术", "参数", "接口", "协议", "容量", "性能", "兼容", "支持", "配置", "功能", "联网", "级联", "存储", "分辨率", "算力"]),
    ("other", ["售后", "培训", "驻场", "备件", "响应时间", "服务"]),
]

STAGE_MAP = {
    "qualification": "qualification",
    "financial": "qualification",
    "submission": "compliance",
    "technical": "technical",
    "commercial": "commercial",
    "other": "compliance",
}

HANDLING_MAP = {
    "qualification": "evidence",
    "financial": "evidence",
    "submission": "submission",
    "technical": "technical",
    "commercial": "commercial",
    "other": "todo",
}

# 陈述式硬约束（盲点 A 的反面样本：替身**故意不提取**，留给扫描层）
DECLARATIVE = re.compile(
    r"(投标保证金|投标有效期|履约保证金|质保金)[^。；]{0,20}?(\d+\s*(万元|元|日|天|年|个月))"
)

GREY = ["基本响应", "部分响应", "大致满足", "原则上满足"]


def _classify(sentence: str) -> str:
    for category, words in CATEGORY_RULES:
        if any(word in sentence for word in words):
            return category
    return "other"


def _strength(sentence: str) -> tuple[str, str, str]:
    """返回 (strength, obligation, consequence)"""
    if any(word in sentence for word in ["不得", "严禁", "禁止", "不允许", "不接受"]):
        return "禁止", "mandatory", "rejection"
    if any(word in sentence for word in ["必须", "须", "不低于", "不少于", "不高于", "不得超过", "至少", "予以否决", "拒收", "否决", "视为放弃", "视为无效", "按无效投标处理", "作为废标处理", "取消资格", "实质性偏离"]):
        return "必须", "mandatory", "rejection"
    if any(word in sentence for word in ["需提供", "应提供", "须提交", "应提交", "需提交"]) or _YING.search(sentence):
        return "应", "mandatory", "rejection"
    if any(word in sentence for word in WEAK):
        return "宜", "scored", "scoring"
    return "可", "optional", "none"


def _split_sentences(text: str):
    """切句并返回 (start, end) 偏移，quote 用原文切片，保证是逐字子串。"""
    spans = []
    start = 0
    for match in re.finditer(r"[。；;！？\n]", text):
        end = match.start()
        if end > start:
            spans.append((start, end))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _norm(value: str) -> str:
    return re.sub(r"[\s\u3000，。、；：（）()【】\[\]“”\"'·,.;:!?]", "", value)


def extract_from_payload(payload: dict, known: dict[str, list[str]]) -> dict:
    blocks = payload.get("blocks") or []
    requirements: list[dict] = []
    reading: list[dict] = []
    seen: set[str] = set()

    for block in blocks:
        block_id = block.get("block_id") or ""
        text = block.get("text") or ""
        if not text.strip():
            continue
        known_words = known.get(block_id) or []
        known_norm = [_norm(word) for word in known_words if word]

        for start, end in _split_sentences(text):
            raw = text[start:end]
            sentence = raw.strip()
            if len(sentence) < 8:
                continue
            if REFER.match(sentence):
                continue
            body = NUMBERING.sub("", sentence).strip()
            if len(body) < 6:
                continue

            # 盲点 A：陈述式硬约束，替身不提取（交给扫描层）
            if SKIP_DECLARATIVE and DECLARATIVE.search(body) and not any(
                word in body for word in STRONG
            ):
                continue

            strength, obligation, consequence = _strength(body)
            if obligation != "mandatory" and strength in {"可"}:
                continue
            if not any(word in body for word in STRONG) and not _YING.search(body):
                continue

            # 第二轮：已知确定性问题清单命中 → 不再重复判断
            body_norm = _norm(body)
            if known_norm and any(
                word_norm and (word_norm in body_norm or body_norm in word_norm)
                for word_norm in known_norm
            ):
                continue

            key = _norm(body)
            if key in seen:
                continue
            seen.add(key)

            if len(requirements) >= MAX_PER_BATCH:
                break

            category = _classify(body)
            confidence = 0.88 if strength in {"禁止", "必须"} else 0.72
            if any(word in body for word in GREY):
                confidence = 0.55
            requirements.append(
                {
                    "category": category,
                    "evaluation_stage": STAGE_MAP.get(category, "compliance"),
                    "evaluation_type": "score" if obligation == "scored" else "binary",
                    "obligation": obligation,
                    "consequence": consequence,
                    "handling_mode": HANDLING_MAP.get(category, "todo"),
                    "title": body[:36],
                    "statement": body,
                    "block_id": block_id,
                    "quote": raw.strip(),
                    "confidence": confidence,
                    "condition_json": {},
                }
            )

        # 背景信息：时间节点 / 项目概况（各批最多 2 条）
        if len(reading) < 2:
            for start, end in _split_sentences(text):
                sentence = text[start:end].strip()
                if not sentence or len(sentence) < 8:
                    continue
                if re.search(r"(截止|开标|递交|获取|答疑)", sentence) and re.search(
                    r"\d{4}\s*年|\d{1,2}\s*月|\d{1,2}\s*日", sentence
                ):
                    reading.append(
                        {
                            "section": "timeline",
                            "title": "关键时间",
                            "content": sentence[:150],
                            "block_id": block_id,
                            "quote": sentence,
                        }
                    )
                    break

    return {"requirements": requirements, "reading_items": reading, "coverage_notes": []}


def _log(record: dict) -> None:
    if not LOG_PATH:
        return
    with _LOCK:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        return

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, {"object": "list", "data": [{"id": MODEL_NAME, "object": "model"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        path = self.path.rstrip("/")

        if path.endswith("/embeddings"):
            inputs = payload.get("input") or []
            if isinstance(inputs, str):
                inputs = [inputs]
            data = []
            for index, text in enumerate(inputs):
                digest = hashlib.sha256(str(text).encode()).digest()
                vector = [((digest[i % 32] / 255.0) - 0.5) for i in range(64)]
                vector[0] += 1.0
                data.append({"object": "embedding", "index": index, "embedding": vector})
            self._send(
                200,
                {
                    "object": "list",
                    "data": data,
                    "model": payload.get("model", "stub-embed"),
                    "usage": {"prompt_tokens": sum(len(str(t)) // 2 for t in inputs), "total_tokens": 0},
                },
            )
            return

        if not path.endswith("/chat/completions"):
            self._send(404, {"error": "not found"})
            return

        messages = payload.get("messages") or []
        system = ""
        user = ""
        for message in messages:
            if message.get("role") == "system":
                system = message.get("content") or ""
            elif message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, list):
                    user = "\n".join(
                        part.get("text", "") for part in content if isinstance(part, dict)
                    )
                else:
                    user = content or ""

        started = time.time()
        with _LOCK:
            _COUNTER["calls"] += 1
            call_no = _COUNTER["calls"]

        # 解析 page_payload
        page_payload: dict = {}
        brace = user.find("{")
        if brace >= 0:
            try:
                page_payload = json.loads(user[brace:])
            except json.JSONDecodeError:
                page_payload = {}

        known = page_payload.get("known_issues") or {}
        if isinstance(known, list):
            merged: dict[str, list[str]] = {}
            for item in known:
                if isinstance(item, dict) and item.get("block_id"):
                    merged.setdefault(item["block_id"], []).append(item.get("quote") or "")
            known = merged

        result = extract_from_payload(page_payload, known if isinstance(known, dict) else {})
        content = json.dumps(result, ensure_ascii=False)

        prompt_tokens = max(1, (len(system) + len(user)) // 2)
        completion_tokens = max(1, len(content) // 2)
        kind = "extract" if "要求提取器" in system else "other"
        _log(
            {
                "call_no": call_no,
                "kind": kind,
                "ts": round(started, 3),
                "page_index": page_payload.get("page_index"),
                "batch": page_payload.get("batch"),
                "n_blocks": len(page_payload.get("blocks") or []),
                "n_known_issues": sum(len(v) for v in known.values()) if isinstance(known, dict) else 0,
                "prompt_chars": len(system) + len(user),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "n_requirements": len(result["requirements"]),
                "n_reading": len(result["reading_items"]),
                "block_ids": [b.get("block_id") for b in (page_payload.get("blocks") or [])],
            }
        )

        self._send(
            200,
            {
                "id": f"chatcmpl-stub-{call_no}",
                "object": "chat.completion",
                "created": int(started),
                "model": payload.get("model", MODEL_NAME),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            },
        )


def main() -> None:
    port = int(os.environ.get("STUB_PORT", "8099"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[stub] listening on http://127.0.0.1:{port}/v1  model={MODEL_NAME}", flush=True)
    print(f"[stub] log={LOG_PATH or '(disabled)'} max_per_batch={MAX_PER_BATCH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
