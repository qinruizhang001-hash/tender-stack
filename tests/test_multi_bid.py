#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多标比对的目录与标识逻辑。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from workbench import server  # noqa: E402


def test_同一文件名得到同一标识():
    assert server.bid_slug("投标人甲.pdf") == server.bid_slug("投标人甲.pdf")


def test_不同文件名不会撞车():
    assert server.bid_slug("甲.pdf") != server.bid_slug("乙.pdf")


def test_标识保留中文且去掉特殊字符():
    slug = server.bid_slug("投标人 甲-最终版.pdf")
    assert "投标人" in slug
    assert " " not in slug


def test_超长文件名会被截断():
    slug = server.bid_slug("甲" * 120 + ".pdf")
    assert len(slug) <= 60


def _run(tmp_path, layout: dict[str, list[str]]) -> Path:
    base = tmp_path / "arm" / "match"
    for rel, files in layout.items():
        folder = base if rel == "." else base / rel
        folder.mkdir(parents=True, exist_ok=True)
        for name in files:
            (folder / name).write_text("req_id\n", encoding="utf-8")
    return base


def test_兼容旧的单份比对目录(tmp_path, monkeypatch):
    _run(tmp_path, {".": ["coverage.csv", "summary.json"]})
    monkeypatch.setattr(server, "OUT", tmp_path)
    dirs = server.match_dirs("arm")
    assert [item["slug"] for item in dirs] == ["default"]


def test_识别多份标书目录(tmp_path, monkeypatch):
    _run(tmp_path, {"jia-001": ["coverage.csv"], "yi-002": ["coverage.csv", "summary.json"]})
    monkeypatch.setattr(server, "OUT", tmp_path)
    dirs = server.match_dirs("arm")
    assert [item["slug"] for item in dirs] == ["jia-001", "yi-002"]


def test_旧目录与新目录可以共存(tmp_path, monkeypatch):
    _run(tmp_path, {".": ["coverage.csv"], "jia-001": ["coverage.csv"]})
    monkeypatch.setattr(server, "OUT", tmp_path)
    slugs = [item["slug"] for item in server.match_dirs("arm")]
    assert slugs == ["default", "jia-001"]


def test_忽略空目录(tmp_path, monkeypatch):
    (tmp_path / "arm" / "match" / "empty").mkdir(parents=True)
    monkeypatch.setattr(server, "OUT", tmp_path)
    assert server.match_dirs("arm") == []


def test_没有比对目录返回空(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUT", tmp_path)
    assert server.match_dirs("nothing") == []
