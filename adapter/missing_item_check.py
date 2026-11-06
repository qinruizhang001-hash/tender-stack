#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""missing_item_check.py — 投标文件「漏项」检查（纯字符串检索，不调模型）

输入：要求实体列表（如 ['ISO13485', '营业执照', '安全生产许可证']）+ 投标文件全文（按页）。
输出：每条实体是「找到，位于第 X 页附近」还是「废标-漏项」。

为什么不用模型
--------------
漏项是**确定性**判断：文本里有没有这串字，是事实问题。模型说"应该写了吧"
不能作为证据——这条底线一旦松，废标判定就不可复现。

匹配分三层（逐层降级，命中即停）
--------------------------------
1. 规范匹配：全角转半角、忽略大小写、去掉所有空白与常见标点
   → 跨行、跨空格、带不带「-」都能命中（"ISO 13485" ≡ "ISO-13485" ≡ "ISO13485"）
2. 模糊匹配：实体长度 ≥4 时，用前 4 个字符定位候选窗口，窗口内算相似度
   → 容忍 OCR 错字、漏字（阈值默认 0.82）
3. 都不中 → 废标-漏项

性能
----
语料只规范化一次；精确匹配走 str.find（C 层），模糊匹配只在候选窗口上算相似度。
300 页 / 30 万字 / 300 个实体，实测 < 1 秒（见 __main__ 基准）。
"""
from __future__ import annotations

import re
import time
from bisect import bisect_right
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable, Sequence

FOUND = "找到"
MISSING = "废标-漏项"
REVIEW = "需人工复核"

# 规范化时要丢掉的标点（中英文都覆盖，含全角）
_PUNCT = set(" \t\r\n\u3000，。、；：？！“”‘’（）【】《》〈〉…—－-_·.,;:?!\"'()[]{}<>/\\|+*=&@#$%^~`")
_FULLWIDTH = str.maketrans(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
)

_SEP = "\x00"
_PREFIX_LEN = 4          # 模糊匹配的定位前缀长度
_MAX_CANDIDATES = 200    # 单个实体的候选窗口上限，防止高频词拖慢
_SNIPPET_PAD = 12


@dataclass
class ItemResult:
    entity: str
    status: str                      # 找到 / 废标-漏项 / 需人工复核
    page: int | None = None
    pages: list[int] = field(default_factory=list)
    method: str = "none"             # exact / fuzzy / none
    score: float = 0.0
    snippet: str = ""

    @property
    def message(self) -> str:
        if self.status == FOUND:
            pages = self.pages or ([self.page] if self.page else [])
            where = "、".join(f"第{p}页" for p in pages[:3])
            extra = " 等" if len(pages) > 3 else ""
            return f"找到，位于{where}附近{extra}"
        if self.status == REVIEW:
            return "需人工复核"
        return "废标-漏项"


def _iter_normalized(text: str):
    """逐字产出 (规范化字符, 原文字符下标)，用于把命中位置映射回原文。"""
    for index, char in enumerate(text):
        char = char.translate(_FULLWIDTH).lower()
        if char in _PUNCT or char.isspace():
            continue
        yield char, index


def normalize(text: str) -> str:
    return "".join(char for char, _ in _iter_normalized(text))


class MissingItemChecker:
    """一次建索引，多次查询。语料只规范化一次。"""

    def __init__(self, pages: Sequence[str], fuzzy_threshold: float = 0.82):
        self.pages = list(pages)
        self.threshold = fuzzy_threshold
        self._maps: dict[int, list[int]] = {}

        self.page_starts: list[int] = []
        offset = 0
        for page in self.pages:
            self.page_starts.append(offset)
            offset += len(normalize(page)) + len(_SEP)
        self.text = _SEP.join(normalize(page) for page in self.pages)

    # ---------- 位置映射 ----------
    def _page_at(self, position: int) -> int:
        return bisect_right(self.page_starts, position) - 1

    def _page_map(self, page_index: int) -> list[int]:
        if page_index not in self._maps:
            self._maps[page_index] = [index for _, index in _iter_normalized(self.pages[page_index])]
        return self._maps[page_index]

    def _snippet(self, page_index: int, position_in_page: int, length: int) -> str:
        mapping = self._page_map(page_index)
        if not mapping:
            return ""
        start = mapping[min(position_in_page, len(mapping) - 1)]
        end = mapping[min(position_in_page + max(length, 1) - 1, len(mapping) - 1)] + 1
        original = self.pages[page_index]
        left = max(0, start - _SNIPPET_PAD)
        right = min(len(original), end + _SNIPPET_PAD)
        return re.sub(r"\s+", " ", original[left:right]).strip()

    # ---------- 三种匹配 ----------
    def _find_all(self, target: str, limit: int = 20) -> list[int]:
        positions: list[int] = []
        start = 0
        while len(positions) < limit:
            position = self.text.find(target, start)
            if position < 0:
                break
            positions.append(position)
            start = position + 1
        return positions

    def _fuzzy(self, target: str) -> tuple[int, int, float] | None:
        if len(target) < _PREFIX_LEN:
            return None
        prefix = target[:_PREFIX_LEN]
        best: tuple[int, int, float] | None = None
        start = 0
        for _ in range(_MAX_CANDIDATES):
            position = self.text.find(prefix, start)
            if position < 0:
                break
            start = position + 1
            # 只在等长窗口上比相似度：窗口拉长会把 ratio 稀释，导致错一个字就判不出来
            score = 0.0
            for offset in (-2, -1, 0, 1, 2):
                begin = position + offset
                if begin < 0:
                    continue
                window = self.text[begin: begin + len(target)]
                if len(window) < len(target):
                    continue
                ratio = SequenceMatcher(None, target, window, autojunk=False).ratio()
                if ratio > score:
                    score = ratio
            if score >= self.threshold and (best is None or score > best[2]):
                best = (position, len(target), score)
                if score > 0.95:
                    break
        return best

    def check(self, entity: str) -> ItemResult:
        raw = (entity or "").strip()
        target = normalize(raw)
        if not target:
            return ItemResult(entity=raw, status=REVIEW)
        if len(target) < 2:
            # 单字实体模糊匹配没有意义，只做精确匹配
            positions = self._find_all(target)
            if not positions:
                return ItemResult(entity=raw, status=MISSING)
            return self._to_result(raw, target, positions[0], len(target), "exact", 1.0, positions[1:])

        positions = self._find_all(target)
        if positions:
            return self._to_result(raw, target, positions[0], len(target), "exact", 1.0, positions[1:])

        fuzzy = self._fuzzy(target)
        if fuzzy:
            return self._to_result(raw, target, fuzzy[0], fuzzy[1], "fuzzy", fuzzy[2])

        return ItemResult(entity=raw, status=MISSING)

    def _to_result(self, raw: str, target: str, position: int, length: int,
                   method: str, score: float, extra: list[int] | None = None) -> ItemResult:
        page_index = self._page_at(position)
        page_offset = position - self.page_starts[page_index]
        found_pages: list[int] = []
        for spot in [position] + list(extra or [])[:19]:
            start_page = self._page_at(spot)
            end_page = self._page_at(max(spot, spot + length - 1))
            found_pages.extend([start_page + 1, end_page + 1])
        pages = sorted(set(found_pages))[:5]
        return ItemResult(
            entity=raw, status=FOUND, page=pages[0] if pages else page_index + 1, pages=pages,
            method=method, score=round(score, 3),
            snippet=self._snippet(page_index, page_offset, length),
        )

    def check_all(self, entities: Iterable[str]) -> list[ItemResult]:
        return [self.check(entity) for entity in entities]


def split_pages(text: str) -> list[str]:
    """把带分页符的全文拆成页：支持 \\f 和「=== 第N页 ===」两种写法。"""
    if "\f" in text:
        return text.split("\f")
    parts = re.split(r"^\s*(?:=+\s*)?第\s*\d+\s*页\s*(?:=+)?\s*$", text, flags=re.M)
    return [part for part in parts if part.strip()] or [text]


def check_items(entities: Iterable[str], pages: Sequence[str] | str) -> list[ItemResult]:
    """便捷入口：pages 可以直接传全文（自动按分页符拆）。"""
    page_list = split_pages(pages) if isinstance(pages, str) else list(pages)
    return MissingItemChecker(page_list).check_all(entities)


# ---------------------------------------------------------------- 实体抽取

_NEGATED_REQ = re.compile(r"不需要|无需|不要求|免于|不作要求")
# 要求的资质名经常不带书名号：以「后缀」定位，再往前取 10 个汉字
# 「认证证书 / 资质证书」太泛，单列会把「ISO13485认证证书」拆出一个假实体
_BARE_SUFFIXES = ("营业执照", "许可证", "登记证", "执业证")
_FILLER_PREFIX = (
    "投标人", "供应商", "应当", "须", "应", "提供", "具有", "具备", "提交", "附",
    "有效的", "有效", "的", "及", "和", "与", "相关", "复印件", "加盖", "公章",
)


def extract_entities(requirement: str) -> list[str]:
    """从要求里抽资质实体：ISO 号、《书名号》证书、以及裸写的「XX许可证」。"""
    try:  # 避免与 verify_rules 形成模块级循环导入
        from verify_rules import extract_certs
    except ImportError:  # pragma: no cover
        from adapter.verify_rules import extract_certs

    entities = set(extract_certs(requirement))
    if _NEGATED_REQ.search(requirement):
        return sorted(entities)

    for suffix in _BARE_SUFFIXES:
        for match in re.finditer(suffix, requirement):
            head = requirement[max(0, match.start() - 10): match.start()]
            head = re.sub(r"[^\u4e00-\u9fff]", "", head)
            # 「A及B许可证」只取最后一个并列项，否则会把前一个证名拼进来
            head = re.split(r"[及和与]", head)[-1]
            changed = True
            while changed and head:
                changed = False
                for word in _FILLER_PREFIX:
                    if head.startswith(word):
                        head = head[len(word):]
                        changed = True
                        break
            name = head + suffix
            if name == "许可证":         # 裸「许可证」太泛，会把无关句子当成要求
                continue
            if 4 <= len(name) <= 16:
                entities.add(name)

    # 《X》 与裸写 X 同时出现时，只留带书名号的那个，避免「文档没写书名号」被误判漏项
    book_names = {item.strip("《》") for item in entities if item.startswith("《")}
    entities = {item for item in entities if item.startswith("《") or item not in book_names}
    return sorted(entities)


# ---------------------------------------------------------------- 基准 + 自测

def _benchmark() -> None:
    import random
    random.seed(7)
    filler = "本项目要求投标人具备相应资质并按招标文件规定提交证明材料。" * 40
    pages = [filler + f"第{i}页特有内容" for i in range(1, 301)]
    pages[150] += "投标人应提供ISO13485医疗器械质量管理体系认证证书。"
    entities = [f"不存在的实体{i}" for i in range(300)] + ["ISO13485", "营业执照"]

    started = time.perf_counter()
    checker = MissingItemChecker(pages)
    built = time.perf_counter()
    results = checker.check_all(entities)
    finished = time.perf_counter()
    found = sum(1 for item in results if item.status == FOUND)
    print(f"建索引 {built - started:.3f}s | 查询 {len(entities)} 条 {finished - built:.3f}s "
          f"| 命中 {found} | 语料 {sum(len(p) for p in pages):,} 字")


if __name__ == "__main__":
    _benchmark()
    demo_pages = [
        "投标文件封面\n投标人：某某科技有限公司",
        "资格证明文件\n营业执照（副本）复印件\nISO 13485 认证证书",
        "安全生产许可\n证 复印件",
    ]
    for item in check_items(["营业执照", "ISO13485", "安全生产许可证", "税务登记证"], demo_pages):
        print(f"[{item.status}] {item.entity} → {item.message} ({item.method}, {item.score})")
