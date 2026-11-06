#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""file_meta_check.py — 标书文件的物理属性校验（纯代码，不走模型）

暗标场景下，文件自带的元数据就是"指纹"：作者、公司、最后修改者、修订记录，
任何一项暴露都能把投标人认出来。这个模块只读文件本身，不解析正文、不调模型。

检查项
------
1. 元数据泄露：Author / Company / LastModifiedBy 只要是有意义的非空值 → 高危
   （"Administrator"、"User"、"微软用户" 这类默认词不算泄露）
2. 修订记录：Word 的 w:ins / w:del / w:moveTo / w:moveFrom，或开启了修订跟踪 → 高危
3. 批注：word/comments.xml 存在 → 计入详情（同样会泄露修改痕迹）
4. 页数：PDF 直接读；Word 读 docProps/app.xml 的上次保存值（可能过期，会标注）

返回
----
"高危-暗标泄露" / "高危-含修订记录" / "通过" / "需人工复核"
"""
from __future__ import annotations

import argparse
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

LEAK = "高危-暗标泄露"
REVISION = "高危-含修订记录"
PASS = "通过"
REVIEW = "需人工复核"

# 默认词 / 无意义值：命中就不算泄露
_PLACEHOLDERS = {
    "user", "administrator", "admin", "owner", "author", "unknown", "default",
    "test", "pc", "windows", "windows user", "microsoft office user",
    "microsoft office", "office", "guest", "none", "null", "n/a", "na",
    "用户", "微软用户", "作者", "管理员", "无", "未知", "匿名", "公司",
}
_MEANINGLESS_RE = re.compile(r"^[\W_\d]+$")          # 只有数字/符号

_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ep": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
}


@dataclass
class FileReport:
    path: str
    kind: str                      # pdf / docx
    pages: Optional[int] = None
    pages_note: str = ""
    leaks: list[dict[str, str]] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    revisions: dict[str, Any] = field(default_factory=dict)
    status: str = PASS
    details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        data = {
            "path": self.path, "kind": self.kind, "status": self.status,
            "pages": self.pages, "pages_note": self.pages_note,
            "leaks": self.leaks, "metadata": self.metadata,
            "revisions": self.revisions, "details": self.details,
        }
        return data


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        for encoding in ("utf-8", "utf-16", "latin-1"):
            try:
                return value.decode(encoding).strip()
            except UnicodeDecodeError:
                continue
        return ""
    return str(value).strip()


def is_meaningful(value: str) -> bool:
    """空值、默认词、纯符号、单字符都不算泄露。"""
    text = _clean(value)
    if not text or len(text) <= 1:
        return False
    if text.lower() in _PLACEHOLDERS:
        return False
    return not _MEANINGLESS_RE.match(text)


# ---------------------------------------------------------------- PDF

def _pdf_report(path: Path) -> FileReport:
    from pypdf import PdfReader

    report = FileReport(path=str(path), kind="pdf")
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                report.status = REVIEW
                report.details.append("PDF 已加密，无法读取元数据")
                return report
        report.pages = len(reader.pages)
    except Exception as error:  # noqa: BLE001
        report.status = REVIEW
        report.details.append(f"PDF 解析失败：{type(error).__name__}")
        return report

    raw = {}
    try:
        info = reader.metadata or {}
        for key, value in dict(info).items():
            raw[_clean(key).lstrip("/")] = _clean(value)
    except Exception:  # noqa: BLE001
        pass

    report.metadata = dict(raw)
    # PDF 没有 Company / LastModifiedBy 的标准字段，尽量从同义键与 XMP 补
    candidates = {
        "Author": raw.get("Author", ""),
        "Company": raw.get("Company", "") or raw.get("Publisher", ""),
        "LastModifiedBy": raw.get("LastModifiedBy", "") or raw.get("ModifiedBy", ""),
    }
    try:
        xmp = reader.xmp_metadata
        if xmp is not None:
            creators = xmp.dc_creator or []
            if isinstance(creators, str):
                creators = [creators]
            candidates["Author"] = candidates["Author"] or _clean(creators[0] if creators else "")
    except Exception:  # noqa: BLE001
        pass

    for field_name, value in candidates.items():
        if is_meaningful(value):
            report.leaks.append({"field": field_name, "value": value})
    return report


# ---------------------------------------------------------------- DOCX

def _read_docx_part(archive: zipfile.ZipFile, name: str) -> Optional[bytes]:
    try:
        return archive.read(name)
    except KeyError:
        return None


def _docx_core_props(archive: zipfile.ZipFile) -> dict[str, str]:
    from lxml import etree

    data = _read_docx_part(archive, "docProps/core.xml")
    if not data:
        return {}
    root = etree.fromstring(data)
    mapping = {
        "Author": f"{{{_NS['dc']}}}creator",
        "LastModifiedBy": f"{{{_NS['cp']}}}lastModifiedBy",
        "Title": f"{{{_NS['dc']}}}title",
        "Subject": f"{{{_NS['dc']}}}subject",
        "Comments": f"{{{_NS['dc']}}}description",
        "Revision": f"{{{_NS['cp']}}}revision",
    }
    result = {}
    for name, tag in mapping.items():
        node = root.find(tag)
        if node is not None and node.text:
            result[name] = _clean(node.text)
    return result


def _docx_app_props(archive: zipfile.ZipFile) -> dict[str, str]:
    from lxml import etree

    data = _read_docx_part(archive, "docProps/app.xml")
    if not data:
        return {}
    root = etree.fromstring(data)
    result = {}
    for name, tag in {"Company": "Company", "Manager": "Manager", "Pages": "Pages",
                      "Application": "Application", "AppVersion": "AppVersion"}.items():
        # 取第一个非空值：有些文档会残留多个同名节点（如 <Company/> + <Company>真实值</Company>）
        for node in root.findall(f"{{{_NS['ep']}}}{tag}"):
            if node.text and node.text.strip():
                result[name] = _clean(node.text)
                break
    return result


def _docx_revisions(archive: zipfile.ZipFile) -> dict[str, Any]:
    from lxml import etree

    result = {"insertions": 0, "deletions": 0, "moves": 0, "comments": 0,
              "tracking_enabled": False, "authors": []}
    document = _read_docx_part(archive, "word/document.xml")
    if document:
        root = etree.fromstring(document)
        for tag, key in (("ins", "insertions"), ("del", "deletions"),
                         ("moveFrom", "moves"), ("moveTo", "moves")):
            nodes = root.findall(f".//{{{_NS['w']}}}{tag}")
            result[key] += len(nodes)
            for node in nodes:
                author = node.get(f"{{{_NS['w']}}}author")
                if author and author not in result["authors"]:
                    result["authors"].append(author)

    settings = _read_docx_part(archive, "word/settings.xml")
    if settings:
        root = etree.fromstring(settings)
        result["tracking_enabled"] = root.find(f".//{{{_NS['w']}}}trackChanges") is not None

    comments = _read_docx_part(archive, "word/comments.xml")
    if comments:
        root = etree.fromstring(comments)
        result["comments"] = len(root.findall(f".//{{{_NS['w']}}}comment"))
    return result


def _docx_report(path: Path) -> FileReport:
    report = FileReport(path=str(path), kind="docx")
    try:
        with zipfile.ZipFile(path) as archive:
            core = _docx_core_props(archive)
            app = _docx_app_props(archive)
            revisions = _docx_revisions(archive)
    except (zipfile.BadZipFile, OSError) as error:
        report.status = REVIEW
        report.details.append(f"DOCX 打开失败：{type(error).__name__}")
        return report

    report.metadata = {**core, **app}
    report.revisions = revisions

    for field_name in ("Author", "Company", "LastModifiedBy"):
        value = core.get(field_name) or app.get(field_name) or ""
        if is_meaningful(value):
            report.leaks.append({"field": field_name, "value": value})

    pages_raw = app.get("Pages", "")
    if pages_raw.isdigit() and int(pages_raw) > 0:
        report.pages = int(pages_raw)
        report.pages_note = "取自 docProps/app.xml 的上次保存值，可能与实际渲染页数不同"
    else:
        report.pages_note = "文件内未记录页数（Word 的页数由渲染决定），需打开确认"
    return report


# ---------------------------------------------------------------- 汇总

def check_file(path: str | Path) -> FileReport:
    file_path = Path(path)
    if not file_path.exists():
        report = FileReport(path=str(file_path), kind="unknown")
        report.status = REVIEW
        report.details.append("文件不存在")
        return report

    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        report = _pdf_report(file_path)
    elif suffix in (".docx", ".docm"):
        report = _docx_report(file_path)
    elif suffix == ".doc":
        report = FileReport(path=str(file_path), kind="doc")
        report.status = REVIEW
        report.details.append("旧版 .doc 无法用 python-docx 读取，请先另存为 .docx")
        return report
    else:
        report = FileReport(path=str(file_path), kind=suffix.lstrip(".") or "unknown")
        report.status = REVIEW
        report.details.append("只支持 PDF / DOCX")
        return report

    revision_hit = (
        report.revisions.get("insertions", 0)
        + report.revisions.get("deletions", 0)
        + report.revisions.get("moves", 0)
    ) > 0

    if report.leaks:
        report.status = LEAK
        fields = "、".join(f"{item['field']}={item['value']}" for item in report.leaks)
        report.details.append(f"元数据可识别投标人：{fields}")
    elif revision_hit or report.revisions.get("comments", 0) > 0:
        report.status = REVISION
        report.details.append(
            f"含修订/批注：插入 {report.revisions.get('insertions', 0)}、"
            f"删除 {report.revisions.get('deletions', 0)}、"
            f"批注 {report.revisions.get('comments', 0)}"
        )
    elif report.revisions.get("tracking_enabled"):
        report.status = REVISION
        report.details.append("已开启修订跟踪（可能下次保存时产生记录）")
    else:
        report.status = PASS

    if report.revisions.get("authors"):
        report.details.append("修订作者：" + "、".join(report.revisions["authors"]))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="标书文件物理属性校验")
    parser.add_argument("path")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args()

    report = check_file(args.path)
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        return
    print(f"{report.status}  {report.path}")
    print(f"  类型 {report.kind} | 页数 {report.pages if report.pages else '未知'} | {report.pages_note}")
    for item in report.leaks:
        print(f"  泄露 {item['field']}: {item['value']}")
    for line in report.details:
        print(f"  · {line}")


if __name__ == "__main__":
    main()
