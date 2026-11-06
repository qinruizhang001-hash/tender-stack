#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资质实体预判的回归测试。

原则：**缺失可以确定性判定；命中不等于满足**——
命中要靠"要求就只是要这几份材料"才敢短路，带盖章/原件等条件的必须交模型。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapter"))

from match_bid import entity_precheck, extract_entities  # noqa: E402
from missing_item_check import MissingItemChecker  # noqa: E402

PAGES = ["封面", "资格文件\n营业执照（副本）\nISO 13485 认证证书"]
CHECKER = MissingItemChecker(PAGES)


def test_抽取ISO号():
    assert extract_entities("须提供ISO13485认证证书") == ["ISO13485"]


def test_抽取裸写证照名():
    assert extract_entities("投标人应具有安全生产许可证") == ["安全生产许可证"]
    assert extract_entities("须提供营业执照") == ["营业执照"]


def test_书名号与裸写去重():
    assert extract_entities("须提供《医疗器械经营许可证》") == ["《医疗器械经营许可证》"]


def test_否定要求不抽实体():
    assert extract_entities("本项目不需要许可证") == []


def test_缺失直接判负偏离():
    result = entity_precheck("须提供安全生产许可证", CHECKER)
    assert result and result["coverage"] == "负偏离"
    assert "未出现" in result["basis"]


def test_命中且要求单一判已响应():
    result = entity_precheck("须提供营业执照", CHECKER)
    assert result and result["coverage"] == "已响应"
    assert result["bid_page"] == "2"


def test_命中但带盖章条件交模型():
    # 文本里有「营业执照」，不代表盖了章
    assert entity_precheck("须提供营业执照复印件并加盖公章", CHECKER) is None


def test_命中但还有别的条件交模型():
    assert entity_precheck("须提供营业执照及近三年业绩", CHECKER) is None


def test_否定语境不算命中():
    pages = ["资格文件", "我方未提供ISO13485认证证书"]
    checker = MissingItemChecker(pages)
    assert entity_precheck("须提供ISO13485认证证书", checker) is None


def test_没有实体交模型():
    assert entity_precheck("投标人须具有良好的商业信誉", CHECKER) is None


def test_checker为空时交模型():
    assert entity_precheck("须提供营业执照", None) is None
