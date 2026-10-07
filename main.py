"""
main.py — نقطة البداية لتشغيل برنامج تحويل كتب الويب إلى EPUB.

طريقة الاستخدام:
  python main.py "https://www.sistani.org/arabic/book/13/"
  python main.py "https://www.sistani.org/arabic/book/13/" --split-mode page
  python main.py --all "https://www.sistani.org/arabic/book/fatwa/"
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# ضبط ترميز الطرفية لدعم اللغة العربية بشكل سليم على أنظمة Windows و Linux
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from adapters.registry import get_adapter_for_url
from config import Config
from downloader import Downloader
from epub_builder import EPUBBuilder
from scraper import BookScraper
from validator import EPUBValidator


def setup_logging(verbose: bool = False, log_file: Path | None = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    # خفض ضجيج مكتبات الطلبات
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def print_progress_bar(current: int, total: int, title: str = "", bar_length: int = 30) -> None:
    if total <= 0:
        percent = 100
        filled = bar_length
    else:
        percent = min(100, int(100 * (current / total)))
        filled = int(bar_length * current // total)
    bar = "█" * filled + "░" * (bar_length - filled)
    short_title = (title[:35] + "…") if len(title) > 35 else title
    sys.stdout.write(f"\r  [{bar}] {percent:>3}% ({current}/{total}) {short_title:<38}")
    sys.stdout.flush()


def process_single_book(url: str, config: Config) -> bool:
    print(f"\n==================================================")
    print(f"📖 جاري فتح الكتاب من الرابط: {url}")
    print(f"==================================================")

    scraper = BookScraper(config)

    def on_progress(curr: int, tot: int, page_name: str):
        print_progress_bar(curr, tot, page_name)

    try:
        # 1. الاستخراج
        print("🔍 جاري فحص بنية الكتاب واكتشاف الصفحات...")
        book = scraper.scrape_book(url, progress_callback=on_progress)
        print()  # سطر جديد بعد شريط التقدم

        print(f"\n✅ تم الانتهاء من استخراج المحتوى:")
        print(f"   • عنوان الكتاب : {book.meta.title}")
        print(f"   • المؤلف       : {book.meta.author or 'غير محدد'}")
        print(f"   • عدد الصفحات  : {len(book.pages)} صفحة ناجحة")
        if book.failed:
            print(f"   • صفحات فشلت   : {len(book.failed)} صفحة (انظر التقرير أدناه)")
        print(f"   • عدد الصور    : {len(book.images)} صورة داخلية" + (" + غلاف" if book.cover else ""))

        # 2. بناء الـ EPUB
        print("\n⚙️  جاري إنشاء ملف EPUB القياسي...")
        builder = EPUBBuilder(book, config)
        out_path = builder.build()
        print(f"🎉 تم إنشاء الملف بنجاح:")
        print(f"   📂 المسار: {out_path.resolve()}")
        size_kb = out_path.stat().st_size / 1024
        print(f"   📦 الحجم : {size_kb:,.1f} كيلوبايت ({out_path.stat().st_size:,} بايت)")

        # 3. التحقق عبر EPUBCheck إن كان مفعلًا
        if config.run_epubcheck:
            print("\n🔬 جاري التحقق من صحة الملف بواسطة EPUBCheck...")
            validator = EPUBValidator(config)
            res = validator.validate(out_path)
            if not res.available:
                print("   ℹ️  أداة EPUBCheck غير متوفرة (تتطلب Java وحزمة epubcheck في مجلد tools).")
            elif res.is_valid:
                print(f"   ✅ فحص EPUBCheck: سليم 100% وبدون أي أخطاء! (0 Fatals / 0 Errors / {res.warning_count} Warnings)")
            else:
                print(f"   ⚠️  فحص EPUBCheck وجد ملاحظات: {res.fatal_count} Fatals / {res.error_count} Errors / {res.warning_count} Warnings")
                for msg in res.messages[:10]:
                    print(f"      - {msg}")

        # 4. تقرير الأخطاء إن وجد
        if book.failed:
            print("\n⚠️  قائمة الصفحات التي تعذر استخراجها:")
            for fp in book.failed:
                print(f"   - {fp.title} ({fp.url}) : {fp.error}")

        return True

    except Exception as e:
        print(f"\n❌ حدث خطأ أثناء معالجة الكتاب: {e}")
        logging.exception(e)
        return False


def main() -> None:
    # الأمر الفرعي الجديد: python main.py sync --pdf ... --epub ...  (الواجهة القديمة لم تتغير)
    if len(sys.argv) > 1 and sys.argv[1] == "sync":
        from pdfsync.cli import main as sync_main
        sys.exit(sync_main(sys.argv[2:]))

    parser = argparse.ArgumentParser(
        description="Book2EPUB — أداة تحويل كتب الويب العربية إلى ملفات EPUB احترافية.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("url", nargs="?", help="رابط صفحة الكتاب (مثال: https://www.sistani.org/arabic/book/13/)")
    parser.add_argument("--all", action="store_true", help="استخراج جميع الكتب في رابط التصنيف (مثل /arabic/book/fatwa/)")
    parser.add_argument("--output", "-o", type=Path, help="مسار مجلد المخرجات أو اسم الملف")
    parser.add_argument("--split-mode", choices=["part", "page"], default="part", help="طريقة تقسيم الملفات: part (حسب الباب) أو page (ملف لكل صفحة ويب)")
    parser.add_argument("--delay", type=float, default=1.5, help="المهلة بالثواني بين كل طلب والآخر لاحترام خادم الموقع (الافتراضي 1.5)")
    parser.add_argument("--no-cache", action="store_true", help="تعطيل الكاش والتنزيل المباشر من الإنترنت")
    parser.add_argument("--refresh", action="store_true", help="تحديث الكاش وإعادة تنزيل الصفحات من الموقع")
    parser.add_argument("--offline", action="store_true", help="الاعتماد التام على الكاش المحلي دون أي اتصال بالإنترنت")
    parser.add_argument("--no-images", action="store_true", help="تخطي تنزيل الصور لتسريع العملية")
    parser.add_argument("--no-epubcheck", action="store_true", help="تخطي فحص EPUBCheck")
    parser.add_argument("--max-pages", type=int, default=3000, help="الحد الأقصى لعدد الصفحات المسموح بزحفها لكل كتاب")
    parser.add_argument("--title", help="تجاوز عنوان الكتاب")
    parser.add_argument("--author", help="تجاوز اسم المؤلف")
    parser.add_argument("--publisher", help="تجاوز اسم الناشر")
    parser.add_argument("--verbose", "-v", action="store_true", help="تفعيل السجلات التفصيلية للمطورين")

    args = parser.parse_args()

    if not args.url:
        parser.print_help()
        sys.exit(1)

    cfg = Config()
    if args.output:
        if args.output.suffix == ".epub":
            cfg.output_dir = args.output.parent
            cfg.filename = args.output.name
        else:
            cfg.output_dir = args.output
    if args.split_mode:
        cfg.split_mode = args.split_mode
    if args.delay:
        cfg.delay = args.delay
    if args.no_cache:
        cfg.use_cache = False
    if args.refresh:
        cfg.refresh = True
    if args.offline:
        cfg.offline = True
    if args.no_images:
        cfg.download_images = False
    if args.no_epubcheck:
        cfg.run_epubcheck = False
    if args.max_pages:
        cfg.max_pages = args.max_pages
    if args.title:
        cfg.title = args.title
    if args.author:
        cfg.author = args.author
    if args.publisher:
        cfg.publisher = args.publisher

    setup_logging(verbose=args.verbose, log_file=cfg.log_dir / "app.log")

    # في حال تفعيل خيار --all لاستخراج عدة كتب من قائمة تصنيف
    if args.all:
        downloader = Downloader(cfg)
        adapter = get_adapter_for_url(args.url)
        print(f"📚 جاري فحص صفحة قائمة الكتب: {args.url}")
        cat_html = downloader.get_html(args.url)
        books_list = adapter.extract_category_books(cat_html, args.url)
        print(f"✨ تم العثور على {len(books_list)} كتابًا في هذه الصفحة:")
        for idx, (b_title, b_url) in enumerate(books_list, start=1):
            print(f"   {idx}. {b_title} ({b_url})")

        success_count = 0
        for idx, (b_title, b_url) in enumerate(books_list, start=1):
            print(f"\n[{idx}/{len(books_list)}] بدء معالجة: {b_title}")
            ok = process_single_book(b_url, cfg)
            if ok:
                success_count += 1

        print(f"\n🏁 اكتملت معالجة القائمة: {success_count} من {len(books_list)} كتب نجحت.")
    else:
        # معالجة كتاب منفرد
        ok = process_single_book(args.url, cfg)
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
