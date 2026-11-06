#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多份招标文件的批量任务：一次提交，逐份建运行，后端排队。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from workbench import server  # noqa: E402


@pytest.fixture
def fake_start(monkeypatch):
    calls: list[dict] = []

    def _start(payload: dict) -> dict:
        calls.append(payload)
        return {"id": f"job-{len(calls)}", "arm": payload["arm"]}

    monkeypatch.setattr(server, "start_job", _start)
    return calls


def _pdf(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4\n% test\n")
    return str(path)


def test_每份文件一个运行(tmp_path, fake_start):
    files = [_pdf(tmp_path, "甲.pdf"), _pdf(tmp_path, "乙.pdf")]
    out = server.api_jobs_batch({"files": files, "mode": "stub"})
    assert out["count"] == 2
    assert len(fake_start) == 2
    assert fake_start[0]["file"] == files[0]


def test_同一批次运行名不重复(tmp_path, fake_start):
    files = [_pdf(tmp_path, f"{i}.pdf") for i in range(3)]
    server.api_jobs_batch({"files": files, "mode": "stub"})
    arms = [call["arm"] for call in fake_start]
    assert len(set(arms)) == 3


def test_忽略不存在的文件(tmp_path, fake_start):
    real = _pdf(tmp_path, "真.pdf")
    out = server.api_jobs_batch({"files": [real, str(tmp_path / "不存在.pdf")], "mode": "stub"})
    assert out["count"] == 1


def test_没有可用文件时报错(tmp_path, fake_start):
    with pytest.raises(HTTPException):
        server.api_jobs_batch({"files": [str(tmp_path / "无.pdf")]})


def test_参数会透传给每个任务(tmp_path, fake_start):
    files = [_pdf(tmp_path, "a.pdf")]
    server.api_jobs_batch({"files": files, "mode": "real", "inject": True, "audit": True})
    assert fake_start[0]["mode"] == "real"
    assert fake_start[0]["inject"] is True
    assert fake_start[0]["audit"] is True
