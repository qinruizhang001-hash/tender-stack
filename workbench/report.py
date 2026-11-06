"""PDF 审查报告：把一次运行的结果渲染成可以直接发给评审的报告。

为什么不用 reportlab / weasyprint
--------------------------------
PyMuPDF（fitz）已经是项目依赖，且自带 CJK 字体（`china-s`），
不必再引一个几 MB 的排版库，也不用担心服务器上缺中文字体。

排版是手写的：A4、固定页边距、逐段测量换行、按行分页。
功能不多，但每一页的边距、页脚、表格线都是确定的。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import fitz

PAGE = fitz.paper_rect("a4")
MARGIN_X = 46.0
MARGIN_TOP = 56.0
MARGIN_BOTTOM = 52.0
CONTENT_WIDTH = PAGE.width - 2 * MARGIN_X


def _pick_font() -> Path | None:
    """优先用系统 TTF：PyMuPDF 自带的 china-s 会把 ASCII 画成全角，表格会散架。"""
    for candidate in (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/Deng.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ):
        path = Path(candidate)
        if path.exists():
            return path
    return None


FONT_PATH = _pick_font()
FONT_NAME = "F0" if FONT_PATH else "china-s"
_FONT = fitz.Font(fontfile=str(FONT_PATH)) if FONT_PATH else None

INK = (0.12, 0.13, 0.15)
MUTED = (0.42, 0.45, 0.50)
ACCENT = (0.15, 0.36, 0.72)
LINE = (0.86, 0.87, 0.90)
SOFT = (0.96, 0.97, 0.98)

RISK_COLORS = {
    "负偏离": (0.72, 0.16, 0.18),
    "未响应": (0.72, 0.16, 0.18),
    "未找到": (0.72, 0.16, 0.18),
    "人工介入": (0.72, 0.45, 0.05),
    "已响应": (0.10, 0.45, 0.28),
    "部分响应": (0.15, 0.36, 0.72),
}

RISK_STATUSES = ("负偏离", "未响应", "未找到", "人工介入")

_CN_NUM = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]


def _measure(text: str, size: float) -> float:
    if _FONT is not None:
        return _FONT.text_length(text, size)
    return fitz.get_text_length(text, fontname="china-s", fontsize=size)


def _wrap(text: str, size: float, width: float) -> list[str]:
    """按字符断行：中文没有空格，按宽度逐字累加最稳。"""
    lines: list[str] = []
    current = ""
    for char in text:
        if char == "\n":
            lines.append(current)
            current = ""
            continue
        if _measure(current + char, size) > width and current:
            lines.append(current)
            current = char
        else:
            current += char
    if current or not lines:
        lines.append(current)
    return lines


class Pdf:
    def __init__(self, footer: str):
        self.doc = fitz.open()
        self.page: fitz.Page | None = None
        self.y = 0.0
        self.footer = footer
        self.section = 0

    # ---------------- 基础绘制 ----------------
    def new_page(self) -> None:
        self.page = self.doc.new_page(width=PAGE.width, height=PAGE.height)
        if FONT_PATH:
            self.page.insert_font(fontname=FONT_NAME, fontfile=str(FONT_PATH))
        self.y = MARGIN_TOP
        number = self.doc.page_count
        page = self.page
        page.insert_text(
            (PAGE.width - MARGIN_X - 44, PAGE.height - 26),
            f"第 {number} 页",
            fontname=FONT_NAME,
            fontsize=8.5,
            color=MUTED,
        )
        page.insert_text(
            (MARGIN_X, PAGE.height - 26),
            self.footer,
            fontname=FONT_NAME,
            fontsize=8.5,
            color=MUTED,
        )
        page.draw_line(
            fitz.Point(MARGIN_X, PAGE.height - 36),
            fitz.Point(PAGE.width - MARGIN_X, PAGE.height - 36),
            color=LINE,
            width=0.6,
        )

    def ensure(self, height: float) -> None:
        if self.page is None or self.y + height > PAGE.height - MARGIN_BOTTOM:
            self.new_page()

    def text(
        self,
        value: str,
        size: float = 10.0,
        color: tuple[float, float, float] = INK,
        indent: float = 0.0,
        leading: float = 1.55,
        gap: float = 0.0,
    ) -> None:
        if not value:
            self.y += gap
            return
        line_height = size * leading
        for line in _wrap(value, size, CONTENT_WIDTH - indent):
            self.ensure(line_height)
            assert self.page is not None
            self.page.insert_text(
                (MARGIN_X + indent, self.y + size),
                line,
                fontname=FONT_NAME,
                fontsize=size,
                color=color,
            )
            self.y += line_height
        self.y += gap

    def heading(self, title: str, size: float = 14.5) -> None:
        self.section += 1
        self.ensure(size * 3)
        self.y += 10
        assert self.page is not None
        self.page.insert_text(
            (MARGIN_X, self.y + size),
            f"{_CN_NUM[self.section - 1]}、{title}" if self.section <= len(_CN_NUM) else title,
            fontname=FONT_NAME,
            fontsize=size,
            color=INK,
        )
        self.y += size * 1.5
        self.page.draw_line(
            fitz.Point(MARGIN_X, self.y),
            fitz.Point(MARGIN_X + 46, self.y),
            color=ACCENT,
            width=2.2,
        )
        self.y += 12

    def kv(self, label: str, value: str, size: float = 10.0) -> None:
        line_height = size * 1.7
        self.ensure(line_height)
        assert self.page is not None
        self.page.insert_text(
            (MARGIN_X, self.y + size),
            label,
            fontname=FONT_NAME,
            fontsize=size,
            color=MUTED,
        )
        self.page.insert_text(
            (MARGIN_X + 92, self.y + size),
            value,
            fontname=FONT_NAME,
            fontsize=size,
            color=INK,
        )
        self.y += line_height

    # ---------------- 表格 ----------------
    def table(
        self,
        headers: list[str],
        widths: list[float],
        rows: Iterable[list[str]],
        size: float = 8.8,
        colors: list[tuple[float, float, float] | None] | None = None,
    ) -> None:
        line_height = size * 1.5

        def draw_header() -> None:
            self.ensure(line_height + 10)
            assert self.page is not None
            self.page.draw_rect(
                fitz.Rect(MARGIN_X, self.y, MARGIN_X + sum(widths), self.y + line_height + 6),
                color=None,
                fill=SOFT,
            )
            x = MARGIN_X
            for head, width in zip(headers, widths):
                self.page.insert_text(
                    (x + 4, self.y + line_height - 1),
                    head,
                    fontname=FONT_NAME,
                    fontsize=size,
                    color=MUTED,
                )
                x += width
            self.y += line_height + 6

        draw_header()
        for index, row in enumerate(rows):
            cells = [_wrap(str(cell), size, width - 8) for cell, width in zip(row, widths)]
            height = max(len(cell) for cell in cells) * line_height + 6
            if self.y + height > PAGE.height - MARGIN_BOTTOM:
                self.new_page()
                draw_header()
            assert self.page is not None
            x = MARGIN_X
            row_color = (colors or [])[index] if colors and index < len(colors) else None
            for cell_lines, width in zip(cells, widths):
                for offset, line in enumerate(cell_lines):
                    self.page.insert_text(
                        (x + 4, self.y + line_height - 2 + offset * line_height),
                        line,
                        fontname=FONT_NAME,
                        fontsize=size,
                        color=row_color or INK,
                    )
                x += width
            self.y += height
            self.page.draw_line(
                fitz.Point(MARGIN_X, self.y),
                fitz.Point(MARGIN_X + sum(widths), self.y),
                color=LINE,
                width=0.5,
            )

    def save(self) -> bytes:
        # 不做 subset 的话，中文字体每次嵌入十几 MB；subset 后约 0.15 MB
        try:
            self.doc.subset_fonts()
        except Exception:  # noqa: BLE001 - 老版本 PyMuPDF 没有这个方法
            pass
        return self.doc.tobytes(garbage=4, deflate=True, clean=True)

def _short(value: Any, limit: int = 90) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] + "…" if len(text) > limit else text


def build_report(
    *,
    name: str,
    meta: dict[str, Any],
    rows: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
    summary: dict[str, Any] | None = None,
    bids: list[dict[str, Any]] | None = None,
    generated_at: datetime | None = None,
) -> bytes:
    stamp = (generated_at or datetime.now()).strftime("%Y-%m-%d %H:%M")
    pdf = Pdf(f"标书工作台 · {name} · 仅供内部审查")
    pdf.new_page()

    # ---------------- 封面 ----------------
    pdf.y = 120
    pdf.text("标书审查报告", size=26, leading=1.3)
    pdf.y += 6
    pdf.text(stamp, size=10.5, color=MUTED, gap=26)
    pdf.text(f"运行：{name}", size=12)
    tender = meta.get("document_filename") or "—"
    pdf.text(f"招标文件：{tender}", size=11, color=MUTED)
    pdf.text(f"页数：{meta.get('page_count', '—')}", size=11, color=MUTED)
    pdf.y += 18

    counts = _coverage_counts(coverage)
    total = len(rows) or len(coverage)
    pdf.text(f"要求条款 {total} 条", size=13)
    if not coverage:
        pdf.text("未执行标书比对，本报告只含台账。", size=10.5, color=MUTED, gap=4)
    if counts:
        pdf.y += 4
        for status in ("已响应", "部分响应", "负偏离", "未响应", "未找到", "人工介入"):
            value = counts.get(status, 0)
            if value:
                pdf.text(f"· {status} {value} 条", size=10.5, color=RISK_COLORS.get(status, INK), indent=8)

    gate = (summary or {}).get("precheck") or {}
    if gate.get("status") == "warn":
        pdf.y += 10
        pdf.text("⚠ 规则预判健康度异常，结论可信度下降", size=10.5, color=(0.72, 0.45, 0.05))
        for line in gate.get("warnings", [])[:3]:
            pdf.text(f"· {line}", size=9.5, color=(0.72, 0.45, 0.05), indent=8)

    pdf.new_page()

    # ---------------- 摘要 ----------------
    pdf.heading("摘要")
    pdf.kv("运行", name)
    pdf.kv("生成时间", stamp)
    pdf.kv("招标文件", _short(tender, 60))
    pdf.kv("文档页数", str(meta.get("page_count", "—")))
    pdf.kv("要求条款", f"{total} 条")
    if counts:
        responded = sum(counts.get(key, 0) for key in ("已响应", "部分响应", "负偏离", "人工介入"))
        pdf.kv("有回应", f"{responded} 条（{round(responded / max(len(coverage), 1) * 100, 1)}%）")
        pdf.kv("合规", f"{counts.get('已响应', 0)} 条（{round(counts.get('已响应', 0) / max(len(coverage), 1) * 100, 1)}%）")
        pdf.kv("待处理", f"{counts.get('未响应', 0) + counts.get('未找到', 0) + counts.get('负偏离', 0)} 条")
        pdf.kv("待人工", f"{counts.get('人工介入', 0)} 条")
    if summary:
        pdf.kv("模型调用", str(summary.get("model_calls", "—")))
        pdf.kv("两票分歧", str(summary.get("vote_disagree", "—")))
        pdf.kv("规则短路", str((summary.get("precheck") or {}).get("short_circuit", "—")))

    # ---------------- 多标对比 ----------------
    if bids and len(bids) > 1:
        pdf.heading("投标文件对比")
        pdf.table(
            ["投标文件", "已响应", "部分响应", "负偏离", "未响应", "未找到", "待人工", "需处理"],
            [180, 44, 48, 44, 44, 44, 44, 48],
            [
                [
                    _short(item.get("name"), 34),
                    str((item.get("coverage") or {}).get("已响应", 0)),
                    str((item.get("coverage") or {}).get("部分响应", 0)),
                    str((item.get("coverage") or {}).get("负偏离", 0)),
                    str((item.get("coverage") or {}).get("未响应", 0)),
                    str((item.get("coverage") or {}).get("未找到", 0)),
                    str((item.get("coverage") or {}).get("人工介入", 0)),
                    str(item.get("missing", 0)),
                ]
                for item in bids
            ],
            size=9.2,
        )
    # ---------------- 风险清单 ----------------
    risky = [row for row in coverage if row.get("coverage") in RISK_STATUSES]
    pdf.heading("风险与缺口")
    if not coverage:
        pdf.text(
            "该运行没有比对结果（match/coverage.csv），本报告只包含台账，不含合规结论。"
            "如需覆盖判定，请先执行「标书比对」。",
            size=10,
            color=MUTED,
        )
        risky = []
    elif not risky:
        pdf.text("未发现负偏离、未响应或需人工确认的条款。", size=10, color=MUTED)
    else:
        pdf.text(f"共 {len(risky)} 条需要处理，按结论严重度排列。", size=10, color=MUTED, gap=2)
        shown = risky[:80]
        pdf.table(
            ["编号", "结论", "页码", "要求原文", "判定依据"],
            [56, 48, 34, 214, 150],
            [
                [
                    row.get("req_id", ""),
                    row.get("coverage", ""),
                    str(row.get("bid_page") or "—"),
                    _short(row.get("requirement"), 110),
                    _short(row.get("judge_basis") or row.get("reason"), 70) or "—",
                ]
                for row in shown
            ],
            colors=[RISK_COLORS.get(row.get("coverage", ""), INK) for row in shown],
        )
        if len(risky) > len(shown):
            pdf.y += 8
            pdf.text(f"其余 {len(risky) - len(shown)} 条见台账 coverage.csv。", size=9.5, color=MUTED)

    # ---------------- 台账节选 ----------------
    pdf.heading("台账节选")
    if not rows:
        pdf.text("该运行没有台账文件。", size=10, color=MUTED)
    else:
        sample = rows[:60]
        pdf.table(
            ["编号", "章节", "类型", "要求原文", "页"],
            [58, 70, 46, 268, 34],
            [
                [
                    row.get("req_id", ""),
                    _short(row.get("section", ""), 12),
                    row.get("type", ""),
                    _short(row.get("requirement"), 130),
                    str(row.get("page") or "—"),
                ]
                for row in sample
            ],
        )
        if len(rows) > len(sample):
            pdf.y += 8
            pdf.text(f"共 {len(rows)} 条，此处展示前 {len(sample)} 条。", size=9.5, color=MUTED)

    # ---------------- 说明 ----------------
    pdf.heading("生成说明")
    pdf.text(
        "本报告由标书工作台自动生成。判定来自规则引擎与模型双跑投票，"
        "覆盖结论均为「已响应 / 部分响应 / 负偏离 / 未响应 / 未找到 / 人工介入」六类之一。",
        size=9.6,
        color=MUTED,
    )
    pdf.text(
        "「人工介入」表示两票结论不一致或规则无法判定，需审查员复核后再定稿；"
        "「未找到」表示标书中没有检索到相近段落，不等同于已确认缺失。",
        size=9.6,
        color=MUTED,
    )
    pdf.text("本报告不构成法律意见，最终结论以评标委员会认定为准。", size=9.6, color=MUTED)

    return pdf.save()


def _coverage_counts(coverage: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in coverage:
        status = row.get("coverage") or ""
        if status:
            counts[status] = counts.get(status, 0) + 1
    return counts