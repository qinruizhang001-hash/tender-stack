#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复杂表格：合并单元格、跨页续表、无框线表格。

切块默认走「按标题层级」的 layout 模式，表格以 Markdown 形式进 text，
同时保留 chunk["table"]（header/rows）供数值规则按列取值。
"""
from __future__ import annotations

import sys
from pathlib import Path

import re

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapter"))

from bid_chunker import chunk_pdf  # noqa: E402


def _grid(page, rows, x0=80, y0=100, width=140, height=30):
    """画一张带框线的表；rows 里 None 表示与左侧合并。"""
    columns = max(len(row) for row in rows)
    for index in range(len(rows) + 1):
        y = y0 + index * height
        page.draw_line((x0, y), (x0 + columns * width, y), color=(0, 0, 0), width=1)
    for index in range(columns + 1):
        x = x0 + index * width
        page.draw_line((x, y0), (x, y0 + len(rows) * height), color=(0, 0, 0), width=1)


def _table_chunks(chunks):
    return [chunk for chunk in chunks if chunk.get("table")]


def _table_count(text: str) -> int:
    """数 Markdown 分隔行（一行一个表），不能用 count("| --- |")——表头行也会命中。"""
    return sum(1 for line in text.splitlines() if re.fullmatch(r"\|( --- \|)+", line))


def test_合并单元格不产生空列(tmp_path):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    _grid(page, [["合并表头", None], ["序号", "项目"], ["1", "社保证明"], ["2", "纳税证明"]])
    page.insert_text((90, 120), "合并表头", fontname="china-s", fontsize=11)
    page.insert_text((90, 180), "序号", fontname="china-s", fontsize=11)
    page.insert_text((230, 180), "项目", fontname="china-s", fontsize=11)
    page.insert_text((90, 210), "1", fontname="china-s", fontsize=11)
    page.insert_text((230, 210), "社保证明", fontname="china-s", fontsize=11)
    page.insert_text((90, 240), "2", fontname="china-s", fontsize=11)
    page.insert_text((230, 240), "纳税证明", fontname="china-s", fontsize=11)
    path = tmp_path / "merged.pdf"
    doc.save(path)
    doc.close()

    chunks, _ = chunk_pdf(path)
    tables = _table_chunks(chunks)
    assert tables, "应当识别出表格"
    body = tables[0]["text"]
    assert "合并表头" in body
    assert "|  |  |" not in body, f"合并单元格留下了空列：{body}"
    assert "序号 | 项目" in body


def test_跨页表格合并成一块(tmp_path):
    doc = fitz.open()
    for page_index in range(2):
        page = doc.new_page(width=595, height=842)
        y = 100
        for column, cell in enumerate(("序号", "项目", "金额")):
            page.insert_text((80 + column * 140, y), cell, fontname="china-s", fontsize=11)
        y += 30
        for row in range(2):
            values = (str(page_index * 2 + row + 1), f"材料{page_index * 2 + row + 1}", f"{row + 3}万")
            for column, cell in enumerate(values):
                page.insert_text((80 + column * 140, y), cell, fontname="china-s", fontsize=11)
            y += 30
    path = tmp_path / "crosspage.pdf"
    doc.save(path)
    doc.close()

    chunks, _ = chunk_pdf(path)
    tables = _table_chunks(chunks)
    assert len(tables) == 1, f"跨页表格应合并，实际 {len(tables)} 块"
    assert tables[0].get("pages") == [1, 2]
    text = tables[0]["text"]
    assert text.count("序号 | 项目 | 金额") == 1, "表头只应出现一次"
    assert "材料4" in text


def test_无框线表格也能识别(tmp_path):
    doc = fitz.open()
    page = doc.new_page()
    rows = [("序号", "项目", "金额"), ("1", "社保缴纳证明", "3万元"), ("2", "纳税证明", "5万元")]
    y = 100
    for row in rows:
        for column, cell in enumerate(row):
            page.insert_text((80 + column * 140, y), cell, fontname="china-s", fontsize=12)
        y += 30
    path = tmp_path / "borderless.pdf"
    doc.save(path)
    doc.close()

    chunks, _ = chunk_pdf(path)
    tables = _table_chunks(chunks)
    assert tables, "无框线表格应被识别"
    assert "社保缴纳证明" in tables[0]["text"]


def test_数值规则按表格列取值(tmp_path):
    from match_bid import numeric_precheck

    doc = fitz.open()
    page = doc.new_page()
    rows = [("序号", "项目", "金额"), ("1", "社保缴纳证明", "3万元"), ("2", "纳税证明", "5万元")]
    y = 100
    for row in rows:
        for column, cell in enumerate(row):
            page.insert_text((80 + column * 140, y), cell, fontname="china-s", fontsize=12)
        y += 30
    path = tmp_path / "numeric.pdf"
    doc.save(path)
    doc.close()

    chunks, _ = chunk_pdf(path)
    table = _table_chunks(chunks)[0]
    verdict = numeric_precheck("投标报价金额不低于5万元", [table])
    assert verdict is not None, "表格列应及时给出数值结论"
    assert verdict["coverage"] == "负偏离"
    assert "3万元" in verdict["evidence"]


def test_表头单位推断到单元格():
    from match_bid import numeric_precheck

    chunk = {
        "page": 2,
        "text": "| 序号 | 项目 | 金额（万元） |",
        "kind": "table",
        "table": {"header": ["序号", "项目", "金额（万元）"], "rows": [["1", "设备", "3"]]},
    }
    verdict = numeric_precheck("投标报价金额不低于50000元", [chunk])
    assert verdict is not None
    assert verdict["coverage"] == "负偏离"
    assert "3万元" in verdict["evidence"]


def test_表头单位换算后满足要求():
    from match_bid import numeric_precheck

    chunk = {
        "page": 2,
        "text": "| 序号 | 项目 | 金额（万元） |",
        "kind": "table",
        "table": {"header": ["序号", "项目", "金额（万元）"], "rows": [["1", "设备", "3"]]},
    }
    verdict = numeric_precheck("投标报价金额不低于2万元", [chunk])
    assert verdict is not None
    assert verdict["coverage"] == "已响应"


def test_表头同义族归纳():
    from match_bid import header_family

    assert header_family("投标总价（万元）") == "金额"
    assert header_family("报价（元）") == "金额"
    assert header_family("合同金额") == "金额"
    assert header_family("交货期") == "日期"
    assert header_family("备注") == ""


def test_同义表头能对上要求():
    from match_bid import numeric_precheck

    chunk = {
        "page": 3,
        "text": "| 序号 | 项目 | 投标总价（万元） |",
        "kind": "table",
        "table": {"header": ["序号", "项目", "投标总价（万元）"], "rows": [["1", "设备", "3"]]},
    }
    verdict = numeric_precheck("投标报价不低于50000元", [chunk])
    assert verdict is not None
    assert verdict["coverage"] == "负偏离"


def test_不同语义不误比():
    from match_bid import numeric_precheck

    chunk = {
        "page": 3,
        "text": "| 序号 | 项目 | 投标总价（万元） |",
        "kind": "table",
        "table": {"header": ["序号", "项目", "投标总价（万元）"], "rows": [["1", "设备", "3"]]},
    }
    assert numeric_precheck("注册资本不低于50000元", [chunk]) is None


def test_rows_to_markdown_转义():
    from layout_parse import rows_to_markdown

    markdown = rows_to_markdown([["a|b", "c"], ["d", "e\nf"]])
    assert markdown.splitlines()[0] == "| a\\|b | c |"

def test_跨页续表_次页无表头(tmp_path):
    """次页直接接数据行（没有重复表头），应并入上一页的表。"""
    doc = fitz.open()
    first = doc.new_page(width=595, height=842)
    y = 100
    for column, cell in enumerate(("序号", "项目", "金额")):
        first.insert_text((80 + column * 140, y), cell, fontname="china-s", fontsize=11)
    for row, values in enumerate((("1", "材料A", "3万"), ("2", "材料B", "4万"))):
        for column, cell in enumerate(values):
            first.insert_text((80 + column * 140, 130 + row * 26), cell, fontname="china-s", fontsize=11)
    second = doc.new_page(width=595, height=842)
    for row, values in enumerate((("3", "材料C", "5万"), ("4", "材料D", "6万"))):
        for column, cell in enumerate(values):
            second.insert_text((80 + column * 140, 100 + row * 26), cell, fontname="china-s", fontsize=11)
    path = tmp_path / "crosspage_noheader.pdf"
    doc.save(path)
    doc.close()

    chunks, _ = chunk_pdf(path)
    tables = _table_chunks(chunks)
    assert len(tables) == 1, f"无表头续表应合并，实际 {len(tables)} 块"
    assert tables[0]["pages"] == [1, 2]
    assert "材料C" in tables[0]["text"]


def test_连续重复标点被压缩():
    from layout_parse import clean_cell

    assert clean_cell("滨湖公园、、、青春公园") == "滨湖公园、青春公园"
    assert clean_cell("完成服务。。") == "完成服务。"


def test_可疑表格标记():
    from layout_parse import _table_suspect

    assert _table_suspect([["", ""], ["a", "b"]]) is True       # 空表头
    assert _table_suspect([["项目", "金额"], ["社保、、", "3万"]]) is True
    assert _table_suspect([["项目", "金额"], ["社保", "3万"]]) is False

def _table_page(doc, rows, y0=100):
    page = doc.new_page(width=595, height=842)
    x0, width, height = 80, 140, 26
    lines = len(rows)
    for index in range(lines + 1):
        page.draw_line((x0, y0 + index * height), (x0 + width * len(rows[0]), y0 + index * height), width=1)
    for index in range(len(rows[0]) + 1):
        page.draw_line((x0 + index * width, y0), (x0 + index * width, y0 + lines * height), width=1)
    for r, values in enumerate(rows):
        for c, value in enumerate(values):
            page.insert_text((x0 + c * width + 6, y0 + r * height + 17), value, fontname="china-s", fontsize=11)
    return page


def test_列数相同的不同表不合并(tmp_path):
    """表头不同、首列编号不连续 → 必须断开，不能焊死。"""
    doc = fitz.open()
    _table_page(doc, [("序号", "项目", "金额"), ("1", "社保", "3万"), ("2", "纳税", "4万"), ("3", "工伤", "5万")])
    _table_page(doc, [("姓名", "岗位", "年限"), ("张三", "电工", "5年"), ("李四", "焊工", "3年"), ("王五", "钳工", "2年")])
    path = tmp_path / "two_tables.pdf"
    doc.save(path)
    doc.close()

    chunks, _ = chunk_pdf(path)
    body = "\n".join(chunk["text"] for chunk in chunks if chunk.get("table"))
    assert _table_count(body) == 2, f"不同表被误合并：{body}"
    assert "姓名" in body and "序号" in body
    assert any(chunk.get("quality") == "low" for chunk in chunks if chunk.get("table")), "断开的表应标 low"


def test_首列连续才可当续表(tmp_path):
    doc = fitz.open()
    _table_page(doc, [("序号", "项目", "金额"), ("1", "社保", "3万"), ("2", "纳税", "4万"), ("3", "工伤", "5万")])
    _table_page(doc, [("4", "生育", "6万"), ("5", "医疗", "7万"), ("6", "失业", "8万")], y0=100)
    path = tmp_path / "continued.pdf"
    doc.save(path)
    doc.close()

    chunks, _ = chunk_pdf(path)
    body = "\n".join(chunk["text"] for chunk in chunks if chunk.get("table"))
    assert _table_count(body) == 1, f"编号连续的无表头页应并入上一张表：{body}"
    assert "医疗" in body


def test_续表标记可合并(tmp_path):
    doc = fitz.open()
    _table_page(doc, [("序号", "项目", "金额"), ("1", "社保", "3万"), ("2", "纳税", "4万")])
    _table_page(doc, [("续表", "项目", "金额"), ("3", "工伤", "5万"), ("4", "医疗", "6万")])
    path = tmp_path / "marked.pdf"
    doc.save(path)
    doc.close()

    chunks, _ = chunk_pdf(path)
    body = "\n".join(chunk["text"] for chunk in chunks if chunk.get("table"))
    assert _table_count(body) == 1, f"带「续表」标记应合并：{body}"
    assert "医疗" in body

def test_隔短注释的续表可合并(tmp_path):
    """上表 | 短注释(<80字) | 下表：允许跨一个短文本合并。"""
    doc = fitz.open()
    _table_page(doc, [("序号", "项目", "金额"), ("1", "社保", "3万"), ("2", "纳税", "4万")])
    note = doc.new_page(width=595, height=842)
    note.insert_text((80, 100), "注：下表为本表续页。", fontname="china-s", fontsize=11)
    _table_page(doc, [("3", "工伤", "5万"), ("4", "生育", "6万")])
    path = tmp_path / "bridge.pdf"
    doc.save(path)
    doc.close()

    chunks, _ = chunk_pdf(path)
    body = "\n".join(chunk["text"] for chunk in chunks if chunk.get("table"))
    assert _table_count(body) == 1, f"短注释后的续表应合并：{body}"
    assert "生育" in body
