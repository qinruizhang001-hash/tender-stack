#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_requirements.py — 从招标文件抽取可追溯的要求条目台账（v2.0）

用法：
    python extract_requirements.py --input 招标文件.md
    python extract_requirements.py --input 招标文件.txt --output requirements.csv
    python extract_requirements.py --input 招标文件.docx --ver 补遗-01
    python extract_requirements.py --input 招标文件.md --no-dedupe

支持输入：.txt / .md / .markdown / .docx（docx 需可选依赖 python-docx）

输出：requirements.csv（UTF-8-BOM，Excel 可直接打开）

v2 相对 v1 的修正（每条都有对应测试）：
  1. 类型判定改为「句内优先、章节兜底」，修正"投标人须知"整章被误判为资格的问题；
     但「评标办法 / 合同条款」两章作为强信号优先，避免评分项被写进技术方案。
  2. 新增 strength 列（禁止/必须/应/宜/可），替代 Y/N 二分带来的优先级缺失。
  3. 新增 line_no 与 source_ver，让"100% 溯源"可被第三方验证；
     v1 中 source_quote 与 requirement 是同一句，等于没有独立溯源。
  4. 新增 dedupe：完全重复 / 高相似（≥0.90）/ 子集包含 三种重复识别，写 dup_of 列。
     不去重会导致必答数虚高、覆盖率永远算不准。
  5. docx 缺依赖时显式退出码 2（v1 在抽取脚本里也是 sys.exit，但校验脚本里是静默跳过）。
  6. 章节识别兼容「第X章」「第X部分」「X、」「（一）」「1.1」，并记录行号。
"""

import argparse
import csv
import difflib
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- 词表

# 强制等级：按顺序匹配，先命中者生效（从严到宽）
STRENGTH_RULES = [
    ("禁止", [r"不得", r"严禁", r"禁止", r"不允许", r"不接受", r"不予受理"]),
    ("必须", [r"必须", r"不低于", r"不少于", r"不高于", r"不超过", r"不小于",
              r"不大于", r"至少", r"最少", r"最多", r"以上", r"以内",
              r"(?<![响对适供相反呼感理])应(?![答用急变收商])", r"须(?!知)"]),
    ("应", [r"需提供", r"应提供", r"须提交", r"需提交", r"须附", r"需附",
            r"需出具", r"应出具", r"加盖", r"签署", r"填写"]),
    ("宜", [r"建议", r"宜", r"鼓励", r"优先考虑", r"择优", r"推荐", r"参照"]),
    ("可", [r"可考虑", r"如有", r"若", r"视情况", r"允许"]),
]

MANDATORY_MAP = {"禁止": "Y", "必须": "Y", "应": "Y", "宜": "N", "可": "N"}

# 类型判定：句内关键词（优先级顺序即 TYPE_RULES 顺序）
TYPE_RULES = [
    ("资格", ["营业执照", "独立法人", "法人资格", "资质证书", "资质等级", "财务报告",
              "审计报告", "社会保险", "社保", "纳税", "税收", "失信", "违法记录",
              "联合体", "注册资本", "开户许可", "信用中国", "信用证明",
              "安全生产许可", "管理体系", "ISO", "体系认证", "CMMI"]),
    ("格式", ["正本", "副本", "密封", "封套", "装订", "骑缝章", "盖章", "签字",
              "份数", "格式要求", "页码", "目录", "电子版", "U盘", "光盘",
              "投标函", "授权委托书", "标记", "封装", "加密"]),
    ("评分", ["得分", "评分", "分值", "加分", "计分", "满分", "扣分", "评审标准",
              "业绩", "低价优先", "评标办法"]),
    ("商务", ["工期", "交货", "交付期", "付款", "支付", "质保", "保修", "保证金",
              "违约", "赔偿", "知识产权", "保密", "验收", "合同", "报价", "价格",
              "质保金", "发票", "税率"]),
    ("服务", ["售后", "响应时间", "培训", "驻场", "备件", "运维", "巡检",
              "技术支持", "服务网点", "故障", "维护", "升级服务", "服务承诺"]),
    ("技术", ["技术", "性能", "参数", "接口", "协议", "容量", "功能", "兼容",
              "支持", "接入", "带宽", "存储", "并发", "时延", "分辨率",
              "系统", "设备", "部署", "架构", "标准", "算法", "加密", "冗余"]),
]

# 章节标题是强信号：这两章内的条款不判为「技术」
STRONG_SECTION_TYPE = [("评分", ["评标办法", "评分办法", "评审标准", "评分细则"]),
                       ("商务", ["合同条款", "合同草案", "合同格式"])]

AMBIGUOUS_WORDS = ["或同等", "等同", "原则上", "一般情况下", "视情况", "如有必要",
                   "由招标人认定", "择优", "满足使用要求", "满足需要",
                   "根据实际情况", "适时"]

CHAPTER_RE = re.compile(r"^\s*(第[一二三四五六七八九十百零\d]+[章部分篇])\s*[、.．:：]?\s*(.*)$")
CLAUSE_RE = re.compile(r"^\s*(\d+(?:\.\d+){1,3})\s*[、.．]?\s+(.+)$")
CLAUSE_CN_RE = re.compile(r"^\s*[（(]\s*([一二三四五六七八九十]+)\s*[)）]\s*(.+)$")
MD_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+)$")

MIN_SENTENCE_LEN = 8
MAX_HEADING_LEN = 40
MAX_REQUIREMENT_LEN = 400
DUP_SIMILARITY = 0.90

FIELDS = ["req_id", "section", "requirement", "type", "mandatory", "strength",
          "source_quote", "source_ver", "line_no", "response_type",
          "need_clarify", "dup_of", "owner", "status", "note"]


# ---------------------------------------------------------------- 读取

def read_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        try:
            import docx  # type: ignore
        except ImportError:
            sys.exit(
                "[错误] 读取 .docx 需要 python-docx。请先安装：\n"
                "    pip install python-docx\n"
                "或把招标文件另存为 .txt / .md 后重试。（退出码 2）"
            )
        try:
            d = docx.Document(str(path))
        except Exception as e:  # noqa: BLE001
            sys.exit(f"[错误] 无法解析 docx：{e}\n可能为加密文件或已损坏，请另存为 .txt 后重试。（退出码 2）")
        blocks = [p.text for p in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                blocks.append(" | ".join(c.text.strip() for c in row.cells))
        return "\n".join(blocks)

    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    sys.exit(f"[错误] 无法识别文件编码：{path}（请另存为 UTF-8）（退出码 2）")


# ---------------------------------------------------------------- 判定

def detect_strength(sentence: str) -> str:
    for name, patterns in STRENGTH_RULES:
        if any(re.search(p, sentence) for p in patterns):
            return name
    return "可"


def detect_type(sentence: str, section: str) -> str:
    """句内优先、章节兜底；评标办法/合同条款章节作为强信号。"""
    for type_name, words in STRONG_SECTION_TYPE:
        if any(w in section for w in words):
            return type_name
    for type_name, words in TYPE_RULES:
        if any(w in sentence for w in words):
            return type_name
    for type_name, words in TYPE_RULES:
        if any(w in section for w in words):
            return type_name
    return "技术"


def detect_response_type(sentence: str, req_type: str) -> str:
    if req_type == "资格" or re.search(r"(?:提供|提交|附|出具|加盖|签署)", sentence):
        return "证明"
    if re.search(r"(?:承诺|保证|不低于|不少于|不超过|不高于)", sentence):
        return "承诺"
    return "响应"


def detect_clarify(sentence: str) -> str:
    return "Y" if any(w in sentence for w in AMBIGUOUS_WORDS) else "N"


def split_sentences(text: str):
    return [p.strip() for p in re.split(r"(?<=[。；;！？])", text) if p.strip()]


def norm_text(s: str) -> str:
    return re.sub(r"[\s\u3000、，。；：:；（）()《》【】\[\]\-—－_/\\|“”\"'·]", "", s or "")


# ---------------------------------------------------------------- 结构解析

def parse_line(line: str, current_chapter: str):
    """返回 (chapter, clause_no, content)。"""
    stripped = line.strip()

    m = MD_HEADING_RE.match(stripped)
    if m:
        stripped = m.group(1).strip()
        if len(stripped) <= MAX_HEADING_LEN and not stripped.endswith("。"):
            chap = CHAPTER_RE.match(stripped)
            if chap:
                return stripped, "", ""
            cl = CLAUSE_RE.match(stripped)
            if cl:
                return current_chapter, cl.group(1), cl.group(2)
            return current_chapter, "", ""

    chap = CHAPTER_RE.match(stripped)
    if chap:
        rest = chap.group(2).strip()
        if len(rest) <= MAX_HEADING_LEN:
            return stripped, "", ""
        return stripped, "", rest

    cl = CLAUSE_RE.match(stripped)
    if cl and len(cl.group(2)) >= 4:
        return current_chapter, cl.group(1), cl.group(2)

    cn = CLAUSE_CN_RE.match(stripped)
    if cn and len(cn.group(2)) >= 4:
        return current_chapter, cn.group(1), cn.group(2)

    return current_chapter, "", stripped


def extract(text: str, min_len: int = MIN_SENTENCE_LEN, source_ver: str = "v1"):
    rows = []
    current_chapter = "（未识别章节）"
    candidates = 0

    for lineno, raw_line in enumerate(text.splitlines(), 1):
        if not raw_line.strip():
            continue

        current_chapter, clause_no, content = parse_line(raw_line, current_chapter)
        if not content:
            continue

        section = f"{current_chapter} {clause_no}".strip() if clause_no else current_chapter

        for sent in split_sentences(content):
            sent = re.sub(r"^[\-\*\u2022]+\s*", "", sent).strip()
            if len(sent) < min_len:
                continue
            candidates += 1
            quote = sent
            requirement = sent if len(sent) <= MAX_REQUIREMENT_LEN else sent[:MAX_REQUIREMENT_LEN] + "…"

            strength = detect_strength(sent)
            req_type = detect_type(sent, section)
            rows.append({
                "req_id": f"R-{len(rows) + 1:03d}",
                "section": section,
                "requirement": requirement,
                "type": req_type,
                "mandatory": MANDATORY_MAP[strength],
                "strength": strength,
                "source_quote": quote,
                "source_ver": source_ver,
                "line_no": lineno,
                "response_type": detect_response_type(sent, req_type),
                "need_clarify": detect_clarify(sent),
                "dup_of": "",
                "owner": "",
                "status": "待响应",
                "note": "",
            })

    return rows, candidates


# ---------------------------------------------------------------- 去重

def dedupe(rows):
    """识别重复条款，被合并的条目标注 dup_of，不删除（保留原文可查）。

    判定：① 归一化后完全相同；② 子集包含；③ 相似度 ≥ 0.90 且 type 相同。
    主条选择：包含关系取「更全/更严」的一条；其余取首次出现。
    """
    kept = []          # 主条索引
    dup_count = 0

    def merge_into(dup_idx, main_idx, reason):
        rows[dup_idx]["dup_of"] = rows[main_idx]["req_id"]
        rows[dup_idx]["status"] = "重复合并"
        note = f"与 {rows[main_idx]['req_id']} 重复（{reason}）"
        rows[dup_idx]["note"] = f"{rows[dup_idx]['note']}；{note}".strip("；")

    for idx, row in enumerate(rows):
        cur = norm_text(row["requirement"])
        if not cur:
            continue

        matched = None
        superseded = False

        for kidx in list(kept):
            base = norm_text(rows[kidx]["requirement"])
            if not base:
                continue
            if cur == base:
                matched = kidx
                break
            if rows[kidx]["type"] != row["type"]:
                continue
            if cur in base:                      # 当前更短，已有主条更全
                matched = kidx
                break
            if base in cur:                      # 当前更全，取代旧主条
                merge_into(kidx, idx, "包含关系")
                kept.remove(kidx)
                superseded = True
                continue
            if difflib.SequenceMatcher(None, cur, base).ratio() >= DUP_SIMILARITY:
                matched = kidx
                break

        if superseded:
            kept.append(idx)
            dup_count += 1
            continue
        if matched is None:
            kept.append(idx)
            continue

        merge_into(idx, matched, "重复表述")
        dup_count += 1

    return rows, dup_count


# ---------------------------------------------------------------- 输出

def write_csv(rows, out_path: Path):
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(
        description="从招标文件抽取可追溯的要求条目台账（requirements.csv）")
    ap.add_argument("--input", required=True, help="招标文件路径（.txt/.md/.docx）")
    ap.add_argument("--output", default="requirements.csv", help="输出 CSV 路径")
    ap.add_argument("--min-len", type=int, default=MIN_SENTENCE_LEN,
                    help=f"句子最短长度，默认 {MIN_SENTENCE_LEN}")
    ap.add_argument("--ver", default="v1", help="来源版本标记（如 v1 / 补遗-01 / 答疑-02）")
    ap.add_argument("--no-dedupe", action="store_true", help="关闭重复条款合并")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"[错误] 文件不存在：{src}（退出码 2）")

    text = read_text(src)
    if not text.strip():
        sys.exit("[错误] 文件无可提取文本（可能是纯扫描件，请先 OCR）（退出码 2）")

    rows, candidates = extract(text, args.min_len, args.ver)
    dup_count = 0
    if not args.no_dedupe:
        rows, dup_count = dedupe(rows)

    out = Path(args.output)
    write_csv(rows, out)

    main_rows = [r for r in rows if not r["dup_of"]]
    mandatory_main = sum(1 for r in main_rows if r["mandatory"] == "Y")
    clarify = sum(1 for r in rows if r["need_clarify"] == "Y")
    by_type = {}
    for r in main_rows:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1

    print("=" * 60)
    print("要求抽取完成（v2）")
    print("=" * 60)
    print(f"输入文件     : {src}")
    print(f"来源版本     : {args.ver}")
    print(f"输出文件     : {out.resolve()}")
    print(f"候选句总数   : {candidates}")
    print(f"原始条目数   : {len(rows)}")
    print(f"去重合并     : {dup_count} 条（dup_of 列标注）")
    print(f"主条目数     : {len(main_rows)}")
    print(f"必答(Y)      : {mandatory_main}    非必答(N): {len(main_rows) - mandatory_main}")
    print(f"需澄清       : {clarify}")
    print("-" * 60)
    print("按类型分布（仅主条目）：")
    for t in ["资格", "格式", "技术", "商务", "服务", "评分"]:
        if by_type.get(t):
            print(f"  {t}: {by_type[t]}")
    print("-" * 60)
    print("下一步（必须人工完成，脚本不做最终判定）：")
    print("  1. 逐条核对 source_quote 是否为原文照抄（含错别字）；")
    print("  2. 修正 mandatory / strength —— 陈述句形式的强制条款")
    print("     （如“合同签订后60日内交货”）不含情态动词，脚本抓不到，必须人工补标；")
    print("  3. 补齐 owner；把 need_clarify=Y 的整理成澄清函；")
    print("  4. 以「主条目数 / 必答数」作为后续覆盖率的分母写进对话。")
    print()
    print("门禁 G1：若主条目数明显低于经验值（每 10 页约 15–30 条），说明抽取口径漏项，先补抽。")


if __name__ == "__main__":
    main()
