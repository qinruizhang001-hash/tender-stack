"""把 REPORT.md + 真实标书台账构建成单文件 HTML 看板。

只依赖标准库。产物：web/index.html（自包含，无外部请求）。
"""

from __future__ import annotations

import csv
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "REPORT.md"
LEDGER = ROOT / "out/armC_real/gated/requirements.csv"
GATE = ROOT / "out/armC_real/gated/gate_report.json"
OUT = ROOT / "web/index.html"


# ---------------------------------------------------------------- markdown

INLINE_CODE = re.compile(r"`([^`]+)`")
BOLD = re.compile(r"\*\*(.+?)\*\*")
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def inline(text: str) -> str:
    placeholders: list[str] = []

    def stash(value: str) -> str:
        placeholders.append(value)
        return f"\x00{len(placeholders) - 1}\x00"

    text = html.escape(text, quote=False)
    text = INLINE_CODE.sub(lambda m: stash(f"<code>{m.group(1)}</code>"), text)
    text = LINK.sub(
        lambda m: stash(
            f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>'
        ),
        text,
    )
    text = BOLD.sub(r"<strong>\1</strong>", text)
    pattern = re.compile(r"\x00(\d+)\x00")
    for _ in range(6):
        if not pattern.search(text):
            break
        text = pattern.sub(lambda m: placeholders[int(m.group(1))], text)
    return text


def split_row(line: str) -> list[str]:
    body = line.strip().strip("|")
    cells: list[str] = []
    current: list[str] = []
    in_code = False
    for char in body:
        if char == "`":
            in_code = not in_code
        if char == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    cells.append("".join(current).strip())
    return cells


def render_markdown(source: str) -> str:
    lines = source.splitlines()
    out: list[str] = []
    index = 0
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            out.append("<p>" + "<br>".join(inline(item) for item in paragraph) + "</p>")
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush()
            index += 1
            block: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                block.append(lines[index])
                index += 1
            index += 1
            out.append("<pre><code>" + html.escape("\n".join(block)) + "</code></pre>")
            continue

        if not stripped:
            flush()
            index += 1
            continue

        if re.fullmatch(r"-{3,}", stripped):
            flush()
            out.append("<hr>")
            index += 1
            continue

        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            flush()
            level = len(heading.group(1))
            title = heading.group(2)
            anchor = re.sub(r"[^\w\u4e00-\u9fa5]+", "-", title).strip("-")
            out.append(
                f'<h{level} id="{anchor}">{inline(title)}</h{level}>'
            )
            index += 1
            continue

        if stripped.startswith("|") and index + 1 < len(lines):
            nxt = lines[index + 1].strip()
            if re.fullmatch(r"\|[\s:\-|]+\|", nxt):
                flush()
                header = split_row(stripped)
                index += 2
                body: list[list[str]] = []
                while index < len(lines) and lines[index].strip().startswith("|"):
                    body.append(split_row(lines[index]))
                    index += 1
                out.append('<div class="table-wrap"><table>')
                out.append(
                    "<thead><tr>"
                    + "".join(
                        f'<th data-col="{html.escape(re.sub(r"[*`]", "", cell))}">'
                        f"{inline(cell)}</th>"
                        for cell in header
                    )
                    + "</tr></thead><tbody>"
                )
                for row in body:
                    cells = []
                    for column, cell in enumerate(row):
                        head = header[column] if column < len(header) else ""
                        label = re.sub(r"[*`]", "", head)
                        cells.append(
                            f'<td data-col="{html.escape(label)}">{inline(cell)}</td>'
                        )
                    out.append("<tr>" + "".join(cells) + "</tr>")
                out.append("</tbody></table></div>")
                continue

        if stripped.startswith(">"):
            flush()
            quote: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote.append(lines[index].strip().lstrip(">").strip())
                index += 1
            out.append("<blockquote>" + inline(" ".join(quote)) + "</blockquote>")
            continue

        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        if bullet:
            flush()
            items: list[str] = []
            while index < len(lines):
                current = lines[index].strip()
                match = re.match(r"^[-*]\s+(.*)$", current)
                if not match:
                    break
                items.append(match.group(1))
                index += 1
            out.append("<ul>" + "".join(f"<li>{inline(i)}</li>" for i in items) + "</ul>")
            continue

        ordered = re.match(r"^(\d+)\.\s+(.*)$", stripped)
        if ordered:
            flush()
            items = []
            while index < len(lines):
                match = re.match(r"^(\d+)\.\s+(.*)$", lines[index].strip())
                if not match:
                    break
                items.append(match.group(2))
                index += 1
            out.append("<ol>" + "".join(f"<li>{inline(i)}</li>" for i in items) + "</ol>")
            continue

        paragraph.append(stripped)
        index += 1

    flush()
    return "\n".join(out)


# ---------------------------------------------------------------- data

def load_ledger() -> list[dict]:
    with LEDGER.open(encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    body = render_markdown(REPORT.read_text(encoding="utf-8"))
    rows = load_ledger()
    gate = json.loads(GATE.read_text(encoding="utf-8"))

    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    stage_rows = []
    for stage in gate.get("stages", []):
        checks = stage.get("checks") or []
        failed = [item for item in checks if not item.get("pass")]
        stage_rows.append(
            {
                "gate": stage.get("gate"),
                "passed": stage.get("passed"),
                "blockers": sum(1 for item in failed if item.get("severity") == "BLOCKER"),
                "warns": sum(1 for item in failed if item.get("severity") == "WARN"),
                "rules": [item.get("rule") for item in failed],
                "details": [item.get("detail") for item in failed],
            }
        )

    template = (ROOT / "web/template.html").read_text(encoding="utf-8")
    document = (
        template.replace("__REPORT_BODY__", body)
        .replace("__LEDGER_JSON__", payload)
        .replace("__GATE_JSON__", json.dumps(stage_rows, ensure_ascii=False))
        .replace("__COUNT__", str(len(rows)))
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(document, encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB), ledger rows {len(rows)}")


if __name__ == "__main__":
    main()
