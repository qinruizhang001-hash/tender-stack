#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""漏项检查的回归测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapter"))

from missing_item_check import (  # noqa: E402
    FOUND,
    MISSING,
    REVIEW,
    MissingItemChecker,
    check_items,
    normalize,
    split_pages,
)

PAGES = [
    "投标文件封面\n投标人：某某科技有限公司",
    "资格证明文件\n营业执照（副本）复印件\nISO 13485 认证证书",
    "安全生产许可\n证 复印件",
]


def _one(entity: str):
    return MissingItemChecker(PAGES).check(entity)


def test_精确命中并给页码():
    result = _one("营业执照")
    assert result.status == FOUND
    assert result.page == 2
    assert "第2页" in result.message


def test_跨行命中():
    result = _one("安全生产许可证")      # 原文是「安全生产许可\n证」
    assert result.status == FOUND
    assert result.page == 3


def test_跨空格与连字符命中():
    assert _one("ISO13485").status == FOUND
    assert _one("ISO-13485").status == FOUND


def test_全角字符命中():
    assert _one("ＩＳＯ１３４８５").status == FOUND


def test_模糊命中错字():
    result = _one("安全生产许可怔")      # 错一个字，模拟 OCR
    assert result.status == FOUND
    assert result.method == "fuzzy"


def test_完全找不到判漏项():
    result = _one("税务登记证")
    assert result.status == MISSING
    assert result.message == "废标-漏项"


def test_短实体只做精确匹配():
    assert _one("证").status == FOUND
    assert _one("章").status == MISSING


def test_空实体交人工():
    assert _one("").status == REVIEW
    assert _one("   ").status == REVIEW


def test_多处命中给出多页():
    pages = ["营业执照", "无关内容", "营业执照复印件"]
    result = MissingItemChecker(pages).check("营业执照")
    assert result.pages == [1, 3]


def test_分页符拆分():
    text = "第一页内容\f第二页内容"
    assert len(split_pages(text)) == 2


def test_页码标记拆分():
    text = "第一页内容\n=== 第2页 ===\n第二页内容"
    pages = split_pages(text)
    assert len(pages) == 2
    assert "第二页" in pages[1]


def test_批量检查保持顺序():
    results = check_items(["营业执照", "税务登记证", "ISO13485"], PAGES)
    assert [item.entity for item in results] == ["营业执照", "税务登记证", "ISO13485"]
    assert [item.status for item in results] == [FOUND, MISSING, FOUND]


def test_规范化忽略大小写与标点():
    assert normalize("ISO-13485：2016") == "iso134852016"
    assert normalize("营 业\n执 照") == "营业执照"


@pytest.mark.parametrize("entity", ["营业执照", "安全生产许可证", "ISO13485"])
def test_命中结果带原文片段(entity):
    result = _one(entity)
    assert result.snippet
    assert normalize(entity) in normalize(result.snippet)
