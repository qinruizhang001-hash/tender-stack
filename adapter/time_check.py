#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""time_check.py — 标书审查的【时间/日期比较】模块

两类时间，分开判：

  1. 时长（duration）：12个月 / 90天 / 2年 / 近三个月
     · 同一族内比较（年/月 为一族，天/周/小时 为一族）
     · 跨族（如「90天」对「3个月」）不硬换算，交人工——1 个月按 30 天还是 31 天，
       在合同里是会产生争议的，规则不该替人做这个决定。

  2. 日期（date）：2026年11月5日 / 2026-11-05 / 2026.11.5 / 2026年11月（月精度）
     · 方向由措辞决定：「有效期至 / 质保期至」是下限（证据不能更早），
       「截止 / 之前 / 不得晚于」是上限（证据不能更晚）；没有方向词就交人工。

返回
----
"已响应" / "废标-时间不满足" / "废标-已逾期" / "需人工复核"
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

OK = "已响应"
SHORT = "废标-时间不满足"
EXPIRED = "废标-已逾期"
REVIEW = "需人工复核"

_FULLWIDTH = str.maketrans("０１２３４５６７８９．，：", "0123456789.,:")

_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}


def normalize(text: str) -> str:
    return (text or "").translate(_FULLWIDTH).strip()


def cn_to_number(text: str) -> Optional[Decimal]:
    """十二 → 12；二十四 → 24；三 → 3。"""
    if not text:
        return None
    total = section = number = Decimal(0)
    for char in text:
        if char in _CN_DIGIT:
            number = Decimal(_CN_DIGIT[char])
        elif char in _CN_UNIT:
            section += (number or Decimal(1)) * _CN_UNIT[char]
            number = Decimal(0)
        else:
            return None
    return total + section + number


# ---------------------------------------------------------------- 日期

# 2026年11月5日 / 2026-11-05 / 2026/11/5 / 2026.11.5 / 2026年11月
_DATE_RE = re.compile(
    r"(?P<y>\d{4})\s*(?:年|[-/.])\s*(?P<m>\d{1,2})\s*"
    r"(?:(?:月|[-/.])\s*(?P<d>\d{1,2})\s*日?)?"
)

# 有效期至 / 质保期至：证据日期不能早于要求（下限）
_MIN_MARKERS = re.compile(r"有效期|有效至|有效到|质保期|保修期|服务期|使用期")
# 截止 / 之前 / 不得晚于：证据日期不能晚于要求（上限）
_MAX_MARKERS = re.compile(r"截止|之前|以前|不得晚于|不迟于|最晚|前提交|前送达")


@dataclass
class DateValue:
    year: int
    month: int
    day: Optional[int]          # None 表示只有年月（月精度）
    raw: str

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.year, self.month, self.day or 0)

    @property
    def precision(self) -> str:
        return "day" if self.day else "month"


def parse_date(text: str) -> Optional[DateValue]:
    matches = list(_DATE_RE.finditer(text))
    if not matches:
        return None
    # 「自 2026年1月1日 至 2026年12月31日」这类区间，取区间终点作边界
    chosen = matches[0]
    for index in range(len(matches) - 1):
        gap = text[matches[index].end(): matches[index + 1].start()]
        if re.fullmatch(r"\s*(至|到|—|~|-)\s*", gap):
            chosen = matches[index + 1]

    year, month = int(chosen.group("y")), int(chosen.group("m"))
    day_raw = chosen.group("d")
    day = int(day_raw) if day_raw else None
    if not (1 <= month <= 12):
        return None
    if day is not None:
        if not (1 <= day <= 31):
            return None
        try:
            date(year, month, day)          # 拦掉 2 月 30 日这类非法日期
        except ValueError:
            return None
    return DateValue(year, month, day, chosen.group(0).strip())


# ---------------------------------------------------------------- 时长

# 长单位在前：「个月」不能被「月」抢走
_DURATION_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?|[零一二两三四五六七八九十百]+)\s*"
    r"(?P<unit>个月|小时|分钟|星期|周|年|月|天|日)"
)

# 单位 → (族, 换算到该族基准的系数)：月族以「月」为基准，天族以「天」为基准
_DURATION_RULES = {
    "年": ("month", Decimal(12)),
    "个月": ("month", Decimal(1)),
    "月": ("month", Decimal(1)),
    "周": ("day", Decimal(7)),
    "星期": ("day", Decimal(7)),
    "天": ("day", Decimal(1)),
    "日": ("day", Decimal(1)),
    "小时": ("day", Decimal(1) / Decimal(24)),
    "分钟": ("day", Decimal(1) / Decimal(1440)),
}


@dataclass
class DurationValue:
    amount: Decimal
    unit: str
    family: str
    base: Decimal               # 归一到族基准后的数值
    raw: str


def parse_duration(text: str) -> Optional[DurationValue]:
    masked = _DATE_RE.sub(lambda m: " " * len(m.group(0)), text)   # 日期里的「月」不算时长
    for match in _DURATION_RE.finditer(masked):
        token = match.group("num")
        amount = Decimal(token) if token[0].isdigit() else cn_to_number(token)
        unit = match.group("unit")
        if amount is None or unit not in _DURATION_RULES:
            continue
        family, factor = _DURATION_RULES[unit]
        return DurationValue(amount=amount, unit=unit, family=family,
                             base=amount * factor, raw=match.group(0).strip())
    return None


# ---------------------------------------------------------------- 判定

def _date_direction(text: str) -> Optional[str]:
    """返回 'min'（证据不能更早）/ 'max'（证据不能更晚）/ None（判不了）。"""
    low, high = bool(_MIN_MARKERS.search(text)), bool(_MAX_MARKERS.search(text))
    if low and not high:
        return "min"
    if high and not low:
        return "max"
    return None


def _duration_direction(text: str) -> str:
    if re.search(r"不超过|以内|最多|以下", text):
        return "max"
    return "min"        # 不少于 / 至少 / 连续 / 近 / 满 都按「不能更短」处理


def compare_time(requirement_text: str, evidence_text: str) -> str:
    return explain(requirement_text, evidence_text)["result"]


def explain(requirement_text: str, evidence_text: str) -> dict:
    requirement, evidence = normalize(requirement_text), normalize(evidence_text)
    result = {"result": REVIEW, "kind": "", "reason": "",
              "requirement": "", "evidence": ""}

    req_date, ev_date = parse_date(requirement), parse_date(evidence)
    req_dur, ev_dur = parse_duration(requirement), parse_duration(evidence)

    # 优先比日期：两边都有日期时才比
    if req_date and ev_date:
        result["kind"] = "date"
        result["requirement"], result["evidence"] = req_date.raw, ev_date.raw
        direction = _date_direction(requirement)
        if direction is None:
            result["reason"] = "要求里没有「有效期至 / 截止」这类方向词，无法判断谁不能早于谁"
            return result
        # 月精度：只比到年月，避免把「2026年11月」当成 11 月 1 日
        if req_date.precision == "month" or ev_date.precision == "month":
            req_key, ev_key = (req_date.year, req_date.month), (ev_date.year, ev_date.month)
        else:
            req_key, ev_key = req_date.key, ev_date.key
        if direction == "min":
            ok = ev_key >= req_key
            result["reason"] = f"有效期至 {req_date.raw}，证据 {ev_date.raw}"
            result["result"] = OK if ok else EXPIRED
            result["reason"] += " → 满足" if ok else " → 早于要求，已逾期"
        else:
            ok = ev_key <= req_key
            result["reason"] = f"截止 {req_date.raw}，证据 {ev_date.raw}"
            result["result"] = OK if ok else EXPIRED
            result["reason"] += " → 满足" if ok else " → 晚于截止，已逾期"
        return result

    # 再比时长：两边都有时长时才比
    if req_dur and ev_dur:
        result["kind"] = "duration"
        result["requirement"], result["evidence"] = req_dur.raw, ev_dur.raw
        if req_dur.family != ev_dur.family:
            result["reason"] = (
                f"时长单位不同族（{req_dur.raw} vs {ev_dur.raw}），"
                "月与天之间换算有争议，交人工"
            )
            return result
        direction = _duration_direction(requirement)
        ok = ev_dur.base >= req_dur.base if direction == "min" else ev_dur.base <= req_dur.base
        result["result"] = OK if ok else SHORT
        result["reason"] = (
            f"要求 {req_dur.raw}（{'至少' if direction == 'min' else '至多'}），"
            f"证据 {ev_dur.raw} → {'满足' if ok else '不满足'}"
        )
        return result

    # 一边日期一边时长，或某一边压根没有时间
    if (req_date and ev_dur) or (req_dur and ev_date):
        result["reason"] = "要求与证据的时间类型不一致（一个日期、一个时长）"
        return result
    if not req_date and not req_dur:
        result["reason"] = "要求文本里没有可识别的时间"
        return result
    if not ev_date and not ev_dur:
        result["reason"] = "证据文本里没有可识别的时间"
        return result
    result["reason"] = "两边存在多种时间表述，规则无法确定比较口径"
    return result


# ---------------------------------------------------------------- 自测

if __name__ == "__main__":
    cases = [
        ("连续近12个月社保", "近三个月社保", SHORT),
        ("连续近12个月社保", "近12个月社保", OK),
        ("连续近十二个月社保", "近三个月社保", SHORT),
        ("不少于2年", "24个月", OK),
        ("不少于2年", "18个月", SHORT),
        ("不超过90天", "120天", SHORT),
        ("不超过90天", "60天", OK),
        ("有效期至2026年11月", "有效期至2026年10月", EXPIRED),
        ("有效期至2026年11月", "有效期至2027年3月", OK),
        ("有效期至2026-11-05", "有效期至2026/11/05", OK),
        ("投标截止时间2026年11月5日", "2026年11月6日提交", EXPIRED),
        ("投标截止时间2026年11月5日", "2026年11月4日提交", OK),
        ("连续近12个月", "详见社保缴纳证明", REVIEW),
        ("近90天", "3个月", REVIEW),
        ("2026年11月", "2026年11月", REVIEW),
        ("截止至2026年11月5日", "2026年11月6日提交", EXPIRED),
        ("服务期自2026年1月1日至2026年12月31日", "服务期至2026年11月30日", EXPIRED),
        ("服务期自2026年1月1日至2026年12月31日", "服务期至2027年3月1日", OK),
    ]
    passed = 0
    for requirement, evidence, expected in cases:
        detail = explain(requirement, evidence)
        actual = detail["result"]
        flag = "PASS" if actual == expected else "FAIL"
        if actual == expected:
            passed += 1
        print(f"[{flag}] [{actual}] {requirement}  <-  {evidence}")
        if actual != expected:
            print(f"        reason: {detail['reason']}")
    print(f"\n{passed}/{len(cases)} 通过")
