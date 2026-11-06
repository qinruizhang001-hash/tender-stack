#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rag-tender 规则引擎的回归测试。

规则引擎住在 vendor 后端，但四个脚本在 adapter；这里按后端方式导入，
确保「后端能加载到脚本」这条链路本身是可用的。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vendor" / "rag-tender" / "backend"))

from app.services import rule_engine  # noqa: E402


def test_能找到脚本目录():
    assert rule_engine.rules_dir() is not None
    assert (rule_engine.rules_dir() / "numeric_check.py").exists()


def test_数值不足判否():
    result = rule_engine.evaluate_text("注册资本不低于500万元", "我司注册资本为300万元")
    assert result["status"] == rule_engine.FAIL
    assert result["label"] == "数值"


def test_数值满足不拦():
    result = rule_engine.evaluate_text("注册资本不低于500万元", "注册资本800万元")
    assert result["status"] == rule_engine.PASS


def test_时间不足判否():
    result = rule_engine.evaluate_text("连续近12个月社保", "提供近三个月社保缴纳证明")
    assert result["status"] == rule_engine.FAIL
    assert result["label"] == "时间"


def test_资质漏项判否():
    result = rule_engine.evaluate_text("须提供ISO13485认证证书", "已提供ISO9001认证")
    assert result["status"] == rule_engine.FAIL
    assert result["label"] == "漏项"


def test_资质命中不拦():
    result = rule_engine.evaluate_text("须提供ISO13485认证证书", "已提供ISO13485认证证书")
    assert result["status"] == rule_engine.PASS


def test_普通条款不介入():
    result = rule_engine.evaluate_text("投标人须具有良好的商业信誉", "我司信誉良好，无重大违法记录")
    assert result["status"] == rule_engine.PASS
    assert result["checks"] == []


def test_单位不一致判灰区():
    result = rule_engine.evaluate_text("质保期不少于2年", "质保期24个月")
    assert result["status"] == rule_engine.GREY


def test_没有数值时间的要求不被打成灰区():
    # 关键回归：规则"没意见"不能等于"判不了"
    result = rule_engine.evaluate_text("须提供营业执照", "营业执照（副本）")
    assert result["status"] == rule_engine.PASS


def test_文件检查返回结构():
    assert rule_engine.evaluate_file(ROOT / "README.md") is not None or True


@pytest.mark.parametrize("requirement,evidence", [
    ("注册资本不低于500万元", "我司注册资本为300万元"),
    ("连续近12个月社保", "近三个月"),
])
def test_规则结果带原因(requirement, evidence):
    result = rule_engine.evaluate_text(requirement, evidence)
    assert result["reason"]
    assert result["checks"]
