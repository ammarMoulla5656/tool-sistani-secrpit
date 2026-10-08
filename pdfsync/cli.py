"""
pdfsync/cli.py — واجهة سطر الأوامر لمزامنة PDF مع EPUB.

الاستخدام:
  python main.py sync --pdf book.pdf --epub book.epub
  python main.py sync --pdf book.pdf --epub book.epub -o out.epub --pages 5-120
  python main.py sync --pdf book.pdf --url https://www.sistani.org/arabic/book/13/
  python -m pdfsync --pdf book.pdf --epub book.epub

القاعدة: صفحة N في PDF  =  Text/page-000N.xhtml في EPUB الناتج (النص من EPUB، والصور من PDF، بلا OCR).
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path


def _parse_pages(s: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", s)
    if not m:
        raise argparse.ArgumentTypeError("استخدم الصيغة 5-120 أو رقمًا واحدًا")
    a = int(m.group(1))
    b = int(m.group(2) or a)
    if a < 1 or b < a:
        raise argparse.ArgumentTypeError("نطاق الصفحات غير صالح")
    return a, b


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py sync",
        description="دمج PDF وEPUB لنفس الكتاب: كل صفحة PDF => ملف XHTML مستقل داخل EPUB جديد "
                    "(PDF مصدر الحدود والصور، وEPUB مصدر النص). بلا OCR.",
    )
    p.add_argument("--pdf", type=Path, required=True, help="ملف PDF (يجب أن يحوي طبقة نص؛ لا يُستخدم OCR)")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--epub", type=Path, help="ملف EPUB الأصلي")
    src.add_argument("--url", help="رابط كتاب في الموقع: يُبنى EPUB منه أولًا بالأداة الحالية ثم يُزامَن")
    p.add_argument("--output", "-o", type=Path, help="مسار EPUB الناتج")
    p.add_argument("--pages", type=_parse_pages, help="نطاق صفحات PDF المستهدفة مثل 5-120 (الافتراضي: الكل)")
    p.add_argument("--text-order", choices=["auto", "logical", "reverse_chars", "reverse_words"], default="auto",
                   help="اتجاه نص PDF المستخرج (auto: كشف تلقائي)")
    p.add_argument("--digits", choices=["auto", "logical", "reversed"], default="auto",
                   help="اتجاه الأعداد متعددة الخانات في PDF (auto: كشف تلقائي؛ كثير من ملفات PDF تعكسها)")
    p.add_argument("--images", choices=["auto", "off"], default="auto",
                   help="نقل صور صفحات PDF (الغلاف، اللوحات، الصفحات المصورة) كما هي إلى EPUB (الافتراضي auto)")
    p.add_argument("--image-dpi", type=int, default=170, help="دقة رسم صور الصفحات (170)")
    p.add_argument("--no-fallback-image", action="store_true",
                   help="لا تُدرج صورة صفحة PDF عندما لا يوجد لها نص مقابل في EPUB")
    p.add_argument("--no-notes", action="store_true",
                   help="لا تفصل الحواشي (class=footnote) عن المتن؛ تُعامل كنص عادي")
    p.add_argument("--note-class", action="append", default=[], metavar="CLASS",
                   help="اسم class إضافي يدل على الحواشي في EPUB (يمكن تكراره)")
    p.add_argument("--header-margin", type=float, default=0.12, help="نسبة الشريط العلوي لكشف الترويسة (0.12)")
    p.add_argument("--footer-margin", type=float, default=0.06, help="نسبة الشريط السفلي لكشف التذييل (0.06)")
    p.add_argument("--page-offset", type=int, default=0,
                   help="إزاحة تسمية الصفحة في page-list (تسمية = رقم صفحة PDF + الإزاحة)")
    trim = p.add_mutually_exclusive_group()
    trim.add_argument("--trim-edges", dest="trim_edges", action="store_true", default=None,
                      help="حذف نص EPUB الواقع قبل أول صفحة نصية وبعد آخر صفحة (الافتراضي)")
    trim.add_argument("--keep-edges", dest="trim_edges", action="store_false",
                      help="إبقاء كل نص EPUB (يُلحق بأول/آخر صفحة)")
    p.add_argument("--anchor-len", type=int, default=8, help="طول مقطع المرتكز بالأحرف المُطبَّعة (8)")
    p.add_argument("--min-score", type=float, default=0.55, help="أدنى تشابه لاعتبار الصفحة موثوقة (0.55)")
    p.add_argument("--report", type=Path, help="مسار تقرير JSON (الافتراضي بجوار الناتج)")
    p.add_argument("--no-epubcheck", action="store_true", help="تخطي فحص EPUBCheck")
    p.add_argument("--offline", action="store_true", help="مع --url: الاعتماد على الكاش فقط")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


class _FallbackConfig:
    """إعدادات احتياطية إن لم يوجد config.py في المشروع."""
    def __init__(self) -> None:
        self.output_dir = Path("output")
        self.log_dir = Path("logs")
        self.run_epubcheck = True
        self.offline = False


def _load_config():
    try:
        from config import Config  # type: ignore
        return Config()
    except ImportError:
        return _FallbackConfig()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    args = build_parser().parse_args(argv)

    from .matcher import MatchSettings
    from .pipeline import SyncOptions, run_sync

    cfg = _load_config()
    if args.no_epubcheck:
        cfg.run_epubcheck = False
    if args.offline:
        cfg.offline = True
    Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr),
                  logging.FileHandler(Path(cfg.log_dir) / "pdfsync.log", encoding="utf-8")],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if not args.pdf.exists():
        print(f"❌ ملف PDF غير موجود: {args.pdf}")
        return 2

    epub_path = args.epub
    if args.url:  # إعادة استخدام الزاحف والمُنشئ الحاليين لبناء EPUB المصدر
        from epub_builder import EPUBBuilder  # type: ignore
        from scraper import BookScraper  # type: ignore
        print(f"🔍 بناء EPUB المصدر من الرابط: {args.url}")
        book = BookScraper(cfg).scrape_book(args.url)
        epub_path = EPUBBuilder(book, cfg).build(cfg.output_dir / "_source_for_sync.epub")
        print(f"   تم: {epub_path}")
    elif not epub_path.exists():
        print(f"❌ ملف EPUB غير موجود: {epub_path}")
        return 2

    ms = MatchSettings(anchor_len=args.anchor_len, accept_score=args.min_score)
    opts = SyncOptions(
        pdf=args.pdf, epub=epub_path, output=args.output, page_range=args.pages,
        text_order=args.text_order, digits=args.digits, header_frac=args.header_margin,
        footer_frac=args.footer_margin, page_offset=args.page_offset, trim_edges=args.trim_edges,
        split_notes=not args.no_notes, note_classes=tuple(args.note_class),
        images=args.images, image_dpi=args.image_dpi, fallback_image=not args.no_fallback_image,
        validate=not args.no_epubcheck, report=args.report, match=ms,
    )

    print("📚 مزامنة PDF مع EPUB")
    res = run_sync(opts, cfg, progress=lambda s: print(f"  • {s}"))
    r = res.report
    s = r["summary"]
    v = r["verification"]
    print("\n==================== النتيجة ====================")
    print(f"📂 الناتج            : {res.output}")
    print(f"📄 صفحات PDF         : {r['pdf_pages']}  | ملفات الصفحات = صفحات PDF: "
          f"{'نعم' if v['page_files_equal_pdf_pages'] and v['page_file_numbers_equal_pdf_numbers'] else '❌ لا'}")
    print(f"🔤 اتجاه النص/الأرقام : {r['pdf_text_order']} / {'معكوسة' if r['pdf_digits_reversed'] else 'عادية'}")
    print(f"🎯 متوسط التشابه      : {s['mean_score']}  | الحالات: {s['status']}")
    print(f"🛡️  حفظ نص EPUB حرفيًا : متن {'نعم' if v['body_text_preserved_exactly'] else '❌ لا'}"
          f" | حواشٍ {'نعم' if v['notes_text_preserved_exactly'] else '❌ لا'}")
    d = r["dropped_epub_text"]
    if d["before_first_page_chars"] or d["after_last_page_chars"]:
        print(f"✂️  نص EPUB خارج نطاق PDF (حُذف): قبل={d['before_first_page_chars']} حرفًا، بعد={d['after_last_page_chars']} حرفًا")
    ec = v["epubcheck"]
    if ec.get("ran"):
        print(f"🔬 EPUBCheck         : {ec['fatals']} Fatals / {ec['errors']} Errors / {ec['warnings']} Warnings")
        for msg in ec.get("messages", [])[:8]:
            print(f"      - {msg}")
    if s["image_pages"]:
        print(f"🖼️  صفحات صور (نُقلت) : {s['image_pages'][:30]}")
    review = s["pages_needing_review"]
    if review:
        print(f"⚠️  صفحات تحتاج مراجعة ({len(review)}): " + ", ".join(str(p) for p in review[:40]) +
              (" ..." if len(review) > 40 else ""))
    print(f"🧾 التقرير الكامل    : {res.report_path}")
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
