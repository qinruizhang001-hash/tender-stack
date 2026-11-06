#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则引擎「不可用」时的显式降级测试。

线上最危险的失败不是报错，是**静默退化**：脚本没加载，规则不拦，
所有结论悄悄变回纯模型判定，而报告上看不出任何异常。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vendor" / "rag-tender" / "backend"))

from app.models import DecisionStatus  # noqa: E402
from app.services import comparison, rule_engine  # noqa: E402


@pytest.fixture
def rules_missing(monkeypatch):
    """模拟脚本目录不可达。"""
    monkeypatch.setattr(rule_engine, "_LOADED", ())
    monkeypatch.setattr(rule_engine, "_RULES_DIR", Path())
    monkeypatch.setattr(rule_engine, "_WARNED", False)
    yield
    monkeypatch.setattr(rule_engine, "_LOADED", None)
    monkeypatch.setattr(rule_engine, "_RULES_DIR", None)


def test_正常环境规则可用():
    assert rule_engine.available() is True


def test_不可用时返回_available_false(rules_missing):
    result = rule_engine.evaluate_text("注册资本不低于500万元", "注册资本300万元")
    assert result["available"] is False
    assert result["status"] == rule_engine.PASS
    assert "未加载" in result["reason"]


def test_不可用时不误判为废标(rules_missing):
    # 宁可放过，也不能在规则失效时凭残缺口径判人废标
    result = rule_engine.evaluate_text("注册资本不低于500万元", "注册资本300万元")
    assert result["status"] != rule_engine.FAIL


def test_不可用时结论标注只来自模型(rules_missing):
    rule = rule_engine.evaluate_text("任意要求", "任意证据")
    status, summary = comparison.apply_rule_gate(DecisionStatus.MET, "模型认为满足", rule)
    assert status == DecisionStatus.MET
    assert "规则未加载" in summary


def test_可用时结论不加标注():
    rule = rule_engine.evaluate_text("注册资本不低于500万元", "注册资本800万元")
    status, summary = comparison.apply_rule_gate(DecisionStatus.MET, "模型认为满足", rule)
    assert "规则未加载" not in summary


def test_灰区仍然降级():
    rule = {"status": rule_engine.GREY, "reason": "单位跨族", "checks": [], "available": True}
    status, summary = comparison.apply_rule_gate(DecisionStatus.MET, "模型认为满足", rule)
    assert status == DecisionStatus.NEEDS_REVIEW
    assert summary.startswith("【灰区】")


def test_灰区不推翻模型的不满足():
    rule = {"status": rule_engine.GREY, "reason": "单位跨族", "checks": [], "available": True}
    status, _ = comparison.apply_rule_gate(DecisionStatus.NOT_MET, "模型认为不满足", rule)
    assert status == DecisionStatus.NOT_MET
