#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""match_bid.py — 要求清单 × 我方投标文件 → 覆盖矩阵

方向很重要：
  招标文件 → 要求清单（已有）
  投标文件 → 只用来「对照」，判断每条要求有没有被响应

流程：
  1. 语义切块（adapter/bid_chunker.py：PyMuPDF 段落块 → 句级打包，带页码/章节）
  2. IDF 加权词覆盖预筛，取最相近的 TOP_K 段
  3. 预筛分低于阈值 → 判「未找到」（不调模型，省钱）
  4. 否则调模型判定：已响应 / 部分响应 / 未响应，并给逐字证据

用法：
  python adapter/match_bid.py --ledger out/armC_v4/gated --bid 投标文件.pdf --out out/armC_v4/match
  python adapter/match_bid.py --ledger ... --bid ... --out ... --dry-run   # 只看预筛分布，不调模型
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bid_chunker import chunk_pdf  # noqa: E402
from numeric_check import REVIEW as NUM_REVIEW, compare as compare_numeric, parse as parse_numeric  # noqa: E402
from time_check import (  # noqa: E402
    EXPIRED as TIME_EXPIRED,
    SHORT as TIME_SHORT,
    explain as explain_time,
    parse_date as parse_time_date,
    parse_duration as parse_time_duration,
)
from verify_rules import apply_rules, describe, extract_certs  # noqa: E402
from missing_item_check import (  # noqa: E402
    FOUND as ENTITY_FOUND,
    MISSING as ENTITY_MISSING,
    REVIEW as ENTITY_REVIEW,
    MissingItemChecker,
)

ROOT = Path(__file__).resolve().parent.parent
MODEL = os.environ.get("MATCH_MODEL", "deepseek-chat")
BASE = os.environ.get("MATCH_BASE", "https://api.deepseek.com/v1")
TOP_K = 3
MIN_SCORE = 0.16          # IDF 加权词覆盖率阈值（0~1），比原来的 Jaccard 更稳
EVIDENCE_CHARS = 1200     # 送进模型的单段最大字数（原来截断到 300，证据常被截掉）
MODEL_CALLS = 0           # 本次运行的模型调用次数，用于对比"开/关数值预判"的成本
CACHE_HITS = 0            # 命中的判定缓存数

# 判定缓存：同一（模型 + 要求 + 候选段 + 第几票）只问一次模型。
# 加上"第几票"是刻意的——否则第二次投票会命中第一次的缓存，永远"一致"。
CACHE_LOCK = threading.Lock()
CACHE: dict[str, dict] | None = None


def load_cache(path: Path) -> int:
    global CACHE
    if path.exists():
        try:
            CACHE = json.loads(path.read_text(encoding="utf-8"))
            return len(CACHE)
        except (OSError, ValueError):
            CACHE = {}
            return 0
    CACHE = {}
    return 0


def save_cache(path: Path) -> int:
    if CACHE is None:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(CACHE, ensure_ascii=False), encoding="utf-8")
    return len(CACHE)

PROMPT = """你是投标文件审查专家。下面是一条招标要求，以及我方投标文件里最相关的几段原文。

【要求】
{requirement}

【投标文件候选段落】
{candidates}

判断我方投标文件是否响应了这条要求，三选一：
- 已响应：有明确对应的内容或证明材料
- 部分响应：提到了，但不完整、缺证明、或答非所问
- 未响应：候选段落与这条要求无关

注意：条款内容与要求不符（如注册资本不足、参数负偏离、缺一项证明）仍属「已响应」，
因为标书确实就这条作出了回应；具体偏离写在 reason 里。

硬性规则（违反即判负偏离/未响应，不许含糊）：
1. 要求与证据都出现数值（金额、尺寸、数量、期限、百分比）时，必须做大小比较。
   证据劣于要求（金额更少、尺寸更小、数量更少、期限更晚）→ 判「未响应」，并在 reason 写清两个数。
2. 资质名称必须完全一致：要求 ISO13485 而证据是 ISO9001、要求《A许可证》而证据是《B许可证》
   → 判「未响应」，不要因为"都是质量体系认证"就算响应。
3. 数值或资质参与判定、但你不能确定是否满足（方向不明、单位不同、多处数值）→ 判「部分响应」，
   并在 reason 说明需要人工确认，不要猜「已响应」。

规则：
1. 只能依据给出的候选段落判断，不要脑补；
2. evidence 必须是候选段落里的逐字片段（找不到就留空）；
3. 拿不准时选「部分响应」，不要选「已响应」。

只输出 JSON：{{"status":"已响应|部分响应|未响应","evidence":"逐字片段","reason":"一句话"}}
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


def bigrams(text: str) -> set[str]:
    target = norm(text)
    return {target[i : i + 2] for i in range(len(target) - 1)}


def evidence_sig(evidence: str, basis: str) -> str:
    """证据指纹：证据或规则依据变了，指纹就变，用来判断人工确认是否还成立。"""
    return hashlib.sha1(f"{norm(evidence)}|{basis}".encode("utf-8")).hexdigest()[:12]


def apply_review_overlay(results: list[dict], out_dir: Path, *, recheck_stale: bool = False) -> dict:
    """把上次的人工确认套回本次结果——重跑不丢审查员的判断。

    指纹一致：静默沿用；
    指纹变了：默认保留人工结论并标 review_stale 提示复核；
              加 --recheck-stale 则把该条退回「人工介入」重新排队，
              原确认记录移到 review.json 的 _invalidated 里留痕。
    台账里已经不存在的 req_id：不套用，只统计 dropped。
    """
    path = out_dir / "review.json"
    if not path.exists():
        return {"reviewed": 0, "stale": 0, "dropped": 0, "rechecked": 0}
    try:
        overlay = json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {"reviewed": 0, "stale": 0, "dropped": 0, "rechecked": 0}

    by_id = {row["req_id"]: row for row in results}
    reviewed = stale = dropped = rechecked = 0
    invalidated: list[dict] = []
    for req_id, patch in list(overlay.items()):
        if req_id.startswith("_"):
            continue  # _invalidated 之类的元数据
        row = by_id.get(req_id)
        if row is None:
            dropped += 1
            continue

        sig = patch.get("sig")
        changed = bool(sig and row.get("sig") and sig != row["sig"])
        if changed:
            stale += 1
            row["review_stale"] = "Y"

        if changed and recheck_stale:
            # 证据变了且要求重新复核：作废旧结论，退回待确认
            row["coverage"] = "人工介入"
            row["need_confirm"] = "Y"
            row["reviewed"] = ""
            row["reason"] = (
                f"证据已变更，原人工结论（{patch.get('status')}）作废，需重新确认"
            )
            invalidated.append({**patch, "req_id": req_id, "invalidated_at": now_stamp()})
            overlay.pop(req_id, None)
            rechecked += 1
            continue

        row["coverage"] = patch.get("status", row["coverage"])
        row["reviewed"] = "Y"
        row["need_confirm"] = ""
        reviewed += 1

    if rechecked:
        overlay.setdefault("_invalidated", [])
        overlay["_invalidated"] = (overlay["_invalidated"] + invalidated)[-200:]
        path.write_text(json.dumps(overlay, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"reviewed": reviewed, "stale": stale, "dropped": dropped, "rechecked": rechecked}


def now_stamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ 切块

def parse_bid(path: Path, target: int = 900, max_chars: int = 1400, backend: str | None = None) -> tuple[list[dict], dict]:
    raw_chunks, stats = chunk_pdf(path, target=target, max_chars=max_chars, backend=backend)
    chunks = []
    for item in raw_chunks:
        text = item["text"]
        if len(norm(text)) < 8:      # 原来是 30，会把「本项目不接受联合体投标」这类短句直接丢掉
            continue
        chunks.append({**item, "grams": bigrams(text)})
    stats["kept"] = len(chunks)
    return chunks, stats


def build_idf(chunks: list[dict]) -> dict[str, float]:
    df: dict[str, int] = {}
    for chunk in chunks:
        for gram in chunk["grams"]:
            df[gram] = df.get(gram, 0) + 1
    total = max(len(chunks), 1)
    return {gram: math.log(1 + total / (1 + count)) for gram, count in df.items()}


def score(query: set[str], chunk_grams: set[str], idf: dict[str, float]) -> float:
    """要求词汇被该段覆盖的比例（IDF 加权），分母固定为要求侧，不受段落长度惩罚。"""
    if not query:
        return 0.0
    hit = query & chunk_grams
    if not hit:
        return 0.0
    default = math.log(2)
    numerator = sum(idf.get(g, default) for g in hit)
    denominator = sum(idf.get(g, default) for g in query)
    return numerator / denominator if denominator else 0.0


# ------------------------------------------------------------------ 模型

def call_model(key: str, prompt: str, timeout: int = 90) -> str:
    global MODEL_CALLS
    MODEL_CALLS += 1
    payload = json.dumps(
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 400,
            "response_format": {"type": "json_object"},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))["choices"][0]["message"]["content"]


def _label(chunk: dict) -> str:
    section = (chunk.get("section") or "").strip()
    tail = f"·{section[:20]}" if section else ""
    return f"[第{chunk['page']}页{tail}]"


def judge_once(key: str, row: dict, candidates: list[dict], attempt: int = 0) -> dict:
    """跑一次「模型判定 + 规则层」，返回单次结论。

    attempt 是第几票（0/1），参与缓存键：两票各自缓存，
    重跑同一份材料时结论完全可复现，且不再产生调用。
    """
    global CACHE_HITS
    blocks = "\n\n".join(f"{_label(c)} {c['text'][:EVIDENCE_CHARS]}" for c in candidates)
    prompt = PROMPT.format(requirement=row["requirement"][:300], candidates=blocks)

    cache_key = hashlib.sha1(
        f"{MODEL}|{PROMPT}|{row['requirement']}|{blocks}|{attempt}".encode("utf-8")
    ).hexdigest()
    data = None
    if CACHE is not None:
        with CACHE_LOCK:
            data = CACHE.get(cache_key)
        if data is not None:
            CACHE_HITS += 1

    if data is None:
        try:
            raw = call_model(key, prompt)
            data = json.loads(re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw.strip()))
        except Exception as error:  # noqa: BLE001
            return {"coverage": "待复核", "bid_page": "", "evidence": "", "reason": f"调用失败: {type(error).__name__}", "basis": "", "rule_fired": False}
        if CACHE is not None:
            with CACHE_LOCK:
                CACHE[cache_key] = data
    status = data.get("status") if data.get("status") in ("已响应", "部分响应", "未响应") else "待复核"
    evidence = (data.get("evidence") or "").strip()
    page = candidates[0]["page"]
    if evidence:
        for candidate in candidates:
            if norm(evidence) in norm(candidate["text"]):
                page = candidate["page"]
                break
        else:
            evidence = ""
    reason = (data.get("reason") or "")[:120]

    # 规则层：数值比较 / 资质一致性，命中就强制改判，模型无权保留「已响应」
    basis_text = evidence or candidates[0]["text"]
    final_status, hits = apply_rules(row["requirement"], basis_text, status)
    basis = describe(hits)
    rule_fired = bool(hits) and final_status != status
    if rule_fired:
        reason = f"规则改判：{basis}" + (f"（模型原判 {status}：{reason}）" if reason else "")
    return {
        "coverage": final_status,
        "bid_page": page,
        "evidence": evidence,
        "reason": reason,
        "basis": basis,
        "model_status": status,
        "rule_fired": rule_fired,
        "sig": evidence_sig(candidates[0]["text"][:400], basis),
    }


def judge(key: str, row: dict, candidates: list[dict], votes: int = 2) -> dict:
    """双跑投票：同一条件跑两次，结论一致才采信，不一致降级为「人工介入」。

    模型在 temperature=0 下仍会漂移（实测约 5%），单跑结论不可复现；
    对废标判定来说，不可复现比多花一次调用更贵。

    两次即可，不做多数投票：三跑只把漂移概率从 p 降到 p²，
    而分歧一律交人工，已经不会再产出错误结论。
    """
    first = judge_once(key, row, candidates, attempt=0)
    first.setdefault("votes", first["coverage"])
    first.setdefault("vote_agree", "")

    # 调用失败重试一次；规则已强制改判的，结论由确定性规则给出，不必再跑
    if votes <= 1 or first["coverage"] == "待复核" or first.get("rule_fired"):
        return first

    second = judge_once(key, row, candidates, attempt=1)
    if second["coverage"] == "待复核":       # 第二次失败，不推翻第一次
        return first

    if second["coverage"] == first["coverage"]:
        first["votes"] = f"{first['coverage']}×2"
        first["vote_agree"] = "Y"
        if len(second.get("evidence") or "") > len(first.get("evidence") or ""):
            first["evidence"] = second["evidence"]
            first["bid_page"] = second["bid_page"]
        return first

    return {
        "coverage": "人工介入",
        "bid_page": first.get("bid_page") or second.get("bid_page") or "",
        "evidence": first.get("evidence") or second.get("evidence") or "",
        "reason": f"两次判定不一致：{first['coverage']} / {second['coverage']}，需人工确认",
        "basis": "；".join(x for x in (first.get("basis", ""), second.get("basis", "")) if x),
        "model_status": f"{first.get('model_status', '')}|{second.get('model_status', '')}",
        "rule_fired": False,
        "votes": f"{first['coverage']}|{second['coverage']}",
        "vote_agree": "N",
        "sig": evidence_sig(first.get("evidence") or second.get("evidence") or "", "vote-conflict"),
    }


# ------------------------------------------------------------------ 数值预判

def cjk_bigrams(text: str) -> set[str]:
    """只保留汉字再取二字组，避免数字/单位制造虚假重合。"""
    zh = re.sub(r"[^\u4e00-\u9fff]", "", text or "")
    return {zh[i:i + 2] for i in range(len(zh) - 1)}


# 只有"门槛式"表述才做数值预判；「低于45%触发审查」这种是条件描述，不是门槛
_STRICT_OP = re.compile(r"不低于|不少于|不小于|不得低于|不得少于|至少|≥|>=|不超过|不大于|不多于|不得高于|至多|≤|<=|以上|以下|以内")
# 条件/程序性条款不做数值判定
_CONDITIONAL = re.compile(r"触发|情形|下列|之一|若|如果|当.{0,8}时|的，应|的，须")


def _context_before(text: str, number) -> str:
    """取数值前面最多 8 个字，用来判断"比的是不是同一个东西"。"""
    raw = getattr(number, "raw", "") or ""
    index = text.find(raw) if raw else -1
    if index < 0:
        return ""
    return text[max(0, index - 8):index]


# 表头同义列：报价 / 投标价 / 总价 / 金额 都是同一类数
_HEADER_FAMILIES = {
    "金额": ("金额", "报价", "投标价", "总价", "价格", "价款", "合同价", "费用", "单价", "合价", "限价"),
    "数量": ("数量", "数目", "件数", "个数", "台数", "套数"),
    "日期": ("日期", "时间", "期限", "工期", "交货期", "交付期", "服务期"),
    "比例": ("比例", "比率", "费率", "百分比", "折扣"),
    "质保": ("质保", "保修", "保质"),
}


def header_family(label: str) -> str:
    """把「投标总价（万元）」这类表头归到同一语义族。"""
    for family, words in _HEADER_FAMILIES.items():
        if any(word in label for word in words):
            return family
    return ""


_HEADER_UNIT = re.compile(r"[（(]\s*([^（）()]{1,6}?)\s*[)）]\s*$")


def numeric_precheck(requirement: str, candidates: list[dict], min_overlap: int = 2) -> dict | None:
    """在调模型之前先做一次确定性数值判定。

    只在"能确定"时返回结论（已响应 / 负偏离）；判不了就返回 None，交回模型。
    四道门控（任何一道不过就放弃）：
      1. 要求必须带比较符号或单位；
      2. 要求必须带单位——"两种及以上"里的"两"是量词不是数值；
      3. 证据句必须带单位，且维度与要求一致；
      4. 数值前面的名词必须对得上——否则"最高限价45%"会被拿去比"平均值50%"。
    """
    parsed = parse_numeric(requirement)
    if parsed.number is None:
        return None
    if not _STRICT_OP.search(requirement):      # 门控 0：必须是门槛式表述
        return None
    if _CONDITIONAL.search(requirement):        # 门控 0b：条件/程序性条款不比数
        return None
    if parsed.op == "eq" and not parsed.number.unit:
        return None
    if not parsed.number.unit:          # 门控 2
        return None

    req_grams = cjk_bigrams(requirement)
    req_context = cjk_bigrams(_context_before(requirement, parsed.number))

    for chunk in candidates:
        # 表格块先按列取值：表头就是标签，"金额 | 3万" 比整行拼接可靠得多
        sentences: list[tuple[str, bool, str]] = []
        table = chunk.get("table") or {}
        header = table.get("header") or []
        for row in table.get("rows") or []:
            for index, cell in enumerate(row):
                if not (index < len(header) and header[index] and str(cell).strip()):
                    continue
                label = str(header[index]).strip()
                unit = ""
                match = _HEADER_UNIT.search(label)
                if match:
                    # 表头写了单位（金额（万元）），单元格里的裸数字要带上它
                    unit = match.group(1).strip()
                    label = label[: match.start()].strip() or label
                value = str(cell).strip()
                probe = parse_numeric(value)
                if not (probe.number and probe.number.unit):
                    value = f"{value}{unit}"
                family = header_family(label)
                sentences.append((f"{label}：{value}", True, family))
        sentences.extend(
            (sentence.strip(), False, "")
            for sentence in re.split(r"(?<=[。；;！？])", chunk["text"])
            if sentence.strip()
        )

        for sentence, from_table, family in sentences:
            evidence = parse_numeric(sentence)
            if evidence.number is None or not evidence.number.unit:
                continue
            if parsed.number.dimension != evidence.number.dimension:
                continue
            # 门控 4：数值前的名词至少两个二字组重合——
            # 只重合「报价」这种泛词不算，否则「最高限价45%」会去比「平均值50%」
            # 表格列自带表头标签，不再要求上下文重合
            if not from_table and req_context and len(
                req_context & cjk_bigrams(_context_before(sentence, evidence.number))
            ) < 2:
                continue
            # 表格列：列头同义族命中要求，或撞上一个二字组就够
            need = 1 if from_table else min_overlap
            family_hit = bool(family) and any(word in requirement for word in _HEADER_FAMILIES[family])
            if family_hit:
                need = 0
            if len(req_grams & cjk_bigrams(sentence)) < need:
                continue
            verdict = compare_numeric(requirement, sentence)
            if verdict == NUM_REVIEW:
                continue
            return {
                "coverage": "负偏离" if verdict == "废标-数值负偏离" else "已响应",
                "bid_page": chunk["page"],
                "evidence": sentence,
                "basis": f"数值预判：{parsed.number.raw} vs {evidence.number.raw} → {verdict}",
            }
    return None


_TIME_VALID = ("有效期", "质保期", "保修期", "服务期", "使用期")
_TIME_DEADLINE = ("截止", "之前", "以前", "不得晚于", "不迟于", "最晚", "提交", "递交", "送达", "上传")


def _time_marker_group(text: str) -> str:
    if any(word in text for word in _TIME_VALID):
        return "valid"
    if any(word in text for word in _TIME_DEADLINE):
        return "deadline"
    return ""


def time_precheck(requirement: str, candidates: list[dict], min_overlap: int = 2) -> dict | None:
    """时间预判：能确定「时长不足 / 已逾期」时，直接出结论，不问模型。

    三道门控（任何一道不过就放弃）：
      1. 要求里必须真的有时间（时长或日期）；
      2. 证据句里也要有时间，且与要求有 ≥2 个汉字二字组重合；
      3. 日期必须在同一语义组——「有效期至」不能拿「开标时间」来比。
    """
    req_date = parse_time_date(requirement)
    req_duration = parse_time_duration(requirement)
    if not req_date and not req_duration:
        return None
    req_group = _time_marker_group(requirement) if req_date else ""
    if req_date and not req_group:
        return None                      # 日期没有方向词，判不了

    req_grams = cjk_bigrams(requirement)
    for chunk in candidates:
        for sentence in re.split(r"(?<=[。；;！？])", chunk["text"]):
            sentence = sentence.strip()
            if not sentence:
                continue
            if not (parse_time_date(sentence) or parse_time_duration(sentence)):
                continue
            if req_group and _time_marker_group(sentence) != req_group:
                continue                 # 「有效期至」只和同组句子比
            if len(req_grams & cjk_bigrams(sentence)) < min_overlap:
                continue
            detail = explain_time(requirement, sentence)
            if detail["result"] not in (TIME_SHORT, TIME_EXPIRED):
                continue
            return {
                "coverage": "负偏离",
                "bid_page": chunk["page"],
                "evidence": sentence,
                "basis": f"时间预判：{detail['reason']}",
            }
    return None


# ------------------------------------------------------------------ 实体预判

# extract_entities 已下沉到 missing_item_check（规则引擎也要用），这里只做转发
from missing_item_check import extract_entities  # noqa: E402

_NEGATION = re.compile(r"未提供|未取得|不具备|没有|缺少|不含|未附|无此|未提交")


def _pages_from_chunks(chunks: list[dict]) -> list[str]:
    """按页号归并切块，返回 list[page-1] = 该页全文。"""
    if not chunks:
        return []
    pages = [""] * (max(chunk["page"] for chunk in chunks) + 1)
    for chunk in chunks:
        pages[chunk["page"]] = (pages[chunk["page"]] + "\n" + chunk["text"]).strip()
    return pages[1:]


_ENTITY_FILLER = (
    "投标人", "供应商", "须", "应", "应当", "提供", "具有", "具备", "有效",
    "证书", "认证", "复印件", "加盖", "公章", "扫描件", "原件", "副本", "正本",
    "相关", "材料", "文件", "证明", "的",
)


_FORMAT_CONDITIONS = ("加盖", "公章", "签字", "签章", "原件", "复印件", "扫描件", "密封", "装订", "逐页")


def _is_entity_only(requirement: str, entities: list[str]) -> bool:
    """要求是不是"只要求这几份材料"——去掉实体和套话后，剩不下多少实质内容。

    带「加盖公章 / 原件 / 复印件」这类**需要看实物**的条件时，一律不算 entity-only：
    文本里有这个词，不代表真的盖了章、交的是原件。
    """
    if any(word in requirement for word in _FORMAT_CONDITIONS):
        return False
    core = requirement
    for entity in entities:
        core = core.replace(entity, "").replace(entity.strip("《》"), "")
    core = re.sub(r"[^\u4e00-\u9fff]", "", core)
    for word in _ENTITY_FILLER:
        core = core.replace(word, "")
    return len(core) <= 3


def entity_precheck(
    requirement: str,
    checker: MissingItemChecker | None,
    entities: list[str] | None = None,
) -> dict | None:
    """资质实体预判。

    · 实体全文未出现 → 负偏离（确定性，直接短路）
    · 实体命中且不是否定语境、且要求就是"要这几份材料" → 已响应
    · 其余一律交模型——命中一个词不代表整条要求都满足
    """
    if checker is None:
        return None
    entities = entities if entities is not None else extract_entities(requirement)
    if not entities:
        return None

    results = [checker.check(entity) for entity in entities]
    missing = [item for item in results if item.status == ENTITY_MISSING]
    if missing:
        return {
            "coverage": "负偏离", "bid_page": "", "evidence": "",
            "basis": "实体检索：" + "、".join(item.entity for item in missing) + " 全文未出现",
        }

    if any(item.status != ENTITY_FOUND for item in results):
        return None
    if any(_NEGATION.search(item.snippet or "") for item in results):
        return None                      # 「未提供 ISO13485」不能算命中
    if not _is_entity_only(requirement, entities):
        return None                      # 还有别的条件，交给模型

    first = results[0]
    return {
        "coverage": "已响应", "bid_page": str(first.page or ""), "evidence": first.snippet or "",
        "basis": "实体检索：" + "、".join(item.entity for item in results) + f" 命中第{first.page}页",
    }


def precheck_gate(
    total: int,
    numeric: int,
    time_count: int,
    entity_hit: int,
    entity_missing: int,
    entity_candidates: int,
    min_sample: int = 50,
    max_rate: float = 0.75,
) -> dict:
    """规则预判的健康度检查：不是看准确率，是看"规则到底有没有在工作"。

    · 样本够大却一次都没命中 → 可能解析/抽取坏了，先别信结论
    · 短路率过高 → 规则可能过松，误杀的代价比漏判更高
    · 抽到了实体却一条都没判 → 实体规则与抽取器脱节
    """
    short = numeric + time_count + entity_hit + entity_missing
    rate = round(short / total * 100, 1) if total else 0.0
    warnings: list[str] = []
    if total >= min_sample and short == 0:
        warnings.append(f"{total} 条要求里三道规则一次都没命中，先确认解析与实体抽取是否失效")
    if total >= min_sample and rate > max_rate * 100:
        warnings.append(f"规则短路率 {rate}% 超过阈值 {max_rate * 100:.0f}%，规则可能过松")
    if entity_candidates and (entity_hit + entity_missing) == 0:
        warnings.append(f"{entity_candidates} 条要求抽到了资质实体，但没有一条被实体规则判定")
    return {
        "status": "warn" if warnings else "ok",
        "total": total,
        "short_circuit": short,
        "rate": rate,
        "numeric": numeric,
        "time": time_count,
        "entity_hit": entity_hit,
        "entity_missing": entity_missing,
        "entity_candidates": entity_candidates,
        "warnings": warnings,
    }


# ------------------------------------------------------------------ 主流程

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--bid", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--top-k", type=int, default=TOP_K)
    parser.add_argument("--min-score", type=float, default=MIN_SCORE)
    parser.add_argument("--chunk-target", type=int, default=900)
    parser.add_argument("--chunk-max", type=int, default=1400)
    parser.add_argument("--backend", default=None, choices=["pymupdf", "fitz", "pdfplumber"])
    parser.add_argument("--dry-run", action="store_true", help="只跑预筛，不调模型")
    parser.add_argument(
        "--votes",
        type=int,
        default=2,
        choices=[1, 2, 3],
        help="每条判几次：2=双跑投票（默认，不一致降级人工），1=单跑省钱",
    )
    parser.add_argument(
        "--no-numeric-precheck",
        action="store_true",
        help="关闭数值预判：所有条目都交给模型判定（用于对比调用量与判定差异）",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="禁用判定缓存：每次都重新问模型（默认开启，保证重跑结论可复现）",
    )
    parser.add_argument("--cache-path", default="", help="缓存文件路径，默认 .cache/judge_cache.json")
    parser.add_argument(
        "--precheck-max-rate",
        type=float,
        default=0.75,
        help="规则短路率上限（0~1），超过则告警：规则可能过松",
    )
    parser.add_argument(
        "--precheck-min-sample",
        type=int,
        default=50,
        help="低于这个条数不做零命中/高短路率告警（小样本的比率没有意义）",
    )
    parser.add_argument(
        "--fail-on-precheck-warn",
        action="store_true",
        help="规则预判告警时以退出码 3 结束（用于 CI 卡口）",
    )
    parser.add_argument(
        "--recheck-stale",
        action="store_true",
        help="人工确认过的条目若证据已变更，作废旧结论并退回「人工介入」重新确认",
    )
    args = parser.parse_args()

    global CACHE
    cache_path = Path(args.cache_path) if args.cache_path else ROOT / ".cache" / "judge_cache.json"
    if args.no_cache:
        CACHE = None
        print("判定缓存：已禁用", flush=True)
    else:
        loaded = load_cache(cache_path)
        print(f"判定缓存：{loaded} 条（{cache_path}）", flush=True)

    ledger_dir = Path(args.ledger)
    csv_path = ledger_dir / "requirements.csv"
    if not csv_path.exists():
        csv_path = ledger_dir / "gated/requirements.csv"
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
    if args.limit:
        rows = rows[: args.limit]

    chunks, stats = parse_bid(Path(args.bid), args.chunk_target, args.chunk_max, args.backend)
    print(
        f"解析后端 {stats['backend']} | 页数 {stats['pages']} | 切块 {stats['chunks']} 段"
        f"（保留 {stats['kept']} 段，平均 {stats['avg_chars']} 字）| 要求 {len(rows)} 条",
        flush=True,
    )
    if stats["ocr_missing_pages"]:
        print(f"⚠ 这些页没有文本层且未完成 OCR：{stats['ocr_missing_pages']}", flush=True)
    if not chunks:
        print("✗ 切块为 0，后续比对必然全「未找到」。请先修解析。", flush=True)
        raise SystemExit(2)

    idf = build_idf(chunks)
    key = load_key()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 实体检索放在词面预筛之前：资质"有没有"不依赖相似度，
    # 缺了就是缺了，不该因为整句和标书不像而被判成笼统的"未找到"
    entity_hit = 0
    entity_missing = 0
    entity_candidates = 0
    checker = None if args.no_numeric_precheck else MissingItemChecker(_pages_from_chunks(chunks))

    results, pending, scores = [], [], []
    for row in rows:
        query = bigrams(row["requirement"])
        scored = sorted(((score(query, c["grams"], idf), c) for c in chunks), key=lambda item: -item[0])[: args.top_k]
        best = scored[0][0] if scored else 0.0
        cands = [c for s, c in scored if s > 0]
        scores.append((round(best, 3), row["req_id"]))

        entities = extract_entities(row["requirement"])
        if entities:
            entity_candidates += 1
        pre = entity_precheck(row["requirement"], checker, entities)
        if pre:
            entity_hit += pre["coverage"] == "已响应"
            entity_missing += pre["coverage"] == "负偏离"
            results.append({
                "req_id": row["req_id"], "requirement": row["requirement"],
                "coverage": pre["coverage"], "bid_page": pre["bid_page"], "evidence": pre["evidence"],
                "score": round(best, 3), "reason": pre["basis"], "judge_basis": pre["basis"],
                "need_confirm": "", "model_status": "",
                "sig": evidence_sig(pre["evidence"], pre["basis"]),
            })
            continue

        if not cands or best < args.min_score:
            results.append({
                "req_id": row["req_id"], "requirement": row["requirement"], "coverage": "未找到",
                "bid_page": "", "evidence": "", "score": round(best, 3), "reason": "标书里没有相近段落",
                "judge_basis": "", "need_confirm": "", "model_status": "",
                "sig": evidence_sig("", ""),
            })
            continue
        pending.append((row, cands))

    # 规则预判：能确定判负偏离的，不必再问模型（可用 --no-numeric-precheck 关闭）
    numeric_fail = 0
    time_fail = 0
    if args.no_numeric_precheck:
        print("规则预判：已关闭，全部条目交给模型", flush=True)
    else:
        still_pending = []
        for row, cands in pending:
            pre = numeric_precheck(row["requirement"], cands)
            if pre and pre["coverage"] == "负偏离":
                numeric_fail += 1
            else:
                pre = time_precheck(row["requirement"], cands)
                if pre and pre["coverage"] == "负偏离":
                    time_fail += 1
                else:
                    pre = None
            if pre:
                results.append({
                    "req_id": row["req_id"], "requirement": row["requirement"],
                    "coverage": pre["coverage"], "bid_page": pre["bid_page"], "evidence": pre["evidence"],
                    "score": 0.0, "reason": pre["basis"], "judge_basis": pre["basis"],
                    "need_confirm": "", "model_status": "",
                    "sig": evidence_sig(pre["evidence"], pre["basis"]),
                })
                continue
            still_pending.append((row, cands))
        pending = still_pending

    print(
        f"预筛：{len(results) - numeric_fail - time_fail - entity_hit - entity_missing} 条直接判未找到，"
        f"数值判负偏离 {numeric_fail}、时间判负偏离 {time_fail}、"
        f"实体缺失 {entity_missing}、实体命中 {entity_hit}，"
        f"{len(pending)} 条送模型判定（阈值 {args.min_score}）",
        flush=True,
    )
    print("分数分布：" + ", ".join(f"{s}" for s, _ in sorted(scores, reverse=True)), flush=True)

    if args.dry_run:
        for row, cands in pending:
            top = cands[0]
            print(f"  {row['req_id']} → p{top['page']} {top['text'][:60]}")
        return

    done = 0
    print(f"判定模式：{'单跑' if args.votes == 1 else f'双跑投票（{args.votes} 票）'}", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(judge, key, row, cands, args.votes): row for row, cands in pending}
        for future in as_completed(futures):
            row = futures[future]
            verdict = future.result()
            query = bigrams(row["requirement"])
            best = max((score(query, c["grams"], idf) for c in chunks), default=0.0)
            results.append({
                "req_id": row["req_id"], "requirement": row["requirement"],
                "coverage": verdict["coverage"], "bid_page": verdict["bid_page"],
                "evidence": verdict["evidence"], "score": round(best, 3), "reason": verdict["reason"],
                "judge_basis": verdict.get("basis", ""),
                "need_confirm": "Y" if verdict["coverage"] == "人工介入" else "",
                "model_status": verdict.get("model_status", ""),
                "votes": verdict.get("votes", ""),
                "vote_agree": verdict.get("vote_agree", ""),
                "sig": verdict.get("sig") or evidence_sig(verdict.get("evidence", ""), verdict.get("basis", "")),
            })
            done += 1
            if done % 20 == 0:
                print(f"  判定 {done}/{len(pending)}", flush=True)

    order = {row["req_id"]: index for index, row in enumerate(rows)}
    results.sort(key=lambda item: order.get(item["req_id"], 9999))

    for item in results:
        item.setdefault("reviewed", "")
        item.setdefault("review_stale", "")
        item.setdefault("sig", evidence_sig(item.get("evidence", ""), item.get("judge_basis", "")))
    overlay_stats = apply_review_overlay(results, out_dir, recheck_stale=args.recheck_stale)

    with (out_dir / "coverage.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "req_id", "requirement", "coverage", "bid_page", "evidence", "score",
            "reason", "judge_basis", "need_confirm", "model_status",
            "votes", "vote_agree", "sig", "reviewed", "review_stale",
        ])
        writer.writeheader()
        writer.writerows(results)

    if CACHE is not None:
        saved = save_cache(cache_path)
        print(f"判定缓存：已保存 {saved} 条（本次命中 {CACHE_HITS} 次）", flush=True)

    counts: dict[str, int] = {}
    for item in results:
        counts[item["coverage"]] = counts.get(item["coverage"], 0) + 1
    overridden = sum(1 for item in results if item["model_status"] and item["model_status"] != item["coverage"])
    summary = {
        "bid": str(args.bid),
        "ledger": str(ledger_dir),
        "requirements": len(results),
        "chunks": len(chunks),
        "chunk_stats": stats,
        "min_score": args.min_score,
        "coverage": counts,
        # 有回应 = 模型/规则在标书里找到了对应内容（负偏离、人工介入也算有回应）
        "responded_rate": round(
            sum(counts.get(key, 0) for key in ("已响应", "部分响应", "负偏离", "人工介入"))
            / max(len(results), 1)
            * 100,
            1,
        ),
        "compliant_rate": round(counts.get("已响应", 0) / max(len(results), 1) * 100, 1),
        # 负偏离是"响应了但不达标"，和未响应一样要改标书
        "missing": counts.get("未响应", 0) + counts.get("未找到", 0) + counts.get("负偏离", 0),
        "negative_deviation": counts.get("负偏离", 0),
        "pending_confirm": counts.get("人工介入", 0),
        "rule_overridden": overridden,
        "numeric_precheck": not args.no_numeric_precheck,
        "numeric_short_circuit": numeric_fail,
        "time_short_circuit": time_fail,
        "entity_hit": entity_hit,
        "entity_missing": entity_missing,
        "votes": args.votes,
        "vote_disagree": sum(1 for item in results if item.get("vote_agree") == "N"),
        "cache_hits": CACHE_HITS,
        "model_calls": MODEL_CALLS,
        "reviewed": overlay_stats["reviewed"],
        "review_stale": overlay_stats["stale"],
        "review_rechecked": overlay_stats["rechecked"],
        "review_dropped": overlay_stats["dropped"],
    }
    summary["precheck"] = precheck_gate(
        total=len(results), numeric=numeric_fail, time_count=time_fail,
        entity_hit=entity_hit, entity_missing=entity_missing,
        entity_candidates=entity_candidates, max_rate=args.precheck_max_rate,
        min_sample=args.precheck_min_sample,
    )
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "chunk_stats"}, ensure_ascii=False, indent=2))

    gate = summary["precheck"]
    if gate["warnings"]:
        print("\n⚠ 规则预判告警：", flush=True)
        for line in gate["warnings"]:
            print(f"  · {line}", flush=True)
        if args.fail_on_precheck_warn:
            raise SystemExit(3)


if __name__ == "__main__":
    main()
