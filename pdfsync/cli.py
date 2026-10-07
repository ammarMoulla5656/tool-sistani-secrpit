"""
pdfsync/cli.py — واجهة سطر الأوامر لمزامنة PDF مع EPUB.

الاستخدام:
  python main.py sync --pdf book.pdf --epub book.epub
  python main.py sync --pdf book.pdf --epub book.epub -o out.epub --pages 5-120
  python main.py sync --pdf book.pdf --url https://www.sistani.org/arabic/book/13/
  python -m pdfsync --pdf book.pdf --epub book.epub
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
                    "(PDF مصدر الحدود، وEPUB مصدر النص).",
    )
    p.add_argument("--pdf", type=Path, required=True, help="ملف PDF (نصي؛ يُستخدم OCR فقط للصفحات المصورة)")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--epub", type=Path, help="ملف EPUB الأصلي")
    src.add_argument("--url", help="رابط كتاب في الموقع: يُبنى EPUB منه أولًا بالأداة الحالية ثم يُزامَن")
    p.add_argument("--output", "-o", type=Path, help="مسار EPUB الناتج")
    p.add_argument("--pages", type=_parse_pages, help="نطاق صفحات PDF المستهدفة مثل 5-120 (الافتراضي: الكل)")
    p.add_argument("--text-order", choices=["auto", "logical", "reverse_chars", "reverse_words"], default="auto",
                   help="اتجاه نص PDF المستخرج (auto: كشف تلقائي)")
    p.add_argument("--ocr", choices=["auto", "off", "force"], default="auto",
                   help="OCR احتياطي للصفحات المصورة/الرديئة فقط (يتطلب Tesseract)")
    p.add_argument("--include-footnotes", action="store_true",
                   help="إدخال حواشي أسفل صفحة PDF في المطابقة (الافتراضي: استبعادها)")
    p.add_argument("--header-margin", type=float, default=0.07, help="نسبة الهامش العلوي لكشف الترويسة (0.07)")
    p.add_argument("--footer-margin", type=float, default=0.07, help="نسبة الهامش السفلي لكشف التذييل (0.07)")
    p.add_argument("--page-offset", type=int, default=0,
                   help="إزاحة تسمية الصفحة في page-list (تسمية = رقم صفحة PDF + الإزاحة)")
    trim = p.add_mutually_exclusive_group()
    trim.add_argument("--trim-edges", dest="trim_edges", action="store_true", default=None,
                      help="حذف نص EPUB الواقع قبل أول صفحة مستهدفة وبعد آخر صفحة (تلقائي مع --pages)")
    trim.add_argument("--keep-edges", dest="trim_edges", action="store_false",
                      help="إبقاء كل نص EPUB (يُلحق بأول/آخر صفحة) حتى مع --pages")
    p.add_argument("--anchor-len", type=int, default=24, help="طول نقطة الارتكاز بالأحرف المُطبَّعة (24)")
    p.add_argument("--min-score", type=float, default=0.55, help="أدنى تشابه لقبول مطابقة غير تامة (0.55)")
    p.add_argument("--report", type=Path, help="مسار تقرير JSON (الافتراضي بجوار الناتج)")
    p.add_argument("--no-epubcheck", action="store_true", help="تخطي فحص EPUBCheck")
    p.add_argument("--offline", action="store_true", help="مع --url: الاعتماد على الكاش فقط")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    args = build_parser().parse_args(argv)

    from config import Config
    from .matcher import MatchSettings
    from .pipeline import SyncOptions, run_sync

    cfg = Config()
    if args.no_epubcheck:
        cfg.run_epubcheck = False
    if args.offline:
        cfg.offline = True
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(cfg.log_dir / "pdfsync.log", encoding="utf-8")],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if not args.pdf.exists():
        print(f"❌ ملف PDF غير موجود: {args.pdf}")
        return 2

    epub_path = args.epub
    if args.url:  # إعادة استخدام الزاحف والمُنشئ الحاليين لبناء EPUB المصدر
        from epub_builder import EPUBBuilder
        from scraper import BookScraper
        print(f"🔍 بناء EPUB المصدر من الرابط: {args.url}")
        book = BookScraper(cfg).scrape_book(args.url)
        tmp_cfg_out = cfg.output_dir
        epub_path = EPUBBuilder(book, cfg).build(tmp_cfg_out / "_source_for_sync.epub")
        print(f"   تم: {epub_path}")
    elif not epub_path.exists():
        print(f"❌ ملف EPUB غير موجود: {epub_path}")
        return 2

    ms = MatchSettings(anchor_len=args.anchor_len, accept_score=args.min_score)
    opts = SyncOptions(
        pdf=args.pdf, epub=epub_path, output=args.output, page_range=args.pages,
        text_order=args.text_order, ocr=args.ocr, include_footnotes=args.include_footnotes,
        header_frac=args.header_margin, footer_frac=args.footer_margin,
        page_offset=args.page_offset, trim_edges=args.trim_edges,
        validate=not args.no_epubcheck, report=args.report, match=ms,
    )

    print("📚 مزامنة PDF مع EPUB")
    res = run_sync(opts, cfg, progress=lambda s: print(f"  • {s}"))
    r = res.report
    s = r["summary"]
    print("\n==================== النتيجة ====================")
    print(f"📂 الناتج          : {res.output}")
    print(f"📄 صفحات PDF       : {r['pdf_pages']}  (ملفات الصفحات في EPUB مطابقة: "
          f"{'نعم' if r['verification']['page_files_equal_pdf_pages'] else 'لا'})")
    print(f"🔤 اتجاه نص PDF    : {r['pdf_text_order']}")
    print(f"🎯 الثقة            : {s['confidence']}  | متوسط التشابه: {s['mean_score']}")
    print(f"🛡️  حفظ النص حرفيًا : {'نعم' if r['verification']['text_preserved_exactly'] else '❌ لا — راجع التقرير'}")
    ec = r["verification"]["epubcheck"]
    if ec.get("ran"):
        print(f"🔬 EPUBCheck       : {ec['fatals']} Fatals / {ec['errors']} Errors / {ec['warnings']} Warnings")
        for msg in ec.get("messages", [])[:8]:
            print(f"      - {msg}")
    if s["scanned_pages"]:
        print(f"🖼️  صفحات مصورة     : {s['scanned_pages'][:20]}{' ...' if len(s['scanned_pages']) > 20 else ''}")
    weak = [p for p in r["pages"] if p["confidence"] == "low"]
    if weak:
        print(f"⚠️  صفحات منخفضة الثقة ({len(weak)}): " + ", ".join(str(p["page"]) for p in weak[:30]))
    print(f"🧾 التقرير الكامل  : {res.report_path}")
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
