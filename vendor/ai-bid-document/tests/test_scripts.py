#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai-bid-document 脚本回归测试（零依赖，python -m unittest 直接跑）

覆盖 v2 修复的关键缺陷，每个用例对应一个曾经真实存在的 bug：
  T01 中文语境下 \b 导致 R-XXX 匹配失效（覆盖率恒为 0）
  T02 覆盖声明行被当作"已覆盖"，正文没写也算通过
  T03 docx 缺依赖时静默生成 "[跳过]" 参与扫描
  T04 章节编号只认 Markdown "#"，docx 转文本后检查恒为空
  T05 类型判定"章节优先"导致资格类条款被误判为技术
  T06 重复条款不去重，必答数虚高
  T07 复用污染（旧项目编号）未被检出
  T08 评分点无承载章节/无证据未被拦截
  T09 输入错误应返回退出码 2 而不是 1
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import extract_requirements as ex  # noqa: E402
import validate_bid as vb  # noqa: E402


def write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class T01_IDRegex(unittest.TestCase):
    def test_match_after_chinese(self):
        # "条目R-001" 中 R 前是中文，\b 版正则匹配不到
        self.assertEqual(vb.R_ID_RE.findall("覆盖条目：R-001, R-002"), ["R-001", "R-002"])
        self.assertEqual(vb.R_ID_RE.findall("见条目R-003（技术要求）"), ["R-003"])

    def test_no_false_positive(self):
        self.assertEqual(vb.R_ID_RE.findall("AR-0011 与 XR-001"), [])


class T02_Coverage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.req = self.dir / "requirements.csv"
        write(self.req, "req_id,section,requirement,type,mandatory\n"
                        "R-001,a,独立法人资格,资格,Y\n"
                        "R-002,b,支持国密算法,技术,Y\n")
        self.rows = vb.load_req(self.req)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_declared_is_blocker(self):
        bid = {"03-技术标.md": "# 第3章 技术方案\n\n> 覆盖条目：R-001, R-002\n\n"
                              "R-001 我们具备独立法人资格并已附营业执照。\n"}
        findings, stats = vb.check_coverage(bid, self.rows)
        rules = [f.rule for f in findings]
        self.assertIn("COV-02", rules)          # R-002 只在声明行
        self.assertNotIn("COV-01", rules)       # R-001 正文有落点
        self.assertEqual(stats["coverage"]["percent"], 50.0)

    def test_both_covered(self):
        bid = {"a.md": "> 覆盖条目：R-001, R-002\n\nR-001 具备独立法人资格。\nR-002 支持 SM2/SM3/SM4。\n"}
        findings, stats = vb.check_coverage(bid, self.rows)
        self.assertEqual([f.rule for f in findings], [])
        self.assertEqual(stats["coverage"]["percent"], 100.0)

    def test_missing_is_cov01(self):
        findings, _ = vb.check_coverage({"a.md": "什么都没写。"}, self.rows)
        self.assertEqual({f.rule for f in findings}, {"COV-01"})


class T03_DocxDependency(unittest.TestCase):
    def test_docx_never_silently_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "broken.docx"
            p.write_bytes(b"not a real docx")
            with self.assertRaises(vb.InputError):
                vb.read_any(p)


class T04_Numbering(unittest.TestCase):
    def test_chinese_chapter_detected(self):
        text = "# 第一章 总则\n内容\n# 第三章 技术方案\n内容\n"
        findings = vb.check_numbering({"a.md": text})
        self.assertTrue(any("跳号" in f.message for f in findings))

    def test_duplicate_detected(self):
        text = "第二章 商务\n内容\n第二章 商务\n内容\n"
        findings = vb.check_numbering({"a.md": text})
        self.assertTrue(any("重复" in f.message for f in findings))

    def test_continuous_ok(self):
        text = "第一章 总则\n第二章 商务\n第三章 技术\n"
        self.assertEqual(vb.check_numbering({"a.md": text}), [])


class T05_TypeDetection(unittest.TestCase):
    def test_sentence_beats_section(self):
        # 写在"技术要求"章里的资格条款，应判资格而不是技术
        self.assertEqual(ex.detect_type("投标人须提供有效的营业执照副本", "第三章 技术要求"), "资格")

    def test_scoring_chapter_is_strong_signal(self):
        self.assertEqual(ex.detect_type("技术方案完整性得10分", "第二章 评标办法"), "评分")

    def test_section_fallback(self):
        self.assertEqual(ex.detect_type("上述内容应完整提交", "第六章 格式要求"), "格式")


class T06_Dedupe(unittest.TestCase):
    def test_identical_merged(self):
        rows = [
            {"req_id": "R-001", "requirement": "提供有效的营业执照副本", "type": "资格",
             "dup_of": "", "status": "待响应", "note": ""},
            {"req_id": "R-002", "requirement": "提供有效的营业执照副本。", "type": "资格",
             "dup_of": "", "status": "待响应", "note": ""},
        ]
        rows, n = ex.dedupe(rows)
        self.assertEqual(n, 1)
        self.assertEqual(rows[1]["dup_of"], "R-001")
        self.assertEqual(rows[1]["status"], "重复合并")

    def test_superset_becomes_main(self):
        rows = [
            {"req_id": "R-001", "requirement": "提供三年质保", "type": "商务",
             "dup_of": "", "status": "待响应", "note": ""},
            {"req_id": "R-002", "requirement": "提供三年质保及7×24小时响应", "type": "商务",
             "dup_of": "", "status": "待响应", "note": ""},
        ]
        rows, n = ex.dedupe(rows)
        self.assertEqual(n, 1)
        self.assertEqual(rows[0]["dup_of"], "R-002")   # 更全的成为主条
        self.assertEqual(rows[1]["dup_of"], "")


class T07_Pollution(unittest.TestCase):
    def test_other_project_no(self):
        files = {"a.md": "项目编号：KM-2026-0901\n"}
        findings = vb.check_pollution(files, "新项目", "KM-2026-1234")
        self.assertTrue(any(f.rule == "POLL-01" for f in findings))

    def test_same_project_no_ok(self):
        files = {"a.md": "项目编号：KM-2026-1234\n"}
        findings = vb.check_pollution(files, "新项目", "KM-2026-1234")
        self.assertFalse(any(f.rule == "POLL-01" for f in findings))

    def test_internal_phrase(self):
        findings = vb.check_pollution({"a.md": "按上次标书模板改写"}, "", "")
        self.assertTrue(any(f.rule == "POLL-03" for f in findings))


class T08_Score(unittest.TestCase):
    def test_missing_chapter_and_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.csv"
            write(p, "score_id,score_item,score_max,score_rule,evidence,chapter,capability,note\n"
                     "S-001,技术方案,10,完整性,方案正文,,高,\n"
                     "S-002,业绩,8,每项2分,,4.2,中,\n")
            findings, stats = vb.check_score(p)
            rules = [f.rule for f in findings]
            self.assertIn("SCORE-01", rules)
            self.assertIn("SCORE-02", rules)
            self.assertEqual(stats["score_total"], 18.0)

    def test_score05_missing_rule_text(self):
        """score_rule 为空 → 无法核对是否漏拆（规范写了这条，代码曾经没实现）"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.csv"
            write(p, "score_id,score_item,score_max,score_rule,evidence,chapter,capability,note\n"
                     "S-001,技术方案,10,,方案正文,3.1,高,\n")
            findings, _ = vb.check_score(p)
            self.assertIn("SCORE-05", [f.rule for f in findings])

    def test_score05_too_coarse(self):
        """一条评分点平均吃 8 分以上且条数偏少 → 拆分过粗"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.csv"
            rows = "".join(f"S-{i:03d},项{i},10,原文{i},证据,3.{i},高,\n" for i in range(1, 6))
            write(p, "score_id,score_item,score_max,score_rule,evidence,chapter,capability,note\n" + rows)
            findings, stats = vb.check_score(p)
            self.assertIn("SCORE-05", [f.rule for f in findings])
            self.assertEqual(stats["score_points"], 5)

    def test_score05_silent_when_fine(self):
        """拆得够细且原文齐全时不应触发 SCORE-05"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.csv"
            rows = "".join(f"S-{i:03d},项{i},2,原文{i}得2分,证据,3.{i},高,\n" for i in range(1, 16))
            write(p, "score_id,score_item,score_max,score_rule,evidence,chapter,capability,note\n" + rows)
            findings, _ = vb.check_score(p)
            self.assertNotIn("SCORE-05", [f.rule for f in findings])


class T09_ExitCodes(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / "validate_bid.py"), *args],
                              capture_output=True, text=True)

    def test_missing_path_is_2(self):
        r = self.run_cli("--bid", "不存在的路径", "--req", "x.csv")
        self.assertEqual(r.returncode, 2)

    def test_empty_dir_is_2(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_cli("--bid", d, "--req", "x.csv")
            self.assertEqual(r.returncode, 2)

    def test_blocker_is_1_and_json_written(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write(root / "bid" / "a.md", "# 第一章 总则\n[待确认: 参数]\n")
            write(root / "requirements.csv", "req_id,section,requirement,type,mandatory\n"
                                            "R-001,a,要求一,技术,Y\n")
            out = root / "validate.json"
            r = self.run_cli("--bid", str(root / "bid"), "--req", str(root / "requirements.csv"),
                             "--report", str(out))
            self.assertEqual(r.returncode, 1)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["conclusion"], "不通过")
            self.assertTrue(payload["findings"][0]["id"].startswith("F-"))
            self.assertIn("rule", payload["findings"][0])


class T10_ExtractSmoke(unittest.TestCase):
    def test_extract_and_count(self):
        text = ("第一章 投标人须知\n"
                "投标人须为独立法人，并提供有效的营业执照副本复印件。\n"
                "第二章 技术要求\n"
                "系统应支持不少于2000路视频接入。\n"
                "系统应支持GB/T 28181标准联网。\n")
        rows, cands = ex.extract(text)
        self.assertGreaterEqual(cands, 3)
        self.assertEqual(rows[0]["type"], "资格")
        self.assertEqual(rows[0]["mandatory"], "Y")
        self.assertEqual(rows[0]["strength"], "必须")
        self.assertTrue(rows[0]["source_quote"].endswith("。"))
        self.assertGreater(rows[1]["line_no"], 0)

    def test_strength_levels(self):
        self.assertEqual(ex.detect_strength("投标人不得转包"), "禁止")
        self.assertEqual(ex.detect_strength("建议提供备件清单"), "宜")
        self.assertEqual(ex.detect_strength("可考虑增加冗余"), "可")


if __name__ == "__main__":
    unittest.main(verbosity=2)
