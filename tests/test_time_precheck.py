#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""时间预判的回归测试：确定能判负偏离才短路，判不了就交模型。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapter"))

from match_bid import time_precheck  # noqa: E402


def _run(requirement: str, text: str, page: int = 3):
    return time_precheck(requirement, [{"page": page, "text": text}])


def test_时长不足直接判负偏离():
    out = _run("连续近12个月社保", "提供近三个月社保缴纳证明。")
    assert out and out["coverage"] == "负偏离"
    assert out["bid_page"] == 3
    assert "时间预判" in out["basis"]


def test_有效期早于要求直接判负偏离():
    out = _run("许可证有效期至2026年11月", "医疗器械经营许可证有效期至2026年10月。", page=15)
    assert out and out["coverage"] == "负偏离"
    assert out["bid_page"] == 15


def test_截止日期晚于要求直接判负偏离():
    out = _run("投标截止时间2026年11月5日", "我方于2026年11月6日提交投标文件。")
    assert out and out["coverage"] == "负偏离"


def test_时间满足不短路():
    assert _run("连续近12个月社保", "提供近12个月社保缴纳证明。") is None


def test_不同语义组不判定():
    # 「有效期至」不能拿「开标时间」来比
    assert _run("许可证有效期至2026年11月", "开标时间2026年11月5日。") is None


def test_要求里没有时间不判定():
    assert _run("投标人须具有良好的商业信誉", "提供近三个月社保缴纳证明。") is None


def test_证据里没有时间不判定():
    assert _run("连续近12个月社保", "详见资格证明材料。") is None


def test_跨族时长不判定():
    assert _run("连续近12个月社保", "提供近90天社保缴纳证明。") is None
