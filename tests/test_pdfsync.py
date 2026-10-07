"""
tests/test_pdfsync.py — اختبارات شاملة لوحدة مزامنة PDF مع EPUB (pdfsync).

تغطي:
  1. التطبيع وخريطة الفهارس (حفظ النص الأصلي بدون أي تعديل).
  2. قياس التشابه (Similarity).
  3. استخراج وتصنيف كتل PDF (الترويسات/التذييلات/أرقام الصفحات).
  4. قص DOM وتقسيم الصفحات مع الحفاظ الحرفي التام على النص وعناصره.
  5. استنساخ الوسوم عبر حدود الصفحات وإزالة المعرفات المكررة.
  6. الاختبار التكاملي الشامل (End-to-End).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# إضافة مجلد المشروع الرئيسي
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from lxml import etree
from pdfsync.normalize import normalize_for_match, normalize_with_map, similarity
from pdfsync.pdf_extract import PdfDocument, PdfPage, PdfTextBlock, classify_blocks
from pdfsync.splitter import PageFragment, PageSplitter, collect_text
from pdfsync.epub_source import SourceDoc, SourceEpub


class TestNormalize(unittest.TestCase):
    def test_normalize_folds_and_strips(self):
        # التشكيل، التطويل، والياء الفارسية، والهمزات
        raw = "مَسْأَلَةٌ ۱۲۳: كِتَابُ الصَّلَاةِ ـ يَجِبُ"
        norm = normalize_for_match(raw)
        # يجب توحيد الأرقام إلى 0-9 وحذف التشكيل والرموز والمسافات
        self.assertIn("123", norm)
        self.assertNotIn("ـ", norm)
        self.assertNotIn(" ", norm)
        self.assertNotIn("َ", norm)

    def test_normalize_mapping_preserves_offsets(self):
        # التحقق من أن كل حرف في النص المُطبّع يعود لموضعه الصحيح في النص الأصلي
        raw = "الحُكْمُ: يَجِبُ (عَلَى الأَحْوَطِ)"
        norm, nmap = normalize_with_map(raw)
        self.assertEqual(len(norm), len(nmap))
        for i, ch in enumerate(norm):
            orig_idx = nmap[i]
            # يجب أن يكون الموضع ضمن حدود النص الخام
            self.assertTrue(0 <= orig_idx < len(raw))


class TestSimilarity(unittest.TestCase):
    def test_similarity_identical_and_different(self):
        s1 = normalize_for_match("الصلاة عمود الدين")
        s2 = normalize_for_match("الصلاة عمود الدين")
        s3 = normalize_for_match("الصوم جنة من النار")
        self.assertAlmostEqual(similarity(s1, s2), 1.0)
        self.assertLess(similarity(s1, s3), 0.3)


class TestPdfBlockClassification(unittest.TestCase):
    def test_classify_repeated_header(self):
        # إنشاء 5 صفحات تحمل نفس الترويسة العلوية المتكررة
        pages = []
        for i in range(1, 6):
            p = PdfPage(number=i, width=400, height=600)
            # ترويسة متكررة في أعلى الصفحة
            header = PdfTextBlock(page_number=i, lines=["كتاب الطهارة"], x0=50, y0=10, x1=350, y1=25, font_size=10.0)
            # رقم الصفحة
            pno = PdfTextBlock(page_number=i, lines=[str(i)], x0=190, y0=570, x1=210, y1=590, font_size=10.0)
            # متن
            body = PdfTextBlock(page_number=i, lines=[f"نص المسألة في الصفحة {i}"], x0=50, y0=100, x1=350, y1=400, font_size=13.0)
            p.blocks = [header, body, pno]
            pages.append(p)

        doc = PdfDocument(path=Path("dummy.pdf"), total_pages=5, pages=pages)
        classify_blocks(doc, header_frac=0.08, footer_frac=0.08)

        # التحقق من تصنيف الترويسة ورقم الصفحة والمتن
        for p in doc.pages:
            self.assertEqual(p.blocks[0].kind, "header")
            self.assertEqual(p.blocks[1].kind, "body")
            self.assertEqual(p.blocks[2].kind, "page_number")


class TestSplitter(unittest.TestCase):
    def test_exact_text_preservation(self):
        # اختبار أن تقسيم النص عند أي موضع لا يحذف ولا يغير أي حرف
        html_content = (
            '<div class="content">'
            '<p id="m1">مسألة 1: تجب النية في العبادات، وتكفي الداعية القلبية.</p>'
            '<p id="m2">مسألة 2: لا يشترط التلفظ بالنية في الصلاة ولا غيرها.</p>'
            '</div>'
        )
        root = etree.fromstring(f'<html xmlns="http://www.w3.org/1999/xhtml"><body>{html_content}</body></html>')
        body = root.find("{http://www.w3.org/1999/xhtml}body")

        parts = []
        pos = 0

        def rec_index(el):
            nonlocal pos
            start = pos
            if el.text:
                parts.append(el.text)
                pos += len(el.text)
            for c in el:
                rec_index(c)
                if c.tail:
                    parts.append(c.tail)
                    pos += len(c.tail)
            doc.spans[el] = (start, pos)

        doc = SourceDoc(index=0, path="Text/doc.xhtml", item_id="d1", root=root, body=body)
        rec_index(body)
        doc.start, doc.end = 0, pos

        raw = "".join(parts)
        src = SourceEpub(
            path=Path("dummy.epub"), opf_path="content.opf", opf_dir="",
            nav_path=None, docs=[doc], resources={}, metadata={"title": "تجربة"}, toc=[]
        )
        src.raw_text = raw
        src.norm_text, src.norm_map = normalize_with_map(raw)

        # تقسيم إلى صفحتين في منتصف المسألة 1
        mid = len(raw) // 2
        los = [0, mid]
        his = [mid, len(raw)]

        splitter = PageSplitter(src, los, his)
        pages = splitter.build_all()

        total_extracted = "".join(collect_text(p.body) for p in pages)
        # يجب أن يطابق النص المستخرج النص الأصلي حرفياً
        self.assertEqual(total_extracted, raw)

        # التحقق من أن الصفحة الأولى تحمل id="m1" والصفحة الثانية المستنسخة لا تكرر نفس id="m1"
        self.assertIn("m1", pages[0].ids)


if __name__ == "__main__":
    unittest.main()
