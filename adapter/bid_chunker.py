#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bid_chunker.py — 投标文件语义切块（PyMuPDF 主路径 / pdfplumber 兜底）

设计目标
--------
1. 中文优先：PyMuPDF 的 text block 本身就是「版面上的段落」，比 pdfplumber 的
   extract_text() 更贴近语义边界，CJK 字符不会丢、不会乱序。
2. 不机械切断句子：先切成句子单元，再按目标长度打包；只有单句超长时才硬切。
3. 保留溯源信息：每个 chunk 带页码、章节标题、字符偏移，供比对时引用证据。
4. 扫描件不静默失败：没有文本层且有图片的页面标记 needs_ocr；装了
   pytesseract + tesseract 就自动 OCR，没装则显式报出来，而不是返回空列表。
5. 页眉页脚清洗：在多数页重复出现的行（含纯页码）不进入正文块。

对外接口
--------
    chunks, stats = chunk_pdf(path, target=900, max_chars=1400)
    # chunks: list[dict]  {page, section, text, kind, char_start}
    # stats : dict        {pages, chunks, ocr_pages, empty_pages, ...}
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 后端选择

def _load_backend() -> str:
    try:  # PyMuPDF >= 1.24 推荐 `import pymupdf`；旧版为 `fitz`
        import pymupdf  # noqa: F401
        return "pymupdf"
    except Exception:  # noqa: BLE001
        pass
    try:
        import fitz  # noqa: F401
        return "fitz"
    except Exception:  # noqa: BLE001
        return "pdfplumber"


def _open_pymupdf(path: Path):
    try:
        import pymupdf as fitz
    except Exception:  # noqa: BLE001
        import fitz
    return fitz.open(str(path))


# ---------------------------------------------------------------- 文本清洗

_WS = re.compile(r"[ \t\u00a0\u3000]+")
_MULTI_NL = re.compile(r"\n{2,}")
# 纯页码 / 页眉页码行：第 3 页 / - 3 - / 3/83 / 共 83 页
_PAGE_NO = re.compile(r"^[\s\-—–]*(第\s*\d+\s*页(\s*[/，,]?\s*共\s*\d+\s*页)?|\d+\s*/\s*\d+|[-—–]\s*\d+\s*[-—–]|第?\s*\d+\s*页?)[\s\-—–]*$")
# 中文/阿拉伯编号标题：一、 / （一） / 1. / 1.2.3 / 第X章
_HEADING = re.compile(
    r"^\s*(第[一二三四五六七八九十百零]+[章节条部分篇]"
    r"|[一二三四五六七八九十]{1,3}\s*[、.．]"
    r"|[（(][一二三四五六七八九十]{1,3}[）)]"
    r"|\d{1,2}(?:\.\d{1,2}){0,3}\s*[、.．\s]"
    r"|\d{1,2}\s*[、.．])"
)
_SENT_END = "。！？；!?;…"


def clean(text: str) -> str:
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = _WS.sub(" ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    return text.strip()


def is_page_number(line: str) -> bool:
    return bool(_PAGE_NO.match(line.strip()))


def is_heading(line: str) -> bool:
    return heading_title(line) is not None


def heading_title(line: str) -> str | None:
    """识别标题行，返回章节名；不是标题返回 None。

    「1. 企业基本情况：我司成立于……」这类「编号 + 冒号小标题」即使整行很长，
    也把冒号前的部分当章节名，方便证据里带上下文。
    """
    line = line.strip()
    if not line or not _HEADING.match(line):
        return None
    for sep in ("：", ":"):
        pos = line.find(sep)
        if 0 < pos <= 30:
            return line[:pos].strip()
    return line if len(line) <= 48 else None


def split_sentences(text: str) -> list[str]:
    """按中英文句末标点切句，标点保留在前一句尾部。换行视为软边界。"""
    out: list[str] = []
    buf: list[str] = []
    for ch in text:
        if ch == "\n":
            if buf and buf[-1] in "：:，,、":
                buf.append(ch)
                continue
            if buf:
                out.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
        if ch in _SENT_END:
            out.append("".join(buf))
            buf = []
    if buf:
        out.append("".join(buf))
    return [s.strip() for s in out if s.strip()]


# ---------------------------------------------------------------- 页级解析

@dataclass
class Page:
    index: int          # 1-based
    text: str = ""
    blocks: list[tuple[str, str]] = field(default_factory=list)  # (kind, text)
    table_structures: dict[str, dict] = field(default_factory=dict)
    has_image: bool = False
    needs_ocr: bool = False


def _pages_pymupdf(path: Path) -> list[Page]:
    doc = _open_pymupdf(path)
    pages: list[Page] = []
    try:
        for i, page in enumerate(doc, start=1):
            tables = _table_regions(page)
            boxes = [tuple(table.bbox) for table in tables]
            raw = page.get_text("blocks") or []
            blocks: list[tuple[str, str]] = []
            for item in raw:
                # (x0, y0, x1, y1, text, block_no, block_type)
                if len(item) < 7 or item[6] != 0:
                    continue
                text = clean(item[4])
                if not text:
                    continue
                if _inside_tables(item[:4], boxes):
                    continue  # 表格里的文字由表格块统一表达，避免重复
                blocks.append(("text", text))
            structures: dict[str, dict] = {}
            for body, structure in _table_blocks(page, "".join(b for _, b in blocks), tables):
                blocks.append(("table", body))
                structures[body] = structure
            page_text = clean(page.get_text("text") or "")
            has_image = bool(page.get_images(full=True))
            pages.append(
                Page(
                    index=i,
                    text=page_text,
                    blocks=blocks,
                    table_structures=structures,
                    has_image=has_image,
                    needs_ocr=len(page_text) < 20 and has_image,
                )
            )
    finally:
        doc.close()
    return pages


def _inside_tables(box, table_boxes) -> bool:
    """文字块中心点落在表格区域内 → 视为表格内容。"""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    for tx0, ty0, tx1, ty1 in table_boxes:
        if tx0 <= cx <= tx1 and ty0 <= cy <= ty1:
            return True
    return False

def _pages_docx(path: Path, chars_per_page: int = 2600) -> list[Page]:
    """Word 文档：按正文顺序读段落与表格；页面按字符数估算。

    .docx 没有固定页，这里用字数折算（约 2600 字/页），只用于给证据标页码。
    """
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(str(path))
    blocks: list[tuple[str, str]] = []
    structures: dict[str, dict] = {}
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            value = clean(Paragraph(child, document).text)
            if value:
                blocks.append(("text", value))
        elif tag == "tbl":
            table = Table(child, document)
            rows: list[list[str]] = []
            for row in table.rows:
                cells: list[str] = []
                for cell in row.cells:
                    value = clean(cell.text)
                    if not cells or cells[-1] != value:  # 合并单元格会重复同一段文字
                        cells.append(value)
                if any(cells):
                    rows.append(cells)
            if len(rows) < 2:
                continue
            body = "\n".join(" | ".join(cells) for cells in rows)
            blocks.append(("table", body))
            structures[body] = {"header": rows[0], "rows": rows[1:]}

    pages: list[Page] = []
    buffer: list[tuple[str, str]] = []
    used = 0
    index = 1
    for block in blocks:
        buffer.append(block)
        used += len(block[1])
        if used >= chars_per_page:
            pages.append(_make_docx_page(index, buffer, structures))
            buffer, used = [], 0
            index += 1
    if buffer:
        pages.append(_make_docx_page(index, buffer, structures))
    return pages


def _make_docx_page(index: int, blocks: list[tuple[str, str]], structures: dict[str, dict]) -> Page:
    texts = "".join(text for _, text in blocks)
    return Page(
        index=index,
        text=texts,
        blocks=blocks,
        table_structures={k: v for k, v in structures.items() if k in texts},
        has_image=False,
        needs_ocr=False,
    )


def _table_regions(page) -> list:
    """先按线框找表，找不到再按文字对齐找（无框线表格）。"""
    for strategy in (None, "text"):
        try:
            finder = page.find_tables() if strategy is None else page.find_tables(strategy=strategy)
            tables = getattr(finder, "tables", []) or []
        except Exception:  # noqa: BLE001
            tables = []
        if tables:
            return tables
    return []


def _table_blocks(page, page_text: str, tables=None) -> list[tuple[str, str]]:
    """PyMuPDF 表格 → 行文本块。"""
    tables = tables if tables is not None else _table_regions(page)
    out: list[tuple[str, dict]] = []
    flat_page = _norm_key(page_text)
    for table in tables:
        try:
            rows = table.extract() or []
        except Exception:  # noqa: BLE001
            continue
        rows = _normalize_rows(rows)
        lines = [" | ".join(clean(cell or "") for cell in row).strip(" |") for row in rows]
        lines = [line for line in lines if line.strip(" |")]
        if len(lines) < 2:
            continue
        body = "\n".join(lines)
        if any(existing == body for existing, _ in out):
            continue
        structure = {"header": rows[0] if rows else [], "rows": rows[1:] if len(rows) > 1 else []}
        out.append((body, structure))
    return out


def _normalize_rows(rows: list[list[str | None]]) -> list[list[str]]:
    """把 PyMuPDF 的表格整理成规整矩阵。

    合并单元格会被拆成「有值列 + 若干空列」，直接 join 会得到一堆多余竖线；
    这里先删掉整列为空的列，再去掉连续重复行（text 策略常产生这种噪音）。
    """
    matrix = [
        ["" if cell is None else str(cell).strip() for cell in row]
        for row in rows
        if row and any((cell or "").strip() for cell in row)
    ]
    if not matrix:
        return []
    width = max(len(row) for row in matrix)
    matrix = [row + [""] * (width - len(row)) for row in matrix]
    keep = [index for index in range(width) if any(row[index] for row in matrix)]
    matrix = [[row[index] for index in keep] for row in matrix]

    deduped: list[list[str]] = []
    for row in matrix:
        if not any(row):
            continue
        if deduped and deduped[-1] == row:
            continue
        deduped.append(row)
    return deduped


def merge_cross_page_tables(chunks: list[dict]) -> list[dict]:
    """跨页表格合并：相邻页、表头相同、列数相同 → 合成一块。"""
    merged: list[dict] = []
    for chunk in chunks:
        if chunk.get("kind") != "table" or not merged:
            merged.append(chunk)
            continue
        previous = merged[-1]
        if previous.get("kind") != "table" or previous.get("page", 0) + 1 != chunk.get("page", 0):
            merged.append(chunk)
            continue
        prev_lines = previous["text"].splitlines()
        cur_lines = chunk["text"].splitlines()
        if not prev_lines or not cur_lines:
            merged.append(chunk)
            continue
        if prev_lines[0] != cur_lines[0]:
            merged.append(chunk)
            continue
        if prev_lines[0].count("|") != cur_lines[0].count("|"):
            merged.append(chunk)
            continue
        # 续页通常重复表头，合并时去掉当前页的表头
        body = "\n".join([previous["text"], *cur_lines[1:]])
        previous["text"] = body
        if previous.get("table") and chunk.get("table"):
            previous["table"]["rows"] = [
                *previous["table"].get("rows", []),
                *chunk["table"].get("rows", []),
            ]
        previous["pages"] = [*previous.get("pages", [previous.get("page")]), chunk.get("page")]
    return merged

def _pages_pdfplumber(path: Path) -> list[Page]:
    import pdfplumber

    pages: list[Page] = []
    with pdfplumber.open(str(path)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = clean(page.extract_text() or "")
            blocks: list[tuple[str, str]] = []
            for para in re.split(r"\n{2,}", text):
                para = para.strip()
                if para:
                    blocks.append(("text", para))
            for table in page.find_tables():
                rows = table.extract() or []
                lines = [
                    " | ".join(clean(c or "") for c in row).strip(" |")
                    for row in rows
                    if row and any((c or "").strip() for c in row)
                ]
                if len(lines) < 2:
                    continue
                body = "\n".join(lines)
                flat_page = _norm_key(text)
                if flat_page and _norm_key(body) and _norm_key(body) in flat_page:
                    continue  # 正文已包含该表格，避免同一内容被切两次
                blocks.append(("table", body))
            pages.append(
                Page(
                    index=i,
                    text=text,
                    blocks=blocks,
                    has_image=bool(page.images),
                    needs_ocr=len(text) < 20 and bool(page.images),
                )
            )
    return pages


def _norm_key(text: str) -> str:
    return re.sub(r"[\s|，。；：、（）()【】\[\]]", "", text or "")


# ---------------------------------------------------------------- OCR

_OCR_ENGINE = None
_OCR_CACHE = Path(__file__).resolve().parent.parent / ".cache" / "pdf_ocr"


def ocr_available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def _ocr_engine():
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def _ocr_page(path: Path, index: int, dpi: int = 200, events: list | None = None) -> str:
    """渲染页面再 OCR；同一页重跑走缓存。

    后端降级必须可观测：events 记录 from/to/reason，同时打 warning；
    TENDER_OCR_STRICT=1 时直接抛错，方便部署时暴露配置问题。
    """
    import hashlib
    import logging
    import os

    import numpy as np

    doc = _open_pymupdf(path)
    try:
        page = doc[index - 1]
        pix = page.get_pixmap(dpi=dpi)
        image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 4:
            image = image[:, :, :3]
        digest = hashlib.sha1(image.tobytes()).hexdigest()[:16]
    finally:
        doc.close()

    cache_file = _OCR_CACHE / f"{digest}.txt"
    if cache_file.exists():
        try:
            return cache_file.read_text(encoding="utf-8")
        except OSError:
            pass

    strict = (os.environ.get("TENDER_OCR_STRICT") or "").strip() in {"1", "true", "yes"}

    def record(reason: str) -> None:
        message = f"OCR 后端降级：paddle → rapidocr（{reason}）"
        logging.warning(message)
        if events is not None:
            events.append({"from": "paddle", "to": "rapidocr", "reason": reason[:200], "page": index})
        if strict:
            raise RuntimeError(message)

    try:
        from ocr_backend import available as _ocr_available

        if _ocr_available().get("paddle"):
            import tempfile

            from PIL import Image

            handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            handle.close()
            Image.fromarray(image).save(handle.name)
            try:
                from ocr_backend import ocr_image_file

                text = clean(ocr_image_file(handle.name)[0])
            finally:
                Path(handle.name).unlink(missing_ok=True)
            try:
                _OCR_CACHE.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(text, encoding="utf-8")
            except OSError:
                pass
            return text
    except Exception as error:  # noqa: BLE001
        record(f"{type(error).__name__}: {error}")

    engine = _ocr_engine()
    result, _ = engine(image)
    lines = [str(item[1]) for item in (result or [])]
    text = clean("\n".join(lines))
    try:
        _OCR_CACHE.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(text, encoding="utf-8")
    except OSError:
        pass
    return text


# ---------------------------------------------------------------- 页眉页脚

def strip_running_heads(pages: list[Page]) -> int:
    """删掉在多页重复出现的短行（页眉/页脚/水印），以及纯页码行。"""
    if len(pages) < 3:
        counts: dict[str, int] = {}
        removed = 0
        for page in pages:
            kept = []
            for kind, text in page.blocks:
                if kind == "text" and is_page_number(text):
                    removed += 1
                    continue
                kept.append((kind, text))
            page.blocks = kept
        return removed

    counts = {}
    for page in pages:
        for _, text in page.blocks:
            for line in text.split("\n"):
                key = _norm_key(line)
                if 0 < len(key) <= 40:
                    counts[key] = counts.get(key, 0) + 1
    threshold = max(3, int(len(pages) * 0.4))
    noise = {k for k, v in counts.items() if v >= threshold}

    removed = 0
    for page in pages:
        kept: list[tuple[str, str]] = []
        for kind, text in page.blocks:
            if kind != "text":
                kept.append((kind, text))
                continue
            lines = []
            for line in text.split("\n"):
                if is_page_number(line) or _norm_key(line) in noise:
                    removed += 1
                    continue
                lines.append(line)
            body = "\n".join(lines).strip()
            if body:
                kept.append((kind, body))
        page.blocks = kept
    return removed


# ---------------------------------------------------------------- 打包成块

def _pack_page(
    blocks: list[tuple[str, str]],
    page_no: int,
    section: str,
    structures: dict[str, dict] | None = None,
    *,
    target: int,
    max_chars: int,
    min_chars: int,
) -> list[dict]:
    """把一页的块按句子打包；表格独占一块；不跨页。"""
    chunks: list[dict] = []
    buf: list[str] = []
    buf_len = 0
    buf_section = section
    chunk_section = section  # 用「块内第一个标题」标注，而不是块尾的标题
    offset = 0

    def flush() -> None:
        nonlocal buf, buf_len, buf_section, chunk_section
        text = "".join(buf).strip()
        if text:
            chunks.append(
                {
                    "page": page_no,
                    "section": chunk_section,
                    "text": text,
                    "kind": "text",
                    "char_start": offset,
                }
            )
        buf, buf_len = [], 0
        chunk_section = buf_section

    for kind, text in blocks:
        if kind == "table":
            flush()
            chunk = {
                "page": page_no,
                "section": buf_section,
                "text": text,
                "kind": "table",
                "char_start": offset,
            }
            structure = (structures or {}).get(text)
            if structure:
                chunk["table"] = structure
            chunks.append(chunk)
            offset += len(text)
            continue

        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            title = heading_title(line)
            if title is not None:
                buf_section = title
                # 只有当前块已经够长才在标题处收块；否则标题直接并入下一段。
                # 这样既不会产生「第一章 投标邀请」这种 8 字独立块，也不会
                # 把每一条编号项都切成碎片。
                if buf_len >= max(min_chars, int(target * 0.6)):
                    flush()
            for sent in split_sentences(line):
                if buf and buf_len + len(sent) > max_chars:
                    flush()
                if len(sent) > max_chars:  # 超长单句：按 max_chars 硬切
                    for start in range(0, len(sent), max_chars):
                        piece = sent[start : start + max_chars]
                        if len(piece) >= min_chars:
                            flush()
                            chunks.append(
                                {
                                    "page": page_no,
                                    "section": buf_section,
                                    "text": piece,
                                    "kind": "text",
                                    "char_start": offset,
                                }
                            )
                            offset += len(piece)
                    continue
                if not buf:
                    chunk_section = buf_section
                buf.append(sent)
                buf_len += len(sent)
                offset += len(sent)
                if buf_len >= target:
                    flush()
    flush()

    # 收尾碎块处理：过短的块并入相邻块，避免出现 8 个字的"段落"
    chunks = _merge_tiny(chunks, min_chars=min_chars, max_chars=max_chars)
    return chunks


def _merge_tiny(chunks: list[dict], *, min_chars: int, max_chars: int) -> list[dict]:
    if len(chunks) < 2:
        return chunks
    merged: list[dict] = [chunks[0]]
    for chunk in chunks[1:]:
        prev = merged[-1]
        same_page = prev["page"] == chunk["page"] and prev["kind"] == chunk["kind"] == "text"
        too_short = len(prev["text"]) < min_chars or len(chunk["text"]) < min_chars
        if same_page and too_short and len(prev["text"]) + len(chunk["text"]) + 1 <= max_chars:
            prev["text"] = f"{prev['text']}\n{chunk['text']}"
            continue
        merged.append(chunk)
    return merged


# ---------------------------------------------------------------- 主入口

def _chunk_sections(path: Path, *, max_chars: int, min_chars: int, backend: str | None) -> tuple[list[dict], dict]:
    """按标题层级切块；文字层太薄（扫描件）时返回空，交给 OCR 路径。"""
    from layout_parse import parse_sections

    chunks, stats = parse_sections(path, max_chars=max_chars, min_chars=min_chars)
    total_chars = sum(len(chunk["text"]) for chunk in chunks)
    # 只有「几乎没读出文字」（扫描件）才退回 OCR 路径；短文档也要走章节切块，
    # 否则表格/条款结构会在回退时丢掉。
    if not chunks or total_chars < 20:
        return [], {}
    stats.update(
        {
            "backend": backend or "layout",
            "chunks": len(chunks),
            "avg_chars": round(total_chars / max(len(chunks), 1), 1),
            "table_chunks": sum(1 for chunk in chunks if chunk["kind"] == "table"),
            "ocr_done_pages": [],
            "ocr_missing_pages": [],
            "ocr_limited_pages": 0,
            "ocr_fallback": [],
            "empty_pages": [],
            "removed_running_lines": 0,
            "ocr_available": ocr_available(),
        }
    )
    return chunks, stats

def chunk_pdf(
    path: str | Path,
    *,
    target: int = 900,
    max_chars: int = 1400,
    min_chars: int = 60,
    ocr: bool = True,
    max_ocr_pages: int = 60,
    backend: str | None = None,
    mode: str = "sections",
) -> tuple[list[dict], dict]:
    path = Path(path)
    if mode == "sections" and path.suffix.lower() == ".pdf":
        chunks, stats = _chunk_sections(path, max_chars=max_chars, min_chars=min_chars, backend=backend)
        if chunks:
            return chunks, stats
        # 扫描件没有文字层：退回逐页解析 + OCR
    if path.suffix.lower() in {".docx", ".doc"}:
        pages = _pages_docx(path)
        backend = "docx"
    else:
        backend = backend or _load_backend()
        pages = _pages_pymupdf(path) if backend in {"pymupdf", "fitz"} else _pages_pdfplumber(path)

    ocr_pages: list[int] = []
    ocr_done = 0
    ocr_limited = 0
    ocr_events: list[dict] = []
    if ocr:
        can_ocr = ocr_available()
        for page in pages:
            if not page.needs_ocr:
                continue
            if can_ocr and ocr_done >= max_ocr_pages:
                ocr_limited += 1
                continue
            if can_ocr:
                try:
                    page.text = _ocr_page(path, page.index, events=ocr_events)
                    page.blocks = [("text", page.text)] if page.text else []
                    page.needs_ocr = False
                    ocr_done += 1
                    ocr_pages.append(page.index)
                except Exception:  # noqa: BLE001
                    pass
            if page.needs_ocr:
                ocr_pages.append(-page.index)  # 负号 = 需要 OCR 但未完成

    removed = strip_running_heads(pages)

    chunks: list[dict] = []
    section = ""
    empty_pages: list[int] = []
    for page in pages:
        if not page.blocks:
            empty_pages.append(page.index)
            continue
        page_chunks = _pack_page(
            page.blocks,
            page.index,
            section,
            page.table_structures,
            target=target,
            max_chars=max_chars,
            min_chars=min_chars,
        )
        if page_chunks:
            section = page_chunks[-1]["section"] or section
        chunks.extend(page_chunks)

    chunks = merge_cross_page_tables(chunks)

    total_chars = sum(len(c["text"]) for c in chunks)
    stats = {
        "backend": backend,
        "pages": len(pages),
        "chunks": len(chunks),
        "avg_chars": round(total_chars / max(len(chunks), 1), 1),
        "table_chunks": sum(1 for c in chunks if c["kind"] == "table"),
        "ocr_done_pages": [p for p in ocr_pages if p > 0],
        "ocr_missing_pages": [-p for p in ocr_pages if p < 0],
        "empty_pages": empty_pages,
        "removed_running_lines": removed,
        "ocr_available": ocr_available(),
        "ocr_limited_pages": ocr_limited,
        "ocr_fallback": ocr_events,
    }
    return chunks, stats


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="投标文件语义切块自检")
    parser.add_argument("pdf")
    parser.add_argument("--target", type=int, default=900)
    parser.add_argument("--max-chars", type=int, default=1400)
    parser.add_argument("--backend", default=None, choices=["pymupdf", "fitz", "pdfplumber"])
    parser.add_argument("--dump", default=None, help="把切块结果写成 jsonl")
    args = parser.parse_args()

    chunks, stats = chunk_pdf(
        args.pdf, target=args.target, max_chars=args.max_chars, backend=args.backend
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    for chunk in chunks[:5]:
        print(f"\n--- p{chunk['page']} [{chunk['section'][:28]}] {len(chunk['text'])}字 ---")
        print(chunk["text"][:220].replace("\n", " ⏎ "))
    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as handle:
            for chunk in chunks:
                handle.write(json.dumps(chunk, ensure_ascii=False) + "\n")
        print(f"\n已写出 {len(chunks)} 段 → {args.dump}")


if __name__ == "__main__":
    main()
