"""
tests/make_sample_pdf.py — توليد PDF عربي تجريبي من ملف EPUB موجود (لأغراض الاختبار فقط).

يبني ملف PDF حقيقيًا (نص قابل للتحديد وليس صورًا) بتقسيم صفحات مستقل تمامًا عن
تقسيم EPUB، مع ترويسة متكررة ورقم صفحة، لمحاكاة ملفات PDF الحقيقية:

    python tests/make_sample_pdf.py "output/الوجيز في أحكام العبادات.epub" sample.pdf

لا علاقة لهذا الملف بمنطق المزامنة نفسه؛ هو أداة اختبار فقط.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

# إضافة مجلد المشروع الرئيسي إلى sys.path
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

from lxml import etree

FONTS_DIR = Path(r"C:\Windows\Fonts")
XHTML = "{http://www.w3.org/1999/xhtml}"


def epub_body_html(epub_path: Path) -> str:
    """تجميع متن مستندات EPUB (بدون nav) في سلسلة HTML واحدة بترتيب الـ spine الحقيقي."""
    from pdfsync.epub_source import read_epub
    src = read_epub(epub_path)
    parts: list[str] = []
    for doc in src.docs:
        body = doc.body
        inner = (body.text or "")
        for child in body:
            s = etree.tostring(child, encoding="unicode", method="html", with_tail=True)
            s = s.replace(' xmlns="http://www.w3.org/1999/xhtml"', "")
            s = s.replace(' xmlns:epub="http://www.idpf.org/2007/ops"', "")
            inner += s
        parts.append(inner)
    return "\n".join(parts)


def make_pdf(epub_path: Path, pdf_path: Path, page_w: int = 400, page_h: int = 560) -> int:
    html = epub_body_html(epub_path)
    css = (
        "@font-face { font-family: AR; src: url(tahoma.ttf); }"
        "@font-face { font-family: AR; font-weight: bold; src: url(tahoma.ttf); }"
        "body { font-family: AR; direction: rtl; text-align: right; font-size: 13pt; line-height: 1.5; }"
        "h1, h2 { font-family: AR; font-weight: bold; }"
    )
    story = fitz.Story(html=html, user_css=css, archive=fitz.Archive(str(FONTS_DIR)))
    writer = fitz.DocumentWriter(str(pdf_path))
    mediabox = fitz.Rect(0, 0, page_w, page_h)
    where = fitz.Rect(36, 50, page_w - 36, page_h - 50)  # هوامش علوية/سفلية للترويسة والتذييل
    more = True
    n = 0
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
        n += 1
    writer.close()

    # إضافة ترويسة متكررة + رقم الصفحة (ليختبرها كاشف الترويسات)
    doc = fitz.open(str(pdf_path))
    for i, page in enumerate(doc, start=1):
        page.insert_text((140, 30), "Sample Header Book Title", fontsize=9)
        page.insert_text((page_w / 2 - 6, page_h - 22), str(i), fontsize=10)
    doc.saveIncr()  # حفظ تزايدي لتفادي قفل الملف على Windows
    doc.close()
    return n


if __name__ == "__main__":
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    pages = make_pdf(src, dst)
    print(f"PDF created: {dst} ({pages} pages)")
