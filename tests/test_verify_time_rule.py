#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则层接入时间判定后的回归测试。

核心约束：模型说「已响应」，但时间明显不满足时，规则必须改判；
反过来，规则判不了时不许乱降级。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapter"))

from verify_rules import apply_rules, check_time  # noqa: E402


def test_时长不足时强制改判():
    status, hits = apply_rules("连续近12个月社保", "近三个月社保", "已响应")
    assert status == "负偏离"
    assert any(hit.rule == "时间" and hit.severity == "violate" for hit in hits)


def test_有效期早于要求时强制改判():
    status, hits = apply_rules("有效期至2026年11月", "有效期至2026年10月", "已响应")
    assert status == "负偏离"
    assert "逾期" in hits[0].detail


def test_截止日期晚于要求时强制改判():
    status, _ = apply_rules("投标截止时间2026年11月5日", "2026年11月6日提交", "已响应")
    assert status == "负偏离"


def test_时间满足时不改判():
    status, hits = apply_rules("连续近12个月社保", "近12个月社保", "已响应")
    assert status == "已响应"
    assert hits == []


def test_规则不把未响应升级为已响应():
    status, _ = apply_rules("连续近12个月社保", "近12个月社保", "未响应")
    assert status == "未响应"


def test_月与天跨族时不下结论():
    assert check_time("近90天", "3个月") == []


def test_十二个月等于一年():
    # 时长归一到「月」族：12 个月 == 1 年，不该判负偏离
    status, _ = apply_rules("服务期不少于12个月", "服务期为1年", "已响应")
    assert status == "已响应"


def test_数值规则不再抢时长():
    # 同一条时长要求，只能由「时间」规则命中，不能同时冒出「数值」命中
    _, hits = apply_rules("连续近12个月社保", "近三个月社保", "已响应")
    assert {hit.rule for hit in hits} == {"时间"}
