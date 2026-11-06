"""Synthetic regression for current-document AI hits and issue triage."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))
import run_pipeline as pipeline

def load(name):
    spec = importlib.util.spec_from_file_location(name, KIT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

harvest = load('harvest_ai_words')
candidates = load('scan_candidates')

class RescanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lines = self.root / 'case.lines.txt'
        self.lines.write_text('1\t特殊情形取消合成资格。\n2\t否决投标。\n', encoding='utf-8')
        self.hits = self.root / 'case.hits.json'
        subprocess.run([sys.executable, str(KIT / 'scripts/scan_keywords.py'), str(self.lines)], check=True, capture_output=True)
        self.md = self.root / 'case.工作区.md'
        self.md.write_text('## 商务标·废标\n\n| ID | 条款 | 出处 |\n|---|---|---|\n| D1 | 原始要求 | 行2 |\n\n'
                           '## AI发现疑似判词\n\n| 疑似判词 | 原文摘要 | 出处 | 建议分类 |\n|---|---|---|---|\n'
                           '| 取消合成资格 | 特殊情形取消合成资格 | 行1 | primary/bid_phase |\n', encoding='utf-8')

    def run_verify(self):
        return subprocess.run([sys.executable, str(KIT / 'run_pipeline.py'), 'verify', str(self.md)], capture_output=True, text=True, encoding='utf-8')

    def test_ai_hits_reach_final_coverage_and_repeat(self):
        original = self.hits.read_bytes()
        for _ in range(2):
            result = self.run_verify()
            self.assertEqual(result.returncode, 0, result.stderr)
            coverage = json.loads(self.md.with_suffix('.coverage.json').read_text(encoding='utf-8'))
            self.assertTrue(any(h['word'] == '取消合成资格' for h in coverage['uncovered_detail']))
            self.assertEqual(coverage['in_scope_primary'], 2)
            self.assertEqual(self.hits.read_bytes(), original)

    def test_both_checks_receive_rescan(self):
        calls = []
        def run(cmd, label):
            calls.append(cmd)
            if 'harvest_ai_words.py' in str(cmd[0]):
                return subprocess.run([sys.executable] + list(map(str, cmd)), capture_output=True).returncode == 0
            return True
        with patch.object(pipeline, 'run', side_effect=run):
            pipeline.verify(self.md, None)
        checks = [c for c in calls if 'check_' in str(c[0])]
        self.assertEqual(len(checks), 2)
        for cmd in checks:
            self.assertTrue(any(str(x).endswith('.ai_rescan.hits.json') for x in cmd))

    def test_no_words_clears_stale_artifacts(self):
        self.run_verify()
        rescan = self.root / 'case.ai_rescan.hits.json'
        rescan.write_text('{ stale', encoding='utf-8')
        self.md.write_text('## 商务标·废标\n\n| ID | 条款 | 出处 |\n|---|---|---|\n| D1 | 原始要求 | 行2 |\n', encoding='utf-8')
        self.assertEqual(self.run_verify().returncode, 0)
        self.assertFalse(rescan.exists())
        self.assertFalse((self.root / 'case.pending_words.json').exists())
        coverage = json.loads(self.md.with_suffix('.coverage.json').read_text(encoding='utf-8'))
        self.assertEqual(coverage['in_scope_primary'], 1)

    def test_rescan_failure_stops_export(self):
        with patch.object(harvest, 'SCAN_KW', self.root / 'missing.py'), patch.object(sys, 'argv', ['harvest', str(self.md)]):
            with self.assertRaises(SystemExit) as error:
                harvest.main()
            self.assertNotEqual(error.exception.code, 0)

    def test_original_unique_hits_are_retained(self):
        data = json.loads(self.hits.read_text(encoding='utf-8'))
        data['hits']['primary'].append({'line': 8, 'word': '历史合成词', 'scope': ['bid_phase'], 'text': '历史原文'})
        self.hits.write_text(json.dumps(data), encoding='utf-8')
        path, _ = harvest.rescan_and_diff(self.lines, self.hits, [{'word': '取消合成资格', 'category': 'primary', 'scope': ['bid_phase']}], '.ai_rescan.hits.json')
        merged = json.loads(path.read_text(encoding='utf-8'))
        self.assertTrue(any(h['word'] == '历史合成词' for h in merged['hits']['primary']))
        self.assertEqual(merged['summary']['primary'], len(merged['hits']['primary']))

    def test_candidate_has_bounded_full_phrase(self):
        import re
        text = '费用视为已经包含在投标报价内；其他事项另行说明。'
        matches = [m.group() for p in candidates.PATTERNS for m in re.finditer(p, text)]
        self.assertIn('视为已经包含在投标报价内', matches)
        long = '视为' + '甲' * 100 + '。'
        self.assertFalse(any(re.search(p, long) for p in candidates.PATTERNS[:1]))
        self.lines.write_text('1\t' + text + '\n2\t' + long + '\n', encoding='utf-8')
        empty_kw = self.root / 'empty.json'
        empty_kw.write_text('{"categories": []}', encoding='utf-8')
        result = subprocess.run([sys.executable, str(KIT / 'scripts/scan_candidates.py'), str(self.lines), '--keywords', str(empty_kw)], capture_output=True)
        self.assertEqual(result.returncode, 0)
        words = [c['word'] for c in json.loads((self.root / 'case.candidates.json').read_text(encoding='utf-8'))['candidates']]
        self.assertEqual(words, ['视为已经包含在投标报价内'])

    def test_all_sixteen_issue_phrases(self):
        scan = load('scan_keywords')
        kw = json.loads((KIT / 'data/keywords.json').read_text(encoding='utf-8'))
        cases = [
            ('视为串通投标', 'primary', '视为串通投标'),
            ('不予退还投标保证金', 'primary', '不予退还投标保证金'),
            ('视为放弃投标', 'primary', '视为放弃'),
            ('取消投标资格的', 'primary', '取消投标资格'),
            ('投标保证金不予退还', 'secondary', '不予退还'),
            ('视为在建工程', None, None),
            ('视为投标无效', 'primary', '无效'),
            ('视为放弃解释权力', 'primary', '视为放弃'),
            ('视为逾期送达', 'secondary', '视为逾期送达'),
            ('不予价格扣除优惠', 'secondary', '不予价格扣除优惠'),
            ('视为自动放弃其投标', 'primary', '视为自动放弃其投标'),
            ('视为已经包含在投标报价内', 'secondary', '视为已经包含在投标报价内'),
            ('视为中标后自动放弃', None, None),
            ('视为验收合格', 'contract', '视为验收合格'),
            ('按负偏离处理', 'secondary', '按负偏离处理'),
            ('取消供应商中标人资格', None, None),
        ]
        words = {c['id']: [w['word'] if isinstance(w, dict) else w for w in c.get('words', [])] for c in kw['categories']}
        for i, (phrase, category, word) in enumerate(cases, 1):
            with self.subTest(phrase=phrase):
                hits, _, _ = scan.scan([(i, phrase + '。')], kw)
                if category:
                    self.assertTrue(any(h.get('word') == word for h in hits[category]), hits)
                else:
                    self.assertFalse(any(phrase in ws for ws in words.values()))
                    self.assertFalse(hits['primary'])
        self.assertNotIn('不予退还投标保证金的', words['primary'])

    def test_changed_source_replaces_stale_rescan(self):
        self.assertEqual(self.run_verify().returncode, 0)
        self.lines.write_text('2\t否决投标。\n', encoding='utf-8')
        self.assertEqual(self.run_verify().returncode, 0)
        coverage = json.loads(self.md.with_suffix('.coverage.json').read_text(encoding='utf-8'))
        self.assertFalse(any(h['word'] == '取消合成资格' for h in coverage['uncovered_detail']))

    def test_missing_lines_and_corrupt_hits_fail_without_export(self):
        self.lines.unlink()
        self.assertNotEqual(self.run_verify().returncode, 0)
        self.assertFalse((self.root / 'case.xlsx').exists())
        self.lines.write_text('1\t取消合成资格。\n', encoding='utf-8')
        self.hits.write_text('{broken', encoding='utf-8')
        self.assertNotEqual(self.run_verify().returncode, 0)
        self.assertFalse((self.root / 'case.xlsx').exists())

    def test_strict_remains_explicit(self):
        self.md.write_text(self.md.read_text(encoding='utf-8').replace('| 行2 |', '| 行9 |'), encoding='utf-8')
        self.assertEqual(self.run_verify().returncode, 0)
        rescan = self.root / 'case.ai_rescan.hits.json'
        for script, args in [('check_coverage.py', [rescan, self.md]), ('check_completeness.py', [self.md, '--hits', rescan])]:
            cmd = [sys.executable, str(KIT / 'scripts' / script)] + list(map(str, args))
            self.assertEqual(subprocess.run(cmd, capture_output=True).returncode, 0)
            self.assertNotEqual(subprocess.run(cmd + ['--strict'], capture_output=True).returncode, 0)

    def test_pdf_fallback_retains_text(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        pdf = self.root / 'synthetic.pdf'
        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        text = 'Synthetic non-sensitive tender PDF. All bidders must supply evidence before submission.'
        stream = DecodedStreamObject()
        stream.set_data(('BT /F1 12 Tf 50 700 Td (' + text + ') Tj ET').encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
        writer.write(pdf)
        extract = load('extract_text')
        with patch.object(extract.subprocess, 'run', side_effect=FileNotFoundError):
            lines, tables = extract.extract_pdf(pdf)
        self.assertIn(text, '\n'.join(lines))
        self.assertEqual(tables, [])

if __name__ == '__main__':
    unittest.main()
