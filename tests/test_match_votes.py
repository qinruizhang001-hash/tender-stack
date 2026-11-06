#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双跑投票与数值预判的回归测试。

运行：
    .venv/Scripts/python.exe -m pytest tests -q
    .venv/Scripts/python.exe tests/test_match_votes.py      # 无 pytest 也能跑
"""
from __future__ import annotations

import sys
from pathlib import Path

ADAPTER = Path(__file__).resolve().parent.parent / "adapter"
sys.path.insert(0, str(ADAPTER))

import match_bid  # noqa: E402

ROW = {"req_id": "R-001", "requirement": "投标人须具有良好的商业信誉"}
CANDS = [{"page": 3, "text": "我司具有良好的商业信誉，无重大违法记录。"}]


def _verdict(status: str, *, rule_fired: bool = False, evidence: str = "证据") -> dict:
    return {
        "coverage": status,
        "bid_page": 3,
        "evidence": evidence,
        "reason": "",
        "basis": "规则" if rule_fired else "",
        "model_status": status,
        "rule_fired": rule_fired,
        "sig": "x",
    }


def _patch(monkeypatch, results: list[dict]) -> list[int]:
    """按顺序返回预设结果，并记录调用次数。"""
    calls: list[int] = []
    queue = list(results)

    def fake(key, row, candidates, attempt=0):  # noqa: ANN001
        calls.append(1)
        return dict(queue.pop(0))

    monkeypatch.setattr(match_bid, "judge_once", fake)
    return calls


# ------------------------------------------------------------------ 投票

def test_两次一致则采信(monkeypatch):
    calls = _patch(monkeypatch, [_verdict("已响应"), _verdict("已响应", evidence="更好的证据")])
    out = match_bid.judge("k", ROW, CANDS, votes=2)
    assert out["coverage"] == "已响应"
    assert out["votes"] == "已响应×2"
    assert out["vote_agree"] == "Y"
    assert out["evidence"] == "更好的证据"      # 取更长的证据
    assert len(calls) == 2


def test_两次不一致降级人工(monkeypatch):
    _patch(monkeypatch, [_verdict("已响应"), _verdict("未响应")])
    out = match_bid.judge("k", ROW, CANDS, votes=2)
    assert out["coverage"] == "人工介入"
    assert out["votes"] == "已响应|未响应"
    assert out["vote_agree"] == "N"
    assert "不一致" in out["reason"]


def test_规则已改判不再投第二票(monkeypatch):
    calls = _patch(monkeypatch, [_verdict("负偏离", rule_fired=True)])
    out = match_bid.judge("k", ROW, CANDS, votes=2)
    assert out["coverage"] == "负偏离"
    assert out["vote_agree"] == ""
    assert len(calls) == 1


def test_第二次失败不推翻第一次(monkeypatch):
    _patch(monkeypatch, [_verdict("部分响应"), _verdict("待复核")])
    out = match_bid.judge("k", ROW, CANDS, votes=2)
    assert out["coverage"] == "部分响应"
    assert out["vote_agree"] == ""


def test_单跑只调一次(monkeypatch):
    calls = _patch(monkeypatch, [_verdict("已响应")])
    out = match_bid.judge("k", ROW, CANDS, votes=1)
    assert out["coverage"] == "已响应"
    assert len(calls) == 1


# ------------------------------------------------------------------ 数值预判

def _pre(req: str, text: str):
    return match_bid.numeric_precheck(req, [{"page": 1, "text": text}])


def test_数值预判_命中负偏离():
    out = _pre("注册资本不低于人民币500万元", "我司注册资本为人民币300万元。")
    assert out and out["coverage"] == "负偏离"
    assert "500万元" in out["basis"]


def test_数值预判_命中已响应():
    out = _pre("屏幕尺寸≥15英寸", "屏幕尺寸为15.6英寸。")
    assert out and out["coverage"] == "已响应"


def test_数值预判_分母不同不判定():
    assert _pre("因报价低于最高限价45%触发审查", "投标报价低于平均值50%的，应启动审查。") is None


def test_数值预判_量词不当数值():
    assert _pre("以一个供应商的身份共同参加", "两个以上自然人组成一个联合体。") is None


def test_数值预判_不同主体不判定():
    assert _pre("注册资本不低于500万元", "项目预算500万元。") is None


def test_数值预判_维度不同不判定():
    assert _pre("质保期不少于2年", "质保期为24个月。") is None


if __name__ == "__main__":
    import inspect

    failures = 0

    class _MP:
        def setattr(self, obj, attr, value):  # noqa: N802
            setattr(obj, attr, value)

    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            if inspect.signature(func).parameters:
                func(_MP())
            else:
                func()
            print(f"[PASS] {name}")
        except AssertionError as error:
            failures += 1
            print(f"[FAIL] {name}: {error}")
    print(f"\n{'全部通过' if not failures else f'{failures} 个失败'}")
    sys.exit(1 if failures else 0)
