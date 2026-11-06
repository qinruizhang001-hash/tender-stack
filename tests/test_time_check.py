#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""时间/日期比对的回归测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapter"))

import time_check as t  # noqa: E402

CASES = [
    # 时长：同族比较
    ("连续近12个月社保", "近三个月社保", t.SHORT),
    ("连续近12个月社保", "近12个月社保", t.OK),
    ("连续近十二个月社保", "近三个月社保", t.SHORT),
    ("不少于2年", "24个月", t.OK),
    ("不少于2年", "18个月", t.SHORT),
    ("不超过90天", "120天", t.SHORT),
    ("不超过90天", "60天", t.OK),
    # 日期：有效期是下限
    ("有效期至2026年11月", "有效期至2026年10月", t.EXPIRED),
    ("有效期至2026年11月", "有效期至2027年3月", t.OK),
    ("有效期至2026-11-05", "有效期至2026/11/05", t.OK),
    # 日期：截止是上限
    ("投标截止时间2026年11月5日", "2026年11月6日提交", t.EXPIRED),
    ("投标截止时间2026年11月5日", "2026年11月4日提交", t.OK),
    ("截止至2026年11月5日", "2026年11月6日提交", t.EXPIRED),
    # 区间取终点
    ("服务期自2026年1月1日至2026年12月31日", "服务期至2026年11月30日", t.EXPIRED),
    ("服务期自2026年1月1日至2026年12月31日", "服务期至2027年3月1日", t.OK),
    # 交人工
    ("连续近12个月", "详见社保缴纳证明", t.REVIEW),
    ("近90天", "3个月", t.REVIEW),
    ("2026年11月", "2026年11月", t.REVIEW),
]


@pytest.mark.parametrize("requirement,evidence,expected", CASES)
def test_compare_time(requirement, evidence, expected):
    assert t.compare_time(requirement, evidence) == expected


def test_区间取终点():
    value = t.parse_date("服务期自2026年1月1日至2026年12月31日")
    assert (value.year, value.month, value.day) == (2026, 12, 31)


def test_日期里的月不算时长():
    assert t.parse_duration("2026年11月5日") is None


def test_非法日期不解析():
    assert t.parse_date("2026年2月30日") is None


def test_两周等于十四天():
    assert t.parse_duration("两周").base == t.parse_duration("14天").base
