#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_rules.py — 投标响应判定的确定性规则层

模型负责"读懂语义"，规则层负责"不许它含糊"。规则在模型给出结论**之后**运行，
命中就强制改判，模型无权推翻：

  1. 数值比较：要求与证据都出现同类数值（金额/尺寸/数量/期限/百分比）时，
     按「不低于/不超过/前/后」的方向做大小比较。证据劣于要求 → 负偏离。
  2. 资质一致性：要求 ISO13485、证据只有 ISO9001 这类同名不同号的情况 → 未响应。
  3. 人工介入：数值或资质确实参与判定、但规则判不了"没问题"时（方向缺失、
     多个候选数、口径不同），不给「已响应」，降级为「人工介入」等审查员确认。

设计原则：宁可交给人，也不要猜。规则只在能给出确定结论时才强制改判。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

try:  # 同目录直接运行
    from time_check import EXPIRED as TIME_EXPIRED, SHORT as TIME_SHORT, explain as explain_time
except ImportError:  # 作为包导入
    from adapter.time_check import EXPIRED as TIME_EXPIRED, SHORT as TIME_SHORT, explain as explain_time

# ------------------------------------------------------------------ 数值

# 长的单位要排在前面，否则「万元」会被「元」、「个月」会被「月」抢先匹配
_UNIT_TABLE = [
    ("亿元", "money", 1e8),
    ("万元", "money", 1e4),
    ("元", "money", 1),
    ("英寸", "length", 25.4),
    ("寸", "length", 33.333),
    ("毫米", "length", 1),
    ("厘米", "length", 10),
    ("米", "length", 1000),
    ("个月", "duration", 30),
    ("月", "duration", 30),
    ("天", "duration", 1),
    ("日", "duration", 1),
    ("年", "duration", 365),
    ("小时", "duration", 1 / 24),
    ("分钟", "duration", 1 / 1440),
    ("份", "count", 1),
    ("个", "count", 1),
    ("台", "count", 1),
    ("套", "count", 1),
    ("名", "count", 1),
    ("人", "count", 1),
    ("页", "count", 1),
    ("项", "count", 1),
    ("次", "count", 1),
    ("%", "percent", 1),
    ("％", "percent", 1),
]
_UNIT_RE = "|".join(re.escape(unit) for unit, _, _ in _UNIT_TABLE)
_UNIT_MAP = {unit: (family, factor) for unit, family, factor in _UNIT_TABLE}

_NUM_RE = re.compile(rf"(?<![\d.])(\d+(?:\.\d+)?)\s*({_UNIT_RE})")
_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_NUM_RE = re.compile(rf"([零一二两三四五六七八九十]{{1,3}})\s*({_UNIT_RE})")

_DATE_RE = re.compile(
    r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"(?:\s*(上午|下午)?\s*(\d{1,2})\s*[:：]\s*(\d{2}))?"
)
_CLOCK_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{2})(?!\d)")

_GE_WORDS = ("不低于", "不少于", "不小于", "至少", "及以上", "以上", "不低于于", "≥", ">=", "起", "最低")
_LE_WORDS = ("不超过", "不大于", "不高于", "最多", "至多", "以内", "以下", "≤", "<=", "最高")
_BEFORE_WORDS = ("之前", "以前", "前完成", "前提交", "前送达", "截止", "前")
_AFTER_WORDS = ("之后", "以后", "起算", "后")


@dataclass
class Value:
    amount: float          # 已换算到基准单位
    unit: str              # 原始单位
    family: str            # money / length / count / duration / percent
    raw: str
    start: int
    end: int
    context: str           # 数值前后文，用于判断它对应哪个主体
    clause: str = ""      # 数值所在的整个小句，主体词（正本/副本）在这一句里
    clause_offset: int = 0  # 数值在小句里的起始下标，用来判断主体词在前还是在后


@dataclass
class Constraint:
    value: Value
    op: str | None         # ">=" / "<=" / "==" / None(方向不明)
    kind: str              # "number" / "date"
    anchors: set[str]      # 要求侧的主体词（2-gram），用于在证据里找对应数值
    raw: str


@dataclass
class RuleHit:
    rule: str              # "数值" / "资质"
    severity: str          # "violate"(强制改判) / "confirm"(需人工确认)
    status: str            # 建议状态
    detail: str            # 给人看的一句话


def _cn_to_int(text: str) -> int | None:
    if not text:
        return None
    if text == "十":
        return 10
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_NUM.get(left, 1) if left else 1
        ones = _CN_NUM.get(right, 0) if right else 0
        return tens * 10 + ones
    digits = [_CN_NUM.get(ch) for ch in text]
    if any(d is None for d in digits):
        return None
    return int("".join(str(d) for d in digits))


def extract_values(text: str) -> list[Value]:
    """抽取带单位的数值，以及形如 2026年11月5日 的日期。"""
    values: list[Value] = []
    masked = list(text or "")

    for match in _DATE_RE.finditer(text or ""):
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        hour = int(match.group(5) or 0)
        minute = int(match.group(6) or 0)
        if match.group(4) == "下午" and hour < 12:
            hour += 12
        try:
            moment = datetime(year, month, day, hour, minute)
        except ValueError:
            continue
        values.append(
            Value(
                amount=moment.timestamp(),
                unit="datetime",
                family="datetime",
                raw=match.group(0),
                start=match.start(),
                end=match.end(),
                context=_window(text, match.start(), match.end()),
                clause=_clause(text, match.start(), match.end())[0],
                clause_offset=_clause(text, match.start(), match.end())[1],
            )
        )
        for i in range(match.start(), match.end()):
            masked[i] = " "

    masked_text = "".join(masked)
    for match in _NUM_RE.finditer(masked_text):
        unit = match.group(2)
        family, factor = _UNIT_MAP[unit]
        values.append(
            Value(
                amount=float(match.group(1)) * factor,
                unit=unit,
                family=family,
                raw=match.group(0),
                start=match.start(),
                end=match.end(),
                context=_window(masked_text, match.start(), match.end()),
                clause=_clause(text, match.start(), match.end())[0],
                clause_offset=_clause(text, match.start(), match.end())[1],
            )
        )
    for match in _CN_NUM_RE.finditer(masked_text):
        if any(v.start <= match.start() < v.end for v in values):
            continue
        number = _cn_to_int(match.group(1))
        if number is None:
            continue
        unit = match.group(2)
        family, factor = _UNIT_MAP[unit]
        values.append(
            Value(
                amount=number * factor,
                unit=unit,
                family=family,
                raw=match.group(0),
                start=match.start(),
                end=match.end(),
                context=_window(masked_text, match.start(), match.end()),
                clause=_clause(text, match.start(), match.end())[0],
                clause_offset=_clause(text, match.start(), match.end())[1],
            )
        )
    return values


def _window(text: str, start: int, end: int, width: int = 14) -> str:
    return text[max(0, start - width) : min(len(text), end + width)]


_CLAUSE_SEP = "，。；：、！？,;:\n（）()"


def _clause(text: str, start: int, end: int) -> tuple[str, int]:
    """取数值所在的整个小句——主体词（正本/副本）就在这一句里。"""
    left = start
    while left > 0 and text[left - 1] not in _CLAUSE_SEP:
        left -= 1
    right = end
    while right < len(text) and text[right] not in _CLAUSE_SEP:
        right += 1
    return text[left:right].strip(), start - left


def _anchors(text: str, start: int, width: int = 14) -> set[str]:
    """数值周边的中文 2-gram，代表这个数值在说哪件事。"""
    window = text[max(0, start - width) : start]
    return _context_anchors(window)


def _context_anchors(text: str) -> set[str]:
    grams: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text or ""):
        grams.update(run[i : i + 2] for i in range(len(run) - 1))
    return grams


def _op_before(text: str, start: int, end: int = 0, *, is_date: bool) -> str | None:
    prefix = text[max(0, start - 16) : start]
    if is_date:
        # 期限的方向词常跟在日期后面：「2026年11月5日17:00前完成」
        suffix = text[end : end + 8] if end else ""
        probe = prefix + "|" + suffix
        for word in _BEFORE_WORDS:
            if word in probe:
                return "<="
        for word in _AFTER_WORDS:
            if word in probe:
                return ">="
        return None
    for word in _GE_WORDS:
        if word in prefix:
            return ">="
    for word in _LE_WORDS:
        if word in prefix:
            return "<="
    return None


def extract_constraints(requirement: str) -> list[Constraint]:
    constraints: list[Constraint] = []
    for value in extract_values(requirement):
        op = _op_before(requirement, value.start, value.end, is_date=value.family == "datetime")
        kind = "date" if value.family == "datetime" else "number"
        constraints.append(
            Constraint(
                value=value,
                op=op,
                kind=kind,
                anchors=_context_anchors(value.clause or value.context),
                raw=value.raw,
            )
        )
    return constraints


def _anchor_score(constraint: Constraint, value: Value) -> int:
    """主体词必须出现在数值**前面**且越近越好。

    「副本3份」→ 副本 紧邻数字，得分高；
    「少了一份副本」→ 副本 在数字后面，不是这个数字的主体，得分 0。
    """
    clause = value.clause or ""
    if not clause:
        return 0
    best = 0
    for gram in constraint.anchors:
        position = clause.rfind(gram, 0, max(0, value.clause_offset))
        if position < 0:
            continue
        gap = value.clause_offset - (position + len(gram))
        best = max(best, 10 - gap)
    return best


def _pick_evidence_value(constraint: Constraint, evidence_values: list[Value]) -> tuple[Value | None, bool]:
    """在证据里找与这条约束对应的数值。返回 (数值, 是否唯一确定)。"""
    family = constraint.value.family
    pool = [v for v in evidence_values if v.family == family]
    if not pool:
        return None, True
    if family == "count":  # 份/台/人 之间不可比
        pool = [v for v in pool if v.unit == constraint.value.unit]
        if not pool:
            return None, True

    scored: list[tuple[int, Value]] = []
    for value in pool:
        scored.append((_anchor_score(constraint, value), value))
    scored.sort(key=lambda item: -item[0])

    best_score = scored[0][0]
    if best_score == 0:
        # 对不上主体：只有一个候选才敢用，否则交给人
        if len(pool) == 1:
            return pool[0], True
        return None, False
    winners = [value for score, value in scored if score == best_score]
    if len(winners) > 1 and len({w.amount for w in winners}) > 1:
        return None, False
    return winners[0], True


def check_numeric(requirement: str, evidence: str) -> list[RuleHit]:
    constraints = extract_constraints(requirement)
    if not constraints:
        return []
    evidence_values = extract_values(evidence)
    hits: list[RuleHit] = []

    for constraint in constraints:
        if constraint.value.family in ("duration", "datetime"):
            # 时长与日期交给 check_time：月/天换算、有效期方向都在那边按更严的口径处理
            continue
        target, certain = _pick_evidence_value(constraint, evidence_values)
        if target is None:
            if not certain:
                hits.append(
                    RuleHit(
                        rule="数值",
                        severity="confirm",
                        status="人工介入",
                        detail=f"要求「{constraint.raw}」在证据里对应到多个数值，无法自动比较",
                    )
                )
            continue

        if constraint.op is None:
            # 没写方向时：给少了在任何口径下都是偏离，强制改判；
            # 给多了只是"超额"，交给人看，不替审查员拍板。
            if target.amount < constraint.value.amount:
                hits.append(
                    RuleHit(
                        rule="数值",
                        severity="violate",
                        status="负偏离",
                        detail=f"要求 {constraint.raw}，证据仅 {target.raw}，数量/金额不足",
                    )
                )
            else:
                hits.append(
                    RuleHit(
                        rule="数值",
                        severity="confirm",
                        status="人工介入",
                        detail=f"要求「{constraint.raw}」未写方向，证据为「{target.raw}」，需人工确认",
                    )
                )
            continue

        req_amount, ev_amount = constraint.value.amount, target.amount
        violated = (constraint.op == ">=" and ev_amount < req_amount) or (
            constraint.op == "<=" and ev_amount > req_amount
        )
        direction = "≥" if constraint.op == ">=" else "≤"
        if violated:
            hits.append(
                RuleHit(
                    rule="数值",
                    severity="violate",
                    status="负偏离",
                    detail=f"要求 {direction} {constraint.raw}，证据为 {target.raw}，不满足",
                )
            )
        else:
            hits.append(
                RuleHit(
                    rule="数值",
                    severity="confirm",
                    status="人工介入",
                    detail=f"数值已核对：要求 {direction} {constraint.raw}，证据 {target.raw}（比较通过，待确认）",
                )
            )
    return hits


# ------------------------------------------------------------------ 资质

_ISO_RE = re.compile(r"ISO\s*/?\s*(?:IEC\s*)?(\d{4,5}(?:-\d+)?)", re.I)
_CERT_BOOK_RE = re.compile(r"《([^》]{2,30})》")
_OTHER_CERT_RE = re.compile(r"\b(CCC|3C|CMA|CNAS|CE|UL|FDA)\b", re.I)


_NEG_PREFIX = ("未提供", "未取得", "未附", "未提交", "未获", "没有", "不具备", "缺少", "不含", "无")


def _negated(text: str, position: int) -> bool:
    """「未提供 ISO13485」这种否定语境里的证号不算"具备"。"""
    prefix = text[max(0, position - 10) : position]
    if any(word in prefix for word in _NEG_PREFIX[:-1]):
        return True
    return prefix.endswith("无")


def extract_certs(text: str) -> set[str]:
    certs: set[str] = set()
    text = text or ""
    for match in _ISO_RE.finditer(text):
        if _negated(text, match.start()):
            continue
        certs.add(f"ISO{match.group(1)}")
    for match in _CERT_BOOK_RE.finditer(text):
        name = match.group(1).strip()
        if not any(word in name for word in ("许可证", "认证", "证书", "执照", "资质")):
            continue
        if _negated(text, match.start()):
            continue
        certs.add(f"《{name}》")
    for match in _OTHER_CERT_RE.finditer(text):
        if _negated(text, match.start()):
            continue
        certs.add(match.group(1).upper())
    return certs


def check_qualification(requirement: str, evidence: str) -> list[RuleHit]:
    required = extract_certs(requirement)
    if not required:
        return []
    found = extract_certs(evidence)
    hits: list[RuleHit] = []

    for cert in sorted(required):
        if cert in found:
            hits.append(
                RuleHit(
                    rule="资质",
                    severity="confirm",
                    status="人工介入",
                    detail=f"资质已核对：{cert} 名称一致（有效期需人工确认）",
                )
            )
            continue
        # 要求 ISO13485，证据是 ISO9001：同类不同号，直接判未响应
        same_family = [other for other in found if other.split("ISO")[-1][:1] == cert.split("ISO")[-1][:1]] if cert.startswith("ISO") else []
        if cert.startswith("ISO") and any(other.startswith("ISO") for other in found):
            others = "、".join(sorted(other for other in found if other.startswith("ISO")))
            hits.append(
                RuleHit(
                    rule="资质",
                    severity="violate",
                    status="未响应",
                    detail=f"要求 {cert}，证据只有 {others}，认证号不一致",
                )
            )
        elif cert.startswith("《") and any(other.startswith("《") for other in found):
            others = "、".join(sorted(other for other in found if other.startswith("《")))
            hits.append(
                RuleHit(
                    rule="资质",
                    severity="violate",
                    status="未响应",
                    detail=f"要求 {cert}，证据为 {others}，证照名称不一致",
                )
            )
        elif same_family:
            continue
        # 证据里压根没提资质：不下结论，交给模型/人工
    return hits


# ------------------------------------------------------------------ 时间

def check_time(requirement: str, evidence: str) -> list[RuleHit]:
    """时间/日期规则：只在能确定「时间不足」或「已逾期」时强制改判。

    判不了的情形一律不下结论（跨月/天换算、缺方向词、某边没时间）——
    时间条款常要靠材料核对，规则不该替审查员拍板。
    """
    detail = explain_time(requirement, evidence)
    if detail["result"] == TIME_SHORT:
        return [RuleHit(rule="时间", severity="violate", status="负偏离",
                        detail=f"时间不足：{detail['reason']}")]
    if detail["result"] == TIME_EXPIRED:
        return [RuleHit(rule="时间", severity="violate", status="负偏离",
                        detail=f"已逾期：{detail['reason']}")]
    return []


# ------------------------------------------------------------------ 汇总

STATUS_ORDER = ("负偏离", "未响应", "人工介入", "部分响应", "已响应")


def apply_rules(requirement: str, evidence: str, model_status: str) -> tuple[str, list[RuleHit]]:
    """规则层入口：返回 (最终状态, 命中规则列表)。

    - 有 violate → 强制改判（模型无权保留「已响应」）
    - 只有 confirm 且模型给了「已响应」→ 降级为「人工介入」
    - 其余保留模型结论
    """
    hits = (
        check_numeric(requirement, evidence)
        + check_time(requirement, evidence)
        + check_qualification(requirement, evidence)
    )
    if not hits:
        return model_status, []

    violations = [hit for hit in hits if hit.severity == "violate"]
    if violations:
        # 多条违规取最严重的一条作为状态
        order = {status: index for index, status in enumerate(STATUS_ORDER)}
        worst = min(violations, key=lambda hit: order.get(hit.status, 99))
        return worst.status, hits

    if model_status in ("已响应",):
        return "人工介入", hits
    return model_status, hits


def describe(hits: list[RuleHit]) -> str:
    return "；".join(hit.detail for hit in hits)[:200]


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="规则层自检")
    parser.add_argument("requirement")
    parser.add_argument("evidence")
    parser.add_argument("--model-status", default="已响应")
    args = parser.parse_args()
    status, hits = apply_rules(args.requirement, args.evidence, args.model_status)
    print(json.dumps(
        {
            "final": status,
            "model": args.model_status,
            "hits": [hit.__dict__ for hit in hits],
        },
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
