#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""numeric_check.py — 标书审查的【数值比对】模块

职责单一：给定一句「要求」和一句「证据」，判断证据里的数值是否满足要求。

设计要点
--------
1. 比较符号：不低于/不少于/≥ → ge；不超过/不大于/≤ → le；大于/超过 → gt；
   小于/低于 → lt；没有符号 → eq（等于）。
2. 单位：万 / 亿 / 万元 / 亿元 自动换算到元；天/日、个月/月、年、小时 各自成维度；
   维度不同（如「万元」vs「天」）不硬比，直接交人工。
3. 一个句子常有多个数字（「近三个月…500万元」），取**比较符号后面最近的那个**；
   符号后面没有，就取前面最近的；开头的「1.」「2、」这类序号会被跳过。
4. 支持中文数字（五百万 / 伍佰万元），因为真实标书里两种写法都常见。
5. 证据里出现「约 / 大约 / 左右 / 余」时不做结论，交人工——这类表述本身不可比。

返回
----
"已响应" / "废标-数值负偏离" / "需人工复核"
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

OK = "已响应"
FAIL = "废标-数值负偏离"
REVIEW = "需人工复核"

# ---------------------------------------------------------------- 文本归一

_FULLWIDTH = str.maketrans("０１２３４５６７８９．，％（）：；　", "0123456789.,%():; ")


def normalize(text: str) -> str:
    """全角转半角、去首尾空白、统一货币符号。"""
    return (text or "").translate(_FULLWIDTH).replace("￥", "¥").strip()


# ---------------------------------------------------------------- 中文数字

_CN_DIGIT = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
    "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9,
}
_CN_UNIT = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_CN_BIG = {"万": 10 ** 4, "亿": 10 ** 8}
_CN_CHARS = set(_CN_DIGIT) | set(_CN_UNIT) | set(_CN_BIG)


def cn_to_decimal(text: str) -> Optional[Decimal]:
    """中文数字转 Decimal：五百万 → 5000000；伍佰万 → 5000000。失败返回 None。"""
    if not text:
        return None
    total = Decimal(0)
    section = Decimal(0)
    number = Decimal(0)
    for char in text:
        if char in _CN_DIGIT:
            number = Decimal(_CN_DIGIT[char])
        elif char in _CN_UNIT:
            section += (number or Decimal(1)) * _CN_UNIT[char]
            number = Decimal(0)
        elif char in _CN_BIG:
            section = (section + number) * _CN_BIG[char]
            total += section
            section = Decimal(0)
            number = Decimal(0)
        else:
            return None
    return total + section + number


# ---------------------------------------------------------------- 数值与单位

# 长的单位写在前面，避免「万元」被拆成「万」、「个月」被拆成「月」
_NUMBER_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?|[零一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟]+)"
    r"\s*(?P<unit>亿元|亿|万元|万|元|美元|个月|月|年|天|日|小时|分钟|英寸|寸|%|个|台|套|份|人|次|项|米|公斤|kg)?"
)

# 单位 → (换算到基准的系数, 维度)；维度不同则不可比
_UNIT_RULES: dict[str, tuple[Decimal, str]] = {
    "元": (Decimal(1), "money"),
    "万元": (Decimal(10 ** 4), "money"),
    "万": (Decimal(10 ** 4), "money"),
    "亿元": (Decimal(10 ** 8), "money"),
    "亿": (Decimal(10 ** 8), "money"),
    "美元": (Decimal(1), "usd"),
    "天": (Decimal(1), "day"),
    "日": (Decimal(1), "day"),
    "个月": (Decimal(1), "month"),
    "月": (Decimal(1), "month"),
    "年": (Decimal(1), "year"),
    "小时": (Decimal(1), "hour"),
    "分钟": (Decimal(1), "minute"),
    "英寸": (Decimal(1), "inch"),
    "寸": (Decimal(1), "inch"),
    "%": (Decimal(1), "percent"),
    "个": (Decimal(1), "count"),
    "台": (Decimal(1), "count"),
    "套": (Decimal(1), "count"),
    "份": (Decimal(1), "count"),
    "人": (Decimal(1), "count"),
    "次": (Decimal(1), "count"),
    "项": (Decimal(1), "count"),
    "米": (Decimal(1), "length"),
    "公斤": (Decimal(1), "weight"),
    "kg": (Decimal(1), "weight"),
}

# 序号：1. / 2、 / （3） 这种开头的数字不是业务数值。
# 注意结尾的 (?!\d)：否则「12.1英寸」会被误判成「12.」开头的序号。
_LIST_MARKER_RE = re.compile(r"^\s*[（(]?\d{1,2}[）).、．]\s*(?!\d)")

# 不确定表述：约 300 万 / 300 万左右 —— 不可比
_UNCERTAIN_RE = re.compile(r"(约|大约|左右|余|近|超过?不多|上下)")


@dataclass
class Number:
    value: Decimal          # 原始数值（未换算）
    unit: str               # 原始单位，可能为空
    raw: str                # 原文片段

    @property
    def base_value(self) -> Optional[Decimal]:
        rule = _UNIT_RULES.get(self.unit)
        return None if rule is None else self.value * rule[0]

    @property
    def dimension(self) -> Optional[str]:
        rule = _UNIT_RULES.get(self.unit)
        return None if rule is None else rule[1]


@dataclass
class Comparison:
    op: str                 # ge / le / gt / lt / eq
    number: Optional[Number]
    raw: str


# ---------------------------------------------------------------- 比较符号

# 顺序很重要：「不低于」必须先于「低于」，「≥」必须先于「>」
_OP_RULES = [
    ("ge", r"不低于|不少于|不小于|不得低于|不得少于|大于等于|大于或等于|大于或超过|至少(?:为|有)?"),
    ("le", r"不超过|不得高于|不得多于|不大于|小于等于|小于或等于|至多(?:为)?|不多于"),
    ("ge", r"及以上|以上"),
    ("le", r"及以下|以下|以内"),
    ("gt", r"大于|高于|超过|多于"),
    ("lt", r"小于|低于|少于"),
    ("ge", r"≥|>="),
    ("le", r"≤|<="),
    ("gt", r">"),
    ("lt", r"<"),
    ("eq", r"等于|为|是|=|："),
]
_OP_COMPILED = [(op, re.compile(pattern)) for op, pattern in _OP_RULES]


def _find_operator(text: str) -> tuple[str, int, int]:
    """返回 (op, start, end)；没有符号则返回 eq 且位置为 -1。"""
    best: Optional[tuple[str, int, int]] = None
    for op, pattern in _OP_COMPILED:
        match = pattern.search(text)
        if not match:
            continue
        if best is None or match.start() < best[1] or (
            match.start() == best[1] and match.end() > best[2]
        ):
            best = (op, match.start(), match.end())
    return best if best else ("eq", -1, -1)


# ---------------------------------------------------------------- 解析

def _to_number(token: str, unit: str) -> Optional[Number]:
    token = token.strip()
    if not token:
        return None
    if token[0].isdigit():
        try:
            value = Decimal(token)
        except InvalidOperation:
            return None
    else:
        value = cn_to_decimal(token)
        if value is None:
            return None
    return Number(value=value, unit=unit or "", raw=f"{token}{unit or ''}")


def parse(text: str) -> Comparison:
    """从一句话里解析出「比较符号 + 数值」。"""
    normalized = normalize(text)
    op, start, end = _find_operator(normalized)
    marker = _LIST_MARKER_RE.match(normalized)
    marker_end = marker.end() if marker else -1

    candidates: list[tuple[int, Number]] = []
    for match in _NUMBER_RE.finditer(normalized):
        if match.start() < marker_end:      # 句首序号，跳过
            continue
        number = _to_number(match.group("num"), match.group("unit") or "")
        if number is not None:
            candidates.append((match.start(), number))
    if not candidates:
        return Comparison(op=op, number=None, raw=normalized)

    after = [item for item in candidates if item[0] >= end]
    if after:
        chosen = after[0][1]
    else:
        before = [item for item in candidates if item[0] < start]
        chosen = before[-1][1] if before else candidates[0][1]
    return Comparison(op=op, number=chosen, raw=normalized)


# ---------------------------------------------------------------- 比较

def _satisfies(op: str, evidence: Decimal, requirement: Decimal) -> bool:
    if op == "ge":
        return evidence >= requirement
    if op == "le":
        return evidence <= requirement
    if op == "gt":
        return evidence > requirement
    if op == "lt":
        return evidence < requirement
    return evidence == requirement


def _direction(op: str) -> str:
    return "up" if op in ("ge", "gt") else "down" if op in ("le", "lt") else "eq"


def explain(requirement_text: str, evidence_text: str) -> dict:
    """返回结构化结果，便于 UI 展示和排查。"""
    requirement = parse(requirement_text)
    evidence = parse(evidence_text)

    detail = {
        "requirement_op": requirement.op,
        "requirement_value": str(requirement.number.base_value) if requirement.number else None,
        "requirement_raw": requirement.number.raw if requirement.number else "",
        "evidence_op": evidence.op,
        "evidence_value": str(evidence.number.base_value) if evidence.number else None,
        "evidence_raw": evidence.number.raw if evidence.number else "",
        "result": REVIEW,
        "reason": "",
    }

    if requirement.number is None:
        detail["reason"] = "要求文本里没有可用数值"
        return detail
    if evidence.number is None:
        detail["reason"] = "证据文本里没有可用数值"
        return detail
    if _UNCERTAIN_RE.search(normalize(evidence_text)):
        detail["reason"] = "证据含「约/左右/余」等不确定表述，不可直接比较"
        return detail

    req_unit = requirement.number.unit
    ev_unit = evidence.number.unit
    if req_unit and not ev_unit:
        evidence.number.unit = req_unit          # 证据省略单位，按要求的单位理解
        detail["reason"] = f"证据未写单位，按要求的「{req_unit}」理解；"
    elif ev_unit and not req_unit:
        requirement.number.unit = ev_unit
        detail["reason"] = f"要求未写单位，按证据的「{ev_unit}」理解；"

    req_dim = requirement.number.dimension
    ev_dim = evidence.number.dimension
    if req_dim and ev_dim and req_dim != ev_dim:
        detail["reason"] += f"单位维度不一致（{req_unit} vs {ev_unit}），无法直接比较"
        return detail

    req_value = requirement.number.base_value
    ev_value = evidence.number.base_value
    if req_value is None or ev_value is None:
        detail["reason"] += "单位无法识别"
        return detail

    # 证据自带比较符号：只有同方向才能比，"不低于300" 回应 "不低于500" 属承诺不足
    if evidence.op != "eq":
        if _direction(evidence.op) != _direction(requirement.op):
            detail["reason"] += f"证据用的是「{evidence.op}」，与要求「{requirement.op}」方向不一致"
            return detail
        ok = _satisfies(requirement.op, ev_value, req_value)
    else:
        ok = _satisfies(requirement.op, ev_value, req_value)

    detail["result"] = OK if ok else FAIL
    detail["reason"] += f"{ev_value} {requirement.op} {req_value} → {'满足' if ok else '不满足'}"
    return detail


def compare(requirement_text: str, evidence_text: str) -> str:
    """对外主函数：只返回三态字符串。"""
    return explain(requirement_text, evidence_text)["result"]


# ---------------------------------------------------------------- 自测

if __name__ == "__main__":
    cases = [
        ("注册资本不低于人民币500万元", "我司注册资本为300万元", FAIL),
        ("注册资本不低于人民币500万元", "注册资本800万元", OK),
        ("注册资本不低于500万元", "不低于300万元", FAIL),
        ("注册资本不低于500万元", "不低于600万元", OK),
        ("交付周期不超过90天", "承诺60天交付", OK),
        ("交付周期不超过90天", "120天", FAIL),
        ("投标保证金3万元", "缴纳30000元", OK),
        ("屏幕尺寸≥15英寸", "12.1英寸", FAIL),
        ("屏幕尺寸≥15英寸", "15.6英寸", OK),
        ("提供不少于3个类似业绩", "提供3个", OK),
        ("提供不少于3个类似业绩", "提供2个", FAIL),
        ("注册资本不低于伍佰万元", "注册资本300万元", FAIL),
        ("近三个月内任意一个月", "两个月", FAIL),
        ("注册资本不低于500万元", "约300万元", REVIEW),
        ("注册资本不低于500万元", "详见营业执照", REVIEW),
        ("交付周期不超过90天", "三个月", REVIEW),  # 月与天维度不同，保守交人工
        ("质保期不少于2年", "24个月", REVIEW),   # 年/月维度不同，交人工
    ]
    width = 0
    for requirement, evidence, _ in cases:
        width = max(width, len(requirement))
    passed = 0
    for requirement, evidence, expected in cases:
        actual = compare(requirement, evidence)
        flag = "PASS" if actual == expected else "FAIL"
        if actual == expected:
            passed += 1
        print(f"[{flag}] [{actual}] {requirement}  <-  {evidence}")
    print(f"\n{passed}/{len(cases)} 通过")
