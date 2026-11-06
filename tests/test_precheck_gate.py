#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则预判健康度门禁的回归测试。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapter"))

from match_bid import precheck_gate  # noqa: E402


def test_正常命中不告警():
    gate = precheck_gate(total=100, numeric=5, time_count=2, entity_hit=3,
                         entity_missing=4, entity_candidates=10)
    assert gate["status"] == "ok"
    assert gate["warnings"] == []
    assert gate["short_circuit"] == 14
    assert gate["rate"] == 14.0


def test_样本够大却零命中要告警():
    gate = precheck_gate(total=120, numeric=0, time_count=0, entity_hit=0,
                         entity_missing=0, entity_candidates=0)
    assert gate["status"] == "warn"
    assert any("一次都没命中" in line for line in gate["warnings"])


def test_样本太小不因零命中告警():
    gate = precheck_gate(total=10, numeric=0, time_count=0, entity_hit=0,
                         entity_missing=0, entity_candidates=0)
    assert gate["status"] == "ok"


def test_短路率过高要告警():
    gate = precheck_gate(total=100, numeric=40, time_count=20, entity_hit=20,
                         entity_missing=10, entity_candidates=30, max_rate=0.75)
    assert gate["status"] == "warn"
    assert any("过松" in line for line in gate["warnings"])


def test_抽到实体却没判定要告警():
    gate = precheck_gate(total=60, numeric=1, time_count=0, entity_hit=0,
                         entity_missing=0, entity_candidates=8)
    assert gate["status"] == "warn"
    assert any("实体规则" in line for line in gate["warnings"])


def test_阈值可调():
    gate = precheck_gate(total=100, numeric=20, time_count=10, entity_hit=5,
                         entity_missing=0, entity_candidates=10, max_rate=0.2)
    assert gate["status"] == "warn"
    relaxed = precheck_gate(total=100, numeric=20, time_count=10, entity_hit=5,
                            entity_missing=0, entity_candidates=10, max_rate=0.9)
    assert relaxed["status"] == "ok"
