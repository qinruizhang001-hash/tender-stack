#!/usr/bin/env python3
# LAYER: load-bearing
# -*- coding: utf-8 -*-
"""layout_parse.py — 按标题层级切块，表格转 Markdown。

设计原则：
1. 标题必须「有编号」才进章节栈；字号只用来识别没有编号的强调行，不当章节；
2. 目录页整页跳过，封面/版本行不进 section；
3. 一个章节（含其子节）自成一块，只有超过上限才在段落边界续块；
4. 表格按单元格坐标重排文字再转 Markdown，跨页表按结构化表头合并。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

HEADING_RULES = [
    (1, re.compile(r"^第[一二三四五六七八九十百零〇]+[章篇]")),
    (1, re.compile(r"^第[一二三四五六七八九十百零〇]+部分")),
    (2, re.compile(r"^第[一二三四五六七八九十百零〇]+节")),
    (2, re.compile(r"^[一二三四五六七八九十]+[、．.]\s*")),
    (3, re.compile(r"^[（(][一二三四五六七八九十]+[）)]\s*")),
    (4, re.compile(r"^\d+\.\d+\.\d+[\.、\s]")),
    (3, re.compile(r"^\d+\.\d+[\.、\s]")),
    (2, re.compile(r"^(附件|附录)\s*[一二三四五六七八九十\d]")),
]

TOC_DOTS = re.compile(r"(\.{3,}|…{2,}|·{4,})")
TOC_TITLE = re.compile(r"^目\s*录$")
TOC_LINE = re.compile(r"[^\d\s]\s*(\d{1,3}|[一二三四五六七八九十]{1,3})\s*$")

BAD_HEADING_TAIL = "；;，,、"
SENTENCE_END = "。；;！？：:"

HEADER_ZONE = 0.05      # 页眉页脚各占 5%
EMPHASIS_LEVEL = 9      # 字号识别出的「强调行」，不进章节栈
REQ_NUMBER = re.compile(r"^\s*(?:\((\d+)\)|（(\d+)）|(\d+(?:\.\d+)*)[\.、)）])\s*")
# 同一行里用「；」分隔的多条要求：只在行首或分号后认编号，避免把「3.5 万」当条款
REQ_INLINE = re.compile(r"(?:^|[；;])\s*(?:\((\d+)\)|（(\d+)）|(\d+(?:\.\d+)*)[\.、)）])")


@dataclass
class Block:
    kind: str                      # text / table
    text: str
    page: int
    size: float = 0.0
    bold: bool = False
    level: int = 0                 # 0 = 正文
    numbered: bool = False         # 是否带章节编号（决定能否进栈）
    markdown: str = ""
    rows: list[list[str]] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    split_hint: bool = False       # 疑似被拆开、但证据不足不敢合并的表

    @property
    def is_heading(self) -> bool:
        return self.level > 0


# ------------------------------------------------------------------ 判定

def _pattern_level(value: str) -> int:
    for level, pattern in HEADING_RULES:
        if pattern.match(value):
            return level
    return 0


def _heading_level(text: str, size: float, body_size: float, bold: bool) -> int:
    """编号规则优先；字号启发式只给「强调行」，不当章节。"""
    value = text.strip()
    if not value or len(value) > 60:
        return 0
    if value.endswith(tuple(BAD_HEADING_TAIL)):
        return 0

    level = _pattern_level(value)
    if level:
        return level

    # 没有编号：只有在明显大于正文、且不像句子的情况下才当作强调行
    if size >= body_size * 1.12 and len(value) <= 30 and not value.endswith(tuple(SENTENCE_END + "。，")):
        return 9   # 9 = 强调行：只用于分段，不进章节栈
    return 0


def _body_size(blocks: list[Block]) -> float:
    sizes: dict[float, int] = {}
    for block in blocks:
        if block.kind == "text" and block.size:
            key = round(block.size, 1)
            sizes[key] = sizes.get(key, 0) + len(block.text)
    if not sizes:
        return 10.0
    return max(sizes.items(), key=lambda item: item[1])[0]


# ------------------------------------------------------------------ 页级抽取

def _line_text(line: dict) -> str:
    return "".join(span.get("text", "") for span in line.get("spans", [])).strip()


def _is_toc_page(texts: list[str]) -> bool:
    """目录判定：点线优先；无点线时用「短行 + 行尾页码」的版式特征。"""
    dotted = sum(1 for text in texts if TOC_DOTS.search(text))
    if dotted >= 3:
        return True
    has_title = any(TOC_TITLE.match(text.replace(" ", "")) for text in texts)
    if has_title and (dotted >= 1 or len(texts) >= 5):
        return True

    # 无点线目录：整页几乎都是短行，且四成以上以页码收尾
    if len(texts) < 5:
        return False
    short = [text for text in texts if len(text) <= 30]
    if len(short) / len(texts) < 0.8:
        return False
    numbered = sum(1 for text in short if TOC_LINE.search(text))
    return numbered / len(short) >= 0.4


def _cell_text(page, rect) -> str:
    """单元格内按字符坐标重排：PDF 的 span 顺序经常把「12.8.2」拆乱。"""
    try:
        raw = page.get_text("rawdict", clip=rect)
    except Exception:  # noqa: BLE001
        raw = {}
    chars = [
        (char["bbox"][1], char["bbox"][0], char["c"])
        for block in raw.get("blocks", [])
        for line in block.get("lines", [])
        for span in line.get("spans", [])
        for char in span.get("chars", [])
        if char.get("c", "").strip()
    ]
    if not chars:
        try:
            words = page.get_text("words", clip=rect)
        except Exception:  # noqa: BLE001
            return ""
        chars = [(word[1], word[0], word[4]) for word in words if str(word[4]).strip()]
    if not chars:
        return ""

    chars.sort(key=lambda item: (item[0], item[1]))
    lines: list[list[tuple[float, float, str]]] = []
    for y, x, value in chars:
        if lines and abs(lines[-1][-1][0] - y) < 5:
            lines[-1].append((y, x, value))
        else:
            lines.append([(y, x, value)])

    parts: list[str] = []
    for line in lines:
        line.sort(key=lambda item: item[1])
        parts.append("".join(item[2] for item in line))
    return "\n".join(parts)


def _table_rows(page, table) -> list[list[str]]:
    """优先按单元格 bbox 取文字；失败再退回 table.extract()。"""
    rows: list[list[str]] = []
    try:
        for row in table.rows:
            cells: list[str] = []
            previous = None
            for rect in row.cells:
                key = None if rect is None else (round(rect.x0, 1), round(rect.y0, 1), round(rect.x1, 1), round(rect.y1, 1))
                if key is not None and key == previous:
                    # 合并单元格：把锚点单元格的文字传播过去，避免整列空白
                    cells.append(cells[-1] if cells else "")
                    continue
                previous = key
                cells.append(_cell_text(page, rect) if rect is not None else "")
            if any(cell.strip() for cell in cells):
                rows.append(cells)
    except Exception:  # noqa: BLE001
        try:
            from bid_chunker import _normalize_rows

            rows = [[str(cell or "") for cell in row] for row in _normalize_rows(table.extract() or [])]
        except Exception:  # noqa: BLE001
            rows = []
    rows = [[clean_cell(cell) for cell in row] for row in rows]
    # 行续接：首列为空且上面还有行，说明是同一行的换行碎片，并回上一行
    merged_rows: list[list[str]] = []
    for row in rows:
        if merged_rows and (not row[0].strip()) and any(cell.strip() for cell in row[1:]):
            previous = merged_rows[-1]
            for index, cell in enumerate(row):
                if not cell.strip():
                    continue
                if index < len(previous):
                    previous[index] = f"{previous[index]}{cell}".strip() if previous[index] else cell
                else:
                    previous.append(cell)
            continue
        merged_rows.append(list(row))
    return merged_rows


def _page_blocks(page, page_no: int) -> tuple[list[Block], bool]:
    """返回 (块列表, 是否目录页)。"""
    from bid_chunker import _table_regions

    height = page.rect.height
    texts: list[str] = []
    for item in page.get_text("dict").get("blocks", []):
        if item.get("type") != 0:
            continue
        for line in item.get("lines") or []:
            text = _line_text(line)
            if text:
                texts.append(text)
    regions = _table_regions(page)
    # 整页被误判成一张表时，正文会被「表内文字过滤」全吃掉：按覆盖率剔除
    page_area = max(page.rect.width * page.rect.height, 1)
    kept_regions = []
    page_chars = sum(len(item) for item in texts)
    for table in regions:
        x0, y0, x1, y1 = table.bbox
        area_ratio = max(0.0, (x1 - x0) * (y1 - y0)) / page_area
        try:
            extracted = table.extract() or []
            table_chars = sum(len(str(cell or "")) for row in extracted for cell in row)
            columns = max((len(row) for row in extracted), default=0)
        except Exception:  # noqa: BLE001
            extracted, table_chars, columns = [], 0, 0
        # 退化表格（≤2 行 ≤2 列却覆盖大半页）是「页面边框」，不是数据表
        if len(extracted) <= 2 and area_ratio >= 0.4:
            continue
        if area_ratio >= 0.8 and page_chars and table_chars < page_chars * 0.6:
            continue  # 伪表格：保留正文
        kept_regions.append(table)
    regions = kept_regions
    dotted_lines = sum(1 for item in texts if TOC_DOTS.search(item))
    # 目录页常被误识别成「表格」（点线像框线）：点线足够多时仍按目录处理
    if _is_toc_page(texts) and (not regions or dotted_lines >= 3):
        return [], True

    blocks: list[tuple[float, Block]] = []
    boxes = [tuple(table.bbox) for table in regions]

    for item in page.get_text("dict").get("blocks", []):
        if item.get("type") != 0:
            continue
        for line in item.get("lines") or []:
            text = _line_text(line)
            if not text or TOC_DOTS.search(text):
                continue  # 点线引导的目录行不进正文
            box = line.get("bbox", item.get("bbox", (0, 0, 0, 0)))
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            if cy < height * HEADER_ZONE or cy > height * (1 - HEADER_ZONE):
                continue  # 页眉页脚
            if any(rect[0] <= cx <= rect[2] and rect[1] <= cy <= rect[3] for rect in boxes):
                continue
            spans = line.get("spans", [])
            sizes = [span.get("size", 0) for span in spans]
            bold = any(span.get("flags", 0) & 16 for span in spans)
            blocks.append((float(box[1]), Block(kind="text", text=text, page=page_no, size=max(sizes or [0]), bold=bold)))

    for table in regions:
        rows = _table_rows(page, table)
        rows = [row for row in rows if not any(TOC_DOTS.search(cell) for cell in row)]
        if len(rows) < 2:
            continue
        blocks.append(
            (
                float(table.bbox[1]),
                Block(
                    kind="table",
                    text="\n".join(" | ".join(cell for cell in row) for row in rows),
                    page=page_no,
                    markdown=rows_to_markdown(rows),
                    rows=rows,
                    pages=[page_no],
                ),
            )
        )
    blocks.sort(key=lambda item: item[0])
    return [block for _, block in blocks], False


# ------------------------------------------------------------------ 清洗

def clean_cell(value: str) -> str:
    """PDF 单元格里的换行会把标点甩到行首，这里按中文阅读习惯复原。"""
    cell = (value or "").replace("\r", "")
    cell = re.sub(r"\s*\n\s*", " ", cell)
    cell = re.sub(r"\s+([，。、；：？！）」】])", r"\1", cell)
    cell = re.sub(r"([（「【])\s+", r"\1", cell)
    cell = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", cell)
    cell = re.sub(r"(\d+)\s+(\d+)\s*\.(?!\d)", r"\1.\2", cell)
    cell = re.sub(r"(\d+)\s+(\d+)(?=\s*(?:万|亿|%|平方米))", r"\1.\2", cell)
    cell = re.sub(r"(\d)\s*\.\s*(?=\d)", r"\1.", cell)
    cell = re.sub(r"(\d)\s+(?=\d)", r"\1", cell)
    cell = re.sub(r"\s+", " ", cell)
    # 连续重复标点压成一个（PDF 排版常见「、、、」）
    cell = re.sub(r"、{2,}", "、", cell)
    cell = re.sub(r"。{2,}", "。", cell)
    cell = re.sub(r"，{2,}", "，", cell)
    return cell.strip()


def rows_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [list(row) + [""] * (width - len(row)) for row in rows]

    def cell(value: str) -> str:
        return clean_cell(value).replace("|", "\\|")

    header = "| " + " | ".join(cell(value) for value in rows[0]) + " |"
    divider = "| " + " | ".join("---" for _ in range(width)) + " |"
    body = ["| " + " | ".join(cell(value) for value in row) + " |" for row in rows[1:]]
    return "\n".join([header, divider, *body])


# ------------------------------------------------------------------ 跨页表

def _norm_cells(cells: list[str]) -> list[str]:
    return [unicodedata.normalize("NFKC", cell or "").replace(" ", "").strip() for cell in cells]


def _looks_like_header(row: list[str]) -> bool:
    values = [cell for cell in row if cell.strip()]
    if not values:
        return False
    if re.fullmatch(r"\d+(?:\.\d+)*", row[0].strip() or ""):
        return False   # 首列是编号 → 数据行，不是表头
    if any("。" in cell for cell in values):
        return False
    return all(len(cell) <= 12 for cell in values)


CONTINUATION_MARK = ("续", "续表", "接上表", "接前表", "上表")


def _first_number(row: list[str]) -> float | None:
    if not row:
        return None
    match = re.match(r"\s*(\d+(?:\.\d+)*)", row[0] or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _has_continuation_mark(rows: list[list[str]]) -> bool:
    if not rows:
        return False
    cells = [cell for cell in rows[0] if cell.strip()] or [cell for row in rows for cell in row if cell.strip()]
    return any(mark in cell for cell in cells for mark in CONTINUATION_MARK)


def _numbering_continues(previous: Block, block: Block) -> bool:
    """上表末行首列 n，次页首行首列 n+1（或 n+1.x）。"""
    if not previous.rows or not block.rows:
        return False
    tail = _first_number(previous.rows[-1])
    head = _first_number(block.rows[0])
    if tail is None or head is None:
        return False
    return int(head) == int(tail) + 1


def _jaccard(left: list[str], right: list[str]) -> float:
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _merge_body(previous: Block, block: Block) -> list[list[str]] | None:
    """判断两张相邻表能否合并；能则返回要追加的行，否则 None。"""
    prev_header = _norm_cells(previous.rows[0])
    cur_first = _norm_cells(block.rows[0])
    same_width = len(cur_first) == len(prev_header)

    if same_width and cur_first == prev_header:
        return block.rows[1:]
    if same_width and len(block.rows) > 1 and _norm_cells(block.rows[0] + block.rows[1]) == prev_header:
        return block.rows[2:]
    if same_width and _jaccard(prev_header, cur_first) >= 0.8:
        return block.rows[1:]          # 表头近似重复（同义列名/空格差异）
    if _has_continuation_mark(block.rows):
        return block.rows if same_width else None
    if same_width and not _looks_like_header(block.rows[0]) and _numbering_continues(previous, block):
        return block.rows
    return None


def _merge_cross_page(blocks: list[Block]) -> list[Block]:
    """相邻页表格合并：表头重复 / Jaccard≥0.8 / 编号连续 / 续表标记。"""
    from sidecar_models import validate_table_columns

    merged: list[Block] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if block.kind != "table":
            merged.append(block)
            index += 1
            continue

        target: Block | None = None
        bridge: Block | None = None
        if merged and merged[-1].kind == "table":
            target = merged[-1]
        elif (
            len(merged) >= 2
            and merged[-1].kind == "text"
            and len(merged[-1].text) < 80
            and merged[-2].kind == "table"
        ):
            # 「上表 | 短注释 | 下表」允许跨一个短文本合并
            candidate = merged[-2]
            if candidate.pages and block.pages and candidate.pages[-1] + 1 == block.pages[0]:
                target, bridge = candidate, merged[-1]

        adjacent = bool(
            target
            and target.rows
            and block.rows
            and target.pages
            and block.pages
            and target.pages[-1] + 1 == block.pages[0]
        )
        if not adjacent:
            merged.append(block)
            index += 1
            continue

        body = _merge_body(target, block)
        if body is None:
            block.split_hint = True
            merged.append(block)
            index += 1
            continue

        if validate_table_columns(None, [target.rows, block.rows]) is False:
            block.split_hint = True       # 表结构模型判定列不对齐
            merged.append(block)
            index += 1
            continue

        target.rows = [*target.rows, *body]
        target.markdown = rows_to_markdown(target.rows)
        target.text = "\n".join(" | ".join(cell for cell in row) for row in target.rows)
        target.pages = [*target.pages, *block.pages]
        if bridge is not None:
            target.text = f"{target.text}\n{bridge.text}"
            merged.pop()                  # 注释块并进表，不再单独成块
        index += 1
    return merged


def _table_suspect(rows: list[list[str]]) -> bool:
    if not rows or not any(cell.strip() for cell in rows[0]):
        return True
    if any(re.search(r"[、。，]{2,}", cell) for row in rows for cell in row):
        return True
    return False


# ------------------------------------------------------------------ 主流程

def parse_sections(path: str | Path, max_chars: int = 1800, min_chars: int = 160) -> tuple[list[dict], dict]:
    import fitz

    from sidecar_models import analyse_page, layout_status

    layout = layout_status()
    layout_fallback = ""
    if layout["enabled"] and not layout["available"]:
        layout_fallback = layout["reason"]

    path = Path(path)
    doc = fitz.open(path)
    blocks: list[Block] = []
    toc_pages: list[int] = []
    try:
        for index, page in enumerate(doc, start=1):
            model_hint = None
            if layout["available"]:
                try:
                    model_hint = analyse_page(page)
                except Exception as error:  # noqa: BLE001 - 侧车失败必须回退规则
                    layout_fallback = f"{type(error).__name__}: {error}"
            if model_hint and model_hint.get("toc"):
                toc_pages.append(index)
                continue
            page_blocks, is_toc = _page_blocks(page, index)
            if is_toc:
                toc_pages.append(index)
                continue
            blocks.extend(page_blocks)
    finally:
        doc.close()

    body = _body_size([block for block in blocks if block.kind == "text"])
    for block in blocks:
        if block.kind == "text":
            block.level = _heading_level(block.text, block.size, body, block.bold)
            block.numbered = bool(_pattern_level(block.text.strip()))

    # 标题行不参与「接回段落」
    joined: list[Block] = []
    for block in blocks:
        if (
            joined
            and block.kind == "text"
            and not block.is_heading
            and joined[-1].kind == "text"
            and not joined[-1].is_heading
            and joined[-1].page == block.page
            and not joined[-1].text.endswith(tuple("。；：！？.!?"))
        ):
            joiner = " " if (joined[-1].text[-1:].isascii() and joined[-1].text[-1:].isalnum()) else ""
            joined[-1].text = joined[-1].text + joiner + block.text
            continue
        joined.append(block)
    blocks = _merge_cross_page(joined)

    chunks: list[dict] = []
    stack: list[tuple[int, str]] = []
    buffer: list[Block] = []
    buffer_chars = 0
    section_level = 0
    chunk_path = ""

    def section_path() -> str:
        return " > ".join(title for _, title in stack)

    def flush() -> None:
        nonlocal buffer, buffer_chars, section_level, chunk_path
        if not buffer:
            return
        heading = chunk_path.split(" > ")[-1] if chunk_path else ""
        parts: list[str] = []
        if all(block.kind == "table" for block in buffer) and heading:
            parts.append(heading)
        for block in buffer:
            parts.append(block.markdown if block.kind == "table" else block.text)
        content = "\n\n".join(part for part in parts if part).strip()
        if len(content) >= 4:
            tables = [block.rows for block in buffer if block.kind == "table" and block.rows]
            chunk = {
                "page": buffer[0].page,
                "pages": sorted({page for block in buffer for page in (block.pages or [block.page])}),
                "section": chunk_path or heading,
                "text": content,
                "kind": "table" if all(block.kind == "table" for block in buffer) else "section",
                "char_start": 0,
            }
            if tables:
                chunk["table"] = {"header": tables[0][0], "rows": tables[0][1:]}
                chunk["tables"] = tables
            chunk["_heading_only"] = all(block.is_heading for block in buffer)
            reasons: list[str] = []
            if any(_table_suspect(block.rows) for block in buffer if block.kind == "table" and block.rows):
                reasons.append("table_suspect")
            if any(block.split_hint for block in buffer):
                reasons.append("table_gap")
            chunk["_quality_reasons"] = reasons
            chunks.append(chunk)
        buffer, buffer_chars, section_level, chunk_path = [], 0, 0, ""

    def consume(block: Block) -> None:
        nonlocal buffer_chars, chunk_path, section_level
        if buffer and buffer_chars + len(block.text) > max_chars:
            held_path, held_level = chunk_path, section_level
            flush()
            chunk_path, section_level = held_path, held_level
        if not buffer and not chunk_path:
            chunk_path = section_path()
        buffer.append(block)
        buffer_chars += len(block.text)

    for block in blocks:
        if block.is_heading and block.numbered:
            if buffer and (section_level == 0 or block.level <= section_level):
                flush()
            level = block.level
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, block.text))
            if not buffer:
                section_level = level
                chunk_path = section_path()
            consume(block)
            continue
        consume(block)

    flush()

    merged: list[dict] = []
    pending: dict | None = None
    for chunk in chunks:
        if pending:
            chunk["text"] = pending["text"] + "\n\n" + chunk["text"]
            chunk["pages"] = sorted({*pending.get("pages", []), *chunk.get("pages", [])})
            pending = None
        if chunk.pop("_heading_only", False) and len(chunk["text"]) < min_chars:
            pending = chunk
            continue
        chunk.pop("_heading_only", None)
        merged.append(chunk)
    if pending:
        merged.append(pending)
    chunks = merged

    suspect = 0
    for chunk in chunks:
        reasons = chunk.pop("_quality_reasons", [])
        if reasons:
            chunk["quality"] = "low"
            chunk["quality_reason"] = ",".join(reasons)
            suspect += 1

    total_chars = sum(len(chunk["text"]) for chunk in chunks)
    stats = {
        "mode": "sections",
        "pages": max((chunk["page"] for chunk in chunks), default=0),
        "chunks": len(chunks),
        "avg_chars": round(total_chars / max(len(chunks), 1), 1),
        "table_chunks": sum(1 for chunk in chunks if chunk.get("table")),
        "sections": len({chunk["section"] for chunk in chunks}),
        "body_size": body,
        "toc_pages": toc_pages,
        "table_suspect": suspect,
        "layout": layout,
        "layout_fallback": layout_fallback,
    }
    return chunks, stats


# ------------------------------------------------------------------ 条款级

def _requirement_number(line: str) -> str:
    # 「12.8.2」这种纯编号单元格：整体就是编号，不能再按「12.8 + .」截断
    bare = re.match(r"^\s*(\d+(?:\.\d+)+)\s*$", line or "")
    if bare:
        return bare.group(1)
    match = REQ_NUMBER.match(line or "")
    if not match:
        return ""
    return next((group for group in match.groups() if group), "")


def _split_segments(text: str) -> list[dict]:
    """把一段文字拆成 (编号, 正文) 片段；支持同一行多个编号。"""
    segments: list[dict] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        matches = list(REQ_INLINE.finditer(stripped))
        if not matches:
            # 行尾编号：「……要求 1.」把编号放行尾，也算一条
            trailing = re.match(r"^(.{10,}?)[\s　]+(\d{1,2}(?:\.\d+){0,2})[\.、]?$", stripped)
            if trailing and not re.search(r"[年月日元%]$", stripped):
                head = trailing.group(1).strip(" ；;，,。")
                if len(head) >= 8:
                    segments.append({"number": trailing.group(2), "lines": [head]})
                    continue
        if not matches:
            if segments:
                segments[-1]["lines"].append(stripped)
            continue

        if matches[0].start() > 0 and segments:
            head = stripped[: matches[0].start()].strip(" ；;，,。")
            if head:
                segments[-1]["lines"].append(head)

        for index, match in enumerate(matches):
            number = next((group for group in match.groups() if group), "")
            if not number:
                continue
            finish = matches[index + 1].start() if index + 1 < len(matches) else len(stripped)
            body = stripped[match.end():finish].strip(" ；;，,。")
            segments.append({"number": number, "lines": [body] if body else []})
    return segments


def _numbering_monotonic(segments: list[dict]) -> bool:
    values: list[float] = []
    for segment in segments:
        try:
            values.append(float(segment["number"]))
        except (TypeError, ValueError):
            return True   # 非数字编号（一、/（一））不参与检查
    # 允许「从 1 重新开始」的嵌套列表；其余回退视为乱序
    for left, right in zip(values, values[1:]):
        if right < left and right != 1.0:
            return False
    return True


CLAUSE_HINT = re.compile(r"★|必须|否决|废标|不接受|不得|应当|须")
CHECKBOX = set("□■☑☐✓")
NUMERIC_ROW = re.compile(r"^[-\d.,%万元亿/年月日\s]*$")


def _display(number: str, body: str) -> str:
    return f"{number}. {body}" if number else body


def _recover_dotted_number(page_text: str, cell_text: str) -> str:
    """「1282 . .」→「12.8.2」：按去点数字串在页面文本里找同源编号。"""
    digits = re.sub(r"[^0-9]", "", cell_text or "")
    if len(digits) < 3 or len(digits) > 8:
        return ""
    for match in re.finditer(r"\d+(?:\.\d+){2,}", page_text or ""):
        candidate = match.group(0)
        if re.sub(r"[^0-9]", "", candidate) == digits:
            return candidate
    return ""


def _table_row_items(chunk: dict, covered: list[str]) -> list[dict]:
    """把「条款型」表行变成 requirement；纯数字矩阵整表留作证据。"""
    items: list[dict] = []
    tables = chunk.get("tables") or []
    page_text = chunk.get("text") or ""
    for table in tables:
        if not table or len(table) < 2:
            continue
        header = table[0]
        rows = table[1:]
        for row_index, row in enumerate(rows, start=1):
            if not row or not any(cell.strip() for cell in row):
                continue
            joined = " | ".join(cell for cell in row)
            first = row[0] if row else ""
            second = row[1] if len(row) > 1 else ""
            number = _requirement_number(first) or _requirement_number(second)
            if not number:
                number = _recover_dotted_number(page_text, first)
            hinted = bool(CLAUSE_HINT.search(joined)) or any(ch in joined for ch in CHECKBOX)
            if not (number or hinted):
                continue
            if all(NUMERIC_ROW.match(cell.strip() or "") for cell in row):
                continue  # 纯报价矩阵，不拆条

            # 勾选行的「（本项目不收取）」常落在上一行：把标题补进来
            title = ""
            if any(ch in joined for ch in CHECKBOX) and not (second.strip()):
                for back in range(1, 4):
                    if row_index - back - 1 < 0:
                        break
                    candidate = rows[row_index - back - 1]
                    text = candidate[1].strip() if len(candidate) > 1 else ""
                    if text and len(text) <= 30 and "。" not in text:
                        title = text
                        break
            composed = f"{title} {joined}" if title and title not in joined else joined
            if any(composed[:40] in text for text in covered):
                continue
            identity = f"{chunk.get('section', '')}|row{row_index}|{composed[:120]}"
            items.append(
                {
                    "id": hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16],
                    "number": number,
                    "kind": "table_row",
                    "section": chunk.get("section", ""),
                    "page": chunk.get("page"),
                    "pages": chunk.get("pages") or [chunk.get("page")],
                    "text": composed,
                    "display_text": _display(number, composed),
                    "source_text": page_text,
                    "table_header": header,
                    "row_index": row_index,
                    "quality": "ok",
                    "quality_reason": "",
                }
            )
    return items


def extract_requirements(chunks: list[dict]) -> list[dict]:
    """把章节块拆成「单条要求」，作为台账/检索的主键粒度。"""
    items: list[dict] = []
    for chunk in chunks:
        text = chunk.get("text") or ""
        if not text.strip():
            continue
        before = len(items)
        segments = _split_segments(text)
        chaos = bool(segments) and not _numbering_monotonic(segments)

        if segments and chaos:
            body = "\n".join(
                "\n".join([f"{segment['number']}.", *segment["lines"]]).strip() for segment in segments
            ).strip()
            segments = [{"number": "", "lines": [body]}]

        for segment in segments:
            body = "\n".join(segment["lines"]).strip()
            if len(body) < (4 if segment["number"] else 8):
                continue
            identity = f"{chunk.get('section', '')}|{segment['number']}|{body[:120]}"
            items.append(
                {
                    "id": hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16],
                    "number": segment["number"],
                    "kind": "text",
                    "section": chunk.get("section", ""),
                    "page": chunk.get("page"),
                    "pages": chunk.get("pages") or [chunk.get("page")],
                    "text": body,
                    "display_text": _display(segment["number"], body),
                    "source_text": text,
                    "quality": "low" if chaos else "ok",
                    "quality_reason": "req_number_chaos" if chaos else "",
                }
            )

        # 表格里的条款行（勾选、★、必须、否决…）
        items.extend(_table_row_items(chunk, [item["text"] for item in items[before:]]))

        if len(items) == before and chunk.get("section"):
            body = text.strip()
            if len(body) >= 8:
                identity = f"{chunk.get('section', '')}||{body[:120]}"
                items.append(
                    {
                        "id": hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16],
                        "number": "",
                        "kind": "text",
                        "section": chunk.get("section", ""),
                        "page": chunk.get("page"),
                        "pages": chunk.get("pages") or [chunk.get("page")],
                        "text": body,
                        "display_text": body,
                        "source_text": text,
                        "quality": "low" if chunk.get("quality") == "low" else "ok",
                        "quality_reason": chunk.get("quality_reason", ""),
                    }
                )
    return items


def to_markdown(path: str | Path) -> str:
    chunks, _ = parse_sections(path)
    parts: list[str] = []
    for chunk in chunks:
        section = chunk.get("section") or ""
        text = chunk["text"].strip()
        if not text:
            continue
        if section and not text.startswith(section.split(" > ")[-1]):
            parts.append(f"## {section}")
        parts.append(text)
    return "\n\n".join(parts).strip() + "\n"


if __name__ == "__main__":
    import sys

    source = Path(sys.argv[1])
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else source.with_suffix(".md")
    target.write_text(to_markdown(source), encoding="utf-8")
    print(f"wrote {target}")
