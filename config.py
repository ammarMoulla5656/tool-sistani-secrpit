"""
config.py — الإعدادات المركزية للمشروع.

كل القيم هنا افتراضية ويمكن تغييرها من سطر الأوامر (انظر main.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

APP_NAME = "Book2EPUB"
APP_VERSION = "1.0.0"


@dataclass
class Config:
    # ---------------- الشبكة واحترام الموقع ----------------
    # User-Agent صريح يعرّف البرنامج ولا يتنكّر في هيئة متصفح.
    user_agent: str = (
        f"Mozilla/5.0 (compatible; {APP_NAME}/{APP_VERSION}; "
        "personal offline reading; respects robots.txt)"
    )
    delay: float = 1.5          # أقل زمن (ثانية) بين طلبين شبكيين متتاليين
    jitter: float = 0.5         # زمن عشوائي إضافي (0..jitter) لتجنّب النمط الآلي الصارم
    timeout: float = 30.0       # مهلة قراءة الاستجابة
    connect_timeout: float = 10.0
    retries: int = 3            # عدد المحاولات لكل رابط (للأخطاء المؤقتة فقط)
    backoff: float = 2.0        # مضاعف الانتظار بين المحاولات
    respect_robots: bool = True

    # ---------------- Cache ----------------
    cache_dir: Path = BASE_DIR / "cache"
    use_cache: bool = True      # قراءة/كتابة الكاش
    refresh: bool = False       # تجاهل الكاش الموجود وإعادة التنزيل (مع تحديث الكاش)
    offline: bool = False       # عدم استخدام الشبكة إطلاقًا (البناء من الكاش فقط)

    # ---------------- المسارات ----------------
    output_dir: Path = BASE_DIR / "output"
    log_dir: Path = BASE_DIR / "logs"
    templates_dir: Path = BASE_DIR / "templates"
    tools_dir: Path = BASE_DIR / "tools"

    # ---------------- حماية الزحف ----------------
    max_pages: int = 3000           # سقف أمان لعدد صفحات الكتاب الواحد
    follow_next_links: bool = True  # اكتشاف صفحات إضافية عبر رابط "التالي" (داخل نطاق الكتاب فقط)

    # ---------------- المحتوى ----------------
    download_images: bool = True
    include_cover: bool = True

    # ---------------- تقسيم EPUB ----------------
    split_mode: str = "part"    # part: ملف لكل باب رئيسي | page: ملف لكل صفحة ويب
    max_file_kb: int = 260      # الحد الأعلى التقريبي لحجم ملف XHTML قبل تقسيمه
    fuzzy_group: bool = True    # دمج عناوين الأبواب المتطابقة تقريبًا (أخطاء إملائية في فهرس الموقع)

    # ---------------- تجاوز البيانات الوصفية ----------------
    title: str | None = None
    author: str | None = None
    publisher: str | None = None
    language: str | None = None
    filename: str | None = None

    # ---------------- التحقق ----------------
    run_epubcheck: bool = True
